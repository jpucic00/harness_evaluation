"""Discovery of the host repo and the snapshot copied into the env."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import stat
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agenteval.dev.repo.common import (
    _ABSENT,
    _EXEC,
    _FILE,
    _GITLINK,
    _NOT_SECRET_SUFFIXES,
    _SYMLINK,
    BUNDLE_NAME,
    CONFIRM_BYTES,
    META_NAME,
    SECRET_PATTERNS,
    SHADOW_DIR,
    SHADOW_EXCLUDES,
    RepoError,
    _diff_tree,
    _index_path,
    _lines,
    _listing,
    _Repo,
    raw_attrs_supported,
)
from agenteval.dev.session import RepoState, validate_session_id
from agenteval.guest.dev import dev_repo
from agenteval.guest.dev.dev_repo import nul_join, nul_split


@dataclass(frozen=True)
class RepoInfo:
    """Where ``agenteval dev`` was started."""

    root: Path
    """The git toplevel, or the directory itself when it is not in a repo."""
    cwd_rel: str
    git: bool
    git_dir: Path | None = None
    common_dir: Path | None = None
    """Differs from ``git_dir`` in a linked worktree (``git worktree add``)."""
    linked_worktree: bool = False


def discover(cwd: Path) -> RepoInfo:
    """The repo containing ``cwd``, or ``cwd`` itself when it is not in one."""
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise RepoError(f"{cwd} is not a directory")
    probe = _Repo(cwd)
    inside = probe.run("rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0:
        if b"not a git repository" in inside.stderr:
            return RepoInfo(root=cwd, cwd_rel="", git=False)
        raise RepoError(f"git cannot use {cwd}: {inside.stderr.decode(errors='replace').strip()}")
    if inside.stdout.strip() != b"true":
        raise RepoError(f"{cwd} is inside a git directory, not a working tree")
    listing = probe.run(
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        "--show-prefix",
    ).stdout
    top, git_dir, common, prefix = [*os.fsdecode(listing).split("\n"), ""][:4]
    return RepoInfo(
        root=Path(top).resolve(),
        cwd_rel=prefix.rstrip("/"),
        git=True,
        git_dir=Path(git_dir),
        common_dir=Path(common),
        linked_worktree=Path(git_dir).resolve() != Path(common).resolve(),
    )


@dataclass
class Snapshot:
    """What ``snapshot`` hands to the env, and the host state it recorded."""

    state: RepoState
    bundle: Path
    meta: Path
    warnings: list[str] = field(default_factory=list)


def snapshot(
    info: RepoInfo,
    session_dir: Path,
    session_id: str,
    includes: Sequence[str],
    *,
    confirm: Callable[[str], bool] | None = None,
) -> Snapshot:
    """Commit the working tree and the index, and bundle them for the env.

    ``includes`` are globs (relative to the repo root) of ignored files to copy anyway.
    ``confirm`` is asked when more than ``CONFIRM_BYTES`` of file content that is not in
    HEAD would be copied; a False answer raises ``RepoError`` (None: go ahead).
    """
    session_dir = session_dir.resolve()
    prefix = _prefix(session_id)
    for pattern in includes:
        if not pattern or "\0" in pattern:
            raise RepoError(f"bad --include pattern {pattern!r}")
    warnings: list[str] = []
    shadow: Path | None = None
    if info.git:
        repo = _Repo(info.root)
        warnings += _in_progress(info)
        head = repo.rev("HEAD^{commit}")
        branch = _branch(repo)
    else:
        shadow = session_dir / SHADOW_DIR
        repo = _init_shadow(info.root, shadow)
        head = branch = None
    index_tree = _index_tree(repo, session_dir, head, warnings) if info.git else None
    tree, flattened = _work_tree(repo, session_dir, head, index_tree, includes, warnings)
    warnings += _size_check(repo, head, tree, confirm)
    secret = [p for p in _tree_paths(repo, tree) if _looks_secret(p)]
    if secret:
        warnings.append(
            f"Secret-looking files are copied into the env: {_listing(secret)}. "
            "Ignore them (.gitignore or .git/info/exclude) to keep them out."
        )

    parents = ["-p", head] if head else []
    base = repo.out("commit-tree", tree, *parents, "-m", f"agenteval dev snapshot {session_id}")
    repo.run("update-ref", f"{prefix}/base", base)
    bundle_refs = [f"{prefix}/base"]
    index_commit = head_ref = None
    if index_tree is not None:
        index_commit = repo.out(
            "commit-tree", index_tree, *parents, "-m", f"agenteval dev index {session_id}"
        )
        repo.run("update-ref", f"{prefix}/index", index_commit)
        bundle_refs.append(f"{prefix}/index")
        if head and branch:
            head_ref = f"refs/heads/{branch}"
        elif head:
            head_ref = f"{prefix}/head"
            repo.run("update-ref", head_ref, head)
        if head_ref:
            bundle_refs.append(head_ref)
    lfs = _lfs_paths(repo, tree)
    bundle = session_dir / BUNDLE_NAME
    bundle.unlink(missing_ok=True)
    repo.run("bundle", "create", "-q", str(bundle), *bundle_refs)

    meta = {
        "version": dev_repo.META_VERSION,
        "session_id": session_id,
        "git": info.git,
        "branch": branch,
        "head": head,
        "object_format": repo.out("rev-parse", "--show-object-format"),
        "raw_attrs": raw_attrs_supported(),
        "refs": {
            "base": f"{prefix}/base",
            "index": f"{prefix}/index" if index_commit else None,
            "head": head_ref,
        },
        "base_commit": base,
        "base_tree": tree,
        "lfs_paths": lfs,
        "shallow": _shallow_commits(repo),
        "exclude": _host_excludes(repo),
    }
    meta_path = session_dir / META_NAME
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    state = RepoState(
        root=str(info.root),
        cwd_rel=info.cwd_rel,
        git=info.git,
        shadow_git_dir=str(shadow) if shadow else None,
        head=head,
        branch=branch,
        base_commit=base,
        base_tree=tree,
        index_commit=index_commit,
        flattened=flattened,
        lfs_paths=lfs,
        includes=list(includes),
    )
    return Snapshot(state=state, bundle=bundle, meta=meta_path, warnings=warnings)


def _prefix(session_id: str) -> str:
    """The session's ref namespace; ``..`` and a ``.lock`` end, valid in ids, are not in refs."""
    name = validate_session_id(session_id).replace("..", "._")
    return f"refs/agenteval/{name}_" if name.endswith(".lock") else f"refs/agenteval/{name}"


