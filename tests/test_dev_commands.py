"""agenteval dev orchestration: session status after reviews, apply later, listing."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agenteval import constants
from agenteval.config import ConfigError
from agenteval.dev import commands
from agenteval.dev.review import DevIO, ReviewItem
from agenteval.dev.session import (
    ConfigState,
    DevSession,
    RepoState,
    ResultState,
    create_session_dir,
    load_session,
    save_session,
)

SID = "20261020T000000Z-proj-abcd"


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "checkout"
    (root / "config").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    monkeypatch.setattr(constants, "checkout_root", lambda: root)
    return root


def make_session(checkout: Path, *, status: str = "pending") -> DevSession:
    session = DevSession(
        id=SID,
        created=datetime.now(UTC),
        status=status,  # type: ignore[arg-type]
        harness="opencode-none",
        model="mock",
        environment="docker-ubuntu",
        vm_root="/workspace",
        vm_cwd="/workspace",
        repo=RepoState(root=str(checkout.parent / "proj"), git=True),
        config=ConfigState(agent="opencode", host_dir=str(checkout.parent / "cfg")),
        result=ResultState(final_commit="a" * 40, final_tree="b" * 40),
    )
    save_session(session, create_session_dir(checkout, SID))
    return session


def item(item_id: str, group: str) -> ReviewItem:
    return ReviewItem(
        id=item_id,
        group=group,  # type: ignore[arg-type]
        label=item_id,
        kind="modified",
        current=b"a",
        proposed=b"b",
        apply=lambda data: None,
    )


@pytest.fixture
def producers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the item producers and the review loop; record ref cleanups."""
    state: dict[str, Any] = {
        "repo": [item("repo:a.py", "repo")],
        "config": [item("config:opencode/opencode.json", "config")],
        "outcome": "done",
        "cleanups": [],
        "decide": True,
    }
    monkeypatch.setattr(commands.repo, "repo_items", lambda s, d: (state["repo"], []))
    monkeypatch.setattr(commands.userconfig, "config_items", lambda s, d: state["config"])
    monkeypatch.setattr(
        commands.repo, "cleanup_refs", lambda s, keep_heads: state["cleanups"].append(keep_heads)
    )

    def fake_review(session: DevSession, items: list[ReviewItem], io: DevIO, **kw: Any) -> str:
        state["shown"] = [i.id for i in items]
        if state["decide"]:
            for i in items:
                session.decisions[i.id] = "applied"
        return str(state["outcome"])

    monkeypatch.setattr(commands, "review", fake_review)
    return state


IO = DevIO(out=lambda text: None, ask=lambda prompt: "y", edit=lambda c, s: c)


def test_config_only_review_keeps_repo_changes_pending(
    checkout: Path, producers: dict[str, Any]
) -> None:
    make_session(checkout)
    assert commands.apply_session(SID, config_only=True, io=IO) == 0
    assert producers["shown"] == ["config:opencode/opencode.json"]
    session = load_session(checkout, SID)
    assert session.status == "pending"  # repo:a.py is still undecided
    assert producers["cleanups"] == []  # the refs it needs stay

    assert commands.apply_session(SID, config_only=False, io=IO) == 0
    session = load_session(checkout, SID)
    assert session.status == "applied"
    assert producers["cleanups"] == [True]  # internal refs go, agent commits stay


def test_deferred_review_keeps_everything(checkout: Path, producers: dict[str, Any]) -> None:
    make_session(checkout)
    producers.update(outcome="deferred", decide=False)
    commands.apply_session(SID, config_only=False, io=IO)
    assert load_session(checkout, SID).status == "pending"
    assert producers["cleanups"] == []


def test_discard_closes_the_session(checkout: Path, producers: dict[str, Any]) -> None:
    make_session(checkout)
    producers.update(outcome="discarded", decide=False)
    commands.apply_session(SID, config_only=False, io=IO)
    assert load_session(checkout, SID).status == "discarded"
    assert producers["cleanups"] == [True]


