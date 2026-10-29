"""Live mode: what a session changed, found without any transport."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from agenteval.dev.repo.common import _ABSENT, _GITLINK, _path_ok, _read_blobs, _Repo
from agenteval.dev.repo.host_paths import repo_path_flags
from agenteval.dev.repo.review_items import _NOT_A_FILE, _read_host
from agenteval.guest.dev.dev_repo import nul_split


def live_hashes(root: Path) -> dict[str, str]:
    """sha256 of the git files a live session could change to run code on the host.

    ``config``, ``config.worktree``, the ``commondir``/``gitdir`` redirects, ``hooks/``
    and ``info/`` of the git dir, the common git dir (shared by linked worktrees), the
    other worktrees' dirs and the submodules' git dirs (``modules/``), plus a ``.git``
    file. Keys are absolute paths.
    """
    proc = _Repo(root).run(
        "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", check=False
    )
    if proc.returncode != 0:
        return {}
    git_dir, common = (Path(p) for p in os.fsdecode(proc.stdout).splitlines())
    files = [root / ".git"]
    git_dirs = [git_dir, common, *sorted(common.glob("worktrees/*")), *_module_dirs(common)]
    for directory in dict.fromkeys(git_dirs):
        files += [directory / n for n in ("config", "config.worktree", "commondir", "gitdir")]
        for sub in ("hooks", "info"):
            for dirpath, dirnames, filenames in os.walk(directory / sub):
                files += [Path(dirpath) / n for n in [*filenames, *dirnames]]
    hashes = {}
    for path in files:
        digest = _digest(path)
        if digest is not None:
            hashes[str(path)] = digest
    return hashes


def _module_dirs(common: Path) -> list[Path]:
    """Git dirs of submodules: ``modules/<name>`` (names may hold ``/``), nested ones too."""
    found = []
    for dirpath, dirnames, filenames in os.walk(common / "modules"):
        if "HEAD" in filenames:
            found.append(Path(dirpath))
            dirnames[:] = [d for d in dirnames if d == "modules"]
    return sorted(found)


def _digest(path: Path) -> str | None:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return hashlib.sha256(b"symlink:" + os.fsencode(os.readlink(path))).hexdigest()
        if stat.S_ISREG(st.st_mode):
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return None


def live_report(root: Path, before: Mapping[str, str]) -> list[str]:
    """What a live session changed that runs on the host: git files, then repo paths."""
    after = live_hashes(root)
    lines = []
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        what = "added" if key not in before else "removed" if key not in after else "changed"
        lines.append(f"{key} was {what} during the session (git reads or runs it on your host)")
        changed.append(Path(key).name)
    if {"config", "config.worktree", "commondir", "gitdir", "attributes", ".git"} & set(changed):
        lines.append(
            "git status was not run because the repo's git configuration changed; "
            "review it before running git in this repo."
        )
        return lines
    repo = _Repo(root)
    proc = repo.run(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        "--ignore-submodules=all",
        check=False,
    )
    if proc.returncode != 0:
        return lines
    paths = [entry[3:] for entry in nul_split(proc.stdout)]
    in_head: dict[str, tuple[str, str]] = {}
    if paths and repo.rev("HEAD^{commit}"):
        for entry in nul_split(repo.run("ls-tree", "-r", "-z", "HEAD").stdout):
            info, _, path = entry.partition("\t")
            mode, _, sha = info.split(" ")
            in_head[path] = (mode, sha)
    wanted = set(paths)
    blobs = _read_blobs(
        repo, [sha for p, (mode, sha) in in_head.items() if p in wanted and mode != _GITLINK]
    )
    for path in paths:
        if not _path_ok(path):
            continue
        old_mode, sha = in_head.get(path, (_ABSENT, ""))
        host = _read_host(root / path)
        if host is _NOT_A_FILE:
            continue
        for flag in repo_path_flags(
            path,
            old_mode,
            host.mode if host else _ABSENT,
            blobs.get(sha),
            host.data if host else None,
        ):
            lines.append(f"{path}: {flag}")
    return lines