def _in_progress(info: RepoInfo) -> list[str]:
    git_dir = info.git_dir or info.root / ".git"
    found = {
        label
        for name, label in (
            ("MERGE_HEAD", "A merge"),
            ("rebase-merge", "A rebase"),
            ("rebase-apply", "A rebase or git am"),
            ("CHERRY_PICK_HEAD", "A cherry-pick"),
            ("REVERT_HEAD", "A revert"),
        )
        if (git_dir / name).exists()
    }
    return [
        f"{label} is in progress: the env gets the files and the index as they are, "
        "but not the operation itself."
        for label in sorted(found)
    ]


def _branch(repo: _Repo) -> str | None:
    proc = repo.run("symbolic-ref", "-q", "HEAD", check=False)
    ref = os.fsdecode(proc.stdout).strip()
    return ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None


def _init_shadow(root: Path, shadow: Path) -> _Repo:
    if not (shadow / "HEAD").exists():
        _Repo(shadow.parent).run("init", "-q", "--bare", str(shadow))
        (shadow / "info").mkdir(exist_ok=True)
        (shadow / "info" / "exclude").write_text("\n".join(SHADOW_EXCLUDES) + "\n")
    repo = _Repo(root, shadow)
    repo.run("config", "core.bare", "false")
    return repo


def _index_tree(repo: _Repo, session_dir: Path, head: str | None, warnings: list[str]) -> str:
    """Tree of the user's index, read from a copy so the index itself is never written."""
    source = _index_path(repo)
    copy = session_dir / "index.host"
    copy.unlink(missing_ok=True)
    try:
        if source.is_file():
            shutil.copyfile(source, copy)
        proc = repo.run("write-tree", index=copy, check=False)
    finally:
        copy.unlink(missing_ok=True)
    if proc.returncode == 0:
        return proc.stdout.decode().strip()
    warnings.append(
        "The index has unmerged entries: the env gets HEAD's tree as its index "
        "(the conflicted files themselves are copied as they are)."
    )
    return repo.out("rev-parse", f"{head}^{{tree}}") if head else repo.empty_tree


