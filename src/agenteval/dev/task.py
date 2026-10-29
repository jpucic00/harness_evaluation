"""The dev session task: one sample that prepares the environment, keeps the model bridge
open while the user works in the harness CLI, and copies the changes out before the
environment is destroyed.

The CLI runs interactively in the user's host terminal, attached with ``docker exec -it``
(Docker) or ``ssh -t`` (Tart). Model calls still go through Inspect's bridge on the host,
so no API key enters the environment, and every call is recorded in the session's log.

The exception is a model profile with ``access: subscription``: Claude Code then calls
Anthropic itself with the user's subscription token, which is copied into the
environment (never through Inspect, which logs what it writes). The agent user's egress
stays denied; Claude Code goes through ``egress_proxy.py``, which runs as ``PROXY_USER``
and tunnels only to ``SUBSCRIPTION_HOSTS``. Those calls are not in the session log.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import termios
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import anyio
from inspect_ai import Task
from inspect_ai.agent import AgentState, sandbox_agent_bridge
from inspect_ai.dataset import Sample
from inspect_ai.log._samples import sample_active
from inspect_ai.model import Model, get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import (
    Limit,
    LimitExceededError,
    SandboxEnvironment,
    cost_limit,
    sandbox,
    token_limit,
)
from inspect_swe._claude_code.env import (
    BLOCKING_MCP_ENV,
    DISABLE_AUTO_MEMORY_ENV,
    claude_code_agent_env,
)
from inspect_swe._claude_code.model import ClaudeCodeModels, resolve_claude_code_models

from agenteval import constants
from agenteval.config import EnvironmentProfile, HarnessProfile, ModelProfile
from agenteval.dev.review import DevIO, visible_text
from agenteval.dev.session import Mount
from agenteval.environments import sandbox_spec
from agenteval.harnesses import claude_code as claude_harness
from agenteval.harnesses import opencode as opencode_harness
from agenteval.harnesses.sandbox_exec import (
    install_root_file,
    install_sandbox_exec,
    render_template,
)
from agenteval.models import build_model
from agenteval.suites.setup import (
    SandboxUser,
    resolve_user,
    run_root,
    run_root_sh,
    script_source,
    setup_dirs_script,
)

LAUNCHER = f"{constants.OPT_DIR}/dev-launch"
RELAY = f"{constants.OPT_DIR}/ptyrelay.py"
DEV_REPO = f"{constants.OPT_DIR}/dev_repo.py"
THINK_SHIM = f"{constants.OPT_DIR}/think_shim.py"
EGRESS = f"{constants.OPT_DIR}/egress.sh"
EGRESS_PROXY = f"{constants.OPT_DIR}/egress_proxy.py"
CLAUDE_SETTINGS = f"{constants.OPT_DIR}/dev/claude-settings.json"
DEV_STATE = "/var/lib/agenteval-dev"
IN_DIR = f"{DEV_STATE}/in"
OUT_DIR = f"{DEV_STATE}/out"
SHARES_MOUNT = "/mnt/agenteval-shares"
BRIDGE_PORT = 13131
SHIM_PORT = 13132
"""OpenCode talks to ``think_shim.py`` here; it forwards to the bridge (``BRIDGE_PORT``)."""
PROXY_PORT = 13133
"""``egress_proxy.py`` (subscription sessions): Claude Code's ``HTTPS_PROXY``."""
PROXY_USER = "nobody"
"""Runs the egress proxy: any user but the agent, whose egress the environment denies."""
PROXY_LOG = "/var/log/agenteval-egress-proxy.log"
SUBSCRIPTION_HOSTS = ("api.anthropic.com", "platform.claude.com")
"""Model calls, and OAuth token refresh (code.claude.com/docs/en/network-config)."""
TOKEN_FILE = f"{DEV_STATE}/claude-oauth-token"
HEADER_TIMEOUT_MS = 3_600_000
"""OpenCode's wait for response headers; matches think_shim's UPSTREAM_TIMEOUT (1 h)."""
POLL_RECOVERY = 86400.0
"""Keep re-polling the bridge proxy for a day: the host may sleep during a session."""
DETACH_KEYS = "ctrl-^,ctrl-_,ctrl-^"
"""Effectively unreachable: a detached ``docker exec`` cannot be re-attached."""
USER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TERM_VARS = ("COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")
MAX_PULL_BYTES = 2 * 1024**3
TERMINAL_RESET = (
    # leave the alternate screen without moving the cursor of an inline TUI (save/restore)
    "\x1b7\x1b[?1049l\x1b8"
    # cursor on, colours off, bracketed paste and mouse reporting off
    "\x1b[?25h\x1b[0m\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
)


@dataclass(frozen=True)
class RepoTransfer:
    """Copy mode: the snapshot bundle and its metadata (see ``agenteval.dev.repo``)."""

    bundle: Path
    meta: Path
    base_commit: str
    base_tree: str
    head: str | None


@dataclass(frozen=True)
class Exported:
    """What the solver pulled out of the environment, in the session directory."""

    commits_bundle: Path | None
    export: dict[str, Any] | None
    config_tar: Path | None


