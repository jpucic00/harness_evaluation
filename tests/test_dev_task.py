"""agenteval dev session task: launcher rendering, CLI environments, env access argv."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
from inspect_ai.util import ExecResult
from inspect_swe._claude_code.model import ClaudeCodeModels

from agenteval import constants
from agenteval.config import ModelProfile, load_profiles
from agenteval.dev import task
from agenteval.dev.review import DevIO
from agenteval.dev.task import DevSpec, DockerHandle, TartHandle
from agenteval.environments.tart.ssh import SSHTarget
from agenteval.suites.common import SandboxUser

REPO = Path(__file__).resolve().parents[1]
PROFILES = load_profiles(REPO / "config")
MODELS = ClaudeCodeModels(
    presented="mockllm/model",
    opus="mockllm/model",
    sonnet="mockllm/model",
    haiku="mockllm/model",
    subagent="mockllm/model",
    aliases={},
    bridge_model="inspect",
)


def make_spec(harness: str, *, model: str | ModelProfile = "mock", **overrides: Any) -> DevSpec:
    h = PROFILES.harnesses[harness]
    e = PROFILES.environments["docker-ubuntu"]
    fields: dict[str, Any] = {
        "session_id": "s1",
        "session_dir": Path("/tmp/none"),
        "model": PROFILES.models[model] if isinstance(model, str) else model,
        "subagent_model": PROFILES.models["mock-worker"] if h.subagents else None,
        "harness": h,
        "environment": e,
        "dev_env": e.model_copy(update={"ambient_secrets": False}),
        "mount": "copy",
        "mounts": (),
        "vm_root": "/workspace",
        "vm_cwd": "/workspace/sub",
        "repo": None,
        "config_tar": None,
        "host_term": {"COLORTERM": "truecolor", "TERM_PROGRAM": "iTerm.app"},
    }
    return DevSpec(**(fields | overrides))


# ------------------------------------------------------------------- launcher


@pytest.mark.parametrize("relay", [True, False])
def test_render_launcher(relay: bool) -> None:
    text = task.render_launcher(
        env={"A_B": "x y", "Q": "it's"},
        cwd="/workspace/a b",
        binary="/var/tmp/x/claude",
        args=["--model", "m"],
        relay=relay,
    )
    assert "A_B='x y'; export A_B" in text
    assert "Q='it'\"'\"'s'; export Q" in text
    assert "cd -- '/workspace/a b'" in text
    command = [constants.SANDBOX_EXEC, "/var/tmp/x/claude", "--model", "m"]
    if relay:
        command[1:1] = ["python3", "-I", task.RELAY, "--"]
    assert f'exec {shlex.join(command)} "$@"' in text
    subprocess.run(["sh", "-n"], input=text, text=True, check=True)


def test_render_launcher_rejects_bad_env_names() -> None:
    with pytest.raises(ValueError):
        task.render_launcher(env={"A;B": "x"}, cwd="/w", binary="/b", args=[], relay=False)


# ------------------------------------------------------------------- claude


def test_claude_launch_plain() -> None:
    spec = make_spec("claude-code-bwrap")
    env, args = task.claude_launch(spec, models=MODELS, home="/home/agent")
    assert env["ANTHROPIC_BASE_URL"] == f"http://localhost:{task.BRIDGE_PORT}"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
    assert env["HOME"] == "/home/agent"
    assert env["COLORTERM"] == "truecolor"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in env  # memory comes back for review
    assert "MCP_CONNECTION_NONBLOCKING" not in env
    assert "ANTHROPIC_API_KEY" not in env  # would show the custom-key dialog
    assert "ANTHROPIC_AUTH_TOKEN" not in env  # the TUI warns next to apiKeyHelper
    assert args[:3] == ["--dangerously-skip-permissions", "--settings", task.CLAUDE_SETTINGS]
    assert args[3:5] == ["--model", "mockllm/model"]
    assert "--agents" not in args


def test_claude_launch_subagents_and_worker_note() -> None:
    _, args = task.claude_launch(
        make_spec("claude-code-subagents-bwrap"), models=MODELS, home="/home/agent"
    )
    agents = json.loads(args[args.index("--agents") + 1])
    assert set(agents) == {"implementer", "reviewer"}
    assert agents["reviewer"]["description"] and agents["reviewer"]["prompt"]
    _, args = task.claude_launch(
        make_spec("claude-code-opencode-none"), models=MODELS, home="/home/agent"
    )
    assert "--agents" not in args
    assert "opencode-worker" in args[args.index("--append-system-prompt") + 1]


# ------------------------------------------------------------------- opencode


def test_opencode_launch() -> None:
    spec = make_spec("opencode-bwrap", model="qwen38-27b")
    env, args = task.opencode_launch(spec, dep_dirs=["/opt/node/bin", "/opt/rg"], home="/h")
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    base = f"http://localhost:{task.SHIM_PORT}/v1"  # through the reasoning relay
    assert config["provider"] == {
        "openrouter": {
            "name": "agenteval bridge",  # display names only: the catalog entry stays
            "options": {"baseURL": base, "headerTimeout": task.HEADER_TIMEOUT_MS},
            "models": {"qwen/qwen3.8-27b": {"name": "Qwen 3.8 27B on OpenRouter (qwen38-27b)"}},
        }
    }
    assert config["model"] == "openrouter/qwen/qwen3.8-27b"
    assert env["OPENROUTER_API_KEY"] == "sk-none"
    assert env["OPENAI_BASE_URL"] == base and env["ANTHROPIC_BASE_URL"] == base
    assert env["PATH"].startswith("/opt/node/bin:/opt/rg:")
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"  # harness env kept
    assert args == ["--auto", "--model", "openrouter/qwen/qwen3.8-27b"]


def test_opencode_launch_label_without_description() -> None:
    spec = make_spec("opencode-none")
    spec = make_spec("opencode-none", model=spec.model.model_copy(update={"description": ""}))
    env, _ = task.opencode_launch(spec, dep_dirs=[], home="/h")
    models = json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["openrouter"]["models"]
    assert models == {"qwen/qwen3.8-27b": {"name": "mock"}}


def test_opencode_header_timeout_covers_the_shim() -> None:
    from agenteval.sandbox_scripts import think_shim

    # OpenCode must wait at least as long as the shim waits for the bridge (no streaming)
    assert task.HEADER_TIMEOUT_MS >= think_shim.UPSTREAM_TIMEOUT * 1000


def test_opencode_launch_needs_cli_model() -> None:
    spec = make_spec("opencode-none")
    spec = make_spec("opencode-none", model=spec.model.model_copy(update={"agent_models": {}}))
    with pytest.raises(ValueError, match="agent_models.opencode"):
        task.opencode_launch(spec, dep_dirs=[], home="/h")


# ------------------------------------------------------------------- env access


def test_docker_handle_argv() -> None:
    handle = DockerHandle("ctr-1")
    argv = handle.attach_argv("agent", "/workspace", [task.LAUNCHER])
    assert argv[:3] == ["docker", "exec", "-it"]
    assert argv[argv.index("--detach-keys") + 1] == task.DETACH_KEYS
    assert "ctrl-p" not in task.DETACH_KEYS
    assert argv[argv.index("-u") + 1] == "agent" and argv[argv.index("-w") + 1] == "/workspace"
    for name in ("TERM", "COLORTERM"):
        assert ["-e", name] == argv[argv.index(name) - 1 : argv.index(name) + 1]
    assert argv[-2:] == ["ctr-1", task.LAUNCHER]
    exec_argv = handle.exec_argv("agent", "/w", ["sh", "-c", "x"], {"K": "v w"})
    assert "-it" not in exec_argv and ["-e", "K=v w"] == exec_argv[6:8]


def test_tart_handle_argv() -> None:
    handle = TartHandle(SSHTarget(host="10.0.0.2", user="admin", key=Path("/k")))
    argv = handle.attach_argv("agent", "/work space", [task.LAUNCHER, "--continue"])
    assert "-t" in argv and argv[-2] == "10.0.0.2"
    remote = shlex.split(argv[-1])
    assert remote[:6] == ["sudo", "-n", "-H", "-u", "agent", "--"]
    assert remote[-3:] == ["/work space", task.LAUNCHER, "--continue"]
    exec_argv = handle.exec_argv("agent", "/w", ["sh"], {"K": "v"})
    assert "-T" in exec_argv and "K=v" in shlex.split(exec_argv[-1])


# ------------------------------------------------------------------- prompts and recovery


class ScriptedIO:
    """DevIO with queued answers; an answer that is an exception is raised instead."""

    def __init__(self, answers: list[str | BaseException]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.output: list[str] = []
        self.io = DevIO(
            out=self.output.append, ask=self._ask, edit=lambda c, s: c, interactive=True
        )

    def _ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


USER = SandboxUser(name="agent", uid=1001, gid=1001, home="/home/agent")


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None, str | None]] = []

    async def exec(self, cmd: list[str], input: str | None = None, user: str | None = None,
                   **kwargs: Any) -> ExecResult[str]:  # fmt: skip
        self.calls.append((cmd, input, user))
        return ExecResult(success=True, returncode=0, stdout="", stderr="")


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), EOFError()])
def test_prompt_interrupt_gives_default(raised: BaseException) -> None:
    scripted = ScriptedIO([raised])
    assert task._prompt(scripted.io, "q? ", default="f") == "f"


def _recovery_setup(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[bool]
) -> tuple[list[Any], list[list[str]]]:
    exported: list[Any] = []
    attached: list[list[str]] = []

    async def fake_export(*args: Any) -> task.Exported:
        if not outcomes.pop(0):
            raise RuntimeError("tar exited 1")
        return task.Exported(None, None, None)

    async def fake_attach(argv: list[str], io: DevIO) -> int:
        attached.append(argv)
        return 0

    monkeypatch.setattr(task, "export", fake_export)
    monkeypatch.setattr(task, "attach", fake_attach)
    return exported, attached


async def test_export_recovery_shell_then_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    exported, attached = _recovery_setup(monkeypatch, [False, False, True])
    scripted = ScriptedIO(["s", "r"])
    handle = DockerHandle("ctr")
    spec = make_spec("opencode-none")
    await task.export_with_recovery(
        RecordingSandbox(),
        handle,
        spec,
        scripted.io,
        USER,
        exported.append,  # type: ignore[arg-type]
    )
    assert len(exported) == 1
    assert attached == [handle.attach_argv("agent", spec.vm_cwd, ["bash", "-l"])]
    assert any(handle.shell_hint("agent", spec.vm_cwd) in line for line in scripted.output)


async def test_export_recovery_abandons_only_on_explicit_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported, _ = _recovery_setup(monkeypatch, [False])
    scripted = ScriptedIO(["", "retry", "y", "a"])
    with pytest.raises(RuntimeError, match="tar exited 1"):
        await task.export_with_recovery(
            RecordingSandbox(), DockerHandle("c"), make_spec("opencode-none"),  # type: ignore[arg-type]
            scripted.io, USER, exported.append,
        )  # fmt: skip
    assert len(scripted.asked) == 4 and not exported


async def test_export_recovery_ctrl_c_abandons(monkeypatch: pytest.MonkeyPatch) -> None:
    exported, _ = _recovery_setup(monkeypatch, [False])
    scripted = ScriptedIO([KeyboardInterrupt()])
    with pytest.raises(RuntimeError):
        await task.export_with_recovery(
            RecordingSandbox(), DockerHandle("c"), make_spec("opencode-none"),  # type: ignore[arg-type]
            scripted.io, USER, exported.append,
        )  # fmt: skip


@pytest.mark.parametrize(
    ("answers", "launches"),
    [(["r", "r"], 3), ([KeyboardInterrupt()], 1), (["f"], 1)],
)
async def test_resume_prompt(
    monkeypatch: pytest.MonkeyPatch, answers: list[str | BaseException], launches: int
) -> None:
    """Non-zero exits offer a resume; Ctrl+C there finishes (the export still runs)."""
    codes = [1, 1, 0]
    seen: list[list[str]] = []

    async def fake_attach(argv: list[str], io: DevIO) -> int:
        seen.append(argv)
        return codes.pop(0)

    monkeypatch.setattr(task, "attach", fake_attach)
    sbox = RecordingSandbox()
    spec = make_spec("opencode-none")
    await task.run_session(sbox, DockerHandle("c"), spec, ScriptedIO(answers).io, USER)  # type: ignore[arg-type]
    assert len(seen) == launches
    assert all(argv[-1] == "--continue" for argv in seen[1:])
    calls = [cmd[:2] if cmd[0] == "pkill" else cmd[2:3] for cmd, _, _ in sbox.calls]
    # the reasoning relay starts before each launch; stop_agent ends it with the CLI
    assert calls == [[task.THINK_SHIM], ["pkill", "-KILL"]] * launches


async def test_import_config_seeds_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[tuple[str, str, str]] = []

    async def fake_install(sbox: Any, path: str, content: str, mode: str = "0755") -> None:
        installed.append((path, content, mode))

    monkeypatch.setattr(task, "install_root_file", fake_install)
    sbox = RecordingSandbox()
    spec = make_spec(
        "claude-code-bwrap",
        claude_seed={"hasCompletedOnboarding": True},
        git_identity={"user.name": "Dev"},
    )
    await task.import_config(sbox, DockerHandle("c"), spec, USER)  # type: ignore[arg-type]
    seeds = [c for c in sbox.calls if c[1] is not None]
    assert len(seeds) == 1
    cmd, content, user = seeds[0]
    assert user == "agent" and "umask 077" in cmd[2]
    assert cmd[-1] == "/home/agent/.claude/.claude.json"
    assert json.loads(content or "") == {"hasCompletedOnboarding": True}
    assert any(c[0][-2:] == ["user.name", "Dev"] for c in sbox.calls)
    assert len(installed) == 1
    path, settings, mode = installed[0]
    assert path == task.CLAUDE_SETTINGS and mode == "0644"
    assert json.loads(settings)["skipDangerousModePermissionPrompt"] is True


# ------------------------------------------------------------------- profile adjustments


def test_dev_harness_adjustments() -> None:
    from agenteval.dev import commands

    h = PROFILES.harnesses["opencode-bwrap"]
    before = h.inner_sandbox.model_dump()
    d = commands._dev_harness(h, ["/Users/me/main/.git"])
    for name in task.TERM_VARS:
        assert d.inner_sandbox.env_passthrough.count(name) == 1
    assert d.inner_sandbox.extra_writable_paths[-1] == "/Users/me/main/.git"
    assert h.inner_sandbox.model_dump() == before
    again = commands._dev_harness(d, [])
    assert again.inner_sandbox.env_passthrough == d.inner_sandbox.env_passthrough


def test_live_mount_refuses_the_checkout(tmp_path: Path) -> None:
    from agenteval.config import ConfigError
    from agenteval.dev import commands
    from agenteval.dev.repo import RepoInfo

    checkout = tmp_path / "home" / "harness_evaluation"
    checkout.mkdir(parents=True)
    parent = RepoInfo(root=tmp_path / "home", cwd_rel="", git=False, git_dir=None,
                      common_dir=None, linked_worktree=False)  # fmt: skip
    with pytest.raises(ConfigError, match="agenteval checkout"):
        commands._live_mounts(parent, checkout.resolve())
    other = tmp_path / "proj"
    other.mkdir()
    info = RepoInfo(root=other, cwd_rel="", git=False, git_dir=None,
                    common_dir=None, linked_worktree=False)  # fmt: skip
    assert commands._live_mounts(info, checkout.resolve()) == [str(other.resolve())]


def test_quiet_relays_only_for_dev() -> None:
    from agenteval.config import InnerSandbox
    from agenteval.harnesses.sandbox_exec import render_sandbox_exec

    inner = InnerSandbox(type="bwrap")
    plain = render_sandbox_exec(inner, agent_home="/home/agent", workspace="/workspace")
    quiet = render_sandbox_exec(
        inner, agent_home="/home/agent", workspace="/workspace", quiet_relays=True
    )
    relays = [line for line in plain.splitlines() if "socat -d0" in line]
    assert len(relays) == 3 and not any("2>/dev/null" in line for line in relays)
    assert quiet.count("</dev/null 2>/dev/null &") == 3
