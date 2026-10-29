"""Commits: notes on the session's commits, the guarded fast-forward and ref cleanup."""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess

from agenteval.dev.repo.common import (
    RepoError,
    _change_blobs,
    _diff_tree,
    _index_path,
    _lines,
    _listing,
    _path_ok,
    _Repo,
    _show,
    _unprintable,
)
from agenteval.dev.repo.discovery import _branch, _lfs_filtered, _prefix, _under
from agenteval.dev.repo.export_import import _session_repo
from agenteval.dev.repo.host_paths import repo_path_flags
from agenteval.dev.review import ReviewItem
from agenteval.dev.session import DevSession, RepoState
from agenteval.guest.dev.dev_repo import nul_split


def _refs(repo: _Repo, prefix: str) -> dict[str, str]:
    listing = repo.run("for-each-ref", "--format=%(refname)%00%(objectname)", prefix).stdout
    refs = {}
    for line in listing.splitlines():
        name, _, sha = line.partition(b"\0")
        refs[os.fsdecode(name).removeprefix(prefix)] = sha.decode()
    return refs


def _is_ancestor(repo: _Repo, older: str, newer: str) -> bool:
    return repo.run("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _commit_items(
    repo: _Repo, session: DevSession, items: list[ReviewItem], notes: list[str]
) -> None:
    state = session.repo
    result = session.result
    assert result is not None
    prefix = _prefix(session.id)
    fetched = _refs(repo, f"{prefix}/heads/")
    vm_head = repo.rev(f"{prefix}/vm-head")
    if not state.git:
        if fetched or vm_head:
            notes.append(
                "The session made commits, but the directory is not a git repo: "
                "only the file changes can be applied."
            )
        return
    git_c = f"git -C {shlex.quote(state.root)}"
    for name, sha in sorted(fetched.items()):
        if name != state.branch:
            ref = shlex.quote(f"{prefix}/heads/{name}")
            notes.append(
                f"The session created or moved branch {name} ({sha[:12]}), kept as {ref}. "
                f"To create it on the host: {git_c} branch {shlex.quote(name)} {ref}"
            )
    if vm_head:
        notes.append(
            f"The session left a detached HEAD at {vm_head[:12]}, kept as {prefix}/vm-head "
            f"(see {git_c} log {prefix}/vm-head)."
        )
    branch = state.branch
    if branch is None:
        return
    new = fetched.get(branch)
    if new is None:
        moved = result.branches.get(branch)
        if moved is not None and moved != state.head:
            notes.append(
                f"The session moved {branch} back to {moved[:12]} (a reset?); "
                "the host branch is not changed."
            )
        return
    tip = repo.rev(f"refs/heads/{branch}")
    if new == state.head or (tip is not None and _is_ancestor(repo, new, tip)):
        return
    reason = _ff_refusal(repo, state, new)
    if reason is not None:
        ref = shlex.quote(f"{prefix}/heads/{branch}")
        notes.append(
            f"The session committed on {branch}, but the commits cannot be fast-forwarded: "
            f"{reason}. They are kept in {ref}; to merge them: {git_c} merge {ref}"
        )
        return
    items.append(_ff_item(repo, session, branch, new))


def _ff_refusal(repo: _Repo, state: RepoState, new: str) -> str | None:
    branch = state.branch
    if _branch(repo) != branch:
        return f"HEAD is no longer on {branch}"
    if repo.rev(f"refs/heads/{branch}^{{commit}}") != state.head:
        return f"{branch} moved on the host since the session started"
    if state.head is not None and not _is_ancestor(repo, state.head, new):
        return "the session rewrote the branch history"
    span = [f"{state.head}..{new}"] if state.head else [new]
    touched = set(
        nul_split(
            repo.run(
                "log",
                "--no-show-signature",
                "--format=",
                "--name-only",
                "-z",
                "-m",
                "--root",  # list a root commit's files even with log.showRoot=false
                "--no-renames",
                *span,
            ).stdout
        )
    )
    # new files can be LFS files too (an env with git-lfs commits pointers whose content
    # never reaches the host)
    safe = sorted(p for p in touched if _path_ok(p))
    lfs = sorted(touched & set(state.lfs_paths) | set(_lfs_filtered(repo, safe)))
    if lfs:
        return f"they change Git LFS files ({_listing(lfs)})"
    flattened = sorted(p for p in touched if _under(p, state.flattened))
    if flattened:
        return f"they change flattened submodule files ({_listing(flattened)})"
    unsafe = sorted(_show(p) for p in touched if not _path_ok(p))
    if unsafe:
        return f"they contain unsafe paths ({_listing(unsafe)})"
    index = _index_path(repo)
    trial = index.with_name(f"{index.name}.agenteval-{secrets.token_hex(4)}")
    try:
        proc = _merge_index(repo, state, new, f"--index-output={trial}")
    finally:
        trial.unlink(missing_ok=True)
    if proc.returncode != 0 and b"index.lock" in proc.stderr:
        return "another git process is using the index (try again once it is done)"
    if proc.returncode != 0:
        return "the index has staged changes that conflict with them"
    return None