@dataclass(frozen=True)
class DevSpec:
    session_id: str
    session_dir: Path
    model: ModelProfile
    subagent_model: ModelProfile | None
    harness: HarnessProfile
    """Dev-adjusted: terminal variables passed through, live-mode writable paths."""
    environment: EnvironmentProfile
    """The profile as configured (sandbox spec)."""
    dev_env: EnvironmentProfile
    """``workspace`` = repo root in the env, no ambient secrets."""
    mount: Mount
    mounts: tuple[str, ...]
    vm_root: str
    vm_cwd: str
    repo: RepoTransfer | None
    config_tar: Path | None
    config_export: tuple[list[str], list[str]] = ([], [])
    """Paths (relative to the agent's HOME, and to the repo root) to bring back."""
    claude_seed: dict[str, Any] | None = None
    subscription_token: str | None = field(default=None, repr=False)
    """``CLAUDE_CODE_OAUTH_TOKEN`` for an ``access: subscription`` model."""
    git_identity: Mapping[str, str] = field(default_factory=dict)
    host_term: Mapping[str, str] = field(default_factory=dict)
    exec_script: str | None = None
    """Test hook: run this ``sh`` script (no TTY) instead of attaching the terminal."""
    cost_limit: float | None = None
    """USD cap on the session's bridged model calls (all models); ``None``: no cap."""
    token_limit: int | None = None
    """Cap on the session's output tokens (all models); ``None``: no cap."""


ExportHandler = Callable[[Exported], None]
"""Host-side processing of the export (fetch, extract); raising keeps the env for retry."""


# ---------------------------------------------------------------- env access


class EnvHandle(Protocol):
    def attach_argv(self, user: str, cwd: str, argv: Sequence[str]) -> list[str]: ...

    def exec_argv(
        self, user: str, cwd: str, argv: Sequence[str], env: Mapping[str, str]
    ) -> list[str]: ...

    async def push(self, local: Path, remote: str) -> None: ...

    async def pull(self, remote: str, local: Path, *, user: str) -> None: ...

    def shell_hint(self, user: str, cwd: str) -> str: ...


@dataclass(frozen=True)
class DockerHandle:
    container: str

    def attach_argv(self, user: str, cwd: str, argv: Sequence[str]) -> list[str]:
        env = [a for name in ("TERM", *TERM_VARS) for a in ("-e", name)]
        return [
            "docker", "exec", "-it", "--detach-keys", DETACH_KEYS,
            "-u", user, "-w", cwd, *env, self.container, *argv,
        ]  # fmt: skip

    def exec_argv(
        self, user: str, cwd: str, argv: Sequence[str], env: Mapping[str, str]
    ) -> list[str]:
        pairs = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
        return ["docker", "exec", "-u", user, "-w", cwd, *pairs, self.container, *argv]

    async def push(self, local: Path, remote: str) -> None:
        await _run_host(["docker", "cp", str(local), f"{self.container}:{remote}"])

    async def pull(self, remote: str, local: Path, *, user: str) -> None:
        # docker cp copies a symlink as a link, never its target
        await _run_host(["docker", "cp", f"{self.container}:{remote}", str(local)])
        if local.is_symlink() or not local.is_file():
            local.unlink(missing_ok=True)
            raise RuntimeError(f"{remote} in the environment is not a regular file")

    def shell_hint(self, user: str, cwd: str) -> str:
        return shlex.join(["docker", "exec", "-it", "-u", user, "-w", cwd, self.container, "bash"])


@dataclass(frozen=True)
class TartHandle:
    target: Any
    """``agenteval.environments.tart.ssh.SSHTarget``."""

    def _ssh(self, remote: str, *, tty: bool) -> list[str]:
        from agenteval.environments.tart.ssh import ssh_argv

        return ssh_argv(self.target, remote, tty=tty)

    def attach_argv(self, user: str, cwd: str, argv: Sequence[str]) -> list[str]:
        remote = [
            "sudo",
            "-n",
            "-H",
            "-u",
            user,
            "--",
            "/bin/sh",
            "-c",
            'cd "$1" && shift && exec "$@"',
        ]
        return self._ssh(shlex.join([*remote, "sh", cwd, *argv]), tty=True)

    def exec_argv(
        self, user: str, cwd: str, argv: Sequence[str], env: Mapping[str, str]
    ) -> list[str]:
        pairs = [f"{k}={v}" for k, v in env.items()]
        remote = ["sudo", "-n", "-H", "-u", user, "--", "env", *pairs, "/bin/sh", "-c"]
        script = 'cd "$1" && shift && exec "$@"'
        return self._ssh(shlex.join([*remote, script, "sh", cwd, *argv]), tty=False)

    async def push(self, local: Path, remote: str) -> None:
        cmd = ["sudo", "-n", "/bin/sh", "-c", 'umask 022; cat >"$1"', "sh", remote]
        with local.open("rb") as f:
            await _run_host(self._ssh(shlex.join(cmd), tty=False), stdin=f)

    async def pull(self, remote: str, local: Path, *, user: str) -> None:
        # read as the agent: a symlink it planted cannot reach root-only files
        cmd = ["sudo", "-n", "-u", user, "--", "cat", "--", remote]
        with local.open("wb") as f:
            await _run_host(self._ssh(shlex.join(cmd), tty=False), stdout=f)

    def shell_hint(self, user: str, cwd: str) -> str:
        return shlex.join(self.attach_argv(user, cwd, ["bash", "-l"]))


SBX_CLI = ("/usr/bin/env", "SBX_NO_TELEMETRY=1", "sbx")
"""How the host runs sbx: never with its usage telemetry."""


