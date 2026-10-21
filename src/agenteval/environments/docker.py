"""Docker environment: compose file generation and image build.

Every sample gets a fresh container of a locally built image (``provision/Dockerfile``,
the same provisioning as the Tart image). The compose file has no host mounts and,
when the environment denies egress, no network besides loopback. The image is
never pulled: a missing image fails fast instead of fetching a same-named image
from a registry.

Confinement depends on the cell's inner sandbox:

- ``none`` cells run with stock Docker confinement (default seccomp profile, AppArmor
  and masked/read-only /proc paths) plus ``DockerSettings.security_opt``.
- ``bwrap`` cells also get ``DockerSettings.bwrap_security_opt`` (seccomp, AppArmor and
  systempaths unconfined by default), because bubblewrap needs user namespaces and a
  fresh /proc. That weakens the container: root inside it can write global
  ``/proc/sys`` entries such as ``kernel.core_pattern`` and ``/proc/sysrq-trigger``,
  and every user can create user namespaces. Scorers therefore never run agent-written
  code as root, and on a shared Docker host a gVisor runtime (``runtime: runsc``) or
  userns-remap is the better choice for bwrap cells.
- On a Linux Docker host with Ubuntu's ``kernel.apparmor_restrict_unprivileged_userns=1``
  the host's AppArmor policy applies to the container's unconfined processes, so bwrap
  cells fail closed unless the host has a userns profile for ``/usr/bin/bwrap`` or the
  sysctl is 0 (``agenteval doctor`` warns about it).

Compose files are named after their content, so a later run with a different
same-named profile writes a new file instead of changing the sandbox of a run that
is still in progress.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml

from agenteval.config import DockerSettings, EnvironmentProfile
from agenteval.constants import COLLECTOR_HOST
from agenteval.environments import PROVISION_DIR

COMPOSE_DIR = Path(".agenteval") / "compose"
"""Generated compose files, relative to the current directory (gitignored)."""
SERVICE = "default"

InnerSandboxType = Literal["none", "bwrap"]

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DockerError(RuntimeError):
    pass


def docker_settings(env: EnvironmentProfile) -> DockerSettings:
    if env.provider != "docker" or env.docker is None:
        raise ValueError(f"environment '{env.name}' is not a docker environment")
    return env.docker


def security_options(
    settings: DockerSettings, inner_sandbox: InnerSandboxType = "none"
) -> list[str]:
    """``security_opt`` for a cell: the bwrap relaxations only when the cell uses bwrap."""
    options = list(settings.security_opt)
    if inner_sandbox == "bwrap":
        options += settings.bwrap_security_opt
    return list(dict.fromkeys(options))


def compose_config(
    env: EnvironmentProfile,
    *,
    inner_sandbox: InnerSandboxType = "none",
    mounts: Sequence[str] = (),
    filtered_network: bool = False,
) -> dict[str, Any]:
    """Compose project for one sample container of ``env`` in a cell with ``inner_sandbox``.

    ``mounts`` are host directories bind-mounted at the same path (``agenteval dev
    --mount live`` only; eval runs never share host folders).

    ``filtered_network`` (``agenteval dev`` on a Claude subscription only): an
    egress-denying environment gets Docker's default bridge network plus ``NET_ADMIN``,
    so ``egress.sh`` can deny the agent user with nftables while the session's egress
    proxy, running as another user, reaches Anthropic. The default bridge, not a compose
    network, because its containers use the host's DNS servers directly instead of
    Docker's resolver on loopback, which the agent could otherwise query.
    """
    settings = docker_settings(env)
    service: dict[str, Any] = {
        "image": settings.image,
        "x-local": True,  # tells Inspect not to pull it
        "pull_policy": "never",
        "init": True,
        "command": ["tail", "-f", "/dev/null"],
        "working_dir": env.workspace,
        # Docker owns /etc/hosts in containers, so the provisioned entry does not survive.
        "extra_hosts": [f"{COLLECTOR_HOST}:127.0.0.1"],
        "stop_grace_period": "1s",
    }
    if env.egress == "deny" and filtered_network:
        service["network_mode"] = "bridge"
        service["cap_add"] = ["NET_ADMIN"]
    elif env.egress == "deny":
        service["network_mode"] = "none"
    if settings.runtime:
        service["runtime"] = settings.runtime
    security_opt = security_options(settings, inner_sandbox)
    if security_opt:
        service["security_opt"] = security_opt
    if settings.memory:
        service["mem_limit"] = settings.memory
    if settings.cpus is not None:
        service["cpus"] = settings.cpus
    if settings.pids_limit is not None:
        service["pids_limit"] = settings.pids_limit
    config: dict[str, Any] = {"services": {SERVICE: service}}
    if mounts:
        service["volumes"] = [{"type": "bind", "source": m, "target": m} for m in mounts]
        # Inspect refuses writable host binds unless the project opts in; the user did
        # (--mount live) and was told what the agent can then change on the host.
        config["x-inspect_docker_sandbox"] = {"unsafe_allow_writable_host_mounts": True}
    return config


def compose_text(
    env: EnvironmentProfile,
    *,
    inner_sandbox: InnerSandboxType = "none",
    mounts: Sequence[str] = (),
    filtered_network: bool = False,
) -> str:
    return (
        f"# Generated by agenteval from environment profile '{env.name}' "
        f"(inner sandbox: {inner_sandbox}). Do not edit:\n"
        "# the file name is a hash of this content.\n"
        + yaml.safe_dump(
            compose_config(
                env,
                inner_sandbox=inner_sandbox,
                mounts=mounts,
                filtered_network=filtered_network,
            ),
            sort_keys=False,
            default_flow_style=False,
        )
    )


def compose_path(env: EnvironmentProfile, text: str, root: Path | None = None) -> Path:
    """``<root>/.agenteval/compose/<env.name>-<sha256(text)[:12]>.yaml`` (absolute)."""
    if not _NAME_RE.fullmatch(env.name):
        raise ValueError(f"environment name '{env.name}' is not usable as a file name")
    digest = hashlib.sha256(text.encode()).hexdigest()[:12]
    return ((root or Path.cwd()) / COMPOSE_DIR / f"{env.name}-{digest}.yaml").resolve()


def write_compose_file(
    env: EnvironmentProfile,
    root: Path | None = None,
    *,
    inner_sandbox: InnerSandboxType = "none",
    mounts: Sequence[str] = (),
    filtered_network: bool = False,
) -> Path:
    """Write the compose file for ``env`` under ``<root>/.agenteval/compose/``; return its path.

    The name carries a hash of the content, so files of runs in progress are never
    rewritten with different settings.
    """
    text = compose_text(
        env, inner_sandbox=inner_sandbox, mounts=mounts, filtered_network=filtered_network
    )
    path = compose_path(env, text, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text() == text:
        return path
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def docker_available() -> str | None:
    """Path of the docker CLI, or None."""
    return shutil.which("docker")


def image_id(image: str) -> str | None:
    """Local image ID (``sha256:...``) of ``image``, or None when docker or the image is missing."""
    docker = docker_available()
    if docker is None:
        return None
    try:
        result = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def image_exists(image: str) -> bool:
    return image_id(image) is not None


def build_command(
    env: EnvironmentProfile, *, force: bool = False, platform: str | None = None
) -> list[str]:
    settings = docker_settings(env)
    cmd = [
        "docker",
        "build",
        "--tag",
        settings.image,
        "--build-arg",
        f"AGENT_USER={env.agent_user}",
        "--build-arg",
        f"WORKSPACE={env.workspace}",
    ]
    if platform:
        cmd += ["--platform", platform]
    if force:
        cmd += ["--no-cache", "--pull"]
    cmd.append(str(PROVISION_DIR))
    return cmd


def build_image(
    env: EnvironmentProfile, *, force: bool = False, platform: str | None = None
) -> None:
    """Build ``env.docker.image`` from ``provision/Dockerfile`` (output streams to the terminal).

    ``force`` rebuilds without the layer cache and re-pulls the base image;
    ``platform`` (e.g. ``linux/arm64``) defaults to the Docker host's.
    """
    cmd = build_command(env, force=force, platform=platform)
    docker = docker_available()
    if docker is None:
        raise DockerError("docker CLI not found on PATH")
    result = subprocess.run([docker, *cmd[1:]], check=False)
    if result.returncode != 0:
        raise DockerError(f"docker build failed with exit code {result.returncode}")