def _merge_index(
    repo: _Repo, state: RepoState, new: str, *options: str
) -> subprocess.CompletedProcess[bytes]:
    """Two-tree ``read-tree -m -i H B'``: the index only (``-i``: the worktree is not read)."""
    return repo.run(
        "read-tree", "-m", "-i", *options, state.head or repo.empty_tree, new, check=False
    )


def _ff_item(repo: _Repo, session: DevSession, branch: str, new: str) -> ReviewItem:
    state = session.repo
    span = f"{state.head}..{new}" if state.head else new
    raw_log = repo.run("log", "--no-show-signature", "--no-color", "--format=%h %s", span).stdout
    log = "".join(
        c if c == "\n" or not _unprintable(c) else "?" for c in raw_log.decode(errors="replace")
    )
    changes = _diff_tree(
        repo, f"{state.head}^{{tree}}" if state.head else repo.empty_tree, f"{new}^{{tree}}"
    )
    blobs = _change_blobs(repo, changes)
    flags = [
        f"{_show(c.path)}: {flag}"
        for c in changes
        for flag in repo_path_flags(
            c.path, c.old_mode, c.new_mode, blobs.get(c.old_sha), blobs.get(c.new_sha)
        )
    ]
    count = len(log.splitlines())
    start = state.head[:12] if state.head else "(no commits yet)"

    def apply(_content: bytes | None) -> None:
        _fast_forward(repo, session, new)

    return ReviewItem(
        id=f"commits:{branch}",
        group="commits",
        label=f"{branch}: fast-forward by {count} commit(s) from the session",
        kind="ff",
        current=f"{branch} at {start}\n".encode(),
        proposed=log.encode(),
        apply=apply,
        editable=False,
        flags=flags,
        note=f"Moves {branch} to {new[:12]} and updates the index to match; "
        "the working tree files are separate items.",
    )


def _fast_forward(repo: _Repo, session: DevSession, new: str) -> None:
    """``read-tree -m -i H B'`` (index only), then a compare-and-swap of the branch ref."""
    state = session.repo
    reason = _ff_refusal(repo, state, new)
    if reason is not None:
        raise RepoError(f"cannot fast-forward {state.branch}: {reason}")
    index = _index_path(repo)
    backup = index.with_name(f"{index.name}.agenteval-{secrets.token_hex(4)}")
    had_index = index.is_file()
    if had_index:
        shutil.copy2(index, backup)
    try:
        proc = _merge_index(repo, state, new)
        if proc.returncode != 0:
            raise RepoError(
                "the index has staged changes that conflict with the session's commits: "
                + proc.stderr.decode(errors="replace").strip()
            )
        old = state.head or "0" * len(new)
        proc = repo.run(
            "update-ref",
            "-m",
            f"agenteval dev: fast-forward from session {session.id}",
            f"refs/heads/{state.branch}",
            new,
            old,
            check=False,
        )
        if proc.returncode != 0:
            if had_index:
                os.replace(backup, index)
            else:
                index.unlink(missing_ok=True)
            raise RepoError(
                f"{state.branch} changed while it was fast-forwarded; nothing was changed"
            )
    finally:
        backup.unlink(missing_ok=True)


def cleanup_refs(session: DevSession, *, keep_heads: bool = True) -> None:
    """Delete the session's internal refs, and the agent's commits unless ``keep_heads``.

    The agent's commits are its fetched branches (``heads/*``) and, when the session ended
    on a detached HEAD, ``vm-head``.
    """
    try:
        repo = _session_repo(session)
    except RepoError:
        return
    prefix = f"{_prefix(session.id)}/"
    doomed = [
        prefix + name
        for name in _refs(repo, prefix)
        if not (keep_heads and (name.startswith("heads/") or name == "vm-head"))
    ]
    if doomed:
        repo.run("update-ref", "--stdin", stdin=_lines(f"delete {ref}" for ref in doomed))