@dataclass(frozen=True)
class SbxHandle:
    """A Docker Sandboxes microVM: ``sbx exec`` / ``sbx cp`` (flags as ``docker exec``).

    Commands drop the variables sbx injects (``unset``), as the provider's exec does.
    """

    sandbox: str
    unset: tuple[str, ...] = ()

    def _as(self, user: str, argv: Sequence[str]) -> list[str]:
        # root + runuser, so the user gets its supplementary groups (see sbx exec_argv)
        drop = [] if user in ("root", "0") else ["/usr/sbin/runuser", "-u", user, "--"]
        return ["/usr/bin/env", *(a for n in self.unset for a in ("-u", n)), *drop, *argv]

    def attach_argv(self, user: str, cwd: str, argv: Sequence[str]) -> list[str]:
        env = [a for name in ("TERM", *TERM_VARS) for a in ("-e", name)]
        return [
            *SBX_CLI, "exec", "-it", "--detach-keys", DETACH_KEYS,
            "-u", "root", "-w", cwd, *env, self.sandbox, *self._as(user, argv),
        ]  # fmt: skip

    def exec_argv(
        self, user: str, cwd: str, argv: Sequence[str], env: Mapping[str, str]
    ) -> list[str]:
        pairs = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
        return [
            *SBX_CLI, "exec", "-u", "root", "-w", cwd, *pairs, self.sandbox, *self._as(user, argv),
        ]  # fmt: skip

    async def push(self, local: Path, remote: str) -> None:
        await _run_host([*SBX_CLI, "cp", str(local), f"{self.sandbox}:{remote}"])

    async def pull(self, remote: str, local: Path, *, user: str) -> None:
        # sbx cp copies a symlink as a link (without -L), never its target
        await _run_host([*SBX_CLI, "cp", f"{self.sandbox}:{remote}", str(local)])
        if local.is_symlink() or not local.is_file():
            local.unlink(missing_ok=True)
            raise RuntimeError(f"{remote} in the environment is not a regular file")

    def shell_hint(self, user: str, cwd: str) -> str:
        return shlex.join(
            [
                *SBX_CLI,
                "exec",
                "-it",
                "-u",
                "root",
                "-w",
                cwd,
                self.sandbox,
                *self._as(user, ["bash"]),
            ]
        )


async def _run_host(
    argv: Sequence[str], *, stdin: Any = None, stdout: Any = None, timeout: float = 1800
) -> None:
    with anyio.fail_after(timeout):
        result = await anyio.run_process(
            list(argv),
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"`{shlex.join(argv[:2])} ...` failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()[-1000:]}"
        )


async def env_handle(sbox: SandboxEnvironment, environment: EnvironmentProfile) -> EnvHandle:
    if environment.provider == "docker":
        connection = await sbox.connection(user="root")
        if not connection.container:
            raise RuntimeError("cannot find the docker container of this session")
        return DockerHandle(connection.container)
    if environment.provider == "sbx":
        from agenteval.environments.sbx.sandbox import SbxSandboxEnvironment

        sandbox = sbox.as_type(SbxSandboxEnvironment)
        return SbxHandle(sandbox.sandbox_name, sandbox.unset_env)
    from agenteval.environments.tart.sandbox import TartSandboxEnvironment

    return TartHandle(sbox.as_type(TartSandboxEnvironment).target)


# ---------------------------------------------------------------- launcher


def _env_lines(env: Mapping[str, str], env_files: Mapping[str, str]) -> str:
    lines = []
    for name in [*env, *env_files]:
        if not name.replace("_", "a").isalnum() or name[0].isdigit():
            raise ValueError(f"invalid environment variable name {name!r}")
    for name, value in env.items():
        lines.append(f"{name}={shlex.quote(value)}; export {name}")
    for name, path in env_files.items():
        lines.append(f"{name}=$(cat {shlex.quote(path)}); export {name}")
    return "\n".join(lines)


def render_launcher(
    *,
    env: Mapping[str, str],
    cwd: str,
    binary: str,
    args: Sequence[str],
    relay: bool,
    env_files: Mapping[str, str] | None = None,
) -> str:
    """``env_files`` are variables read from a file in the environment at start (secrets
    that must not be in the launcher, which Inspect logs when it writes it)."""
    # -I: the user's PYTHONPATH / site-packages cannot change the relay
    command = [constants.SANDBOX_EXEC, *(["python3", "-I", RELAY, "--"] if relay else [])]
    command += [binary, *args]
    return render_template(
        "dev-launch.sh",
        {
            "ENV": _env_lines(env, env_files or {}),
            "CWD": shlex.quote(cwd),
            "COMMAND": shlex.join(command),
        },
        folder="dev",
    )


def subscription(spec: DevSpec) -> bool:
    return spec.model.access == "subscription"