def test_apply_refuses_failed_sessions(checkout: Path, producers: dict[str, Any]) -> None:
    make_session(checkout, status="failed")
    with pytest.raises(ConfigError, match="failed"):
        commands.apply_session(SID, config_only=False, io=IO)


def test_format_sessions(checkout: Path) -> None:
    assert commands.format_sessions() == "No dev sessions."
    make_session(checkout)
    lines = commands.format_sessions().splitlines()
    assert lines[0].split() == ["SESSION", "STATUS", "HARNESS", "MODEL", "ENV", "REPO"]
    assert lines[1].split()[:2] == [SID, "pending"]


def test_run_eval_bounds_model_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inspect retries a failing model call forever by default; display="none" hides it."""
    seen: dict[str, Any] = {}

    def fake_eval(task: object, **kwargs: Any) -> list[object]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(commands, "inspect_eval", fake_eval)
    assert commands._run_eval(lambda: object(), tmp_path, None) is None  # type: ignore[arg-type,return-value]
    assert seen["max_retries"] == commands.DEV_MAX_RETRIES
    assert seen["attempt_timeout"] == commands.DEV_ATTEMPT_TIMEOUT >= 600  # slow turns survive
    assert seen["display"] == "none"


# ------------------------------------------------------------------- spend limits

REPO = Path(__file__).resolve().parents[1]


def _start(checkout: Path, tmp_path: Path, **kwargs: Any) -> int:
    defaults: dict[str, Any] = {
        "config_dir": REPO / "config",
        "cwd": tmp_path,
        "harness": "claude-code-none",
        "environment": "docker-ubuntu",
        "model": "mock",
        "mount": "copy",
        "includes": [],
        "agent_config_dir": None,
        "host_config": False,
        "io": IO,
    }
    return commands.start_session(**(defaults | kwargs))


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        # gateway profiles have no pricing: a cost cap could never trip
        ({"model": "gateway-qwen", "cost_limit": 1.0}, "no pricing in: gateway-qwen"),
        (
            {"harness": "claude-code-subagents-none", "subagent_model": "gateway-qwen",
             "cost_limit": 1.0},
            "no pricing in: gateway-qwen",
        ),
        # the subscription's calls never reach Inspect
        ({"model": "claude-sonnet-subscription", "token_limit": 100}, "bypass Inspect"),
        ({"model": "claude-sonnet-subscription", "cost_limit": 1.0}, "bypass Inspect"),
        ({"cost_limit": 0.0}, "--cost-limit must be more than 0"),
        ({"token_limit": -5}, "--token-limit must be more than 0"),
    ],
)  # fmt: skip
def test_start_refuses_unenforceable_limits(
    checkout: Path, tmp_path: Path, kwargs: dict[str, Any], error: str
) -> None:
    with pytest.raises(ConfigError, match=error):
        _start(checkout, tmp_path, **kwargs)
    assert not (checkout / ".agenteval").exists()  # refused before anything started


def test_start_refuses_unknown_harness_options(checkout: Path, tmp_path: Path) -> None:
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config)
    profile = config / "harnesses" / "claude-code-none.yaml"
    profile.write_text(profile.read_text() + "options:\n  no_such_option: 1\n")
    with pytest.raises(ConfigError, match="no_such_option"):
        _start(checkout, tmp_path, config_dir=config)
    assert not (checkout / ".agenteval").exists()


def test_limits_allowed_with_priced_models_or_tokens_only() -> None:
    from agenteval.config import load_profiles

    models = load_profiles(REPO / "config").models
    priced = [models["mock"], models["mock-worker"]]
    commands._check_limits(priced, 2.5, 10_000)
    # a token cap needs no pricing
    commands._check_limits([models["gateway-qwen"]], None, 10_000)
    # no limit: nothing is checked, not even a subscription
    commands._check_limits([models["claude-sonnet-subscription"]], None, None)
