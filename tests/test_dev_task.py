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
from agenteval.suites.setup import SandboxUser

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
    from agenteval.guest.dev import think_shim

    # OpenCode must wait at least as long as the shim waits for the bridge (no streaming)
    assert task.HEADER_TIMEOUT_MS >= think_shim.UPSTREAM_TIMEOUT * 1000


def test_opencode_launch_needs_provider_in_cli_model() -> None:
    mock = PROFILES.models["mock"]
    model = ModelProfile.model_validate(
        mock.model_dump(exclude_unset=True) | {"agent_models": {"opencode": "qwen3"}}
    )
    spec = make_spec("opencode-none", model=model)
    with pytest.raises(ValueError, match=r"'qwen3' .* not in provider/model form"):
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
        exported.append,
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
            RecordingSandbox(), DockerHandle("c"), make_spec("opencode-none"),
            scripted.io, USER, exported.append,
        )  # fmt: skip
    assert len(scripted.asked) == 4 and not exported


async def test_export_recovery_ctrl_c_abandons(monkeypatch: pytest.MonkeyPatch) -> None:
    exported, _ = _recovery_setup(monkeypatch, [False])
    scripted = ScriptedIO([KeyboardInterrupt()])
    with pytest.raises(RuntimeError):
        await task.export_with_recovery(
            RecordingSandbox(), DockerHandle("c"), make_spec("opencode-none"),
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
    await task.run_session(sbox, DockerHandle("c"), spec, ScriptedIO(answers).io, USER)
    assert len(seen) == launches
    assert all(argv[-1] == "--continue" for argv in seen[1:])
    calls = [cmd[:2] if cmd[0] == "pkill" else cmd[2:3] for cmd, _, _ in sbox.calls]
    # the reasoning relay starts before each launch; stop_agent ends it with the CLI
    assert calls == [[task.THINK_SHIM], ["pkill", "-KILL"]] * launches


async def test_exec_output_control_characters_are_escaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--exec`` output comes from the environment: escapes must not reach the terminal."""

    async def fake_run_process(argv: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b"ok\x1b[2J\tdone\n", b"warn\x07\n")

    monkeypatch.setattr(task.anyio, "run_process", fake_run_process)
    scripted = ScriptedIO([])
    spec = make_spec("opencode-none", exec_script="true")
    await task.run_session(RecordingSandbox(), DockerHandle("c"), spec, scripted.io, USER)
    assert scripted.output == ["ok\\x1b[2J\tdone\nwarn\\x07\n"]


async def test_export_failure_message_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_export(*args: Any) -> task.Exported:
        raise RuntimeError("no JSON result in output: \x1b]0;title\x07")

    monkeypatch.setattr(task, "export", fake_export)
    scripted = ScriptedIO(["a"])
    with pytest.raises(RuntimeError):
        await task.export_with_recovery(
            RecordingSandbox(), DockerHandle("c"), make_spec("opencode-none"),
            scripted.io, USER, lambda exported: None,
        )  # fmt: skip
    assert "\x1b" not in scripted.output[0]
    assert "output: \\x1b]0;title\\x07" in scripted.output[0]


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
    await task.import_config(sbox, DockerHandle("c"), spec, USER)
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


# ------------------------------------------------------------------- subscription

TOKEN = "sk-ant-oat01-test-not-a-real-token"


def subscription_spec(harness: str = "claude-code-bwrap", **overrides: Any) -> DevSpec:
    fields = {"subscription_token": TOKEN} | overrides
    return make_spec(harness, model="claude-sonnet-subscription", **fields)


def test_render_launcher_env_files() -> None:
    text = task.render_launcher(
        env={"A": "1"}, cwd="/w", binary="/b", args=[], relay=False,
        env_files={"CLAUDE_CODE_OAUTH_TOKEN": "/var/lib/x/a b"},
    )  # fmt: skip
    assert "CLAUDE_CODE_OAUTH_TOKEN=$(cat '/var/lib/x/a b'); export CLAUDE_CODE_OAUTH_TOKEN" in text
    subprocess.run(["sh", "-n"], input=text, text=True, check=True)
    with pytest.raises(ValueError):
        task.render_launcher(
            env={}, cwd="/w", binary="/b", args=[], relay=False, env_files={"A B": "/f"}
        )


def test_claude_launch_subscription() -> None:
    spec = subscription_spec()
    env, args = task.claude_launch(spec, models=None, home="/home/agent")
    assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{task.PROXY_PORT}"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
    # nothing routes to the bridge or overrides the subscription login
    assert not [k for k in env if k.startswith("ANTHROPIC_")]
    assert TOKEN not in json.dumps([env, args])  # the launcher reads it from TOKEN_FILE
    assert args[3:5] == ["--model", "claude-sonnet-5-5"]
    assert TOKEN not in repr(spec)


async def test_import_config_subscription_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[str] = []

    async def fake_install(sbox: Any, path: str, content: str, mode: str = "0755") -> None:
        installed.append(content)

    monkeypatch.setattr(task, "install_root_file", fake_install)
    spec = subscription_spec(claude_seed={"hasCompletedOnboarding": True})
    await task.import_config(RecordingSandbox(), DockerHandle("c"), spec, USER)
    # an apiKeyHelper would win over the subscription token
    assert json.loads(installed[0]) == {"skipDangerousModePermissionPrompt": True}


class PushHandle:
    def __init__(self) -> None:
        self.pushed: list[tuple[Path, str, str]] = []

    async def push(self, local: Path, remote: str) -> None:
        self.pushed.append((local, local.read_text(), remote))


async def test_push_token_bypasses_inspect() -> None:
    sbox, handle = RecordingSandbox(), PushHandle()
    await task.push_token(sbox, handle, subscription_spec(), USER)
    [(local, content, remote)] = handle.pushed
    assert content == TOKEN and remote == task.TOKEN_FILE
    assert not local.exists()  # the host copy is gone
    [(cmd, stdin, user)] = sbox.calls
    assert user == "root" and cmd[-2:] == ["1001", task.TOKEN_FILE] and "0400" in cmd[2]
    assert TOKEN not in json.dumps(sbox.calls)
    with pytest.raises(RuntimeError, match="no subscription token"):
        await task.push_token(sbox, handle, subscription_spec(subscription_token=None), USER)


async def test_start_egress_proxy() -> None:
    sbox = RecordingSandbox()
    pins = {"api.anthropic.com": ["192.0.2.1", "192.0.2.2"]}
    await task.start_egress_proxy(sbox, pins)
    (log_cmd, _, log_user), (cmd, _, user) = sbox.calls
    assert log_user == "root" and task.PROXY_USER in log_cmd and log_cmd[-1] == task.PROXY_LOG
    assert user == task.PROXY_USER != USER.name
    assert cmd[2] == task.EGRESS_PROXY and cmd[-1] == "--daemon"
    allowed = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--allow"]
    assert allowed == list(task.SUBSCRIPTION_HOSTS)
    assert cmd[cmd.index("--resolve") + 1] == "api.anthropic.com=192.0.2.1,192.0.2.2"


class EgressSandbox(RecordingSandbox):
    def __init__(self, policy: dict[str, Any]) -> None:
        super().__init__()
        self.policy = policy

    async def exec(self, cmd: list[str], input: str | None = None, user: str | None = None,
                   **kwargs: Any) -> ExecResult[str]:  # fmt: skip
        self.calls.append((cmd, input, user))
        out = json.dumps(self.policy) if task.EGRESS in cmd else ""
        return ExecResult(success=True, returncode=0, stdout=out, stderr="")


@pytest.mark.parametrize("effective", [True, False])
async def test_subscription_needs_effective_egress_rule(
    monkeypatch: pytest.MonkeyPatch, effective: bool
) -> None:
    async def fake_install(sbox: Any, path: str, content: str, mode: str = "0755") -> None:
        pass

    monkeypatch.setattr(task, "install_root_file", fake_install)
    sbox = EgressSandbox({"effective": effective, "reason": "nft failed: denied"})
    spec = subscription_spec()
    if effective:
        await task.base_setup(sbox, spec, USER)
    else:
        with pytest.raises(RuntimeError, match="nft failed"):
            await task.base_setup(sbox, spec, USER)
    # bridge sessions keep the environment's own network policy (no network on Docker)
    await task.base_setup(sbox, make_spec("claude-code-bwrap"), USER)


def test_build_dev_task_subscription(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    io = DevIO(out=print, ask=input, edit=lambda c, s: c, interactive=False)
    built = task.build_dev_task(subscription_spec(), io, lambda e: None)
    assert str(built.model) == "none/none"
    compose = Path(str(built.sandbox.config)).read_text()
    assert "network_mode: bridge" in compose and "NET_ADMIN" in compose
    plain = task.build_dev_task(make_spec("claude-code-bwrap"), io, lambda e: None)
    assert "network_mode: none" in Path(str(plain.sandbox.config)).read_text()


@pytest.mark.parametrize(
    ("harness", "error"),
    [
        ("claude-code-bwrap", None),
        ("claude-code-none", None),
        ("opencode-bwrap", "only Claude Code itself"),
        ("claude-code-subagents-bwrap", "only Claude Code itself"),
        ("claude-code-opencode-none", "only Claude Code itself"),
    ],
)
def test_dev_profiles_subscription(harness: str, error: str | None) -> None:
    from agenteval.config import ConfigError
    from agenteval.dev import commands

    args = (PROFILES, harness, "docker-ubuntu", "claude-sonnet-subscription", None)
    if error is None:
        assert commands._profiles(*args)[2].access == "subscription"
    else:
        with pytest.raises(ConfigError, match=error):
            commands._profiles(*args)


def test_dev_profiles_subscription_worker_refused() -> None:
    from agenteval.config import ConfigError
    from agenteval.dev import commands

    with pytest.raises(ConfigError, match="cannot serve workers"):
        commands._profiles(
            PROFILES, "claude-code-subagents-bwrap", "docker-ubuntu", "mock",
            "claude-sonnet-subscription",
        )  # fmt: skip


def test_dev_harness_subscription_passes_proxy() -> None:
    from agenteval.dev import commands

    h = PROFILES.harnesses["claude-code-bwrap"]
    assert "HTTPS_PROXY" not in commands._dev_harness(h, []).inner_sandbox.env_passthrough
    sub = commands._dev_harness(h, [], subscription=True)
    assert sub.inner_sandbox.env_passthrough.count("HTTPS_PROXY") == 1


# ------------------------------------------------------------------- spend limits


def test_spend_limits_are_scoped_and_opt_in() -> None:
    assert task.spend_limits(make_spec("claude-code-none")) == []
    token, cost = task.spend_limits(make_spec("claude-code-none", token_limit=500, cost_limit=2.5))
    assert (token.limit, getattr(token, "type", None)) == (500, "output")
    assert cost.limit == 2.5


def test_tripped_limit_only_for_own_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from inspect_ai.util import LimitExceededError, token_limit

    ours, other = token_limit(1), token_limit(1)
    error = LimitExceededError("token", value=33, limit=1, source=ours)
    monkeypatch.setattr(task, "sample_active", lambda: SimpleNamespace(limit_exceeded_error=error))
    assert task.tripped_limit([ours]) is error
    assert task.tripped_limit([other]) is None  # e.g. a sample limit: not ours to handle
    monkeypatch.setattr(task, "sample_active", lambda: None)
    assert task.tripped_limit([ours]) is None


def test_limit_message() -> None:
    from inspect_ai.util import LimitExceededError

    cost = task.limit_message(LimitExceededError("cost", value=5.2, limit=5.0))
    assert cost.startswith("Spend limit reached ($5): the CLI was stopped;")
    tokens = task.limit_message(LimitExceededError("token", value=20_001, limit=20_000))
    assert tokens.startswith("Output token limit reached (20,000 tokens)")
