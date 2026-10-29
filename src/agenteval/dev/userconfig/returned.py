"""Return: the config the env exports, unpacked into the session directory."""

from __future__ import annotations

import json
import shutil
import tarfile
from collections.abc import Collection
from pathlib import Path

from agenteval.dev.session import ConfigState
from agenteval.dev.userconfig.common import (
    _BAD_NAME,
    CLAUDE_DIRS,
    CLAUDE_FILES,
    LOCAL_FILE,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    OPENCODE_DIRS,
    OPENCODE_FILES,
    SKIPPED_FILE,
)
from agenteval.dev.userconfig.staging import _keep


def export_paths(state: ConfigState) -> tuple[list[str], list[str]]:
    """Paths the env archives back: (relative to the agent's HOME, relative to the
    repo root in the env). Directories are listed whole so new files come back; the
    env archives the ones that exist as ``home/<path>`` and ``workspace/<path>``
    members (an empty archive means everything was deleted)."""
    if state.agent == "claude_code":
        home = [f".claude/{name}" for name in (*CLAUDE_FILES, *CLAUDE_DIRS)]
        if state.vm_key:
            home.append(f".claude/projects/{state.vm_key}/memory")
    else:
        home = [f".config/opencode/{name}" for name in (*OPENCODE_FILES, *OPENCODE_DIRS)]
    return home, [LOCAL_FILE] if state.local_file else []


def _valid_rel(rel: str) -> bool:
    parts = rel.split("/")
    return bool(rel) and not _BAD_NAME.search(rel) and all(p not in ("", ".", "..") for p in parts)


def _allowed(rest: str, files: Collection[str], dirs: Collection[str]) -> bool:
    parts = rest.split("/")
    allowed = parts[0] in files if len(parts) == 1 else parts[0] in dirs
    return allowed and _valid_rel(rest) and _keep(rest)


def _logical_name(member: str, state: ConfigState) -> str | None:
    """Map an archive member back to its logical name (None: not on the allow-list)."""
    name = member
    while name.startswith("./"):
        name = name[2:]
    if not _valid_rel(name):
        return None
    if state.local_file and name == f"workspace/{LOCAL_FILE}":
        return f"local/{LOCAL_FILE}"
    if state.agent == "claude_code":
        if state.vm_key:
            prefix = f"home/.claude/projects/{state.vm_key}/memory/"
            if name.startswith(prefix):
                rest = name[len(prefix) :]
                return f"memory/{rest}" if _valid_rel(rest) and _keep(rest) else None
        if name.startswith("home/.claude/"):
            rest = name[len("home/.claude/") :]
            return f"claude/{rest}" if _allowed(rest, CLAUDE_FILES, CLAUDE_DIRS) else None
        return None
    if name.startswith("home/.config/opencode/"):
        rest = name[len("home/.config/opencode/") :]
        return f"opencode/{rest}" if _allowed(rest, OPENCODE_FILES, OPENCODE_DIRS) else None
    return None


def extract_returned(tar: Path, state: ConfigState, session_dir: Path) -> list[str]:
    """Unpack the env's config archive into ``<session>/config/vm/<logical name>``.

    Only regular files on the allow-list are kept (tar ``data`` filter on top). Files
    over 2 MB are skipped, returned and recorded in ``config/skipped.json`` (review
    leaves them alone); more than 50 MB in total raises ValueError and keeps nothing.
    """
    config_dir = session_dir / "config"
    dest = config_dir / "vm"
    if dest.exists():
        shutil.rmtree(dest)
    (config_dir / SKIPPED_FILE).unlink(missing_ok=True)
    dest.mkdir(parents=True)
    skipped: list[str] = []
    total = 0

    def member_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal total
        if not member.isfile():
            return None
        logical = _logical_name(member.name, state)
        if logical is None:
            return None
        if member.size > MAX_FILE_BYTES:
            skipped.append(logical)
            return None
        total += member.size
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"returned config is larger than {MAX_TOTAL_BYTES >> 20} MB")
        return tarfile.data_filter(member.replace(name=logical, deep=False), path)

    try:
        # "r:": uncompressed only, so a compressed archive cannot expand without bound.
        with tarfile.open(tar, "r:") as archive:
            archive.extractall(dest, filter=member_filter)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    names = sorted(set(skipped))
    (config_dir / SKIPPED_FILE).write_text(json.dumps(names) + "\n")
    return names