def claude_launch(
    spec: DevSpec, *, models: ClaudeCodeModels | None, home: str
) -> tuple[dict[str, str], list[str]]:
    """Environment and arguments for an interactive Claude Code.

    ``models`` is the bridge routing (``None`` on a subscription). On a subscription the
    token is not in ``env``: the launcher reads it from ``TOKEN_FILE``
    (``CLAUDE_CODE_OAUTH_TOKEN``).
    """
    harness = spec.harness
    if models is None:
        env = {
            "HTTPS_PROXY": f"http://127.0.0.1:{PROXY_PORT}",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "IS_SANDBOX": "1",
        }
        model = spec.model.agent_models["claude_code"]
    else:
        env = claude_code_agent_env(bridge_port=BRIDGE_PORT, models=models)
        for name in (*BLOCKING_MCP_ENV, *DISABLE_AUTO_MEMORY_ENV):
            env.pop(name, None)  # memory stays on: it is part of what comes back
        # the TUI warns when both are set; the --settings apiKeyHelper supplies the key
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        model = models.presented
    env |= harness.env
    env |= _base_env(spec, home)
    env["CLAUDE_CONFIG_DIR"] = f"{home}/.claude"  # as the claude wrapper sets it
    args = ["--dangerously-skip-permissions", "--settings", CLAUDE_SETTINGS]
    args += ["--model", model]
    subagents = harness.subagents
    if subagents is not None and subagents.runner == "claude_code" and subagents.agents:
        agents = {
            name: {
                "description": agent.description,
                "prompt": agent.prompt.strip(),
                **({"tools": agent.tools} if agent.tools is not None else {}),
            }
            for name, agent in subagents.agents.items()
        }
        args += ["--agents", json.dumps(agents)]
    prompt = "\n\n".join(
        p for p in (harness.system_prompt, claude_harness.delegation_note(harness)) if p
    )
    if prompt:
        args += ["--append-system-prompt", prompt]
    return env, args


def opencode_launch(
    spec: DevSpec, *, dep_dirs: Sequence[str], home: str
) -> tuple[dict[str, str], list[str]]:
    """Environment and arguments for an interactive OpenCode TUI."""
    harness = spec.harness
    cli_model = harness.agent_model or spec.model.agent_models.get("opencode")
    if not cli_model or "/" not in cli_model:
        raise ValueError(
            f"model '{spec.model.name}' has no agent_models.opencode entry (provider/model) "
            f"for harness '{harness.name}'"
        )
    provider, model_id = cli_model.split("/", 1)
    # through think_shim.py, so reasoning shows as OpenCode's own "Thinking" block
    base_url = f"http://localhost:{SHIM_PORT}/v1"
    profile = spec.model
    # display names only: OpenCode keeps the catalog entry's limits, flags and request format
    label = f"{profile.description} ({profile.name})" if profile.description else profile.name
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": cli_model,
        "provider": {
            provider: {
                "name": "agenteval bridge",
                # the shim answers only once the whole reply is in (no streaming), so wait as
                # long as it does instead of OpenCode's 300 s default, which re-sends the turn
                "options": {"baseURL": base_url, "headerTimeout": HEADER_TIMEOUT_MS},
                "models": {model_id: {"name": label}},
            }
        },
        "autoupdate": False,
        "share": "disabled",
    }
    env = {
        "ANTHROPIC_BASE_URL": base_url,
        "OPENAI_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "sk-none",
        "OPENAI_API_KEY": "sk-none",
        f"{provider.upper().replace('-', '_')}_API_KEY": "sk-none",
        "OPENCODE_CONFIG_CONTENT": json.dumps(config),
    }
    env |= harness.env
    env |= _base_env(spec, home)
    env["PATH"] = ":".join([*dep_dirs, USER_PATH])
    # The TUI has no system-prompt flag (harness.system_prompt applies to eval runs only).
    return env, ["--auto", "--model", cli_model]


def _base_env(spec: DevSpec, home: str) -> dict[str, str]:
    env = {"HOME": home, "PATH": USER_PATH, "LANG": "C.UTF-8"}
    env |= {k: v for k, v in spec.host_term.items() if k in TERM_VARS and v}
    return env


# ---------------------------------------------------------------- solver steps


async def _sh_as(
    sbox: SandboxEnvironment, user: str, script: str, *args: str, what: str, cwd: str | None = None
) -> str:
    result = await sbox.exec(
        ["/bin/sh", "-c", script, "sh", *args], user=user, cwd=cwd, timeout=1800
    )
    if not result.success:
        raise RuntimeError(
            f"{what} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[-2000:]}"
        )
    return result.stdout


async def prepare_live(sbox: SandboxEnvironment, spec: DevSpec, user: SandboxUser) -> SandboxUser:
    """Mount the shared folders (Tart) and make sure the agent can write them."""
    q = shlex.quote
    if spec.environment.provider == "tart":
        from agenteval.environments.tart.image import SHARE_TAG, share_name

        lines = [
            "set -eu",
            f"mkdir -p {q(SHARES_MOUNT)}",
            f"mountpoint -q {q(SHARES_MOUNT)} || mount -t virtiofs {SHARE_TAG} {q(SHARES_MOUNT)}",
        ]
        for i, path in enumerate(spec.mounts):
            lines += [
                f"mkdir -p {q(path)}",
                f"mountpoint -q {q(path)} || mount --bind {q(SHARES_MOUNT + '/' + share_name(i))} "
                f"{q(path)}",
            ]
        await run_root_sh(sbox, "\n".join(lines), "mount shared folders")
    probe = 'f="$1/.agenteval-write-test-$$" && : >"$f" && rm -f "$f"'
    for path in spec.mounts:
        check = await sbox.exec(["/bin/sh", "-c", probe, "sh", path], user=user.name)
        if check.success:
            continue
        owner = await sbox.exec(["stat", "-c", "%u", path], user="root")
        uid = owner.stdout.strip()
        if not owner.success or not uid.isdigit() or uid in ("0", str(user.uid)):
            raise RuntimeError(
                f"the agent user cannot write {path} in the environment; use --mount copy"
            )
        await run_root(
            sbox,
            ["usermod", "-o", "-u", uid, user.name],
            f"map the agent user to the folder owner (uid {uid})",
        )
        user = await resolve_user(sbox, user.name)
        check = await sbox.exec(["/bin/sh", "-c", probe, "sh", path], user=user.name)
        if not check.success:
            raise RuntimeError(f"the agent user cannot write {path}; use --mount copy")
    return user


