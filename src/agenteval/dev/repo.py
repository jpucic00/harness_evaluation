"""Host-side git for ``agenteval dev``: snapshot, bundle transport and per-file apply.

Copy mode moves the user's repo into the env as git objects. ``snapshot`` commits the
working tree as it is (untracked files, ``--include``d ignored files and flattened
submodules included) as ``refs/agenteval/<id>/base`` and the index as ``.../index``, and
bundles them with the branch history into ``in.bundle``; ``sandbox_scripts/dev_repo.py``
rebuilds the repo from it inside the env. At the end the env bundles its final working
tree and new commits; ``import_export`` verifies and fetches them into
``refs/agenteval/<id>/*`` (never into ``refs/heads``) and ``repo_items`` turns the
difference into review items. Each item writes one file with a 3-way rule; the branch
moves only through a guarded fast-forward, the only step that touches the user's index.

Blobs carry file bytes unconverted when git supports ``--attr-source`` (2.42+), so a host
file can be compared with its snapshot byte for byte. Every git call turns off hooks,
fsmonitor, the untracked cache, LFS filters and commit signing, and internal commits use a
fixed identity. A directory that is not a repo is tracked by a shadow git dir in the
session directory.

Live mode needs no transport: ``live_hashes`` and ``live_report`` point out what a session
changed that git or other host tools run.
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agenteval.dev.review import ItemKind, ReviewItem
from agenteval.dev.session import DevSession, RepoState, ResultState, validate_session_id
from agenteval.sandbox_scripts import dev_repo
from agenteval.sandbox_scripts.dev_repo import nul_join, nul_split

CONFIRM_BYTES = 500 * 1024 * 1024
"""More new file content than this in a snapshot asks before it is copied into the env."""
RAW_ATTRS_VERSION = (2, 42)
BUNDLE_NAME = "in.bundle"
META_NAME = "meta.json"
PATCH_NAME = "worktree.patch"
CONFLICTS_DIR = "conflicts"
SHADOW_DIR = "shadow.git"
SHADOW_EXCLUDES = (
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".env",
    ".env.*",
    "dist/",
    "build/",
    ".next/",
    ".cache/",
    "target/",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    ".terraform/",
    "*.tfstate*",
    ".DS_Store",
    "/CLAUDE.local.md",
)
"""``info/exclude`` of the shadow repo for a directory that is not a git repo.

