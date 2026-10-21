from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import types
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ConfigDict

from agenteval.config import (
    DockerSettings,
    EnvironmentProfile,
    TartSettings,
    load_profiles,
)
from agenteval.constants import BWRAP_GROUP, COLLECTOR_HOST
from agenteval.environments import PROVISION_DIR, PROVISION_SCRIPT, docker, sandbox_spec

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = DockerSettings().image


def docker_env(**overrides: Any) -> EnvironmentProfile:
    data: dict[str, Any] = {"name": "docker-test", "provider": "docker"} | overrides
    return EnvironmentProfile.model_validate(data)


BWRAP_OPTS = ["seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined"]
"""What bubblewrap needs inside a container: user namespaces (seccomp) and a fresh
procfs (Docker's masked /proc paths forbid that unless systempaths=unconfined)."""


def service(
    env: EnvironmentProfile, inner_sandbox: docker.InnerSandboxType = "none"
) -> dict[str, Any]:
    config = docker.compose_config(env, inner_sandbox=inner_sandbox)
    assert list(config) == ["services"]
    assert list(config["services"]) == ["default"]
    svc: dict[str, Any] = config["services"]["default"]
    return svc


# --------------------------------------------------------------------------- compose


def test_compose_filtered_network() -> None:
    """agenteval dev on a subscription: a network, with egress denied by nftables."""
    svc = docker.compose_config(docker_env(), filtered_network=True)["services"]["default"]
    assert svc["network_mode"] == "bridge"
    assert svc["cap_add"] == ["NET_ADMIN"]
    allow = docker_env().model_copy(update={"egress": "allow"})
    svc = docker.compose_config(allow, filtered_network=True)["services"]["default"]
    assert "network_mode" not in svc and "cap_add" not in svc


def test_compose_defaults_deny_egress() -> None:
    svc = service(docker_env())
    assert svc["image"] == IMAGE
    assert svc["init"] is True
    assert svc["command"] == ["tail", "-f", "/dev/null"]
    assert svc["working_dir"] == "/workspace"
    assert svc["network_mode"] == "none"
    assert "security_opt" not in svc  # stock Docker confinement outside bwrap cells
    assert svc["mem_limit"] == "4g"
    assert svc["pids_limit"] == 4096
    assert svc["x-local"] is True
    assert svc["pull_policy"] == "never"
    assert svc["extra_hosts"] == [f"{COLLECTOR_HOST}:127.0.0.1"]
    assert "runtime" not in svc
    assert "cpus" not in svc


def test_compose_bwrap_cells_get_bwrap_security_opts() -> None:
    env = docker_env()
    assert service(env, "bwrap")["security_opt"] == BWRAP_OPTS
    assert DockerSettings().bwrap_security_opt == BWRAP_OPTS
    without_bwrap = {k: v for k, v in service(env, "bwrap").items() if k != "security_opt"}
    assert without_bwrap == service(env, "none")


def test_compose_security_opt_merges_and_dedupes() -> None:
    env = docker_env(docker={"security_opt": ["no-new-privileges:true", "seccomp=unconfined"]})
    assert service(env)["security_opt"] == ["no-new-privileges:true", "seccomp=unconfined"]
    assert service(env, "bwrap")["security_opt"] == [
        "no-new-privileges:true",
        "seccomp=unconfined",
        "apparmor=unconfined",
        "systempaths=unconfined",
    ]
    no_relax = docker_env(docker={"bwrap_security_opt": []})
    assert "security_opt" not in service(no_relax, "bwrap")


def test_compose_allow_egress_runtime_and_limits() -> None:
    env = docker_env(
        egress="allow",
        workspace="/src",
        docker={
            "image": "example/img:1",
            "runtime": "runsc",
            "security_opt": [],
            "memory": "2g",
            "cpus": 1.5,
            "pids_limit": 512,
        },
    )
    svc = service(env)
    assert "network_mode" not in svc
    assert svc["runtime"] == "runsc"
    assert "security_opt" not in svc
    assert svc["mem_limit"] == "2g"
    assert svc["cpus"] == 1.5
    assert svc["pids_limit"] == 512
    assert "pids_limit" not in service(docker_env(docker={"pids_limit": None}))
    assert svc["working_dir"] == "/src"
    assert svc["image"] == "example/img:1"


