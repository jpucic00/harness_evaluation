"""Review items for returned config and memory."""

from __future__ import annotations

import copy
import difflib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenteval.dev.review import GROUP_ORDER, Group, ItemKind, ReviewItem
from agenteval.dev.session import ConfigState, DevSession
from agenteval.dev.userconfig.common import (
    _ANY_FILE_REFERENCE,
    CLAUDE_DIRS,
    CLAUDE_FILES,
    LOCAL_FILE,
    OPENCODE_DIRS,
    OPENCODE_FILES,
    SKIPPED_FILE,
    JsonObject,
    _dump_json,
)
from agenteval.dev.userconfig.flags import (
    _text,
    definition_flags,
    memory_flags,
    settings_flags,
    text_flags,
)
from agenteval.dev.userconfig.merge import (
    _opencode_protected,
    _protect,
    _same,
    merge_json,
    settings_protected,
)
from agenteval.dev.userconfig.reports import _all_strings, _display
from agenteval.dev.userconfig.returned import _allowed, _valid_rel
from agenteval.dev.userconfig.sanitizing import (
    _parse_jsonc,
    _strip_secrets,
    sanitize_claude_settings,
    sanitize_opencode,
)


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if root.is_dir():
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath, name)
                if path.is_file() and not path.is_symlink():
                    found[path.relative_to(root).as_posix()] = path
    return found


def _group(logical: str) -> Group:
    area = logical.split("/", 1)[0]
    return "memory" if area == "memory" else "local" if area == "local" else "config"


def _item_id(logical: str) -> str:
    area, _, rest = logical.partition("/")
    if area == "memory":
        return f"memory:{rest}"
    if area == "local":
        return f"local:{rest}"
    return f"config:{logical}"


def _host_target(logical: str, state: ConfigState) -> Path | None:
    area, _, rest = logical.partition("/")
    if not _valid_rel(rest):
        return None
    if area == "claude" and state.agent == "claude_code":
        return Path(state.host_dir, rest) if _allowed(rest, CLAUDE_FILES, CLAUDE_DIRS) else None
    if area == "opencode" and state.agent == "opencode":
        allowed = _allowed(rest, OPENCODE_FILES, OPENCODE_DIRS)
        return Path(state.host_dir, rest) if allowed else None
    if area == "memory":
        if not (state.host_key and state.vm_key):
            return None
        return Path(state.host_dir, "projects", state.host_key, "memory", rest)
    if area == "local" and rest == LOCAL_FILE and state.local_file:
        return Path(state.local_file)
    return None


def _read(path: Path | None) -> bytes | None:
    try:
        return path.read_bytes() if path is not None and path.is_file() else None
    except OSError:
        return None


def _is_binary(*sides: bytes | None) -> bool:
    return any(side is not None and b"\0" in side[:8000] for side in sides)


def _write_atomic(target: Path, data: bytes, executable: bool) -> None:
    """Write through an existing symlink to its real path, creating parent dirs."""
    real = Path(os.path.realpath(target))
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = real.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o755 if executable else 0o644
    # Short prefix: a 255-byte name plus the temp suffix would be too long.
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name[:32]}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _writer(target: Path, executable: bool) -> Callable[[bytes | None], None]:
    def apply(content: bytes | None) -> None:
        if content is None:
            if target.is_symlink() or target.exists():
                target.unlink()
        else:
            _write_atomic(target, content, executable)

    return apply


def merge_text(ours: bytes, base: bytes, theirs: bytes) -> tuple[bytes, bool] | None:
    """``git merge-file -p`` (labels host / session start / env): (merged, conflicted).
    None when git cannot merge (binary content)."""
    with tempfile.TemporaryDirectory(prefix="agenteval-merge-") as tmp:
        paths = [Path(tmp, name) for name in ("host", "base", "env")]
        for path, data in zip(paths, (ours, base, theirs), strict=True):
            path.write_bytes(data)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "-L", "host", "-L", "session start", "-L", "env"]
            + [str(p) for p in paths],
            capture_output=True,
            cwd=tmp,
            check=False,
        )
    if proc.returncode < 0 or proc.returncode > 127:
        return None
    return proc.stdout, proc.returncode > 0


