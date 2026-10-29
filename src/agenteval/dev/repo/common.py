"""Constants, errors and the git helpers the dev repo modules share."""

from __future__ import annotations

import functools
import os
import re
import subprocess
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agenteval.guest.dev import dev_repo

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


def _unprintable(char: str) -> bool:
    """Control and bidi characters, and lone surrogates (bytes that are not UTF-8)."""
    return unicodedata.category(char) in ("Cc", "Cs") or char in _BIDI


@dataclass(frozen=True)
class _Change:
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    path: str


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


def _index_path(repo: _Repo) -> Path:
    return Path(repo.out("rev-parse", "--path-format=absolute", "--git-path", "index"))
