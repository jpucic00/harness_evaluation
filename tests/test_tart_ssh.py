"""Unit tests for the Tart provider's pure parts: SSH/remote command construction,
failure classification, config handling and image-build helpers. No Tart needed."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
from inspect_ai.util import SandboxEnvironmentSpec, SandboxUserUnsupportedError
from inspect_ai.util._sandbox.registry import registry_find_sandboxenv

import agenteval._registry  # noqa: F401  (registers "tart")
from agenteval.config import TartSettings
from agenteval.environments.tart import image, ssh
from agenteval.environments.tart.sandbox import (
    TartSandboxConfig,
    TartSandboxEnvironment,
    _timed_out,
    resolve_config,
)
from agenteval.environments.tart.ssh import (
    EXEC_SCRIPT_INPUT,
    EXEC_SCRIPT_NOINPUT,
    RemoteTools,
    SSHTarget,
    remote_command,
    ssh_argv,
    ssh_options,
)

KEY = Path("/keys/id_ed25519")


def _opt_pairs(opts: list[str]) -> dict[str, str]:
    pairs = {}
    for flag, value in zip(opts, opts[1:], strict=False):
        if flag == "-o":
            k, _, v = value.partition("=")
            pairs[k] = v
    return pairs


# --------------------------------------------------------------------------- ssh options


def test_ssh_options_harden_the_client() -> None:
    opts = ssh_options(KEY)
    assert opts[:2] == ["-F", "/dev/null"]
    assert opts[opts.index("-i") + 1] == str(KEY)
    pairs = _opt_pairs(opts)
    assert pairs == {
        "BatchMode": "yes",
        "IdentitiesOnly": "yes",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "StrictHostKeyChecking": "no",
        "UserKnownHostsFile": "/dev/null",
        "LogLevel": "ERROR",
        "ForwardAgent": "no",
        "ForwardX11": "no",
        "ClearAllForwardings": "yes",
        "ConnectTimeout": "10",
        "ServerAliveInterval": "15",
        "ServerAliveCountMax": "4",
    }


def test_password_options_have_no_key_and_no_batch_mode() -> None:
    opts = ssh_options(None, password_auth=True)
    assert "-i" not in opts
    pairs = _opt_pairs(opts)
    assert pairs["BatchMode"] == "no"
    assert pairs["PubkeyAuthentication"] == "no"
    assert pairs["ForwardAgent"] == "no"
    with pytest.raises(ValueError):
        ssh_options(None)


def test_ssh_argv_layout() -> None:
    target = SSHTarget(host="192.168.64.7", user="admin", key=KEY)
    argv = ssh_argv(target, "true", ssh="/usr/bin/ssh")
    assert argv[0] == "/usr/bin/ssh"
    assert argv[-3:] == ["--", "192.168.64.7", "true"]
    assert "-T" in argv and "-t" not in argv
    assert argv[argv.index("-l") + 1] == "admin"
    assert argv[argv.index("-p") + 1] == "22"
    login = ssh_argv(target, None, tty=True)
    assert login[0] == "ssh" and login[-1] == "192.168.64.7" and "-t" in login


# --------------------------------------------------------------------------- remote command


def test_remote_command_full_wrapper() -> None:
    remote = remote_command(
        ["echo", "hi"],
        cwd="/workspace",
        env={"A": "1", "B": "two words"},
        user="agent",
        timeout=30,
    )
    assert shlex.split(remote) == [
        "/usr/bin/sudo", "-n", "-H", "-u", "agent", "--",
        "/usr/bin/timeout", "--kill-after=5", "30",
        "/usr/bin/env", "A=1", "B=two words",
        "/bin/bash", "-c", EXEC_SCRIPT_NOINPUT, "agenteval-exec", "/workspace",
        "echo", "hi",
    ]  # fmt: skip


def test_remote_command_minimal_without_sudo() -> None:
    remote = remote_command(["true"], cwd="/w", use_sudo=False)
    assert shlex.split(remote) == [
        "/bin/bash", "-c", EXEC_SCRIPT_NOINPUT, "agenteval-exec", "/w", "true",
    ]  # fmt: skip
    piped = remote_command(["cat"], cwd="/w", use_sudo=False, has_input=True)
    assert shlex.split(piped) == [
        "/bin/bash", "-c", EXEC_SCRIPT_INPUT, "agenteval-exec", "/w", "cat",
    ]  # fmt: skip


def test_command_never_gets_the_session_stdin() -> None:
    """sshd's stdin is the session socket; a daemon inheriting it would hold the exec open."""
    assert EXEC_SCRIPT_NOINPUT.endswith('exec "$@" </dev/null')
    assert EXEC_SCRIPT_INPUT.endswith('cat | "$@"')
    # without input the command reads /dev/null, even if the caller's stdin has data
    quiet = remote_command(["cat"], cwd="/", use_sudo=False)
    out = subprocess.run(["/bin/bash", "-c", quiet], input="leak", capture_output=True, text=True)
    assert (out.returncode, out.stdout) == (0, "")
    # with input it arrives through cat, and the command's own status is returned
    piped = remote_command(
        ["/bin/sh", "-c", "cat; exit 7"], cwd="/", use_sudo=False, has_input=True
    )
    out = subprocess.run(["/bin/bash", "-c", piped], input="data", capture_output=True, text=True)
    assert (out.returncode, out.stdout) == (7, "data")


