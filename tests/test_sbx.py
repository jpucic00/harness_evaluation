"""The Docker Sandboxes (sbx) provider against a fake ``sbx`` executable.

The fake keeps "sandboxes" as directories and runs ``sbx exec`` commands locally as the
current user (dropping the guest's ``timeout`` wrapper, which macOS lacks), with one
injected variable the way sbx injects its proxy settings. No real sbx or VM is involved.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.util import SandboxEnvironment, SandboxUnavailableError

from agenteval.config import EnvironmentProfile, SbxSettings
from agenteval.dev.task import SbxHandle
from agenteval.environments import sandbox_spec
from agenteval.environments.sbx import cli
from agenteval.environments.sbx.sandbox import (
    SbxSandboxConfig,
    SbxSandboxEnvironment,
    exec_argv,
)

FAKE_SBX = r"""#!/bin/bash
state="$FAKE_SBX_HOME"
mkdir -p "$state/boxes"
printf '%s\n' "$*" >> "$state/calls.log"
cmd=$1; shift
case "$cmd" in
  ls)
    if [ -n "${FAKE_SBX_SIGNED_OUT:-}" ]; then echo "error: not signed in to Docker" >&2; exit 1; fi
    printf '['; sep=''
    for d in "$state"/boxes/*; do
      [ -d "$d" ] || continue
      printf '%s{"name":"%s","status":"running"}' "$sep" "$(basename "$d")"; sep=','
    done
    printf ']\n' ;;
  settings)
    key=${@: -1}
    case "$key" in
      ssh.agentForwardingEnabled) v=${FAKE_SBX_FORWARDING:-false} ;;
      browser.enabled) v=${FAKE_SBX_BROWSER:-false} ;;
      *) v=null ;;
    esac
    printf '{"key":"%s","value":%s}\n' "$key" "$v" ;;
  template)
    if [ "$1" = ls ]; then
      if [ -n "${FAKE_SBX_NO_TEMPLATE:-}" ]; then echo '[]'; else
        repo=docker.io/agenteval/ubuntu-24.04-sbx
        echo "[{\"id\":\"a153f7d05cb6\",\"repository\":\"$repo\",\"tag\":\"latest\"}]"
      fi
    fi ;;
  create)
    name=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name=$2; shift 2 ;;
        --template|--pull|--memory|--cpus) shift 2 ;;
        *) shift ;;
      esac
    done
    if [ -n "${FAKE_SBX_CREATE_FAIL:-}" ]; then echo "error: boom" >&2; exit 1; fi
    mkdir "$state/boxes/$name" && echo "Created sandbox $name" ;;
  exec)
    while [ $# -gt 0 ]; do
      case "$1" in
        -u|-w|-e|--detach-keys) shift 2 ;;
        -i|-t|-it) shift ;;
        *) break ;;
      esac
    done
    name=$1; shift
    [ -d "$state/boxes/$name" ] || { echo "error: sandbox '$name' not found" >&2; exit 1; }
    [ "$1" = /usr/bin/timeout ] && shift 3
    export SBX_FAKE_INJECTED=proxy-managed
    args=(); skip=0
    for a in "$@"; do
      if [ $skip -gt 0 ]; then skip=$((skip - 1)); continue; fi
      case "$a" in
        /usr/sbin/runuser) skip=3 ;;  # "-u USER --": the fake runs as the current user
        /usr/bin/python3) args+=("$FAKE_PYTHON") ;;
        /usr/sbin/nft) args+=("${FAKE_SBX_NFT:-true}") ;;
        *) args+=("$a") ;;
      esac
    done
    exec "${args[@]}" ;;
  rm)
    for n in "$@"; do
      case "$n" in --*) ;; *) rm -rf "$state/boxes/$n"; echo "Removed sandbox $n" ;; esac
    done ;;
  *) echo "fake sbx: unsupported $cmd" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def fake_sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "sbx"
    script.write_text(FAKE_SBX)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "sbx-home"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SBX_HOME", str(home))
    monkeypatch.setenv("FAKE_PYTHON", sys.executable)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(cli, "image_env_names", lambda image, docker="docker": frozenset())
    return home


def _config(workspace: Path, **kw: object) -> SbxSandboxConfig:
    return SbxSandboxConfig(default_cwd=str(workspace), **kw)  # type: ignore[arg-type]


def _calls(home: Path) -> list[str]:
    return (home / "calls.log").read_text().splitlines()


async def _start(workspace: Path, **kw: object) -> tuple[SbxSandboxConfig, SbxSandboxEnvironment]:
    cfg = _config(workspace, **kw)
    await SbxSandboxEnvironment.task_init("t", cfg)
    envs = await SbxSandboxEnvironment.sample_init("t", cfg, {})
    return cfg, envs["default"].as_type(SbxSandboxEnvironment)


# --------------------------------------------------------------------------- cli


@pytest.mark.parametrize(
    ("image", "ref"),
    [
        ("agenteval/ubuntu-24.04:latest", ("docker.io/agenteval/ubuntu-24.04", "latest")),
        ("ubuntu:24.04", ("docker.io/library/ubuntu", "24.04")),
        ("ubuntu", ("docker.io/library/ubuntu", "latest")),
        ("ghcr.io/acme/img:1", ("ghcr.io/acme/img", "1")),
        ("localhost:5000/img", ("localhost:5000/img", "latest")),
    ],
)
def test_template_ref(image: str, ref: tuple[str, str]) -> None:
    assert cli.template_ref(image) == ref


def test_sample_names() -> None:
    name = cli.new_sandbox_name()
    assert cli.is_sample_sandbox(name)
    assert not cli.is_sample_sandbox("my-sandbox")
    assert not cli.is_sample_sandbox("agenteval-sbx-build")


def test_readiness(fake_sbx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.readiness_problems() == []
    monkeypatch.setenv("FAKE_SBX_FORWARDING", "true")
    (problem,) = cli.readiness_problems()
    assert "ssh.agentForwardingEnabled" in problem and "sbx daemon restart" in problem
    monkeypatch.setenv("FAKE_SBX_SIGNED_OUT", "1")
    (problem,) = cli.readiness_problems()
    assert "sbx login" in problem


def test_readiness_without_sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "sbx_binary", lambda: None)
    assert cli.readiness_problems() == [cli.SBX_MISSING_MESSAGE]


def test_setting_warnings(fake_sbx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.setting_warnings() == []
    monkeypatch.setenv("FAKE_SBX_BROWSER", "true")
    (warning,) = cli.setting_warnings()
    assert "browser.enabled" in warning


def test_template_id(fake_sbx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.template_id("agenteval/ubuntu-24.04-sbx:latest") == "a153f7d05cb6"
    assert cli.template_id("agenteval/ubuntu-24.04:latest") is None
    monkeypatch.setenv("FAKE_SBX_NO_TEMPLATE", "1")
    assert cli.template_id("agenteval/ubuntu-24.04-sbx:latest") is None


def test_cli_runs_without_telemetry(fake_sbx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SBX_NO_TELEMETRY", raising=False)
    assert cli.sbx_environ()["SBX_NO_TELEMETRY"] == "1"


def test_cleanup_removes_only_sample_sandboxes(fake_sbx: Path) -> None:
    boxes = fake_sbx / "boxes"
    for name in ("agenteval-sbx-0123abcd", "agenteval-sbx-ffff0000", "my-project"):
        (boxes / name).mkdir(parents=True)
    assert sorted(cli.cleanup()) == ["agenteval-sbx-0123abcd", "agenteval-sbx-ffff0000"]
    assert [p.name for p in boxes.iterdir()] == ["my-project"]


# --------------------------------------------------------------------------- exec argv


def test_exec_argv_quotes_nothing_into_shell_source() -> None:
    argv = exec_argv(
        "sbx",
        "agenteval-sbx-0123abcd",
        ["echo", "$(id)"],
        cwd="/work space",
        user="agent",
        env={"K": "v w"},
        unset=("HTTPS_PROXY", "OPENAI_API_KEY"),
        timeout=30,
    )
    assert argv[:6] == ["sbx", "exec", "-u", "root", "agenteval-sbx-0123abcd", "/usr/bin/timeout"]
    assert argv[8:15] == [
        "/usr/bin/env",
        "-u",
        "HTTPS_PROXY",
        "-u",
        "OPENAI_API_KEY",
        "K=v w",
        "/usr/sbin/runuser",
    ]
    assert argv[15:18] == ["-u", "agent", "--"]
    assert argv[-3:] == ["/work space", "echo", "$(id)"]
    assert "-i" not in argv


def test_exec_argv_input_and_validation() -> None:
    argv = exec_argv("sbx", "n", ["cat"], cwd="/", user="root", has_input=True)
    assert argv[2] == "-i" and "cat |" in argv[-4]
    with pytest.raises(ValueError):
        exec_argv("sbx", "n", ["true"], cwd="/", user="root", env={"BAD-NAME": "x"})
    with pytest.raises(Exception, match="Invalid user"):
        exec_argv("sbx", "n", ["true"], cwd="/", user="root; rm -rf /")
    with pytest.raises(ValueError):
        exec_argv("sbx", "n", [], cwd="/", user="root")


# --------------------------------------------------------------------------- provider


async def test_task_init_needs_the_template(
    fake_sbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SBX_NO_TEMPLATE", "1")
    with pytest.raises(PrerequisiteError, match="agenteval env build"):
        await SbxSandboxEnvironment.task_init("t", _config(tmp_path))


async def test_task_init_refuses_ssh_agent_forwarding(
    fake_sbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SBX_FORWARDING", "true")
    with pytest.raises(PrerequisiteError, match="ssh.agentForwardingEnabled"):
        await SbxSandboxEnvironment.task_init("t", _config(tmp_path))


async def test_sample_lifecycle_exec_and_files(fake_sbx: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shared = tmp_path / "repo"
    cfg, env = await _start(workspace, cpus=2, memory="2g", shared_dirs=(str(shared),))
    name = env.sandbox_name
    assert cli.is_sample_sandbox(name)
    create = next(c for c in _calls(fake_sbx) if c.startswith("create"))
    assert f"--template {cfg.template} --pull never --name {name}" in create
    assert "--memory 2g --cpus 2" in create and create.endswith(str(shared))

    # sbx's injected variables are gone (the fake runs with the host's whole environment,
    # so everything outside the keep list goes), the caller's are there
    assert "SBX_FAKE_INJECTED" in env.unset_env
    assert not {"PATH", "HOME"} & set(env.unset_env)
    result = await env.exec(
        ["sh", "-c", 'echo "${SBX_FAKE_INJECTED:-unset} $K $PWD"'], env={"K": "v"}, user="agent"
    )
    assert "/usr/sbin/runuser -u agent --" in _calls(fake_sbx)[-1]
    assert result.stdout.strip() == f"unset v {workspace}"
    failed = await env.exec(["sh", "-c", "echo oops >&2; exit 3"])
    assert (failed.returncode, failed.stderr.strip()) == (3, "oops")
    echoed = await env.exec(["cat"], input="hello")
    assert echoed.stdout == "hello"

    await env.write_file("sub/note.txt", "data")
    assert (workspace / "sub" / "note.txt").read_text() == "data"
    assert await env.read_file("sub/note.txt") == "data"
    with pytest.raises(FileNotFoundError):
        await env.read_file("missing.txt")
    with pytest.raises(IsADirectoryError):
        await env.read_file("sub")

    connection = await env.connection(user="agent")
    assert connection.container == name and "sbx exec -it -u agent" in connection.command

    environments: dict[str, SandboxEnvironment] = {"default": env}
    await SbxSandboxEnvironment.sample_cleanup("t", cfg, environments, False)
    assert not (fake_sbx / "boxes" / name).exists()
    with pytest.raises(SandboxUnavailableError):
        await env.exec(["true"])


async def test_missing_sandbox_is_unavailable(fake_sbx: Path, tmp_path: Path) -> None:
    _, env = await _start(tmp_path)
    (fake_sbx / "boxes" / env.sandbox_name).rmdir()
    with pytest.raises(SandboxUnavailableError):
        await env.exec(["true"])


async def test_sample_init_removes_the_sandbox_when_an_ssh_agent_answers(
    fake_sbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PYTHON", "/usr/bin/true")  # the probe "reaches an agent"
    with pytest.raises(RuntimeError, match="SSH agent"):
        await _start(tmp_path)
    assert not any((fake_sbx / "boxes").iterdir())


async def test_sample_init_fails_without_workspace(fake_sbx: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        await _start(tmp_path / "nope")
    assert not any((fake_sbx / "boxes").iterdir())


async def test_sample_init_needs_firewall_rights_for_root(
    fake_sbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SBX_NFT", "false")
    with pytest.raises(RuntimeError, match="cannot manage the firewall"):
        await _start(tmp_path)
    assert not any((fake_sbx / "boxes").iterdir())


async def test_create_failure(
    fake_sbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_SBX_CREATE_FAIL", "1")
    with pytest.raises(RuntimeError, match="sbx create failed"):
        await _start(tmp_path)


# --------------------------------------------------------------------------- wiring


def _profile() -> EnvironmentProfile:
    return EnvironmentProfile(name="sbx-ubuntu", provider="sbx")


def test_profile_defaults() -> None:
    assert _profile().sbx == SbxSettings()


def test_sandbox_spec() -> None:
    spec = sandbox_spec(_profile(), mounts=["/Users/me/repo"])
    assert spec.type == "sbx"
    config = spec.config
    assert isinstance(config, SbxSandboxConfig)
    assert config.default_cwd == "/workspace" and config.shared_dirs == ("/Users/me/repo",)
    with pytest.raises(ValueError, match="subscription"):
        sandbox_spec(_profile(), filtered_network=True)


def test_dev_handle_argv() -> None:
    handle = SbxHandle("agenteval-sbx-0123abcd", ("HTTPS_PROXY",))
    attach = handle.attach_argv("agent", "/repo", ["bash"])
    assert attach[:5] == ["/usr/bin/env", "SBX_NO_TELEMETRY=1", "sbx", "exec", "-it"]
    assert attach[-8:] == [
        "/usr/bin/env", "-u", "HTTPS_PROXY", "/usr/sbin/runuser", "-u", "agent", "--", "bash",
    ]  # fmt: skip
    assert attach[attach.index("-u") + 1] == "root"
    run = handle.exec_argv("root", "/repo", ["true"], {"K": "v"})
    assert "-e" in run and "K=v" in run and "/usr/sbin/runuser" not in run and run[-1] == "true"
    assert handle.shell_hint("agent", "/repo").endswith(
        "agenteval-sbx-0123abcd /usr/bin/env -u HTTPS_PROXY /usr/sbin/runuser -u agent -- bash"
    )
