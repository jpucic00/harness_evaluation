"""Review items for the session's file changes: 3-way file writes and path checks."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from agenteval.dev.repo.commits import _commit_items
from agenteval.dev.repo.common import (
    _ABSENT,
    _EXEC,
    _FILE,
    _GITLINK,
    _SYMLINK,
    CONFLICTS_DIR,
    RepoError,
    UnsafePathError,
    _Change,
    _change_blobs,
    _check_components,
    _diff_tree,
    _listing,
    _Repo,
    _show,
)
from agenteval.dev.repo.export_import import _session_repo
from agenteval.dev.repo.host_paths import repo_path_flags
from agenteval.dev.review import ItemKind, ReviewItem
from agenteval.dev.session import DevSession


@dataclass(frozen=True)
class _Blob:
    """File content with its git mode (a symlink's content is its target)."""

    mode: str
    data: bytes

    @property
    def binary(self) -> bool:
        return self.mode != _SYMLINK and b"\0" in self.data[:8000]

    @property
    def is_file(self) -> bool:
        return self.mode in (_FILE, _EXEC)


_NOT_A_FILE = _Blob("040000", b"")
"""Host state of a directory (or other non-file) where the session has a file."""


def _blob(mode: str, sha: str, blobs: Mapping[str, bytes]) -> _Blob | None:
    return None if mode == _ABSENT else _Blob(mode, blobs[sha])


def _read_host(target: Path) -> _Blob | None:
    try:
        st = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(st.st_mode):
        return _Blob(_SYMLINK, os.fsencode(os.readlink(target)))
    if stat.S_ISREG(st.st_mode):
        return _Blob(_EXEC if st.st_mode & stat.S_IXUSR else _FILE, target.read_bytes())
    return _NOT_A_FILE


def repo_items(session: DevSession, session_dir: Path) -> tuple[list[ReviewItem], list[str]]:
    """Review items for the session's repo changes, plus notes on what cannot be applied.

    Computed against the host as it is now, so it can run again for a deferred review.
    """
    result = session.result
    if result is None:
        raise RepoError(f"session {session.id} has no export to review")
    session_dir = session_dir.resolve()
    repo = _session_repo(session)
    items: list[ReviewItem] = []
    notes: list[str] = []
    _commit_items(repo, session, items, notes)
    changes = _diff_tree(repo, session.repo.base_tree, result.final_tree)
    blobs = _change_blobs(repo, changes)
    for change in changes:
        item = _file_item(repo, session_dir, change, blobs, notes)
        if item is not None:
            items.append(item)
    if result.excluded:
        notes.append(
            "Not brought back (untracked and over the export size limit, or nested repos): "
            f"{_listing([_show(p) for p in result.excluded])}."
        )
    return items, notes


def _file_item(
    repo: _Repo,
    session_dir: Path,
    change: _Change,
    blobs: Mapping[str, bytes],
    notes: list[str],
) -> ReviewItem | None:
    path = change.path
    try:
        target = validate_path(repo.root, path)
    except UnsafePathError as ex:
        notes.append(f"Not applied: {ex}.")
        return None
    if _GITLINK in (change.old_mode, change.new_mode):
        notes.append(f"Not applied: {path} is a nested repo in the session (not exported).")
        return None
    base = _blob(change.old_mode, change.old_sha, blobs)
    theirs = _blob(change.new_mode, change.new_sha, blobs)
    host = _read_host(target)
    if host == theirs:
        return None
    current = host.data if host is not None and host is not _NOT_A_FILE else None
    flags = repo_path_flags(
        path,
        change.old_mode,
        change.new_mode,
        base.data if base else None,
        theirs.data if theirs else None,
    )
    item_id = f"repo:{path}"
    if host == base:
        return ReviewItem(
            id=item_id,
            group="repo",
            label=path,
            kind=_kind(base, theirs),
            current=current,
            proposed=theirs.data if theirs else None,
            apply=_writer(repo.root, path, theirs.mode if theirs else _FILE, host),
            binary=any(b is not None and b.binary for b in (base, theirs)),
            editable=theirs is not None and theirs.is_file and not theirs.binary,
            flags=flags,
            note=_symlink_note(theirs),
        )
    if (
        host is not None
        and base is not None
        and theirs is not None
        and all(b.is_file and not b.binary for b in (host, base, theirs))
    ):
        merged, conflicted = _merge_file(repo, session_dir, host.data, base.data, theirs.data)
        mode = theirs.mode if host.mode == base.mode else host.mode
        if merged == host.data and mode == host.mode:
            return None
        return ReviewItem(
            id=item_id,
            group="repo",
            label=path,
            kind="modified",
            current=host.data,
            proposed=merged,
            apply=_writer(repo.root, path, mode, host),
            conflict=conflicted,
            host_changed=True,
            flags=flags,
            note="Changed on the host since the session started: merged 3-way"
            + (" with conflict markers." if conflicted else "."),
        )
    if theirs is None:
        notes.append(
            f"{path}: deleted in the session, but changed on the host since it started; "
            "the host file is kept."
        )
        return None
    if host is None:
        reason = "deleted on the host since the session started"
    elif host is _NOT_A_FILE:
        reason = "the host has a directory or special file there"
    else:
        reason = "changed on the host too, and it cannot be merged as text"
    conflicts = session_dir / CONFLICTS_DIR
    return ReviewItem(
        id=item_id,
        group="repo",
        label=path,
        kind=_kind(base, theirs),
        current=current,
        proposed=theirs.data,
        apply=_conflict_writer(conflicts, path),
        binary=theirs.binary or (host is not None and host.binary),
        conflict=True,
        host_changed=True,
        editable=False,
        flags=flags,
        note=f"Conflict ({reason}): the host file is left alone; applying writes the "
        f"session's version to {conflicts / path}.",
    )


def _kind(base: _Blob | None, theirs: _Blob | None) -> ItemKind:
    if base is None:
        return "added"
    if theirs is None:
        return "deleted"
    if base.data == theirs.data:
        return "mode"
    return "modified"


def _symlink_note(blob: _Blob | None) -> str | None:
    if blob is None or blob.mode != _SYMLINK:
        return None
    return f"Symlink to {os.fsdecode(blob.data)!r}."


def _merge_file(
    repo: _Repo, session_dir: Path, ours: bytes, base: bytes, theirs: bytes
) -> tuple[bytes, bool]:
    with tempfile.TemporaryDirectory(dir=session_dir, prefix=".merge-") as tmp:
        files = []
        for name, data in (("host", ours), ("base", base), ("session", theirs)):
            path = Path(tmp) / name
            path.write_bytes(data)
            files.append(str(path))
        proc = repo.run(
            "merge-file",
            "-p",
            "-L",
            "host",
            "-L",
            "session start",
            "-L",
            "session",
            *files,
            check=False,
        )
    if not 0 <= proc.returncode <= 127:
        raise RepoError(f"git merge-file failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout, proc.returncode > 0


def _writer(
    root: Path, path: str, mode: str, expected: _Blob | None
) -> Callable[[bytes | None], None]:
    """Apply for one host file: ``None`` deletes it; refuses if the host file changed since."""

    def apply(content: bytes | None) -> None:
        target = validate_path(root, path)
        if _read_host(target) != expected:
            raise RepoError(f"{path} changed on the host during the review; left alone")
        try:
            if content is None:
                _remove(root, target)
            else:
                _put(target, _Blob(mode, content))
        except OSError as ex:
            raise RepoError(f"cannot write {path}: {ex}") from ex

    return apply


def _conflict_writer(conflicts: Path, path: str) -> Callable[[bytes | None], None]:
    def apply(content: bytes | None) -> None:
        if content is None:
            return
        target = validate_path(conflicts, path)
        try:
            _put(target, _Blob(_FILE, content))
        except OSError as ex:
            raise RepoError(f"cannot write {target}: {ex}") from ex

    return apply


def _put(target: Path, blob: _Blob) -> None:
    """Write ``blob`` to ``target`` atomically (a temp file next to it, then rename).

    Like a git checkout, a file gets 0666 (0777 if executable) less the user's umask.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".agenteval-{secrets.token_hex(6)}.tmp"
    try:
        if blob.mode == _SYMLINK:
            os.symlink(os.fsdecode(blob.data), tmp)
        else:
            perm = 0o777 if blob.mode == _EXEC else 0o666
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, perm)
            with os.fdopen(fd, "wb") as f:
                f.write(blob.data)
        os.replace(tmp, target)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _remove(root: Path, target: Path) -> None:
    """Delete ``target`` and the directories it leaves empty (up to ``root``)."""
    if os.path.lexists(target):
        os.unlink(target)
    parent = target.parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def validate_path(root: Path, path: str) -> Path:
    """Host location of the repo path ``path``; ``UnsafePathError`` if it could escape ``root``.

    Rejects empty, absolute, ``.``/``..`` and ``.git`` components (case-folded, with the
    code points HFS+ ignores), control and bidi characters, and symlinked parents.
    """
    _check_components(path)
    parts = path.split("/")
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"{path!r}: {current.relative_to(root)} is a symlink")
    target = root.joinpath(*parts)
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(target.parent)]) != real_root:
        raise UnsafePathError(f"{path!r} resolves outside {root}")
    return target