def test_remote_command_numeric_user_and_timeout_floor() -> None:
    argv = shlex.split(remote_command(["id"], cwd="/", user="1001", timeout=0))
    assert argv[argv.index("-u") + 1] == "#1001"
    assert argv[argv.index("--kill-after=5") + 1] == "1"


def test_remote_command_uses_tools_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh, "REMOTE_TOOLS", RemoteTools(bash="/opt/bash"))
    assert shlex.split(remote_command(["x"], cwd="/", use_sudo=False))[0] == "/opt/bash"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cmd": [], "cwd": "/"},
        {"cmd": ["x"], "cwd": "/", "use_sudo": True},
        {"cmd": ["x"], "cwd": "/", "use_sudo": False, "env": {"BAD-KEY": "1"}},
        {"cmd": ["x"], "cwd": "/", "use_sudo": False, "env": {"A=B": "1"}},
    ],
)
def test_remote_command_rejects_bad_input(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        remote_command(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("user", ["-oProxyCommand=x", "a b", "agent;id", "", "x/y"])
def test_invalid_users_are_unsupported(user: str) -> None:
    with pytest.raises(SandboxUserUnsupportedError):
        remote_command(["true"], cwd="/", user=user)


def test_hostile_values_stay_literal(tmp_path: Path) -> None:
    """Quoting round-trip through a real shell: nothing is evaluated."""
    marker = tmp_path / "pwned"
    nasty = [f"$(touch {marker})", f"`touch {marker}`", "it's", 'a"b', "x;y", "line\nbreak", "*"]
    cwd = tmp_path / "dir with 'quote' $(x)"
    cwd.mkdir()
    remote = remote_command(
        ["printf", "%s|", *nasty],
        cwd=str(cwd),
        env={"V": f"$(touch {marker})'"},
        use_sudo=False,
    )
    out = subprocess.run(["/bin/bash", "-c", remote], capture_output=True, text=True, check=True)
    assert out.stdout == "|".join(nasty) + "|"
    env_out = subprocess.run(
        ["/bin/bash", "-c", remote_command(["/bin/sh", "-c", 'printf %s "$V"; pwd'], cwd=str(cwd),
                                           env={"V": f"$(touch {marker})'"}, use_sudo=False)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert env_out.stdout == f"$(touch {marker})'{cwd}\n"
    assert not marker.exists()


def test_failed_cd_does_not_run_command(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    remote = remote_command(["touch", str(marker)], cwd=str(tmp_path / "missing"), use_sudo=False)
    result = subprocess.run(["/bin/bash", "-c", remote], capture_output=True, text=True)
    assert result.returncode != 0 and not marker.exists()


# --------------------------------------------------------------------------- classification


def test_ssh_failure_classification() -> None:
    refused = "ssh: connect to host 192.168.64.3 port 22: Connection refused\n"
    assert ssh.is_connect_failure(refused) and ssh.is_ssh_failure(refused)
    kex = "kex_exchange_identification: read: Connection reset by peer\n"
    assert ssh.is_connect_failure(kex)
    closed = "Connection closed by 192.168.64.3 port 22\n"
    assert ssh.is_ssh_failure(closed) and not ssh.is_connect_failure(closed)
    denied = "admin@192.168.64.3: Permission denied (publickey).\r\n"  # OpenSSH 10 form
    assert ssh.is_ssh_failure(denied) and not ssh.is_connect_failure(denied)
    assert ssh.is_ssh_failure("Permission denied (publickey).\n")
    assert not ssh.is_ssh_failure("tool output: admin@h: Permission denied (publickey).\n")
    assert not ssh.is_ssh_failure("my tool says: ssh: connect to host failed\n")
    assert not ssh.is_ssh_failure("")


def test_timeout_exit_codes() -> None:
    assert _timed_out(124, 0.1, 10)
    assert _timed_out(137, 0.1, 10)
    assert not _timed_out(143, 1.0, 10)
    assert _timed_out(143, 10.5, 10)
    assert not _timed_out(1, 20, 10)


# --------------------------------------------------------------------------- config


def _config(**overrides: object) -> TartSandboxConfig:
    settings = TartSettings(**overrides)  # type: ignore[arg-type]
    return TartSandboxConfig(**settings.model_dump(), default_cwd="/workspace")


def test_config_from_settings_and_spec_roundtrip() -> None:
    config = _config(cpus=2, bootstrap_password="not-for-logs")
    assert config.cpus == 2 and config.default_user == "root" and config.default_cwd == "/workspace"
    dumped = config.model_dump()
    assert "bootstrap_password" not in dumped
    assert "not-for-logs" not in config.model_dump_json() and "not-for-logs" not in repr(config)
    assert TartSandboxEnvironment.config_deserialize(dumped) == config.model_copy(
        update={"bootstrap_password": TartSettings().bootstrap_password}
    )
    hash(config)
    spec = SandboxEnvironmentSpec("tart", config)
    restored = SandboxEnvironmentSpec.model_validate(spec.model_dump())
    assert isinstance(restored.config, TartSandboxConfig)
    assert restored.config.cpus == 2


def test_config_from_profile_dump_and_validation() -> None:
    settings = TartSettings()
    config = TartSandboxConfig(**settings.model_dump(), default_cwd="/workspace")
    assert config.image == settings.image
    with pytest.raises(ValueError):
        TartSandboxConfig(default_cwd="relative")
    with pytest.raises(ValueError):
        TartSandboxConfig(default_cwd="/w", unknown=1)  # type: ignore[call-arg]


def test_resolve_config_variants(tmp_path: Path) -> None:
    config = _config()
    assert resolve_config(config) is config
    path = tmp_path / "tart.yaml"
    path.write_text("default_cwd: /srv\nmemory: 2g\n")
    assert resolve_config(str(path)).memory == "2g"
    path.write_text("default_cwd: /srv\nmemory_mb: 2048\n")
    with pytest.raises(ValueError, match="'memory_mb' was renamed: use memory"):
        resolve_config(str(path))
    with pytest.raises(ValueError):
        resolve_config(None)


def test_registered_as_tart() -> None:
    assert registry_find_sandboxenv("tart") is TartSandboxEnvironment
    assert TartSandboxEnvironment.default_concurrency() == 2
    assert TartSandboxEnvironment.config_files() == []


# --------------------------------------------------------------------------- image helpers


def test_sample_vm_names() -> None:
    name = image.new_vm_name()
    assert image.is_sample_vm(name)
    for keep in ["agenteval-ubuntu-24.04", "agenteval-build-0123abcd", "agenteval-0123ABCD",
                 "agenteval-0123abcd9", "ubuntu"]:  # fmt: skip
        assert not image.is_sample_vm(keep)


def test_askpass_script_prints_password_from_env() -> None:
    with image.askpass_script() as script:
        assert stat.S_IMODE(script.stat().st_mode) == 0o700
        assert stat.S_IMODE(script.parent.stat().st_mode) == 0o700
        assert "hunter2" not in script.read_text()
        out = subprocess.run(
            [str(script)],
            env={**os.environ, image.ASKPASS_ENV: "hunter2 'x' $y"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout == "hunter2 'x' $y\n"
    assert not script.exists()


def test_tart_missing_message_points_at_release_tarball() -> None:
    assert image.TART_RELEASES_URL in image.TART_MISSING_MESSAGE
    assert "Homebrew" in image.TART_MISSING_MESSAGE


def test_dhcp_lease_check(tmp_path: Path) -> None:
    import plistlib

    missing = tmp_path / "missing.plist"
    assert image.dhcp_lease_seconds(missing) == 86400
    problem = image.dhcp_lease_problem(missing)
    assert problem is not None and "DHCPLeaseTimeSecs -int 600" in problem and "softnet" in problem

    short = tmp_path / "short.plist"
    short.write_bytes(plistlib.dumps({"bootpd": {"DHCPLeaseTimeSecs": 600}}))
    assert image.dhcp_lease_seconds(short) == 600
    assert image.dhcp_lease_problem(short) is None

    long = tmp_path / "long.plist"
    long.write_bytes(plistlib.dumps({"bootpd": {"DHCPLeaseTimeSecs": 86400}, "x": 1}))
    assert image.dhcp_lease_problem(long) is not None

    for name, content in [("garbage", b"not a plist"), ("nobootpd", plistlib.dumps({"a": 1}))]:
        path = tmp_path / name
        path.write_bytes(content)
        assert image.dhcp_lease_seconds(path) == 86400


async def test_ensure_ssh_key(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "id_ed25519"
    key = await image.ensure_ssh_key(key_path)
    assert key == key_path
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    pub = key.with_name("id_ed25519.pub").read_text()
    assert pub.startswith("ssh-ed25519 ")
    before = key.read_bytes()
    await image.ensure_ssh_key(key_path)
    assert key.read_bytes() == before
    key.with_name("id_ed25519.pub").unlink()
    await image.ensure_ssh_key(key_path)
    assert key.with_name("id_ed25519.pub").read_text().split()[:2] == pub.split()[:2]