``CLAUDE.local.md`` travels with the CLI config (``agenteval.dev.userconfig``) instead.
"""
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".pypirc",
    ".git-credentials",
    "*.tfstate",
    "*.tfstate.*",
    "credentials*.json",
)
_NOT_SECRET_SUFFIXES = (".example", ".sample", ".template", ".dist", ".pub")

_ABSENT = "000000"
_FILE = "100644"
_EXEC = "100755"
_SYMLINK = "120000"
_GITLINK = "160000"
_BIDI = frozenset("‌‍‎‏‪‫‬‭‮⁦⁧⁨⁩⁪⁫⁬⁭⁮⁯﻿")
"""Bidi controls and the code points HFS+ ignores in names (``.git‌`` is ``.git``)."""
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")

# case-folded, like the paths they are compared with (see ``repo_path_flags``)
_AGENT_DIRS = frozenset({".claude", ".opencode", ".cursor", ".gemini", ".codex"})
_AGENT_FILES = frozenset(
    name.casefold()
    for name in (
        "CLAUDE.md",
        "CLAUDE.local.md",
        "AGENTS.md",
        "GEMINI.md",
        ".mcp.json",
        "opencode.json",
        "opencode.jsonc",
        ".cursorrules",
        ".windsurfrules",
    )
)
_TOOL_VERSION_FILES = frozenset(
    {
        ".tool-versions",
        ".nvmrc",
        ".node-version",
        ".python-version",
        ".ruby-version",
        ".go-version",
        ".java-version",
        ".terraform-version",
        ".sdkmanrc",
        ".bun-version",
        "mise.toml",
        ".mise.toml",
        "rust-toolchain",
        "rust-toolchain.toml",
    }
)


class RepoError(RuntimeError):
    """A git step failed or the repo is not in a usable state."""


class UnsafePathError(ValueError):
    """A path from the session that must not be written on the host."""


@functools.cache
def raw_attrs_supported() -> bool:
    """Whether host git has ``--attr-source`` (2.42+), which byte-exact transfer needs."""
    try:
        text = subprocess.run(["git", "version"], capture_output=True, text=True).stdout
    except OSError:
        return False
    match = re.search(r"(\d+)\.(\d+)", text)
    return match is not None and (int(match[1]), int(match[2])) >= RAW_ATTRS_VERSION


@dataclass(frozen=True)
class _Repo:
    """Where host git runs: the user's repo, or a shadow git dir over a plain directory."""

    root: Path
    git_dir: Path | None = None

    def run(
        self,
        *args: str,
        stdin: bytes = b"",
        index: Path | None = None,
        raw: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        env = {k: v for k, v in os.environ.items() if k not in dev_repo.STRIP_ENV}
        env.update(dev_repo.IDENTITY, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
        if self.git_dir is not None:
            env["GIT_DIR"] = str(self.git_dir)
            env["GIT_WORK_TREE"] = str(self.root)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        attr_source = self.empty_tree if raw and raw_attrs_supported() else None
        argv = ["git", *dev_repo.raw_attr_args(attr_source)]
        for item in dev_repo.SAFE_CONFIG:
            argv += ["-c", item]
        argv += args
        try:
            proc = subprocess.run(
                argv, cwd=self.root, env=env, input=stdin, capture_output=True, check=False
            )
        except OSError as ex:
            raise RepoError(f"cannot run git in {self.root}: {ex}") from ex
        if check and proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            command = next(
                (a for i, a in enumerate(args) if "-c" not in (a, args[i - 1] if i else "")), ""
            )
            raise RepoError(f"git {command} failed in {self.root}: {detail}")
        return proc

    def out(self, *args: str, stdin: bytes = b"", index: Path | None = None) -> str:
        return os.fsdecode(self.run(*args, stdin=stdin, index=index).stdout).strip()

    def rev(self, spec: str) -> str | None:
        """The object id ``spec`` names, or None."""
        proc = self.run("rev-parse", "--verify", "-q", spec, check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode().strip() or None

    @functools.cached_property
    def empty_tree(self) -> str:
        return self.out("hash-object", "-t", "tree", "/dev/null")


# --- discovery and snapshot -------------------------------------------------------------


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


# --- import of the env's export ---------------------------------------------------------


def import_export(
    session: DevSession, session_dir: Path, bundle: Path, export: Mapping[str, object]
) -> ResultState:
    """Verify the env's ``commits.bundle`` and fetch it into ``refs/agenteval/<id>/*``.

    ``export`` is the JSON ``dev_repo.py export`` printed; it is validated and must match
    the bundle. Also writes ``worktree.patch`` (a readable diff of the working tree).
    """
    result = _parse_export(export)
    session_dir = session_dir.resolve()
    bundle = bundle.resolve()
    repo = _session_repo(session)
    prefix = _prefix(session.id)
    repo.run("bundle", "verify", "-q", str(bundle))
    heads = {}
    for line in repo.out("bundle", "list-heads", str(bundle)).splitlines():
        sha, _, ref = line.partition(" ")
        if ref.startswith("refs/heads/") and not safe_branch_name(ref[len("refs/heads/") :]):
            raise RepoError(f"the session bundle has an unsafe branch name {ref!r}")
        heads[ref] = sha
    if dev_repo.FINAL_REF not in heads:
        raise RepoError("the session bundle has no final commit")
    specs = [f"+{dev_repo.FINAL_REF}:{prefix}/final"]
    if any(ref.startswith("refs/heads/") for ref in heads):
        specs.append(f"+refs/heads/*:{prefix}/heads/*")
    if dev_repo.VM_HEAD_REF in heads:
        specs.append(f"+{dev_repo.VM_HEAD_REF}:{prefix}/vm-head")
    repo.run(
        "-c",
        "fetch.fsckObjects=true",
        "-c",
        "transfer.fsckObjects=true",
        "fetch",
        "-q",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        str(bundle),
        *specs,
    )
    fetched = (repo.rev(f"{prefix}/final^{{commit}}"), repo.rev(f"{prefix}/final^{{tree}}"))
    if fetched != (result.final_commit, result.final_tree):
        raise RepoError("the export JSON does not match the bundle's final commit")
    for name, sha in result.branches.items():
        if f"refs/heads/{name}" in heads and repo.rev(f"{prefix}/heads/{name}") != sha:
            raise RepoError(f"the export JSON does not match the bundle's branch {name}")
    patch = repo.run(
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--full-index",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        session.repo.base_tree,
        result.final_tree,
    ).stdout
    (session_dir / PATCH_NAME).write_bytes(patch)
    return result


def _hex(value: object, what: str) -> str:
    if not isinstance(value, str) or not dev_repo.HEX.fullmatch(value):
        raise RepoError(f"export: {what} is not an object id: {value!r}")
    return value


def _parse_export(export: Mapping[str, object]) -> ResultState:
    head = export.get("head")
    head_branch = export.get("head_branch")
    if head_branch is not None and not (
        isinstance(head_branch, str) and safe_branch_name(head_branch)
    ):
        raise RepoError(f"export: unsafe branch name {head_branch!r}")
    raw_branches = export.get("branches", {})
    if not isinstance(raw_branches, Mapping):
        raise RepoError("export: branches must be an object")
    branches = {}
    for name, sha in raw_branches.items():
        if not isinstance(name, str) or not safe_branch_name(name):
            raise RepoError(f"export: unsafe branch name {name!r}")
        branches[name] = _hex(sha, f"branch {name}")
    excluded = export.get("excluded", [])
    if not isinstance(excluded, list) or not all(
        isinstance(p, str) and "\0" not in p for p in excluded
    ):
        raise RepoError("export: excluded must be a list of paths")
    return ResultState(
        final_commit=_hex(export.get("final_commit"), "final_commit"),
        final_tree=_hex(export.get("final_tree"), "final_tree"),
        head=None if head is None else _hex(head, "head"),
        head_branch=head_branch,
        branches=branches,
        excluded=excluded,
    )


def safe_branch_name(name: str) -> bool:
    """A valid git branch name without control or bidi characters (shown in the terminal)."""
    if not name or len(name) > 255 or name == "@" or name.startswith("-"):
        return False
    if _REF_FORBIDDEN.search(name) or any(_unprintable(c) for c in name):
        return False
    if ".." in name or "@{" in name or name.endswith((".", "/")):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/")
    )


def _unprintable(char: str) -> bool:
    """Control and bidi characters, and lone surrogates (bytes that are not UTF-8)."""
    return unicodedata.category(char) in ("Cc", "Cs") or char in _BIDI


def _session_repo(session: DevSession) -> _Repo:
    state = session.repo
    root = Path(state.root)
    if not root.is_dir():
        raise RepoError(f"the session's directory {root} no longer exists")
    if state.git:
        return _Repo(root)
    if not state.shadow_git_dir or not Path(state.shadow_git_dir).is_dir():
        raise RepoError(f"the shadow repo of session {session.id} is missing")
    return _Repo(root, Path(state.shadow_git_dir))


# --- review items -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    path: str


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


def _diff_tree(repo: _Repo, a: str, b: str) -> list[_Change]:
    """``diff-tree -z`` of two trees: ``:<modes> <shas> <status>`` NUL ``<path>`` NUL pairs."""
    tokens = repo.run("diff-tree", "-r", "-z", "--no-renames", a, b).stdout.split(b"\0")
    changes = []
    for header, path in zip(tokens[0:-1:2], tokens[1::2], strict=False):
        fields = header.decode(errors="replace").split(" ")
        if len(fields) != 5 or not header.startswith(b":"):
            raise RepoError(f"unexpected git diff-tree output: {header[:100]!r}")
        old_mode, new_mode, old_sha, new_sha, _ = fields
        changes.append(_Change(old_mode[1:], new_mode, old_sha, new_sha, os.fsdecode(path)))
    return changes


def _read_blobs(repo: _Repo, shas: Iterable[str]) -> dict[str, bytes]:
    wanted = sorted(set(shas))
    if not wanted:
        return {}
    data = repo.run("cat-file", "--batch", stdin=_lines(wanted)).stdout
    blobs = {}
    pos = 0
    for sha in wanted:
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        if len(header) != 3:
            raise RepoError(f"object {sha} is missing from the repo")
        size = int(header[2])
        blobs[sha] = data[end + 1 : end + 1 + size]
        pos = end + 2 + size
    return blobs


def _change_blobs(repo: _Repo, changes: Iterable[_Change]) -> dict[str, bytes]:
    """Contents of both sides of ``changes`` (gitlinks and absent sides left out)."""
    return _read_blobs(
        repo,
        [
            sha
            for c in changes
            for mode, sha in ((c.old_mode, c.old_sha), (c.new_mode, c.new_sha))
            if mode not in (_ABSENT, _GITLINK)
        ],
    )


def _lines(items: Iterable[str]) -> bytes:
    return "".join(f"{item}\n" for item in items).encode()


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


def _check_components(path: str) -> None:
    if not path:
        raise UnsafePathError("empty path")
    if "\0" in path:
        raise UnsafePathError(f"{path!r}: NUL byte")
    if any(_unprintable(c) for c in path):
        # not UTF-8: macOS cannot store the name, and the session record cannot keep it
        raise UnsafePathError(f"{path!r}: control or bidi characters, or not UTF-8")
    if path.startswith("/"):
        raise UnsafePathError(f"{path!r}: absolute path")
    for part in path.split("/"):
        if part in ("", ".", ".."):
            raise UnsafePathError(f"{path!r}: empty, '.' or '..' component")
        if part.rstrip(". ").casefold() in (".git", "git~1"):
            raise UnsafePathError(f"{path!r}: .git component")


def _path_ok(path: str) -> bool:
    try:
        _check_components(path)
    except UnsafePathError:
        return False
    return True


def _show(path: str) -> str:
    return path if _path_ok(path) else repr(path)


def _listing(paths: Sequence[str], limit: int = 5) -> str:
    shown = ", ".join(paths[:limit])
    return shown if len(paths) <= limit else f"{shown} and {len(paths) - limit} more"


# --- commits ----------------------------------------------------------------------------


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


def _index_path(repo: _Repo) -> Path:
    return Path(repo.out("rev-parse", "--path-format=absolute", "--git-path", "index"))


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


# --- host-executed paths ----------------------------------------------------------------


def repo_path_flags(
    path: str, old_mode: str, new_mode: str, before: bytes | None, after: bytes | None
) -> list[str]:
    """Why a changed repo path deserves a careful look: something on the host runs or obeys it.

    Attention pointers, not a verdict. ``before``/``after`` are the contents (None: absent;
    a symlink's target), the modes git's octal strings (``000000`` when absent). Names are
    compared case-insensitively: on a macOS host ``.GitHub/x.yml`` lands in ``.github/``.
    """
    if after is None:
        return []
    parts = path.casefold().split("/")
    name = parts[-1]
    rules = (
        (name == ".envrc", "direnv runs .envrc in your shell when you enter the directory"),
        (
            name in ("makefile", "gnumakefile") or name.endswith(".mk"),
            "make runs its recipes on your host",
        ),
        (name in ("justfile", ".justfile"), "just runs its recipes on your host"),
        (
            name in ("taskfile.yml", "taskfile.yaml", "taskfile.dist.yml", "taskfile.dist.yaml"),
            "Task runs its commands on your host",
        ),
        (
            name == "package.json" and _changed(before, after, _package_scripts),
            "package.json scripts changed: npm, yarn and pnpm run them on your host "
            "(install hooks included)",
        ),
        (
            name in (".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs"),
            "package manager config: can change registries and run code on install",
        ),
        (
            name == "pyproject.toml" and _changed(before, after, _pyproject_hooks),
            "pyproject.toml build system or scripts changed: they run when the project is "
            "built, installed or run",
        ),
        (name == "setup.py", "setup.py runs when the package is built or installed"),
        (name == "conftest.py", "pytest runs conftest.py on every test run"),
        (name in ("tox.ini", "noxfile.py"), "tox and nox run its commands on your host"),
        (
            parts[0] == ".github",
            "GitHub config: workflows run in CI with repository secrets once pushed",
        ),
        (name == ".gitlab-ci.yml", "GitLab CI config: runs with CI secrets once pushed"),
        (
            name in (".pre-commit-config.yaml", ".pre-commit-hooks.yaml"),
            "pre-commit runs these hooks on your host on git commit",
        ),
        (
            parts[0] in (".husky", ".githooks")
            or name.removeprefix(".").removesuffix(".yml").removesuffix(".yaml")
            in ("lefthook", "lefthook-local"),
            "git hook config: runs on your host on git operations",
        ),
        (
            name in _AGENT_FILES or any(p in _AGENT_DIRS for p in parts[:-1]),
            "coding-agent config or instructions: agents on your host read and act on it",
        ),
        (
            parts[0] == ".vscode" and name in ("tasks.json", "settings.json", "launch.json"),
            "VS Code config: can run commands when the folder is opened",
        ),
        (
            parts[0] == ".devcontainer" or name == ".devcontainer.json",
            "dev container config: runs commands when the container is built or opened",
        ),
        (
            name == ".gitattributes",
            ".gitattributes selects filters and diff/merge drivers that git runs on your host",
        ),
        (name == ".gitmodules", ".gitmodules sets where submodules are fetched from"),
        (
            name in _TOOL_VERSION_FILES,
            "tool-version file: picks the toolchain version your host installs and runs",
        ),
        (
            new_mode == _EXEC and old_mode != _EXEC,
            "new executable file" if old_mode == _ABSENT else "made executable",
        ),
        (
            new_mode == _SYMLINK and _escapes(path, after),
            "symlink pointing outside the repo",
        ),
        (
            new_mode == _SYMLINK
            and any(p.rstrip(". ").casefold() == ".git" for p in os.fsdecode(after).split("/")),
            "symlink pointing into a .git directory (hooks and config run on your host)",
        ),
    )
    return list(dict.fromkeys(text for hit, text in rules if hit))


def _changed(before: bytes | None, after: bytes, extract: Callable[[bytes | None], object]) -> bool:
    try:
        return extract(before) != extract(after)
    except (ValueError, RecursionError):  # unparsable, or nested too deeply to parse
        return True


def _package_scripts(data: bytes | None) -> object:
    if data is None:
        return None
    doc = json.loads(data)
    return doc.get("scripts") if isinstance(doc, dict) else None


def _pyproject_hooks(data: bytes | None) -> object:
    if data is None:
        return None
    doc = tomllib.loads(data.decode())
    picked: dict[str, object] = {"build-system": doc.get("build-system")}
    project = doc.get("project")
    if isinstance(project, dict):
        for key in ("scripts", "gui-scripts", "entry-points"):
            picked[f"project.{key}"] = project.get(key)
    tool = doc.get("tool")
    if isinstance(tool, dict):
        for name, section in tool.items():
            if isinstance(section, dict):
                picked[f"tool.{name}"] = (section.get("scripts"), section.get("tasks"))
        hatch = tool.get("hatch")
        envs = hatch.get("envs") if isinstance(hatch, dict) else None
        if isinstance(envs, dict):
            picked["tool.hatch.envs"] = {
                env: spec.get("scripts") for env, spec in envs.items() if isinstance(spec, dict)
            }
    return picked


def _escapes(path: str, target: bytes) -> bool:
    link = os.fsdecode(target)
    if link.startswith("/"):
        return True
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
    return resolved == ".." or resolved.startswith("../")


# --- live mode --------------------------------------------------------------------------


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
