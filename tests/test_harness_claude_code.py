"""Claude Code harness: config, agent arguments, subagent files and the OpenCode worker."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from inspect_ai.model import ModelUsage, get_model

from agenteval.config import (
    ConfigError,
    EnvironmentProfile,
    HarnessProfile,
    Limits,
    MatrixCell,
    ModelProfile,
    ProfileSet,
    RunConfig,
    SubagentDef,
    SuiteSelection,
    expand_matrix,
    load_profiles,
)
from agenteval.harnesses import claude_code, harness_solvers
from agenteval.report.economics import usage_economics

REPO = Path(__file__).resolve().parents[1]
MAIN = ModelProfile(name="main", inspect_model="mockllm/model")
WORKER = ModelProfile(name="worker", inspect_model="mockllm/worker")
ENV = EnvironmentProfile(name="docker", provider="docker")


def harness(**subagents: object) -> HarnessProfile:
    data: dict[str, object] = {"name": "cc", "agent": "claude_code", "version": "2.1.292"}
    if subagents:
        data["subagents"] = subagents
    return HarnessProfile.model_validate(data)


def cell(h: HarnessProfile, worker: ModelProfile | None = None) -> MatrixCell:
    return MatrixCell(
        run="r",
        suite=SuiteSelection(name="capability"),
        model=MAIN,
        harness=h,
        environment=ENV,
        limits=Limits(),
        subagent_model=worker,
    )


# --------------------------------------------------------------------------- config


def test_shipped_profiles_load() -> None:
    profiles = load_profiles(REPO / "config")
    for name in ("claude-code-none", "claude-code-subagents-bwrap", "claude-code-opencode-none"):
        assert profiles.harnesses[name].agent == "claude_code"
    assert profiles.harnesses["claude-code-opencode-bwrap"].subagents is not None
    for name in ("claude-sonnet-api", "claude-sonnet-bedrock", "gateway-qwen", "mock-worker"):
        assert name in profiles.models


def test_subagents_only_for_claude_code() -> None:
    with pytest.raises(ValueError, match="only supported for agent: claude_code"):
        HarnessProfile.model_validate(
            {"name": "x", "agent": "opencode", "subagents": {"runner": "opencode", "model": "m"}}
        )


def test_subagent_names_and_runner_checked() -> None:
    with pytest.raises(ValueError, match="subagent name"):
        harness(
            runner="claude_code",
            model="m",
            agents={"Bad Name": {"description": "d", "prompt": "p"}},
        )
    with pytest.raises(ValueError, match="only used with runner: claude_code"):
        harness(runner="opencode", model="m", agents={"a": {"description": "d", "prompt": "p"}})


def _profiles(h: HarnessProfile) -> ProfileSet:
    return ProfileSet(
        models={
            "main": MAIN,
            "worker": WORKER,
            "other": WORKER.model_copy(update={"name": "other"}),
        },
        harnesses={h.name: h},
        environments={"docker": ENV},
    )


def _run(**extra: object) -> RunConfig:
    return RunConfig.model_validate(
        {
            "name": "r",
            "models": ["main"],
            "harnesses": ["cc"],
            "environments": ["docker"],
            "suites": ["capability"],
            **extra,
        }
    )


def test_expand_matrix_resolves_subagent_model() -> None:
    h = harness(runner="opencode", model="worker")
    (only,) = expand_matrix(_run(), _profiles(h))
    assert only.subagent_model == WORKER
    assert only.metadata()["agenteval"]["subagent_model"]["name"] == "worker"
    (overridden,) = expand_matrix(_run(subagent_model="other"), _profiles(h))
    assert overridden.subagent_model is not None and overridden.subagent_model.name == "other"


def test_expand_matrix_unknown_subagent_model() -> None:
    with pytest.raises(ConfigError, match="subagent model of harness 'cc'"):
        expand_matrix(_run(), _profiles(harness(runner="opencode", model="nope")))


def test_plain_harness_has_no_subagent_model() -> None:
    (only,) = expand_matrix(_run(), _profiles(harness()))
    assert only.subagent_model is None


# --------------------------------------------------------------------------- agent arguments


def test_agent_kwargs_plain() -> None:
    kwargs = claude_code.agent_kwargs(
        cell(harness()), user="agent", system_prompt="S", env={"A": "1"}
    )
    assert kwargs == {
        "cwd": "/workspace",
        "system_prompt": "S",
        "version": "sandbox",
        "user": "agent",
        "env": {"A": "1"},
    }


def test_agent_kwargs_claude_code_subagents() -> None:
    h = harness(
        runner="claude_code", model="worker", agents={"impl": {"description": "d", "prompt": "p"}}
    )
    worker = get_model("mockllm/worker")
    kwargs = claude_code.agent_kwargs(
        cell(h, WORKER), user="agent", system_prompt=None, env={}, worker=worker
    )
    assert kwargs["subagent_model"] == "mockllm/worker"
    assert kwargs["model_aliases"] == {worker.name: worker}
    assert "(impl)" in kwargs["system_prompt"]


def test_agent_kwargs_opencode_workers() -> None:
    h = harness(runner="opencode", model="worker")
    worker = get_model("mockllm/worker")
    kwargs = claude_code.agent_kwargs(
        cell(h, WORKER), user="agent", system_prompt=None, env={}, worker=worker
    )
    assert kwargs["model_aliases"] == {claude_code.WORKER_MODEL_NAME: worker}
    assert "subagent_model" not in kwargs
    assert "opencode-worker" in kwargs["system_prompt"]


def test_agent_kwargs_custom_instructions_and_reserved_options() -> None:
    h = harness(runner="opencode", model="worker", instructions="Use the worker.")
    kwargs = claude_code.agent_kwargs(
        cell(h, WORKER),
        user="agent",
        system_prompt=None,
        env={},
        worker=get_model("mockllm/worker"),
    )
    assert kwargs["system_prompt"] == "Use the worker."
    bad = HarnessProfile.model_validate(
        {"name": "cc", "agent": "claude_code", "options": {"model_aliases": {}}}
    )
    with pytest.raises(ValueError, match="set by agenteval"):
        claude_code.agent_kwargs(cell(bad), user="agent", system_prompt=None, env={})


def test_agent_kwargs_requires_resolved_worker() -> None:
    with pytest.raises(ValueError, match="not resolved"):
        claude_code.agent_kwargs(
            cell(harness(runner="opencode", model="worker")), user="a", system_prompt=None, env={}
        )


def test_harness_solvers_dispatch() -> None:
    assert len(harness_solvers(cell(harness()))) == 3


# --------------------------------------------------------------------------- rendered files


def test_render_subagent_front_matter() -> None:
    text = claude_code.render_subagent(
        "reviewer",
        SubagentDef(description='Checks: "bugs"', prompt=" Review.\n", tools=["Read", "Grep"]),
    )
    assert text.startswith('---\nname: reviewer\ndescription: "Checks: \\"bugs\\""\n')
    assert 'tools: "Read, Grep"\n---\n\nReview.\n' in text


def test_render_wrapper_sets_config_dir() -> None:
    text = claude_code.render_wrapper("/opt/claude bin")
    assert claude_code.WRAPPER_MARKER in text
    assert "export CLAUDE_CONFIG_DIR" in text
    assert "'/opt/claude bin' \"$@\"" in text


def _run_worker(tmp_path: Path, base_url: str | None) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "opencode"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@"\necho "CFG=$OPENCODE_CONFIG KEY=$OPENROUTER_API_KEY"\n'
    )
    fake.chmod(0o755)
    script = tmp_path / "opencode-worker"
    script.write_text(claude_code.render_worker(str(fake), ["/opt/node/bin"]))
    script.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    if base_url is not None:
        env["ANTHROPIC_BASE_URL"] = base_url
    return subprocess.run(
        [str(script), "fix", "the bug"], env=env, capture_output=True, text=True, check=False
    )


def test_worker_routes_through_bridge(tmp_path: Path) -> None:
    result = _run_worker(tmp_path, "http://localhost:3001")
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:3] == ["run", "--model", f"openrouter/{claude_code.WORKER_MODEL_NAME}"]
    assert args[-2] == "fix the bug"
    config = json.loads((tmp_path / ".config/opencode/agenteval-worker.json").read_text())
    provider = config["provider"]["openrouter"]
    assert provider["options"]["baseURL"] == "http://localhost:3001/v1"
    assert claude_code.WORKER_MODEL_NAME in provider["models"]
    assert "KEY=sk-none" in args[-1]


def _fake_worker(tmp_path: Path, body: str) -> tuple[Path, dict[str, str]]:
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(0o755)
    script = tmp_path / "opencode-worker"
    script.write_text(claude_code.render_worker(str(fake), []))
    script.chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    env["ANTHROPIC_BASE_URL"] = "http://localhost:3001"
    return script, env


def test_worker_strips_think_blocks_and_keeps_exit_code(tmp_path: Path) -> None:
    script, env = _fake_worker(
        tmp_path,
        'printf \'> build\\n<think signature="x">\\nplan\\n\\n</think>\\n'
        "Read a.py\\nkeep <think>inline</think>this\\nDone.\\n'\n"
        "exit 3\n",
    )
    result = subprocess.run(
        [str(script), "task"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 3
    assert result.stdout == "> build\nRead a.py\nkeep this\nDone.\n"


def test_worker_gives_opencode_empty_stdin(tmp_path: Path) -> None:
    # `opencode run` reads stdin to EOF; an inherited pipe that stays open must not hang it.
    script, env = _fake_worker(tmp_path, "cat >/dev/null\necho done\n")
    proc = subprocess.Popen(
        [str(script), "task"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.wait(timeout=10) == 0
        assert proc.stdout is not None and proc.stdout.read() == "done\n"
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.kill()


def test_worker_stops_silent_opencode(tmp_path: Path) -> None:
    script, env = _fake_worker(tmp_path, "exec sleep 30\n")
    env["AGENTEVAL_WORKER_QUIET_LIMIT"] = "1"
    result = subprocess.run(
        [str(script), "task"], env=env, capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 124
    assert "printed nothing for 1s" in result.stderr


@pytest.mark.parametrize("base_url", [None, "https://api.anthropic.com", "http://evil:3001"])
def test_worker_refuses_non_bridge_url(tmp_path: Path, base_url: str | None) -> None:
    result = _run_worker(tmp_path, base_url)
    assert result.returncode == 2
    assert "model bridge" in result.stderr


def test_worker_usage_message(tmp_path: Path) -> None:
    script = tmp_path / "w"
    script.write_text(claude_code.render_worker("/bin/true", []))
    script.chmod(0o755)
    result = subprocess.run([str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "usage" in result.stderr


# --------------------------------------------------------------------------- economics


def test_profile_pricing_not_applied_to_multi_model_samples() -> None:
    pricing = {"input": 1.0, "output": 1.0}
    one = {"mockllm/model": ModelUsage(input_tokens=1_000_000, output_tokens=0)}
    assert usage_economics(one, {}, pricing).cost_usd == pytest.approx(1.0)
    two = one | {"mockllm/worker": ModelUsage(input_tokens=10, output_tokens=0)}
    econ = usage_economics(two, {}, pricing)
    assert econ.cost_usd is None and econ.cost_source == "unknown"


def test_seeded_settings_match_inspect_swe() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from inspect_swe._claude_code.env import claude_code_agent_env

    names = SimpleNamespace(presented="m", opus="m", sonnet="m", haiku="m", subagent="m")
    env = claude_code_agent_env(bridge_port=3001, models=cast(Any, names))
    assert env["ANTHROPIC_AUTH_TOKEN"] == claude_code.BRIDGE_API_KEY
    expected = '{"apiKeyHelper": "echo ' + claude_code.BRIDGE_API_KEY + '"}\n'
    assert claude_code.SEEDED_SETTINGS == expected