@dataclass
class _Change:
    """One returned file and the host file it targets."""

    logical: str
    target: Path
    base: bytes | None
    theirs: bytes | None
    ours: bytes | None
    executable: bool
    home: Path

    def item(
        self,
        proposed: bytes | None,
        *,
        conflict: bool = False,
        host_changed: bool = False,
        binary: bool = False,
        flags: Sequence[str] = (),
        note: str | None = None,
    ) -> ReviewItem | None:
        if proposed == self.ours:
            return None
        kind: ItemKind = (
            "added" if self.ours is None else "deleted" if proposed is None else "modified"
        )
        label = _display(self.target, self.home)
        if self.logical.startswith("memory/"):
            # the project key makes the full path long; show where it goes as a note
            where = f"writes {label}"
            note = f"{note}\n{where}" if note else where
            label = self.logical
        return ReviewItem(
            id=_item_id(self.logical),
            group=_group(self.logical),
            label=label,
            kind=kind,
            current=self.ours,
            proposed=proposed,
            apply=_writer(self.target, self.executable),
            binary=binary,
            conflict=conflict,
            host_changed=host_changed,
            flags=list(flags),
            note=note,
        )


def _text_item(change: _Change, present: Collection[str]) -> ReviewItem | None:
    base, theirs, ours = change.base, change.theirs, change.ours
    binary = _is_binary(base, theirs, ours)
    host_changed = ours != base
    note: str | None = None
    if theirs is None:
        if ours is None:
            return None
        if host_changed:
            note = "deleted in the env, but the host file changed since the session started"
        return change.item(None, conflict=host_changed, host_changed=host_changed, note=note)

    proposed: bytes | None = theirs
    conflict = False
    if ours is not None and ours != base:
        merged = None if binary else merge_text(ours, base or b"", theirs)
        if merged is None:
            conflict = True
            note = "changed on the host too and cannot be merged: applying replaces the host file"
        else:
            proposed, conflict = merged
            note = (
                "conflict markers: edit before applying" if conflict else "merged with host changes"
            )
    elif ours is None and base is not None:
        conflict = True
        note = "deleted on the host since the session started"

    flags: list[str] = []
    if not binary:
        if change.logical.startswith("opencode/opencode.json"):
            flags += _opencode_settings_flags(ours, proposed)
        else:
            flags += text_flags(ours, proposed)
        if change.logical.startswith("memory/"):
            flags += memory_flags(ours, proposed, rel=change.logical[7:], present=present)
    flags += definition_flags(change.logical, ours, proposed, executable=change.executable)
    return change.item(
        proposed,
        conflict=conflict,
        host_changed=host_changed,
        binary=binary,
        flags=flags,
        note=note,
    )


def _json_or_none(data: bytes | None) -> JsonObject | None:
    if data is None:
        return None
    try:
        value, _ = _parse_jsonc(data.decode("utf-8"))
    except ValueError:
        return None
    return value