async def base_setup(sbox: SandboxEnvironment, spec: DevSpec, user: SandboxUser) -> None:
    """Directories, helper scripts and the egress policy; no eval fixtures."""
    q = shlex.quote
    await run_root_sh(
        sbox,
        "\n".join(
            [
                setup_dirs_script(user, spec.dev_env.workspace, ambient=False),
                f"install -d -m 0755 -o 0 -g 0 {q(DEV_STATE)} {q(IN_DIR)}",
                f"rm -rf {q(OUT_DIR)}",
                f"install -d -m 0700 -o {user.uid} -g {user.gid} {q(OUT_DIR)}",
            ]
        ),
        "dev directories",
    )
    for folder, name, path, mode in (
        ("scripts", "egress.sh", EGRESS, "0755"),
        ("dev", "ptyrelay.py", RELAY, "0644"),
        ("dev", "dev_repo.py", DEV_REPO, "0644"),
        ("dev", "think_shim.py", THINK_SHIM, "0644"),
        ("dev", "egress_proxy.py", EGRESS_PROXY, "0644"),
    ):
        await install_root_file(sbox, path, script_source(name, folder).decode(), mode)
    policy = await run_root(
        sbox,
        ["/bin/sh", EGRESS, "apply", spec.dev_env.egress, "--uid", str(user.uid)],
        "egress policy",
    )
    if subscription(spec) and spec.dev_env.egress == "deny":
        # the environment has a network for the egress proxy: the rule is all that
        # keeps the agent off it
        result = _last_json(policy.stdout)
        if result.get("effective") is not True:
            raise RuntimeError(
                f"cannot deny the agent's egress ({result.get('reason')}); "
                "not starting a subscription session with open network"
            )


async def install_cli(sbox: SandboxEnvironment, spec: DevSpec, user: str) -> tuple[str, list[str]]:
    harness = spec.harness
    if harness.agent == "claude_code":
        binary = await claude_harness.install_claude(
            sbox, harness, user, seed_settings=False, write_subagents=False
        )
        dep_dirs: list[str] = []
    else:
        binary, dep_dirs = await opencode_harness.install_opencode(sbox, harness.version, user)
    if binary.startswith("/usr/local/bin/"):
        raise RuntimeError(
            f"harness '{harness.name}' must pin a version: agenteval dev needs the real binary"
        )
    return binary, dep_dirs


async def import_repo(
    sbox: SandboxEnvironment, handle: EnvHandle, spec: DevSpec, user: SandboxUser
) -> None:
    transfer = spec.repo
    if transfer is None:
        return
    bundle, meta = f"{IN_DIR}/repo.bundle", f"{IN_DIR}/meta.json"
    await handle.push(transfer.bundle, bundle)
    await handle.push(transfer.meta, meta)
    await run_root(sbox, ["chmod", "0644", bundle, meta], "repo bundle permissions")
    out = await _sh_as(
        sbox,
        user.name,
        'exec python3 -I "$1" import --bundle "$2" --meta "$3" --workspace "$4"',
        DEV_REPO,
        bundle,
        meta,
        spec.vm_root,
        what="repo import",
    )
    result = _last_json(out)
    if result.get("base_tree") != transfer.base_tree:
        raise RuntimeError(
            f"repo import mismatch: env tree {result.get('base_tree')} != host {transfer.base_tree}"
        )


async def import_config(
    sbox: SandboxEnvironment, handle: EnvHandle, spec: DevSpec, user: SandboxUser
) -> None:
    if spec.config_tar is not None:
        remote = f"{IN_DIR}/config.tar"
        await handle.push(spec.config_tar, remote)
        await run_root(sbox, ["chmod", "0644", remote], "config permissions")
        script = (
            "set -eu; t=$(mktemp -d); trap 'rm -rf \"$t\"' EXIT; "
            'tar -xf "$1" --no-same-owner --no-same-permissions -C "$t"; '
            'if [ -d "$t/home" ]; then cp -R "$t/home/." "$HOME/"; fi; '
            'if [ -d "$t/workspace" ]; then cp -R "$t/workspace/." "$2/"; fi'
        )
        await _sh_as(sbox, user.name, script, remote, spec.vm_root, what="config import")
    identity = [a for k, v in spec.git_identity.items() for a in (k, v)]
    if identity:
        await _sh_as(
            sbox,
            user.name,
            'while [ $# -gt 1 ]; do git config --global "$1" "$2"; shift 2; done',
            *identity,
            what="git identity",
        )
    if spec.claude_seed is not None:
        path = f"{user.home}/.claude/.claude.json"
        result = await sbox.exec(
            ["/bin/sh", "-c", 'umask 077; mkdir -p "$(dirname "$1")" && cat >"$1"', "sh", path],
            input=json.dumps(spec.claude_seed, indent=2) + "\n",
            user=user.name,
        )
        if not result.success:
            raise RuntimeError(f"cannot seed {path}: {result.stderr.strip()}")
        settings: dict[str, Any] = {"skipDangerousModePermissionPrompt": True}
        if not subscription(spec):
            settings["apiKeyHelper"] = f"echo {claude_harness.BRIDGE_API_KEY}"
        await install_root_file(sbox, CLAUDE_SETTINGS, json.dumps(settings) + "\n", "0644")


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            value = json.loads(line)
            if isinstance(value, dict):
                return value
    raise RuntimeError(f"no JSON result in output: {text.strip()[-500:]}")


