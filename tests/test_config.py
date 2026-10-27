"""Profile schema: templates, JSON Schema files, and settings rejected for having no effect."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agenteval.config import (
    DEFAULT_AGENT_MODELS,
    SCHEMA_KINDS,
    ConfigError,
    DockerSettings,
    EnvironmentProfile,
    HarnessProfile,
    InnerSandbox,
    ModelProfile,
    SbxSettings,
    Subagents,
    TartSettings,
    _read_yaml,
    config_schema,
    load_profiles,
    load_run,
    size_mb,
)
from agenteval.runner import _run_files, resolve_run_file

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
DIRS = {"model": "models", "harness": "harnesses", "environment": "environments", "run": "runs"}


@pytest.mark.parametrize("kind", SCHEMA_KINDS)
def test_template_is_a_valid_profile(kind: str) -> None:
    template = CONFIG / DIRS[kind] / "_template.yaml"
    SCHEMA_KINDS[kind].model_validate(_read_yaml(template))


@pytest.mark.parametrize("kind", SCHEMA_KINDS)
def test_committed_schema_is_current(kind: str) -> None:
    path = CONFIG / "schema" / f"{kind}.schema.json"
    assert path.is_file() and json.loads(path.read_text()) == config_schema(kind), (
        f"{path} is stale: run `uv run python scripts/gen_config_schema.py`"
    )


@pytest.mark.parametrize("kind", SCHEMA_KINDS)
def test_every_profile_points_at_its_schema(kind: str) -> None:
    header = f"# yaml-language-server: $schema=../schema/{kind}.schema.json"
    for path in sorted((CONFIG / DIRS[kind]).glob("*.y*ml")):
        assert path.read_text().splitlines()[0] == header, path


def test_schema_does_not_require_name() -> None:
    for kind in SCHEMA_KINDS:
        schema = config_schema(kind)
        assert "name" not in schema.get("required", schema.get("else", {}).get("required", []))
    suites = config_schema("run")["properties"]["suites"]["items"]["anyOf"]
    assert suites[0]["enum"] == ["containment", "capability", "injection"]


def test_shipped_profiles_and_runs_load() -> None:
    profiles = load_profiles(CONFIG)
    assert profiles.models and profiles.harnesses and profiles.environments
    for path in _run_files(CONFIG):
        load_run(path)


def test_underscore_files_are_skipped(tmp_path: Path) -> None:
    for kind in ("models", "harnesses", "environments", "runs"):
        (tmp_path / kind).mkdir()
    (tmp_path / "models" / "m.yaml").write_text("inspect_model: mockllm/model\n")
    (tmp_path / "models" / "_draft.yaml").write_text("not_a_field: 1\n")
    (tmp_path / "harnesses" / "_template.yml").write_text("agent: nope\n")
    (tmp_path / "environments" / "_x.yaml").write_text("provider: nope\n")
    (tmp_path / "runs" / "r.yaml").write_text("models: []\n")
    (tmp_path / "runs" / "_template.yaml").write_text("models: []\n")

    profiles = load_profiles(tmp_path)
    assert list(profiles.models) == ["m"]
    assert not profiles.harnesses and not profiles.environments
    assert _run_files(tmp_path) == [tmp_path / "runs" / "r.yaml"]
    assert resolve_run_file("r", tmp_path) == tmp_path / "runs" / "r.yaml"
    with pytest.raises(ConfigError, match="run file not found"):
        resolve_run_file("_template", tmp_path)


@pytest.mark.parametrize(
    ("provider", "block"), [("docker", "tart"), ("tart", "sbx"), ("sbx", "docker")]
)
def test_settings_of_another_provider_are_rejected(provider: str, block: str) -> None:
    with pytest.raises(ValueError, match=f"{block}: settings have no effect with provider"):
        EnvironmentProfile.model_validate({"name": "e", "provider": provider, block: {}})


@pytest.mark.parametrize(
    "options",
    [
        {"hide_paths": ["~/.ssh"]},
        {"extra_writable_paths": ["/tmp/x"]},
        {"env_passthrough": ["FOO"]},
        {"network": "host"},
        {"clear_env": False},
    ],
)
def test_inner_sandbox_options_without_sandbox_are_rejected(options: dict[str, Any]) -> None:
    (name,) = options
    with pytest.raises(ValueError, match=f"inner_sandbox {name} has no effect with type: none"):
        InnerSandbox.model_validate({"type": "none", **options})
    InnerSandbox.model_validate({"type": "bwrap", **options})


def test_inner_sandbox_defaults_spelled_out_are_fine() -> None:
    InnerSandbox.model_validate({"type": "none", "network": "proxy-only", "clear_env": True})
    InnerSandbox.model_validate({"type": "none", "hide_paths": []})


def test_opencode_version_needs_opencode_runner() -> None:
    with pytest.raises(ValueError, match="opencode_version is only used with runner: opencode"):
        Subagents.model_validate(
            {"runner": "claude_code", "model": "w", "opencode_version": "1.19.3"}
        )
    Subagents.model_validate({"runner": "opencode", "model": "w", "opencode_version": "1.19.3"})
    Subagents.model_validate({"runner": "claude_code", "model": "w"})


def test_rejection_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "harnesses").mkdir()
    bad = tmp_path / "harnesses" / "h.yaml"
    bad.write_text("agent: opencode\ninner_sandbox: {type: none, network: host}\n")
    with pytest.raises(ConfigError, match=r"(?s)h\.yaml.*network has no effect"):
        load_profiles(tmp_path)
    assert HarnessProfile.model_validate({"name": "h", "agent": "opencode"})


# --------------------------------------------------------------------------- extends


def _write(directory: Path, files: dict[str, str]) -> None:
    for kind in ("models", "harnesses", "environments"):
        (directory / kind).mkdir(exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)


def test_shipped_harness_profiles_are_unchanged_by_extends() -> None:
    """The *-bwrap profiles extend their *-none twins; the loaded result is what the
    hand-copied files gave (snapshot taken before the switch to extends)."""
    expected = json.loads((REPO / "tests" / "data" / "harness_profiles.json").read_text())
    harnesses = load_profiles(CONFIG).harnesses
    assert {n: h.model_dump(mode="json") for n, h in harnesses.items()} == expected
    for name in expected:
        if name.endswith("-bwrap"):
            assert _read_yaml(CONFIG / "harnesses" / f"{name}.yaml")["extends"] == name.replace(
                "-bwrap", "-none"
            )


def test_extends_merges_mappings_and_replaces_lists(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "harnesses/base.yaml": (
                "agent: opencode\nversion: '1.0'\nenv: {A: '1', B: '2'}\n"
                "inner_sandbox: {type: bwrap, hide_paths: [/a, /b], network: host}\n"
            ),
            "harnesses/child.yaml": (
                "extends: base\ndescription: child\nenv: {B: '3'}\n"
                "inner_sandbox: {hide_paths: [/c]}\n"
            ),
        },
    )
    child = load_profiles(tmp_path).harnesses["child"]
    assert child.name == "child" and child.description == "child" and child.version == "1.0"
    assert child.env == {"A": "1", "B": "3"}
    assert child.inner_sandbox.type == "bwrap" and child.inner_sandbox.network == "host"
    assert child.inner_sandbox.hide_paths == ["/c"]
    assert "extends" not in child.model_dump()


def test_extends_chain_and_name_not_inherited(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "models/a.yaml": "name: alpha\ninspect_model: mockllm/a\ndescription: A\n",
            "models/b.yaml": "extends: alpha\nrequired_env: [X]\n",
            "models/c.yaml": "extends: b\ndescription: C\n",
        },
    )
    models = load_profiles(tmp_path).models
    assert sorted(models) == ["alpha", "b", "c"]
    c = models["c"]
    assert (c.name, c.inspect_model, c.required_env, c.description) == (
        "c",
        "mockllm/a",
        ["X"],
        "C",
    )


def test_extends_cycle_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "harnesses/a.yaml": "extends: b\nagent: opencode\n",
            "harnesses/b.yaml": "extends: c\n",
            "harnesses/c.yaml": "extends: a\n",
        },
    )
    with pytest.raises(ConfigError, match=r"extends cycle (a -> b -> c -> a|b -> c -> a -> b)"):
        load_profiles(tmp_path)
    _write(tmp_path, {"harnesses/a.yaml": "extends: a\nagent: opencode\n"})
    with pytest.raises(ConfigError, match="extends cycle a -> a"):
        load_profiles(tmp_path)


def test_extends_unknown_or_other_kind_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "models/m.yaml": "inspect_model: mockllm/model\n",
            "harnesses/_t.yaml": "agent: opencode\n",
            "harnesses/h.yaml": "extends: m\nagent: opencode\n",
        },
    )
    with pytest.raises(ConfigError, match=r"h\.yaml: extends unknown profile 'm'"):
        load_profiles(tmp_path)
    _write(tmp_path, {"harnesses/h.yaml": "extends: _t\n"})  # templates are not profiles
    with pytest.raises(ConfigError, match="extends unknown profile '_t'"):
        load_profiles(tmp_path)


def test_extended_profile_is_validated_after_merging(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "harnesses/none.yaml": "agent: opencode\ninner_sandbox: {type: none}\n",
            "harnesses/bad.yaml": "extends: none\ninner_sandbox: {network: host}\n",
        },
    )
    with pytest.raises(ConfigError, match=r"(?s)bad\.yaml.*network has no effect"):
        load_profiles(tmp_path)


def test_schema_allows_extends_and_relaxes_required() -> None:
    for kind in ("model", "harness", "environment"):
        schema = config_schema(kind)
        assert schema["properties"]["extends"]["type"] == "string"
        assert "required" not in schema
    assert config_schema("harness")["else"]["required"] == ["agent"]
    assert "extends" not in config_schema("run")["properties"]


# --------------------------------------------------------------------------- agent models


def test_opencode_default_agent_model() -> None:
    model = ModelProfile(name="m", inspect_model="mockllm/model")
    assert model.agent_models == DEFAULT_AGENT_MODELS
    custom = ModelProfile(
        name="m", inspect_model="mockllm/model", agent_models={"opencode": "anthropic/x"}
    )
    assert custom.agent_models == {"opencode": "anthropic/x"}
    subscription = ModelProfile(
        name="s", access="subscription", agent_models={"claude_code": "sonnet"}
    )
    assert subscription.agent_models == {"claude_code": "sonnet"}


def test_shipped_models_keep_their_opencode_model() -> None:
    """What OpenCode was told before the default existed (inspect_swe's own default for
    the Claude profiles, which had no entry)."""
    qwen = "openrouter/qwen/qwen3.8-27b"
    expected = {
        "claude-sonnet-api": "anthropic/claude-sonnet-4-5",
        "claude-sonnet-bedrock": "anthropic/claude-sonnet-4-5",
        "gateway-devstral-2512": qwen,
        "gateway-gpt-oss-120b": qwen,
        "gateway-qwen": qwen,
        "mock": qwen,
        "mock-worker": qwen,
        "qwen38-27b": qwen,
    }
    models = load_profiles(CONFIG).models
    assert {n: m.agent_models.get("opencode") for n, m in models.items() if n in expected} == (
        expected
    )
    assert "opencode" not in models["claude-sonnet-subscription"].agent_models


# --------------------------------------------------------------------------- environments


@pytest.mark.parametrize(
    ("old", "new"), [("cpu", "use cpus"), ("memory_mb", "use memory \\(a size")]
)
def test_renamed_tart_keys_are_rejected(old: str, new: str) -> None:
    with pytest.raises(ValueError, match=f"'{old}' was renamed: {new}"):
        EnvironmentProfile.model_validate({"name": "e", "provider": "tart", "tart": {old: 4}})


def test_memory_sizes() -> None:
    assert size_mb("4g") == 4096 and size_mb("512m") == 512 and size_mb("4096MB") == 4096
    assert size_mb("1t") == 1024 * 1024
    assert TartSettings(memory="8G").memory == "8G"
    for settings in (TartSettings, DockerSettings, SbxSettings):
        with pytest.raises(ValueError, match="not a size"):
            settings.model_validate({"memory": "lots"})
        with pytest.raises(ValueError, match="below the 512m minimum"):
            settings.model_validate({"memory": "256m"})
    assert DockerSettings(memory=None).memory is None
    with pytest.raises(ValueError):
        TartSettings.model_validate({"memory": None})
    with pytest.raises(ValueError):
        DockerSettings.model_validate({"cpus": 0})