def _opencode_flags(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[str]:
    """``settings_flags`` plus newly added ``{file:...}`` references (read on the host)."""
    flags = settings_flags(before, after, agent="opencode")
    old = {m for s in _all_strings(before) for m in _ANY_FILE_REFERENCE.findall(s)}
    new = sorted({m for s in _all_strings(after) for m in _ANY_FILE_REFERENCE.findall(s)} - old)
    if new:
        flags.append("adds {file:} references: " + ", ".join(new))
    return flags


def _opencode_settings_flags(before: bytes | None, after: bytes | None) -> list[str]:
    old, new = _json_or_none(before), _json_or_none(after)
    if after is not None and new is None:
        return ["invalid JSON", *text_flags(before, after)]
    return _opencode_flags(old, new)


def _json_item(change: _Change, *, opencode: bool) -> ReviewItem | None:
    """settings.json (always) and OpenCode configs whose host file holds literal keys.

    A settings.json deleted in the env merges as ``{}``: keys the env never saw stay.
    """
    theirs = {} if change.theirs is None else _json_or_none(change.theirs)
    if theirs is None:
        return change.item(
            change.theirs,
            conflict=True,
            flags=["invalid JSON"],
            note="the env left this file as invalid JSON: applying replaces the host file "
            "as is, dropping its host-only keys (credential helpers, secrets); [e] to fix it",
        )
    base = _json_or_none(change.base) or {}
    ours = _json_or_none(change.ours)
    protected = (_opencode_protected if opencode else settings_protected)(base, theirs)

    def flags(before: JsonObject | None, after: JsonObject) -> list[str]:
        return _opencode_flags(before, after) if opencode else settings_flags(before, after)

    if change.ours is not None and ours is None:
        merged = _protect(base, theirs, protected)
        return change.item(
            _dump_json(merged),
            conflict=True,
            host_changed=True,
            flags=flags(None, merged),
            note="the host file is not valid JSON: applying replaces it",
        )
    merged, conflicts = merge_json(base, ours or {}, theirs, protected=protected)
    if (ours is None and not merged) or (ours is not None and _same(merged, ours)):
        return None
    if ours is None:
        clean: JsonObject = {}
    elif opencode:
        clean = copy.deepcopy(ours)
        _strip_secrets(clean)
    else:
        clean = sanitize_claude_settings(ours)[0]
    notes: list[str] = []
    if change.theirs is None:
        notes.append("deleted in the env: only keys the env never saw are kept")
    if opencode:
        notes.append("merged as JSON (comments dropped): the host file holds literal credentials")
    if conflicts:
        notes.append("kept the host value for: " + ", ".join(conflicts))
    return change.item(
        _dump_json(merged),
        conflict=bool(conflicts),
        host_changed=not _same(clean, base),
        flags=flags(ours, merged),
        note="; ".join(notes) or None,
    )


def _read_skipped(config_dir: Path) -> set[str]:
    try:
        data = json.loads((config_dir / SKIPPED_FILE).read_text())
    except (OSError, ValueError):
        return set()
    return {str(name) for name in data} if isinstance(data, list) else set()


def config_items(session: DevSession, session_dir: Path) -> list[ReviewItem]:
    """One review item per returned config/memory file that differs from what was
    copied in, merged 3-way against the host file as it is now."""
    state = session.config
    config_dir = session_dir / "config"
    vm_dir = config_dir / "vm"
    if state is None or not vm_dir.is_dir():
        return []
    base_files, vm_files = _files(config_dir / "base"), _files(vm_dir)
    skipped = _read_skipped(config_dir)
    present = {name[len("memory/") :] for name in vm_files if name.startswith("memory/")}
    home = Path.home()
    items: list[ReviewItem] = []
    for logical in sorted(set(base_files) | set(vm_files)):
        target = _host_target(logical, state)
        if target is None or logical in skipped:
            continue
        base, theirs = _read(base_files.get(logical)), _read(vm_files.get(logical))
        if base == theirs:
            continue
        theirs_path = vm_files.get(logical)
        # Only skill scripts may come back executable (and they are flagged).
        executable = logical.startswith("claude/skills/") and bool(
            theirs_path and theirs_path.stat().st_mode & 0o111
        )
        change = _Change(
            logical=logical,
            target=target,
            base=base,
            theirs=theirs,
            ours=_read(target),
            executable=executable,
            home=home,
        )
        item: ReviewItem | None
        if logical == "claude/settings.json":
            item = _json_item(change, opencode=False)
        elif (
            theirs is not None
            and logical in ("opencode/opencode.json", "opencode/opencode.jsonc")
            and change.ours is not None
            and _has_literal_keys(change.ours)
        ):
            item = _json_item(change, opencode=True)
        else:
            item = _text_item(change, present)
        if item is not None:
            items.append(item)
    items.sort(key=lambda i: (GROUP_ORDER.index(i.group), i.id))
    return items


def _has_literal_keys(text: bytes) -> bool:
    try:
        return bool(sanitize_opencode(text.decode("utf-8"))[1])
    except ValueError:
        return False


def config_patch(session_dir: Path) -> str:
    """Readable diff of the returned config against what was copied in."""
    config_dir = session_dir / "config"
    base_files, vm_files = _files(config_dir / "base"), _files(config_dir / "vm")
    chunks: list[str] = []
    for logical in sorted(set(base_files) | set(vm_files)):
        old, new = _read(base_files.get(logical)), _read(vm_files.get(logical))
        if old == new:
            continue
        if _is_binary(old, new):
            chunks.append(f"Binary files a/{logical} and b/{logical} differ\n")
            continue
        diff = difflib.unified_diff(
            _text(old).splitlines(keepends=True),
            _text(new).splitlines(keepends=True),
            f"a/{logical}" if old is not None else "/dev/null",
            f"b/{logical}" if new is not None else "/dev/null",
        )
        chunks.extend(line if line.endswith("\n") else line + "\n" for line in diff)
    return "".join(chunks)