def _prompt(io: DevIO, text: str, *, default: str) -> str:
    """``io.ask`` inside the eval's event loop; Ctrl+C / Ctrl+D answer ``default``.

    asyncio's own SIGINT handler would otherwise swallow Ctrl+C at the prompt and cancel
    the whole session (skipping the export), so Python's default handler is put back
    while the prompt waits.
    """
    previous: Any = None
    try:
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    except ValueError:  # not the main thread
        previous = None
    try:
        return io.ask(text).strip().lower()
    except (KeyboardInterrupt, EOFError):
        io.out("")
        return default
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


async def start_think_shim(sbox: SandboxEnvironment, user: SandboxUser) -> None:
    """Start the reasoning relay as the agent user, outside the inner sandbox; returns
    once it listens (a bwrap ``sandbox-exec`` relays its port in like the bridge's)."""
    cmd = ["python3", "-I", THINK_SHIM, "--listen", str(SHIM_PORT)]
    cmd += ["--upstream", str(BRIDGE_PORT), "--daemon"]
    result = await sbox.exec(cmd, user=user.name, timeout=60)
    if not result.success:
        raise RuntimeError(f"cannot start the reasoning relay: {result.stderr.strip()}")


async def push_token(
    sbox: SandboxEnvironment, handle: EnvHandle, spec: DevSpec, user: SandboxUser
) -> None:
    """Copy the subscription token to ``TOKEN_FILE`` (readable by the agent only).

    It goes with ``docker cp`` / ``ssh``, not Inspect's ``write_file``, which would
    put it in the session log.
    """
    if not spec.subscription_token:
        raise RuntimeError("no subscription token for an access: subscription model")
    fd, local = tempfile.mkstemp(prefix="agenteval-token-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(spec.subscription_token)
        await handle.push(Path(local), TOKEN_FILE)
    finally:
        os.remove(local)
    await run_root(
        sbox,
        ["/bin/sh", "-c", 'chown "$1" "$2" && chmod 0400 "$2"', "sh", str(user.uid), TOKEN_FILE],
        "token permissions",
    )


async def resolve_hosts(hosts: Sequence[str]) -> dict[str, list[str]]:
    """IPv4 addresses of ``hosts`` as the host machine resolves them.

    The proxy gets them pinned: on Tart the environment's resolver may not forward
    lookups off the box (``egress.sh``'s resolver guard), so it could not resolve them.
    """
    found: dict[str, list[str]] = {}
    for host in hosts:
        try:
            infos = await anyio.getaddrinfo(host, 443, family=socket.AF_INET)
        except OSError as ex:
            raise RuntimeError(f"cannot resolve {host} on this machine: {ex}") from ex
        found[host] = list(dict.fromkeys(str(info[4][0]) for info in infos))
    return found


async def start_egress_proxy(sbox: SandboxEnvironment, pins: Mapping[str, Sequence[str]]) -> None:
    """Start ``egress_proxy.py`` as ``PROXY_USER``; returns once it listens."""
    await run_root(
        sbox,
        ["install", "-m", "0644", "-o", PROXY_USER, "/dev/null", PROXY_LOG],
        "egress proxy log",
    )
    cmd = ["python3", "-I", EGRESS_PROXY, "--listen", str(PROXY_PORT), "--log", PROXY_LOG]
    cmd += [a for host in SUBSCRIPTION_HOSTS for a in ("--allow", host)]
    cmd += [a for host, ips in pins.items() for a in ("--resolve", f"{host}={','.join(ips)}")]
    result = await sbox.exec([*cmd, "--daemon"], user=PROXY_USER, cwd="/", timeout=60)
    if not result.success:
        raise RuntimeError(f"cannot start the egress proxy: {result.stderr.strip()}")


async def stop_agent(sbox: SandboxEnvironment, user: SandboxUser) -> None:
    """Stop everything still running as the agent (a CLI that outlived a dropped
    connection, servers it started), so the export sees a settled workspace."""
    await sbox.exec(["pkill", "-KILL", "-u", str(user.uid)], user="root")


async def attach(argv: Sequence[str], io: DevIO) -> int:
    """Run ``argv`` on the user's terminal; the event loop keeps serving the bridge."""
    saved: list[Any] | None = None
    stdin = sys.__stdin__
    try:
        if stdin is not None and stdin.isatty():
            saved = termios.tcgetattr(stdin.fileno())
    except termios.error:
        saved = None
    try:
        async with await anyio.open_process(
            list(argv), stdin=None, stdout=None, stderr=None
        ) as process:
            return await process.wait()
    finally:
        if saved is not None and stdin is not None:
            termios.tcsetattr(stdin.fileno(), termios.TCSAFLUSH, saved)
        out = sys.__stdout__
        if out is not None and out.isatty():
            out.write(TERMINAL_RESET)
            out.flush()


async def run_session(
    sbox: SandboxEnvironment, handle: EnvHandle, spec: DevSpec, io: DevIO, user: SandboxUser
) -> None:
    """Attach until the user is done (resume with ``--continue`` after a crash)."""
    if spec.harness.agent == "opencode":
        await start_think_shim(sbox, user)
    if spec.exec_script is not None:
        argv = handle.exec_argv(
            user.name, spec.vm_cwd, ["/bin/sh", "-c", spec.exec_script],
            {"AGENTEVAL_DEV_LAUNCH": LAUNCHER},
        )  # fmt: skip
        result = await anyio.run_process(argv, check=False)
        output = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
        io.out(visible_text(output))
        if result.returncode != 0:
            raise RuntimeError(f"dev --exec script failed (exit {result.returncode})")
        return
    extra: list[str] = []
    while True:
        code = await attach(handle.attach_argv(user.name, spec.vm_cwd, [LAUNCHER, *extra]), io)
        # the CLI (or what it started) may outlive a dropped connection: stop it so the
        # export sees a settled workspace
        await stop_agent(sbox, user)
        if code == 0 or not io.interactive:
            return
        prompt = f"The CLI exited with code {code}. [r] resume the conversation  [f] finish: "
        if _prompt(io, prompt, default="f") != "r":
            return
        extra = ["--continue"]
        if spec.harness.agent == "opencode":
            await start_think_shim(sbox, user)  # stop_agent stopped it with the CLI


async def export(
    sbox: SandboxEnvironment, handle: EnvHandle, spec: DevSpec, user: SandboxUser
) -> Exported:
    """Copy the changes out (as the agent user; the host validates everything)."""
    export_json: dict[str, Any] | None = None
    bundle: Path | None = None
    if spec.repo is not None:
        args = ["--workspace", spec.vm_root, "--base", spec.repo.base_commit, "--out", OUT_DIR]
        if spec.repo.head:
            args += ["--head", spec.repo.head]
        out = await _sh_as(
            sbox, user.name, 'f="$1"; shift; exec python3 -I "$f" export "$@"', DEV_REPO, *args,
            what="repo export",
        )  # fmt: skip
        export_json = _last_json(out)
        bundle = spec.session_dir / "commits.bundle"
        await _pull_checked(sbox, handle, f"{OUT_DIR}/commits.bundle", bundle, user)
    config_tar: Path | None = None
    home_paths, workspace_paths = spec.config_export
    if home_paths or workspace_paths:
        script = (
            'set -eu; out="$1"; ws="$2"; shift 2; s=$(mktemp -d); trap \'rm -rf "$s"\' EXIT; '
            'ln -s "$HOME" "$s/home"; ln -s "$ws" "$s/workspace"; : >"$s/list"; '
            'for p in "$@"; do if [ -e "$s/$p" ] || [ -L "$s/$p" ]; then '
            'printf "%s\\n" "$p" >>"$s/list"; fi; done; '
            'tar -cf "$out" -C "$s" --no-recursion -T /dev/null; '
            'if [ -s "$s/list" ]; then tar -cf "$out" -C "$s" -T "$s/list"; fi'
        )
        members = [f"home/{p}" for p in home_paths] + [f"workspace/{p}" for p in workspace_paths]
        remote = f"{OUT_DIR}/config.tar"
        await _sh_as(sbox, user.name, script, remote, spec.vm_root, *members, what="config export")
        config_tar = spec.session_dir / "config-return.tar"
        await _pull_checked(sbox, handle, remote, config_tar, user)
    return Exported(commits_bundle=bundle, export=export_json, config_tar=config_tar)


async def _pull_checked(
    sbox: SandboxEnvironment, handle: EnvHandle, remote: str, local: Path, user: SandboxUser
) -> None:
    size = await sbox.exec(["stat", "-c", "%s %F", remote], user=user.name)
    parts = size.stdout.split(maxsplit=1)
    if not size.success or len(parts) != 2 or parts[1].strip() != "regular file":
        raise RuntimeError(f"{remote} is missing or not a regular file")
    if int(parts[0]) > MAX_PULL_BYTES:
        raise RuntimeError(f"{remote} is larger than {MAX_PULL_BYTES} bytes")
    local.unlink(missing_ok=True)
    await handle.pull(remote, local, user=user.name)
    # checked again here: the env side is the agent's, the local copy is ours
    if local.is_symlink() or not local.is_file() or local.stat().st_size > MAX_PULL_BYTES:
        local.unlink(missing_ok=True)
        raise RuntimeError(f"{remote}: the copied file is not a regular file within the limit")


async def export_with_recovery(
    sbox: SandboxEnvironment,
    handle: EnvHandle,
    spec: DevSpec,
    io: DevIO,
    user: SandboxUser,
    on_export: ExportHandler,
) -> None:
    while True:
        try:
            exported = await export(sbox, handle, spec, user)
            on_export(exported)
            return
        except Exception as ex:
            if not io.interactive:
                raise
            io.out(
                f"\nCopying the changes out failed: {visible_text(str(ex))}\n"
                "The environment is still running. You can get in with:\n"
                f"  {handle.shell_hint(user.name, spec.vm_cwd)}\n"
            )
            prompt = (
                "[r] retry  [s] open a shell in the environment  "
                "[a] abandon (destroys the environment and its changes): "
            )
            answer = _prompt(io, prompt, default="a")
            while answer not in ("r", "s", "a"):  # only an explicit 'a' destroys anything
                io.out("Answer r, s or a.")
                answer = _prompt(io, prompt, default="a")
            if answer == "s":
                await attach(handle.attach_argv(user.name, spec.vm_cwd, ["bash", "-l"]), io)
                await stop_agent(sbox, user)
            elif answer == "a":
                raise


def spend_limits(spec: DevSpec) -> list[Limit]:
    """Scoped limits for the bridge, never sample limits: see ``tripped_limit``."""
    limits: list[Limit] = []
    if spec.token_limit is not None:
        # output tokens: in a long session the total is mostly cache re-reads
        limits.append(token_limit(spec.token_limit, type="output"))
    if spec.cost_limit is not None:
        limits.append(cost_limit(spec.cost_limit))
    return limits


def tripped_limit(limits: Sequence[Limit]) -> LimitExceededError | None:
    """The error of one of ``limits`` that ended the sample, if any.

    A bridged call that trips a limit raises ``LimitExceededError`` in the bridge's
    sandbox service task, which never propagates it: it calls
    ``ActiveSample.limit_exceeded``, which records the error and cancels the sample's
    task group. The solver only sees a cancellation, wherever it was waiting.
    """
    active = sample_active()
    error = active.limit_exceeded_error if active is not None else None
    if error is None or not any(error.source is limit for limit in limits):
        return None
    return error


def limit_message(error: LimitExceededError) -> str:
    if error.type == "cost":
        reached = f"Spend limit reached (${error.limit:g})"
    else:
        reached = f"Output token limit reached ({int(error.limit):,} tokens)"
    return f"{reached}: the CLI was stopped; your changes are copied out for review as usual."


@solver
def dev_session(spec: DevSpec, io: DevIO, on_export: ExportHandler) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sbox = sandbox()
        handle = await env_handle(sbox, spec.environment)
        user = await resolve_user(sbox, spec.dev_env.agent_user)
        if spec.mount == "live":
            user = await prepare_live(sbox, spec, user)
        await base_setup(sbox, spec, user)
        await install_sandbox_exec(
            sbox, spec.harness.inner_sandbox, spec.dev_env, verify_user=user.name, quiet_relays=True
        )
        binary, dep_dirs = await install_cli(sbox, spec, user.name)
        await import_repo(sbox, handle, spec, user)
        if spec.vm_cwd != spec.vm_root:  # e.g. started in a folder git does not track
            await _sh_as(sbox, user.name, 'mkdir -p -- "$1"', spec.vm_cwd, what="working dir")
        await import_config(sbox, handle, spec, user)

        harness = spec.harness
        aliases: dict[str, str | Model] = {}
        bridge_model: str | None = None
        env_files: dict[str, str] = {}
        if subscription(spec):
            await push_token(sbox, handle, spec, user)
            await start_egress_proxy(sbox, await resolve_hosts(SUBSCRIPTION_HOSTS))
            env_files["CLAUDE_CODE_OAUTH_TOKEN"] = TOKEN_FILE
            env, args = claude_launch(spec, models=None, home=user.home)
        elif harness.agent == "claude_code":
            worker = build_model(spec.subagent_model) if spec.subagent_model else None
            subagent, routing = claude_harness.subagent_routing(
                harness, spec.subagent_model, worker
            )
            models = resolve_claude_code_models(
                None, None, subagent_model=subagent, model_aliases=routing or None
            )
            env, args = claude_launch(spec, models=models, home=user.home)
            aliases, bridge_model = models.aliases, models.bridge_model
        else:
            env, args = opencode_launch(spec, dep_dirs=dep_dirs, home=user.home)
        launcher = render_launcher(
            env=env,
            cwd=spec.vm_cwd,
            binary=binary,
            args=args,
            relay=harness.inner_sandbox.type == "bwrap",
            env_files=env_files,
        )

        bridge = (
            contextlib.nullcontext()
            if subscription(spec)
            else sandbox_agent_bridge(
                AgentState(messages=list(state.messages)),
                model=bridge_model,
                model_aliases=aliases or None,
                port=BRIDGE_PORT,
                poll_timeout_recovery=POLL_RECOVERY,
                web_search=True if spec.dev_env.egress == "allow" else None,
            )
        )
        limits = spend_limits(spec)
        try:
            # entered before the bridge: its model service task inherits the limits
            with contextlib.ExitStack() as stack:
                for limit in limits:
                    stack.enter_context(limit)
                async with bridge:
                    await install_root_file(sbox, LAUNCHER, launcher, "0755")
                    await run_session(sbox, handle, spec, io, user)
        except BaseException:
            tripped = tripped_limit(limits)
            if tripped is None:
                raise
            # Inspect cancels the whole sample when a bridged call trips a limit; finish
            # the session's normal end shielded from that, then let the sample end
            with anyio.CancelScope(shield=True):
                io.out(f"\n{limit_message(tripped)}")
                await stop_agent(sbox, user)
                await export_with_recovery(sbox, handle, spec, io, user, on_export)
            raise
        await stop_agent(sbox, user)
        await export_with_recovery(sbox, handle, spec, io, user, on_export)
        return state

    return solve


def build_dev_task(spec: DevSpec, io: DevIO, on_export: ExportHandler) -> Task:
    return Task(
        name=f"dev--{spec.harness.name}--{spec.environment.name}--{spec.model.name}",
        dataset=[Sample(input="agenteval dev session", id=spec.session_id)],
        solver=dev_session(spec, io, on_export),
        sandbox=sandbox_spec(
            spec.environment,
            inner_sandbox=spec.harness.inner_sandbox.type,
            mounts=spec.mounts,
            filtered_network=subscription(spec),
        ),
        # a subscription session has no model on the Inspect side
        model=get_model("none") if subscription(spec) else build_model(spec.model),
        metadata={
            "agenteval_dev": {
                "session_id": spec.session_id,
                "mount": spec.mount,
                "model": spec.model.model_dump(),
                "harness": spec.harness.model_dump(),
                "environment": spec.environment.model_dump(),
            }
        },
    )