def _work_tree(
    repo: _Repo,
    session_dir: Path,
    head: str | None,
    index_tree: str | None,
    includes: Sequence[str],
    warnings: list[str],
) -> tuple[str, list[str]]:
    """Tree of the working tree as ``git add -A`` sees it, plus includes and nested repos."""
    index = session_dir / "index.snapshot"
    index.unlink(missing_ok=True)
    try:
        repo.run("read-tree", head or "--empty", index=index)
        untracked = nul_split(
            repo.run("ls-files", "-o", "--exclude-standard", "-z", index=index).stdout
        )
        nested = [p.rstrip("/") for p in untracked if p.endswith("/")]
        gitlinks = [p for p in _gitlinks(repo, index) if _is_repo_dir(repo.root / p)]
        flatten = sorted(set(nested + gitlinks))
        specs = [".", *(f":(exclude,literal){p}" for p in flatten)]
        repo.run(
            "add",
            "-A",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin=nul_join(specs),
            index=index,
            raw=True,
        )
        force = _staged_only(repo, head, index_tree, flatten)
        force += _included(repo, index, includes, warnings)
        if force:
            repo.run(
                "add",
                "-f",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                stdin=nul_join([f":(literal){p}" for p in dict.fromkeys(force)]),
                index=index,
                raw=True,
            )
        for path in flatten:
            _flatten(repo, index, path)
        tree = repo.out("write-tree", index=index)
    finally:
        index.unlink(missing_ok=True)
    return tree, flatten


def _gitlinks(repo: _Repo, index: Path) -> list[str]:
    found = []
    for entry in nul_split(repo.run("ls-files", "-s", "-z", index=index).stdout):
        info, _, path = entry.partition("\t")
        if info.startswith(_GITLINK + " "):
            found.append(path)
    return found


def _is_repo_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and os.path.lexists(path / ".git")


def _staged_only(
    repo: _Repo, head: str | None, index_tree: str | None, flatten: Sequence[str]
) -> list[str]:
    """Files staged on the host (``git add -f``) that ``add -A`` skips as ignored."""
    if index_tree is None:
        return []
    added = nul_split(
        repo.run(
            "diff-tree",
            "-r",
            "-z",
            "--name-only",
            "--no-renames",
            "--diff-filter=A",
            head or repo.empty_tree,
            index_tree,
        ).stdout
    )
    return [
        p
        for p in added
        if not _under(p, flatten)
        and os.path.lexists(repo.root / p)
        and not (repo.root / p).is_dir()
    ]


def _included(repo: _Repo, index: Path, includes: Sequence[str], warnings: list[str]) -> list[str]:
    found: list[str] = []
    for pattern in includes:
        listing = repo.run(
            "ls-files",
            "-o",
            "-i",
            "--exclude-standard",
            "-z",
            "--",
            f":(top,glob){pattern}",
            index=index,
        ).stdout
        files = [p for p in nul_split(listing) if not p.endswith("/")]
        if not files:
            warnings.append(f"--include {pattern!r} matched no ignored files.")
        found += files
    return found


