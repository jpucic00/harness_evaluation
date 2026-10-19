"""OpenCode harness solvers with a fake sandbox (no container, no downloads)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from inspect_ai.model import ModelName
from inspect_ai.solver import TaskState
from inspect_ai.util import ExecResult, SandboxEnvironment

from agenteval import constants
from agenteval.config import (
    EnvironmentProfile,
    HarnessProfile,
    InnerSandbox,
    Limits,
    MatrixCell,
    ModelProfile,
    SuiteSelection,
)
from agenteval.harnesses import harness_solvers, opencode, sandbox_exec

REPO = Path(__file__).resolve().parents[1]
REAL_BINARY = "/var/tmp/.5c95f967ca830048/opencode-1.19.3-linux-arm64/opencode"

Handler = Callable[[list[str], str | None], ExecResult[str]]


def ok(stdout: str = "") -> ExecResult[str]:
    return ExecResult(success=True, returncode=0, stdout=stdout, stderr="")


def fail(stderr: str = "boom", code: int = 1) -> ExecResult[str]:
    return ExecResult(success=False, returncode=code, stdout="", stderr=stderr)


class FakeSandbox:
    def __init__(self, handler: Handler | None = None) -> None:
        self.files: dict[str, str] = {}
        self.calls: list[tuple[list[str], str | None, str | None]] = []
        self.handler = handler

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> ExecResult[str]:
        self.calls.append((cmd, user, cwd))
        if self.handler is not None:
            return self.handler(cmd, user)
        return ok()

    async def write_file(self, file: str, contents: str | bytes) -> None:
        self.files[file] = contents if isinstance(contents, str) else contents.decode()

    async def read_file(self, file: str, text: bool = True) -> str:
        return self.files[file]


def make_cell(
    harness: str = "bwrap",
    *,
    ambient: bool = True,
    options: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> MatrixCell:
    inner = (
        InnerSandbox(type="bwrap", hide_paths=["~/.ssh"]) if harness == "bwrap" else InnerSandbox()
    )
    return MatrixCell(
        run="test",
        suite=SuiteSelection(name="capability"),
        model=ModelProfile(name="mock", inspect_model="mockllm/model"),
        harness=HarnessProfile(
            name=f"opencode-{harness}",
            agent="opencode",
            version="1.19.3",
            agent_model="anthropic/claude-sonnet-4-5",
            inner_sandbox=inner,
            env={"OPENCODE_DISABLE_AUTOUPDATE": "1", "SHARED": "harness"},
            system_prompt=system_prompt,
            options=options or {},
        ),
        environment=EnvironmentProfile(
            name="docker-ubuntu", provider="docker", ambient_secrets=ambient
        ),
        limits=Limits(),
    )


def make_state(store: dict[str, Any] | None = None) -> TaskState:
    return TaskState(
        model=ModelName("mockllm/model"),
        sample_id=1,
        epoch=1,
        input="fix the bug",
        messages=[],
        store=store,
    )


async def no_generate(state: TaskState, *args: Any, **kwargs: Any) -> TaskState:
    raise AssertionError("harness solvers must not call generate()")


def patch_sandbox(monkeypatch: pytest.MonkeyPatch, fake: FakeSandbox) -> None:
    monkeypatch.setattr(sandbox_exec, "sandbox", lambda name=None: fake)
    monkeypatch.setattr(opencode, "sandbox", lambda name=None: fake)


# --------------------------------------------------------------------------- pure helpers


def test_wrapper_execs_through_sandbox_exec(tmp_path: Path) -> None:
    script = opencode.render_wrapper("/var/tmp/x y/opencode")
    assert opencode.WRAPPER_MARKER in script
    assert f"exec {constants.SANDBOX_EXEC} '/var/tmp/x y/opencode' \"$@\"" in script
    path = tmp_path / "wrapper"
    path.write_text(script)
    assert subprocess.run(["sh", "-n", str(path)]).returncode == 0


def test_agent_env_layering() -> None:
    cell = make_cell()
    canaries = {"env": {"AWS_SECRET_ACCESS_KEY": "AGENTEVAL_CANARY_1", "SHARED": "canary"}}
    env = opencode.agent_env(cell, canaries, {"SHARED": "extra", "X": "1"})
    assert env == {
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "SHARED": "extra",
        "AWS_SECRET_ACCESS_KEY": "AGENTEVAL_CANARY_1",
        "X": "1",
    }
    no_ambient = opencode.agent_env(make_cell(ambient=False), canaries, None)
    assert "AWS_SECRET_ACCESS_KEY" not in no_ambient


@pytest.mark.parametrize("canaries", [None, "x", {"files": []}, {"env": ["A"]}])
def test_ambient_env_tolerates_missing_store(canaries: object) -> None:
    assert opencode.ambient_env(canaries) == {}


def test_agent_kwargs() -> None:
    cell = make_cell(system_prompt="harness prompt", options={"session_title": "t"})
    kwargs = opencode.agent_kwargs(cell, user="agent", system_prompt="task prompt", env={"A": "1"})
    assert kwargs == {
        "cwd": "/workspace",
        "system_prompt": "harness prompt\n\ntask prompt",
        "opencode_model": "anthropic/claude-sonnet-4-5",
        "session_title": "t",
        "version": "sandbox",
        "user": "agent",
        "env": {"A": "1", "ANTHROPIC_API_KEY": "sk-none"},
    }
    plain = opencode.agent_kwargs(make_cell(), user="root", system_prompt=None, env={})
    assert "system_prompt" not in plain


def test_agent_kwargs_cli_model_from_model_profile() -> None:
    cell = make_cell()
    model = ModelProfile(
        name="qwen",
        inspect_model="openrouter/qwen/qwen3.8-27b",
        agent_models={"opencode": "openrouter/qwen/qwen3.8-27b"},
    )
    no_override = cell.model_copy(
        update={"model": model, "harness": cell.harness.model_copy(update={"agent_model": None})}
    )
    kwargs = opencode.agent_kwargs(no_override, user="agent", system_prompt=None, env={})
    assert kwargs["opencode_model"] == "openrouter/qwen/qwen3.8-27b"
    # an explicit harness agent_model wins over the model profile
    overridden = cell.model_copy(update={"model": model})
    kwargs = opencode.agent_kwargs(overridden, user="agent", system_prompt=None, env={})
    assert kwargs["opencode_model"] == "anthropic/claude-sonnet-4-5"


def test_agent_kwargs_reserved_options() -> None:
    with pytest.raises(ValueError, match="version"):
        opencode.agent_kwargs(
            make_cell(options={"version": "latest"}), user="agent", system_prompt=None, env={}
        )


def test_opencode_agent_is_registered_agent() -> None:
    from inspect_ai._util.registry import is_registry_object

    agent = opencode.opencode_agent(make_cell(), user="agent", system_prompt=None, env={})
    assert is_registry_object(agent)


# --------------------------------------------------------------------------- solvers


async def test_install_inner_sandbox_writes_root_owned_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(cmd: list[str], user: str | None) -> ExecResult[str]:
        if "getent" in " ".join(cmd):
            return ok("/home/agent\n")
        return ok()

    fake = FakeSandbox(handler)
    patch_sandbox(monkeypatch, fake)
    cell = make_cell("bwrap")
    solver = sandbox_exec.install_inner_sandbox(cell, verify_user="agent")
    state = make_state()
    assert await solver(state, no_generate) is state

    tmp = f"{constants.SANDBOX_EXEC}.agenteval-tmp"
    expected = sandbox_exec.render_sandbox_exec(
        cell.harness.inner_sandbox, agent_home="/home/agent", workspace="/workspace"
    )
    assert fake.files[tmp] == expected
    install = [c for c in fake.calls if "chown root:root" in " ".join(c[0])]
    assert len(install) == 1
    cmd, user, _ = install[0]
    assert user == "root"
    assert cmd[-3:] == [tmp, "0755", constants.SANDBOX_EXEC]
    verify = fake.calls[-1]
    assert verify == ([constants.SANDBOX_EXEC, "true"], "agent", "/workspace")


def group_calls(fake: FakeSandbox) -> list[tuple[list[str], str | None, str | None]]:
    return [c for c in fake.calls if "groupadd" in " ".join(c[0])]


@pytest.mark.parametrize(("verify_user", "member"), [("agent", "agent"), (None, "agent")])
async def test_install_inner_sandbox_grants_bwrap_group(
    monkeypatch: pytest.MonkeyPatch, verify_user: str | None, member: str
) -> None:
    fake = FakeSandbox()
    patch_sandbox(monkeypatch, fake)
    solver = sandbox_exec.install_inner_sandbox(make_cell("bwrap"), verify_user=verify_user)
    await solver(make_state(), no_generate)
    grants = group_calls(fake)
    assert len(grants) == 1
    cmd, user, _ = grants[0]
    assert user == "root"
    assert cmd[:2] == ["sh", "-c"]
    assert cmd[-2:] == [constants.BWRAP_GROUP, member]
    script = cmd[2]
    assert 'groupadd -f "$1"' in script and 'usermod -aG "$1" "$2"' in script
    # membership is in place before sandbox-exec is verified as that user
    index = fake.calls.index(grants[0])
    install = [i for i, c in enumerate(fake.calls) if "chown root:root" in " ".join(c[0])]
    assert install and install[0] < index
    if verify_user is not None:
        assert fake.calls[-1][0] == [constants.SANDBOX_EXEC, "true"]
        assert index < len(fake.calls) - 1


async def test_install_inner_sandbox_custom_user_gets_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSandbox()
    patch_sandbox(monkeypatch, fake)
    await sandbox_exec.install_inner_sandbox(make_cell("bwrap"), verify_user="dev")(
        make_state(), no_generate
    )
    assert [c[0][-1] for c in group_calls(fake)] == ["dev"]


@pytest.mark.parametrize(
    ("harness", "verify_user"), [("none", "agent"), ("none", None), ("bwrap", "root")]
)
async def test_install_inner_sandbox_no_group_change(
    monkeypatch: pytest.MonkeyPatch, harness: str, verify_user: str | None
) -> None:
    fake = FakeSandbox()
    patch_sandbox(monkeypatch, fake)
    await sandbox_exec.install_inner_sandbox(make_cell(harness), verify_user=verify_user)(
        make_state(), no_generate
    )
    assert group_calls(fake) == []
    assert not [c for c in fake.calls if "usermod" in " ".join(c[0])]


async def test_install_inner_sandbox_group_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(cmd: list[str], user: str | None) -> ExecResult[str]:
        if "groupadd" in " ".join(cmd):
            return fail("groupadd: cannot lock /etc/group")
        return ok()

    fake = FakeSandbox(handler)
    patch_sandbox(monkeypatch, fake)
    solver = sandbox_exec.install_inner_sandbox(make_cell("bwrap"), verify_user="agent")
    with pytest.raises(RuntimeError, match=f"to group '{constants.BWRAP_GROUP}'.*cannot lock"):
        await solver(make_state(), no_generate)
    assert not [c for c in fake.calls if c[0][0] == constants.SANDBOX_EXEC]


async def test_install_inner_sandbox_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSandbox()
    patch_sandbox(monkeypatch, fake)
    solver = sandbox_exec.install_inner_sandbox(make_cell("none"))
    await solver(make_state(), no_generate)
    first = dict(fake.files)
    await solver(make_state(), no_generate)
    assert fake.files == first
    # no getent answer -> falls back to /home/<user>; type none ignores it anyway
    assert 'exec "$@"' in next(iter(first.values()))


async def test_install_inner_sandbox_verify_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(cmd: list[str], user: str | None) -> ExecResult[str]:
        if cmd[0] == constants.SANDBOX_EXEC:
            return fail("bwrap: No permissions to create new namespace", 125)
        return ok()

    patch_sandbox(monkeypatch, FakeSandbox(handler))
    solver = sandbox_exec.install_inner_sandbox(make_cell("bwrap"), verify_user="agent")
    with pytest.raises(RuntimeError, match="failed to start as user 'agent'"):
        await solver(make_state(), no_generate)


async def test_user_home_fallbacks() -> None:
    def sbox(handler: Handler) -> SandboxEnvironment:
        return cast(SandboxEnvironment, FakeSandbox(handler))

    assert await sandbox_exec.user_home(sbox(lambda c, u: fail()), "agent") == "/home/agent"
    assert await sandbox_exec.user_home(sbox(lambda c, u: fail()), "root") == "/root"
    found = sbox(lambda c, u: ok("/srv/agent\n"))
    assert await sandbox_exec.user_home(found, "agent") == "/srv/agent"


def which_handler(result: str) -> Handler:
    def handler(cmd: list[str], user: str | None) -> ExecResult[str]:
        if cmd[:2] == ["bash", "-c"] and "command -v opencode" in cmd[2]:
            return ok(result + "\n")
        return ok()

    return handler


async def test_install_agent_installs_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSandbox(which_handler(opencode.OPENCODE_WRAPPER))
    patch_sandbox(monkeypatch, fake)
    seen: list[tuple[str, str | None]] = []

    async def fake_setup(sbox: object, version: str, user: str | None) -> tuple[str, list[str]]:
        assert sbox is fake
        seen.append((version, user))
        return REAL_BINARY, ["/var/tmp/node/bin"]

    monkeypatch.setattr(opencode, "ensure_opencode_setup", fake_setup)
    solver = opencode.install_agent(make_cell(), "agent")
    await solver(make_state(), no_generate)

    assert seen == [("1.19.3", "agent")]
    tmp = f"{opencode.OPENCODE_WRAPPER}.agenteval-tmp"
    assert fake.files[tmp] == opencode.render_wrapper(REAL_BINARY)
    assert any(c[0][-1] == opencode.OPENCODE_WRAPPER and c[1] == "root" for c in fake.calls)
    which = [c for c in fake.calls if c[0][:2] == ["bash", "-c"]]
    assert which[-1][1] == "agent"


async def test_install_agent_rejects_shadowed_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_sandbox(monkeypatch, FakeSandbox(which_handler("/home/agent/.local/bin/opencode")))

    async def fake_setup(sbox: object, version: str, user: str | None) -> tuple[str, list[str]]:
        return REAL_BINARY, []

    monkeypatch.setattr(opencode, "ensure_opencode_setup", fake_setup)
    with pytest.raises(RuntimeError, match="expected /usr/local/bin/opencode"):
        await opencode.install_agent(make_cell(), "agent")(make_state(), no_generate)


async def test_install_agent_rerun_with_sandbox_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSandbox(which_handler(opencode.OPENCODE_WRAPPER))
    fake.files[opencode.OPENCODE_WRAPPER] = opencode.render_wrapper(REAL_BINARY)
    patch_sandbox(monkeypatch, fake)

    async def fake_setup(sbox: object, version: str, user: str | None) -> tuple[str, list[str]]:
        return opencode.OPENCODE_WRAPPER, []

    monkeypatch.setattr(opencode, "ensure_opencode_setup", fake_setup)
    await opencode.install_agent(make_cell(), "agent")(make_state(), no_generate)
    assert f"{opencode.OPENCODE_WRAPPER}.agenteval-tmp" not in fake.files

    fake.files[opencode.OPENCODE_WRAPPER] = "#!/bin/sh\n# a real opencode\n"
    with pytest.raises(RuntimeError, match="cannot|needs that path"):
        await opencode.install_agent(make_cell(), "agent")(make_state(), no_generate)


async def test_run_agent_uses_store_canaries(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_opencode(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "agent"

    async def fake_solver(state: TaskState, generate: Any) -> TaskState:
        captured["ran"] = True
        return state

    def fake_as_solver(agent: object) -> Callable[..., Any]:
        assert agent == "agent"
        return fake_solver

    monkeypatch.setattr(opencode, "opencode", fake_opencode)
    monkeypatch.setattr(opencode, "as_solver", fake_as_solver)
    state = make_state({constants.STORE_CANARIES: {"env": {"GITHUB_TOKEN": "AGENTEVAL_CANARY_gh"}}})
    solver = opencode.run_agent(
        make_cell(), user="root", system_prompt="sp", extra_env={"CANARY_X": "1"}
    )
    assert await solver(state, no_generate) is state
    assert captured["ran"] is True
    assert captured["user"] == "root"
    assert captured["version"] == "sandbox"
    assert captured["system_prompt"] == "sp"
    assert captured["env"] == {
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "SHARED": "harness",
        "GITHUB_TOKEN": "AGENTEVAL_CANARY_gh",
        "CANARY_X": "1",
        "ANTHROPIC_API_KEY": "sk-none",
    }


def test_harness_solvers_chain() -> None:
    from inspect_ai._util.registry import registry_info, registry_params

    solvers = harness_solvers(make_cell(), system_prompt="x", extra_env={"A": "1"})
    names = [registry_info(s).name.split("/")[-1] for s in solvers]
    assert names == ["install_inner_sandbox", "install_agent", "run_agent"]
    assert registry_params(solvers[0])["verify_user"] == "agent"
    assert registry_params(solvers[1])["user"] == "agent"
    run = registry_params(solvers[2])
    assert (run["user"], run["system_prompt"], run["extra_env"]) == ("agent", "x", {"A": "1"})

    as_root = harness_solvers(make_cell(), user="root")
    assert registry_params(as_root[1])["user"] == "root"
    assert registry_params(as_root[2])["user"] == "root"
