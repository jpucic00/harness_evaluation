"""Dev session records: ``<checkout>/.agenteval/sessions/<id>/session.json``.

A session directory holds everything brought back from one ``agenteval dev`` session
until the user has reviewed it: the agent's commits (``commits.bundle``, also fetched
into ``refs/agenteval/<id>/*`` of the user's repo), a readable ``worktree.patch``, the
returned CLI config and memory (``config/``) and this record.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SESSIONS_DIR = Path(".agenteval") / "sessions"
"""Relative to the agenteval checkout (already gitignored)."""
SESSION_FILE = "session.json"
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

Status = Literal["running", "export_failed", "pending", "applied", "discarded", "failed"]
Mount = Literal["copy", "live"]
AgentName = Literal["opencode", "claude_code"]
Decision = Literal["applied", "skipped"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepoState(_Record):
    """The user's repo at session start (host side)."""

    root: str
    """Absolute host path: the git toplevel, or the directory itself when not a repo."""
    cwd_rel: str = ""
    """Where ``agenteval dev`` was started, relative to ``root``."""
    git: bool
    shadow_git_dir: str | None = None
    """Non-git directories: bare repo in the session dir that tracks the snapshot."""
    head: str | None = None
    """HEAD commit at start (None: unborn or not a repo)."""
    branch: str | None = None
    """Checked-out branch at start (None: detached or not a repo)."""
    base_commit: str = ""
    """Snapshot commit of the working tree at start (``refs/agenteval/<id>/base``)."""
    base_tree: str = ""
    index_commit: str | None = None
    """Commit of the index at start, so staged state is reproduced in the env."""
    flattened: list[str] = Field(default_factory=list)
    """Submodules / nested repos copied as plain files (no history)."""
    lfs_paths: list[str] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)
    live_hashes: dict[str, str] = Field(default_factory=dict)
    """Live mode: sha256 of host-executed git files (``.git/config``, hooks) at start."""


class ConfigState(_Record):
    """The host CLI config copied in (see ``agenteval.dev.userconfig``)."""

    agent: AgentName
    host_dir: str
    """``~/.claude`` or ``~/.config/opencode`` (or the override)."""
    host_key: str | None = None
    """Claude project key of the repo on the host (memory dir name)."""
    vm_key: str | None = None
    local_file: str | None = None
    """Host path of ``CLAUDE.local.md`` when it was copied in."""
    staged: list[str] = Field(default_factory=list)
    """Logical names of the files copied in (``claude/settings.json``, ``memory/...``)."""
    reports: list[str] = Field(default_factory=list)
    """Things that cannot work in the env (host-path hooks, MCP servers, plugins)."""


class ResultState(_Record):
    """What the env handed back at export."""

    final_commit: str
    final_tree: str
    head: str | None = None
    head_branch: str | None = None
    branches: dict[str, str] = Field(default_factory=dict)
    excluded: list[str] = Field(default_factory=list)
    """Untracked files left out of the export (too large)."""


class DevSession(_Record):
    id: str
    created: datetime
    finished: datetime | None = None
    status: Status = "running"
    harness: str
    model: str
    environment: str
    mount: Mount = "copy"
    vm_root: str
    """Repo root inside the env (``/workspace`` in copy mode, the host path when live)."""
    vm_cwd: str
    repo: RepoState
    config: ConfigState | None = None
    result: ResultState | None = None
    decisions: dict[str, Decision] = Field(default_factory=dict)
    """Review decisions per item id; items not listed are still pending."""
    log_dir: str | None = None
    error: str | None = None


def new_session_id(repo_name: str, now: datetime | None = None) -> str:
    """``<UTC stamp>-<repo name>-<4 hex>``: sortable and safe as a directory name."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    # no dots: the id is also a git ref component (refs/agenteval/<id>/...)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", repo_name).strip("-")[:60] or "repo"
    return f"{stamp}-{name}-{secrets.token_hex(2)}"


def validate_session_id(session_id: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    return session_id


def sessions_root(checkout: Path) -> Path:
    return checkout / SESSIONS_DIR


def session_dir(checkout: Path, session_id: str) -> Path:
    return sessions_root(checkout) / validate_session_id(session_id)


def create_session_dir(checkout: Path, session_id: str) -> Path:
    path = session_dir(checkout, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    return path


def save_session(session: DevSession, directory: Path) -> None:
    """Write ``session.json`` atomically."""
    text = session.model_dump_json(indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".session.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, directory / SESSION_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_session(checkout: Path, session_id: str) -> DevSession:
    path = session_dir(checkout, session_id) / SESSION_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no dev session '{session_id}' under {sessions_root(checkout)}")
    return DevSession.model_validate_json(path.read_text())


def list_sessions(checkout: Path) -> list[DevSession]:
    """All readable sessions, newest first."""
    root = sessions_root(checkout)
    found: list[DevSession] = []
    if root.is_dir():
        for path in root.glob(f"*/{SESSION_FILE}"):
            try:
                found.append(DevSession.model_validate_json(path.read_text()))
            except ValueError:
                continue
    return sorted(found, key=lambda s: s.created, reverse=True)