@pytest.mark.parametrize("inner_sandbox", ["none", "bwrap"])
def test_compose_has_no_host_access(inner_sandbox: docker.InnerSandboxType) -> None:
    config = docker.compose_config(docker_env(egress="allow"), inner_sandbox=inner_sandbox)
    text = yaml.safe_dump(config)
    for key in (
        "volumes",
        "volumes_from",
        "configs",
        "secrets",
        "privileged",
        "cap_add",
        "devices",
        "ports",
        "env_file",
        "build",
    ):
        assert key not in text, key


def test_compose_rejects_non_docker_env() -> None:
    env = EnvironmentProfile(name="vm", provider="tart")
    with pytest.raises(ValueError, match="not a docker environment"):
        docker.compose_config(env)


HASHED_NAME = re.compile(r"docker-test-[0-9a-f]{12}\.yaml")


def test_write_compose_file(tmp_path: Path) -> None:
    env = docker_env()
    path = docker.write_compose_file(env, root=tmp_path)
    assert path.parent == (tmp_path / ".agenteval" / "compose").resolve()
    assert HASHED_NAME.fullmatch(path.name), path.name
    assert path.is_absolute()
    text = path.read_text()
    assert text.startswith("# Generated by agenteval")
    assert yaml.safe_load(text) == docker.compose_config(env)
    assert path == docker.compose_path(env, text, tmp_path)

    mtime = path.stat().st_mtime_ns
    assert docker.write_compose_file(env, root=tmp_path) == path
    assert path.stat().st_mtime_ns == mtime  # unchanged content is not rewritten
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_changed_profile_never_rewrites_a_file_in_use(tmp_path: Path) -> None:
    """A second run with a same-named, different profile gets its own compose file."""
    deny = docker.write_compose_file(docker_env(), root=tmp_path)
    before = deny.read_text()
    allow = docker.write_compose_file(docker_env(egress="allow"), root=tmp_path)
    assert allow != deny
    assert deny.read_text() == before and "network_mode: none" in before
    assert "network_mode" not in allow.read_text()

    bwrap = docker.write_compose_file(docker_env(), root=tmp_path, inner_sandbox="bwrap")
    assert bwrap not in (deny, allow)
    assert "systempaths=unconfined" in bwrap.read_text()
    assert "security_opt" not in deny.read_text()
    assert sorted(p.name for p in deny.parent.iterdir()) == sorted(
        p.name for p in (deny, allow, bwrap)
    )


def test_write_compose_file_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = docker.write_compose_file(docker_env())
    assert path.parent == (tmp_path / ".agenteval/compose").resolve()
    assert HASHED_NAME.fullmatch(path.name)


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "x y"])
def test_compose_path_rejects_unsafe_names(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not usable as a file name"):
        docker.compose_path(docker_env(name=name), "text", tmp_path)


# --------------------------------------------------------------------------- sandbox_spec


def test_sandbox_spec_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = sandbox_spec(docker_env())
    assert spec.type == "docker"
    assert isinstance(spec.config, str)
    config_path = Path(spec.config)
    assert config_path.is_absolute()
    assert config_path.parent == (tmp_path / ".agenteval/compose").resolve()
    svc = yaml.safe_load(config_path.read_text())["services"]["default"]
    assert svc["image"] == IMAGE
    assert "security_opt" not in svc


def test_sandbox_spec_docker_bwrap_cell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    none_spec = sandbox_spec(docker_env())
    bwrap_spec = sandbox_spec(docker_env(), inner_sandbox="bwrap")
    assert isinstance(bwrap_spec.config, str) and bwrap_spec.config != none_spec.config
    svc = yaml.safe_load(Path(bwrap_spec.config).read_text())["services"]["default"]
    assert svc["security_opt"] == BWRAP_OPTS


def test_sandbox_spec_example_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles = load_profiles(REPO_ROOT / "config")
    monkeypatch.chdir(tmp_path)
    for env in profiles.environments.values():
        if env.provider == "docker":
            spec = sandbox_spec(env)
            assert re.fullmatch(
                rf"{re.escape(env.name)}-[0-9a-f]{{12}}\.yaml", Path(spec.config).name
            )


class _FakeTartSandboxConfig(TartSettings):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_cwd: str
    default_user: str = "root"


def test_sandbox_spec_tart_with_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("agenteval.environments.tart.sandbox")
    fake.TartSandboxConfig = _FakeTartSandboxConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agenteval.environments.tart.sandbox", fake)

    env = EnvironmentProfile(
        name="vm", provider="tart", workspace="/work", tart=TartSettings(cpu=2, image="img")
    )
    spec = sandbox_spec(env)
    assert spec.type == "tart"
    assert isinstance(spec.config, _FakeTartSandboxConfig)
    assert spec.config.default_cwd == "/work"
    assert spec.config.default_user == "root"
    assert spec.config.cpu == 2
    assert spec.config.image == "img"


def test_sandbox_spec_tart_real_module() -> None:
    """Integration with the Tart provider once it exists (owned by another module)."""
    tart_sandbox = pytest.importorskip("agenteval.environments.tart.sandbox")
    env = EnvironmentProfile(name="vm", provider="tart", workspace="/work")
    spec = sandbox_spec(env)
    assert spec.type == "tart"
    assert isinstance(spec.config, tart_sandbox.TartSandboxConfig)
    assert spec.config.default_cwd == "/work"


# --------------------------------------------------------------------------- image build


def test_build_command() -> None:
    env = docker_env(agent_user="dev", workspace="/src")
    cmd = docker.build_command(env)
    assert cmd[:4] == ["docker", "build", "--tag", IMAGE]
    assert "AGENT_USER=dev" in cmd and "WORKSPACE=/src" in cmd
    assert cmd[-1] == str(PROVISION_DIR)
    assert "--no-cache" not in cmd and "--platform" not in cmd

    forced = docker.build_command(env, force=True, platform="linux/arm64")
    assert forced[forced.index("--platform") + 1] == "linux/arm64"
    assert "--no-cache" in forced and "--pull" in forced
    assert forced[-1] == str(PROVISION_DIR)


def test_build_image_runs_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(docker, "docker_available", lambda: "/opt/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    docker.build_image(docker_env())
    assert len(calls) == 1
    assert calls[0][0] == "/opt/bin/docker"
    assert calls[0][1:] == docker.build_command(docker_env())[1:]


def test_build_image_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "docker_available", lambda: "/opt/bin/docker")
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: subprocess.CompletedProcess(cmd, 3))
    with pytest.raises(docker.DockerError, match="exit code 3"):
        docker.build_image(docker_env())