def _under(path: str, dirs: Iterable[str]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def _flatten(repo: _Repo, index: Path, rel: str) -> None:
    """Replace the submodule or nested repo at ``rel`` by its files as plain blobs."""
    entries = list(_nested_files(repo.root, rel))
    repo.run("update-index", "--force-remove", "--", rel, index=index)
    shas = iter(_hash_files(repo, [full for _, mode, full in entries if mode != _SYMLINK]))
    lines = []
    for path, mode, full in entries:
        if mode == _SYMLINK:
            link = os.fsencode(os.readlink(full))
            sha = repo.out("hash-object", "-w", "--stdin", stdin=link)
        else:
            sha = next(shas)
        lines.append(f"{mode} {sha}\t{path}")
    if lines:
        repo.run("update-index", "-z", "--index-info", stdin=nul_join(lines), index=index)


def _nested_files(root: Path, rel: str) -> Iterator[tuple[str, str, Path]]:
    """``(path, mode, file)`` for what the nested repo at ``rel`` tracks or would add."""
    listing = _Repo(root / rel).run("ls-files", "-c", "-o", "--exclude-standard", "-z").stdout
    for name in dict.fromkeys(p.rstrip("/") for p in nul_split(listing)):
        full = root / rel / name
        if _is_repo_dir(full):
            yield from _nested_files(root, f"{rel}/{name}")
            continue
        try:
            st = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            yield f"{rel}/{name}", _SYMLINK, full
        elif stat.S_ISREG(st.st_mode):
            yield f"{rel}/{name}", _EXEC if st.st_mode & stat.S_IXUSR else _FILE, full


def _hash_files(repo: _Repo, files: Sequence[Path]) -> list[str]:
    if not files:
        return []
    if any("\n" in str(f) for f in files):
        return [
            repo.out("hash-object", "-w", "--no-filters", "--stdin", stdin=f.read_bytes())
            for f in files
        ]
    listing = b"".join(os.fsencode(f) + b"\n" for f in files)
    shas = repo.out("hash-object", "-w", "--no-filters", "--stdin-paths", stdin=listing).split()
    if len(shas) != len(files):
        raise RepoError("git hash-object returned an unexpected number of ids")
    return shas


def _size_check(
    repo: _Repo, head: str | None, tree: str, confirm: Callable[[str], bool] | None
) -> list[str]:
    new = {
        c.new_sha: c.path
        for c in _diff_tree(repo, f"{head}^{{tree}}" if head else repo.empty_tree, tree)
        if c.new_mode not in (_ABSENT, _GITLINK)
    }
    sizes = _object_sizes(repo, new)
    total = sum(sizes.values())
    if total <= CONFIRM_BYTES:
        return []
    largest = sorted(new, key=lambda sha: -sizes[sha])[:5]
    detail = ", ".join(f"{new[sha]} ({sizes[sha] / 2**20:.0f} MB)" for sha in largest)
    text = (
        f"The snapshot copies {total / 2**20:.0f} MB of files that are not in HEAD "
        f"into the env (largest: {detail})."
    )
    if confirm is None:
        return [text]
    if not confirm(f"{text} Continue?"):
        raise RepoError(
            "Snapshot cancelled. Ignore the large files (.gitignore or .git/info/exclude) "
            "and start the session again."
        )
    return []


def _object_sizes(repo: _Repo, shas: Iterable[str]) -> dict[str, int]:
    wanted = sorted(set(shas))
    if not wanted:
        return {}
    listing = repo.out(
        "cat-file", "--batch-check=%(objectname) %(objectsize)", stdin=_lines(wanted)
    )
    sizes = {}
    for line in listing.splitlines():
        sha, _, size = line.partition(" ")
        sizes[sha] = int(size) if size.isdigit() else 0
    return {sha: sizes.get(sha, 0) for sha in wanted}


def _tree_paths(repo: _Repo, tree: str) -> list[str]:
    return nul_split(repo.run("ls-tree", "-r", "-z", "--name-only", tree).stdout)


def _looks_secret(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    if name.endswith(_NOT_SECRET_SUFFIXES):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_PATTERNS)


def _host_excludes(repo: _Repo) -> str:
    """The global excludes file, then ``info/exclude``: host ignore rules the env lacks."""
    proc = repo.run("config", "--path", "--get", "core.excludesFile", check=False)
    configured = os.fsdecode(proc.stdout).strip()
    if configured:
        files = [Path(configured)]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        files = [Path(xdg) / "git" / "ignore"]
    exclude = repo.out("rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    files.append(Path(exclude))
    texts = []
    for path in files:
        try:
            texts.append(path.read_bytes().decode("utf-8", errors="replace"))
        except OSError:
            continue
    return "".join(text if text.endswith("\n") else text + "\n" for text in texts if text)


def _shallow_commits(repo: _Repo) -> list[str]:
    """Boundary commits of a shallow clone; the env repo gets the same boundary."""
    path = Path(repo.out("rev-parse", "--path-format=absolute", "--git-path", "shallow"))
    return path.read_text().split() if path.is_file() else []


def _lfs_paths(repo: _Repo, tree: str) -> list[str]:
    return _lfs_filtered(repo, _tree_paths(repo, tree))


def _lfs_filtered(repo: _Repo, paths: list[str]) -> list[str]:
    """The ``paths`` the host's attributes give ``filter=lfs``."""
    if not paths:
        return []
    fields = repo.run("check-attr", "-z", "--stdin", "filter", stdin=nul_join(paths)).stdout
    parts = fields.split(b"\0")
    return sorted(
        os.fsdecode(parts[i]) for i in range(0, len(parts) - 2, 3) if parts[i + 2] == b"lfs"
    )