def test_build_image_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker, "docker_available", lambda: None)
    with pytest.raises(docker.DockerError, match="not found"):
        docker.build_image(docker_env())
    assert docker.image_exists(IMAGE) is False


def test_build_image_rejects_tart_env() -> None:
    with pytest.raises(ValueError):
        docker.build_image(EnvironmentProfile(name="vm", provider="tart"))


# --------------------------------------------------------------------------- provisioning files


def test_provision_script_syntax() -> None:
    assert PROVISION_SCRIPT.is_file()
    result = subprocess.run(["bash", "-n", str(PROVISION_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_provision_script_shellcheck() -> None:
    result = subprocess.run(
        ["shellcheck", "-S", "warning", str(PROVISION_SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def test_provision_script_contents() -> None:
    text = PROVISION_SCRIPT.read_text()
    for pkg in (
        "bubblewrap",
        "socat",
        "python3",
        "python3-pytest",
        "git",
        "curl",
        "ca-certificates",
        "nftables",
        "iproute2",
        "procps",
        "sudo",
        "passwd",
    ):
        assert f" {pkg}" in text, pkg
    assert "set -euo pipefail" in text
    assert COLLECTOR_HOST in text
    assert "userns," in text
    # bwrap is limited to root and the group the agent joins only in bwrap cells
    assert f'BWRAP_GROUP="{BWRAP_GROUP}"' in text
    assert 'dpkg-statoverride --update --add root "$BWRAP_GROUP" 0750 "$BWRAP"' in text
    # apt lists lock retries; periodic package jobs are off in VMs
    assert 'retry_on_lock apt-get "${apt_opts[@]}" update -q' in text
    for unit in ("apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service"):
        assert unit in text
    assert "/etc/agenteval/image.json" in text


def test_provision_retry_on_lock(tmp_path: Path) -> None:
    """retry_on_lock retries lock errors only and passes other failures through."""
    text = PROVISION_SCRIPT.read_text()
    start = text.index("retry_on_lock() {")
    func = text[start : text.index("\n}\n", start) + 3]
    counter = tmp_path / "n"
    script = (
        "log() { :; }\nsleep() { :; }\n"
        + func
        + f"""
locked_twice() {{
  n=$(( $(cat {counter} 2>/dev/null || echo 0) + 1 )); echo "$n" > {counter}
  [ "$n" -ge 3 ] && {{ echo updated; return 0; }}
  echo "E: Could not get lock /var/lib/apt/lists/lock. It is held by process 42"; return 100
}}
broken() {{ echo "E: something else"; return 100; }}
retry_on_lock locked_twice && echo "rc=0 n=$(cat {counter})"
retry_on_lock broken || echo "rc=$?"
"""
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n") == ["updated", "rc=0 n=3", "rc=1", ""]
    assert "something else" in result.stderr


def test_dockerfile() -> None:
    text = (PROVISION_DIR / "Dockerfile").read_text().replace("\\\n", " ")
    lines = [
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "FROM ubuntu:24.04"
    assert any(line.startswith("COPY provision.sh ") for line in lines)
    assert any("provision.sh" in line and line.startswith("RUN") for line in lines)
    assert "WORKDIR ${WORKSPACE}" in lines
    assert not any(line.startswith(("USER", "VOLUME", "EXPOSE")) for line in lines)


# --------------------------------------------------------------------------- docker (optional)


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False
    return info.returncode == 0 and docker.image_exists(IMAGE)


needs_image = pytest.mark.skipif(
    not _docker_ready(), reason=f"docker daemon or image {IMAGE} unavailable"
)


def run_in_image(
    script: str, *, security_opt: list[str] | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "run", "--rm", "--network", "none"]
    if stdin is not None:
        cmd.append("-i")
    for opt in security_opt or []:
        cmd += ["--security-opt", opt]
    cmd += [IMAGE, "bash", "-c", script]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)


@pytest.mark.docker
@needs_image
def test_image_provisioned_state() -> None:
    result = run_in_image(
        "set -e; id -u agent; id -nG agent; getent passwd agent | cut -d: -f6,7;"
        "stat -c '%U:%a' /workspace /opt/agenteval /var/lib/agenteval /etc/agenteval/secrets;"
        "pwd; git config --system safe.directory; git config --system user.email;"
        "for b in bwrap socat python3 git curl nft ip ps sudo; do command -v $b >/dev/null; done;"
        "python3 -m pytest --version >/dev/null; passwd -S agent | cut -d' ' -f2;"
        "su agent -c 'sudo -n true' 2>/dev/null && echo SUDO_OK || echo NO_SUDO"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\n") == [
        "1001",
        "agent",
        "/home/agent:/bin/bash",
        "agent:755",
        "root:755",
        "root:700",
        "root:755",
        "/workspace",
        "*",
        "agent@agenteval.test",
        "L",
        "NO_SUDO",
        "",
    ]


@pytest.mark.docker
@needs_image
def test_provision_is_idempotent_in_image() -> None:
    """Re-running provision.sh in the built image changes nothing it already did."""
    script = PROVISION_SCRIPT.read_text()
    result = run_in_image(
        "set -e; cat > /tmp/p.sh; bash /tmp/p.sh >/dev/null; bash /tmp/p.sh;"
        "git config --system --get-all safe.directory | wc -l;"
        "stat -c '%U:%G:%a' /usr/bin/bwrap /etc/agenteval/image.json;"
        "cat /etc/agenteval/image.json",
        stdin=script,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "packages already installed" in result.stdout
    assert "creating user" not in result.stdout
    assert "restricted /usr/bin/bwrap" not in result.stdout  # already done by the first run
    lines = result.stdout.strip().splitlines()
    assert lines[-4:-1] == ["1", f"root:{BWRAP_GROUP}:750", "root:root:644"]
    assert json.loads(lines[-1]) == {
        "agent_user": "agent",
        "workspace": "/workspace",
        "provision_sha256": hashlib.sha256(script.encode()).hexdigest(),
    }


@pytest.mark.docker
@needs_image
def test_provisioned_bwrap_is_group_only() -> None:
    """After provisioning, the agent can run bwrap only once it is in the bwrap group."""
    bwrap = "bwrap --ro-bind / / --unshare-all --dev /dev --proc /proc true"
    result = run_in_image(
        f"cat > /tmp/p.sh && bash /tmp/p.sh >/dev/null || exit 9;"
        f"su agent -c {bwrap!r} 2>/dev/null; echo outsider=$?;"
        f"usermod -aG {BWRAP_GROUP} agent; su agent -c {bwrap!r}; echo member=$?;"
        f"{bwrap}; echo root=$?",
        security_opt=BWRAP_OPTS,
        stdin=PROVISION_SCRIPT.read_text(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.split() == ["outsider=126", "member=0", "root=0"]


# Group setup works on images built before bwrap was restricted to the group, too.
_JOIN_BWRAP_GROUP = (
    f"getent group {BWRAP_GROUP} >/dev/null || groupadd {BWRAP_GROUP};"
    f"usermod -aG {BWRAP_GROUP} agent;"
)


@pytest.mark.docker
@needs_image
def test_bwrap_works_for_agent_in_image() -> None:
    bwrap = "bwrap --ro-bind / / --unshare-all --dev /dev --proc /proc true"
    result = run_in_image(
        f"{_JOIN_BWRAP_GROUP} su agent -c {bwrap!r}",
        security_opt=DockerSettings().bwrap_security_opt,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.docker
@needs_image
def test_default_security_opts_keep_stock_confinement() -> None:
    """Outside bwrap cells: no user namespaces for the agent, read-only kernel knobs for root.

    Only access(2) checks and a no-op unshare: nothing is written.
    """
    script = (
        f"{_JOIN_BWRAP_GROUP}"
        "su agent -c 'unshare -Ur true' 2>/dev/null; echo userns=$?;"
        "su agent -c 'bwrap --ro-bind / / --unshare-all --dev /dev true' 2>/dev/null;"
        "echo bwrap=$?;"
        "test -w /proc/sys/kernel/core_pattern; echo core_pattern_w=$?;"
        "test -w /proc/sysrq-trigger; echo sysrq_w=$?"
    )
    stock = run_in_image(script, security_opt=DockerSettings().security_opt)
    assert stock.returncode == 0, stock.stderr
    assert stock.stdout.split() == ["userns=1", "bwrap=1", "core_pattern_w=1", "sysrq_w=1"]


@pytest.fixture
def compose_project(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    name = f"agenteval-test-{uuid.uuid4().hex[:8]}"
    path = docker.write_compose_file(docker_env(), root=tmp_path)
    try:
        yield name, path
    finally:
        subprocess.run(
            ["docker", "compose", "-p", name, "-f", str(path), "down", "--volumes", "-t", "1"],
            capture_output=True,
            timeout=120,
        )


@pytest.mark.docker
@needs_image
def test_compose_file_runs(compose_project: tuple[str, Path]) -> None:
    name, path = compose_project
    base = ["docker", "compose", "-p", name, "-f", str(path)]
    up = subprocess.run([*base, "up", "-d", "--wait"], capture_output=True, text=True, timeout=180)
    assert up.returncode == 0, up.stderr
    check = subprocess.run(
        [
            *base,
            "exec",
            "-T",
            "default",
            "bash",
            "-c",
            "pwd; getent hosts collector.agenteval.test | cut -d' ' -f1; cat /proc/1/comm;"
            "ip -o -4 addr show | grep -v ' lo ' | wc -l; ls -A /workspace | wc -l;"
            "cat /sys/fs/cgroup/pids.max",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.split() == ["/workspace", "127.0.0.1", "docker-init", "0", "0", "4096"]


# --- dev live mounts ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("inner", "digest"),
    [
        ("none", "ce000cbbde46ddcf5e74bf0318afb604f2876332d80ab13559b4dd96e3572317"),
        ("bwrap", "e9c79508167d486ba538dea13969c5a4f78a2a17bbf19b2efdb1ab61458040aa"),
    ],
)
def test_compose_text_unchanged_without_mounts(inner: str, digest: str) -> None:
    """Eval runs share no host folders: adding dev mounts must not change their compose file."""
    from agenteval.config import load_profiles

    env = load_profiles(REPO_ROOT / "config").environments["docker-ubuntu"]
    text = docker.compose_text(env, inner_sandbox=inner)  # type: ignore[arg-type]
    assert hashlib.sha256(text.encode()).hexdigest() == digest
    assert "volumes" not in text


def test_compose_mounts_bind_same_path() -> None:
    assert "x-inspect_docker_sandbox" not in docker.compose_config(docker_env())
    config = docker.compose_config(docker_env(), mounts=["/Users/me/proj", "/Users/me/x y"])
    assert config["x-inspect_docker_sandbox"] == {"unsafe_allow_writable_host_mounts": True}
    assert config["services"]["default"]["volumes"] == [
        {"type": "bind", "source": "/Users/me/proj", "target": "/Users/me/proj"},
        {"type": "bind", "source": "/Users/me/x y", "target": "/Users/me/x y"},
    ]


def test_sandbox_spec_tart_shared_dirs() -> None:
    from agenteval.environments.tart.sandbox import TartSandboxConfig

    env = EnvironmentProfile(name="t", provider="tart")
    spec = sandbox_spec(env, mounts=["/Users/me/proj"])
    assert isinstance(spec.config, TartSandboxConfig)
    assert spec.config.shared_dirs == ("/Users/me/proj",)
    assert sandbox_spec(env).config.shared_dirs == ()  # type: ignore[union-attr]
