#!/usr/bin/env python3
"""Repo transport inside the env for ``agenteval dev`` copy mode (stdlib only, Python 3.12).

Runs as the agent user; the host uploads it root-owned to ``/opt/agenteval/dev_repo.py``.
The host side is ``agenteval.dev.repo``: ``snapshot`` writes ``in.bundle`` and
``meta.json``, ``import_export`` reads what ``export`` writes and prints.

Usage::

    dev_repo.py import --bundle PATH --meta PATH --workspace DIR
    dev_repo.py export --workspace DIR --base CB [--head H] --out DIR [--max-file-bytes N]

``import`` turns the empty workspace into a repo whose working tree is the host's working
tree (untracked files and flattened submodules included), whose index is the host's index
and whose HEAD is the host's branch or commit (``main`` at the snapshot for a directory
that is not a repo). ``export`` commits the final working tree on top of the snapshot
(``refs/agenteval/final``) and bundles it with every new branch commit into
``OUT/commits.bundle``. Both print one JSON object on stdout; on failure it is
``{"error": ...}`` and the exit status is 1.

meta.json (version 1, written by the host):

    session_id      host session id
    git             false: the host directory is not a repo
    branch, head    host branch and HEAD commit (null: detached / unborn)
    object_format   ``sha1`` or ``sha256``
    raw_attrs       move file bytes unconverted (``--attr-source`` set to the empty tree)
    refs            bundle refs to fetch: ``base`` (snapshot commit), ``index`` (index
                    commit, null when not a repo), ``head`` (the branch or detached HEAD
                    ref, null when unborn or not a repo)
    base_commit, base_tree  the snapshot, checked after the fetch
    lfs_paths       Git LFS paths: the index keeps the pointer, the file is the host
                    content, so they are marked skip-worktree
    shallow         shallow boundary commits of a shallow clone (the bundle stops there)
    exclude         ignore patterns the host applies outside ``.gitignore`` files (its
                    global excludes file and ``info/exclude``, or the defaults for a
                    directory that is not a repo); written to ``.git/info/exclude`` so the
                    env ignores the same files and ``export`` does not bring them back
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

META_VERSION = 1
MAX_FILE_BYTES = 100 * 1024 * 1024
SAFE_CONFIG = (
    "core.fsmonitor=false",
    "core.untrackedCache=false",
    "core.hooksPath=/dev/null",
    "core.autocrlf=false",
    "core.safecrlf=false",
    "commit.gpgSign=false",
    "filter.lfs.clean=",
    "filter.lfs.smudge=",
    "filter.lfs.process=",
    "filter.lfs.required=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "advice.addEmbeddedRepo=false",
)
"""``-c`` settings on every git call (host and env): no hooks, fsmonitor, LFS or signing."""
IDENTITY = {
    "GIT_AUTHOR_NAME": "agenteval",
    "GIT_AUTHOR_EMAIL": "agenteval@localhost",
    "GIT_COMMITTER_NAME": "agenteval",
    "GIT_COMMITTER_EMAIL": "agenteval@localhost",
}
"""Fixed identity of the internal snapshot and export commits."""
STRIP_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_ATTR_SOURCE",
)
EMPTY_TREES = {
    "sha1": "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "sha256": "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
}
HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
FINAL_REF = "refs/agenteval/final"
VM_HEAD_REF = "refs/agenteval/vm-head"
BUNDLE_NAME = "commits.bundle"


class Failure(Exception):
    """Reported as ``{"error": ...}``."""


def raw_attr_args(empty_tree: str | None) -> list[str]:
    """Git options that make blobs equal file bytes (no eol/encoding/filter conversion)."""
    if not empty_tree:
        return []
    return [f"--attr-source={empty_tree}", "-c", "core.attributesFile=/dev/null"]


def git(
    cwd: Path,
    *args: str,
    stdin: bytes | None = None,
    index: Path | None = None,
    attr_source: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env.update(IDENTITY, GIT_TERMINAL_PROMPT="0")
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    argv = ["git", *raw_attr_args(attr_source)]
    for item in SAFE_CONFIG:
        argv += ["-c", item]
    argv += args
    if stdin is None:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False
        )
    else:
        proc = subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True, check=False)
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise Failure(f"git {args[0]} failed ({proc.returncode}): {detail}")
    return proc


def out(cwd: Path, *args: str, index: Path | None = None) -> str:
    return git(cwd, *args, index=index).stdout.decode().strip()


def rev(cwd: Path, spec: str) -> str | None:
    """The object id ``spec`` names, or None."""
    proc = git(cwd, "rev-parse", "--verify", "-q", spec, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip() or None


def nul_join(items: list[str]) -> bytes:
    return b"".join(os.fsencode(item) + b"\0" for item in items)


def nul_split(data: bytes) -> list[str]:
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def _sha(value: object, what: str) -> str:
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise Failure(f"{what} is not a commit id: {value!r}")
    return value


def _ref(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("refs/") or value.startswith("refs/-"):
        raise Failure(f"meta.json: bad {what} ref {value!r}")
    return value


def load_meta(path: Path) -> dict[str, object]:
    meta = json.loads(path.read_text())
    if not isinstance(meta, dict) or meta.get("version") != META_VERSION:
        raise Failure(f"meta.json: expected version {META_VERSION}")
    if meta.get("object_format") not in EMPTY_TREES:
        raise Failure(f"meta.json: unknown object format {meta.get('object_format')!r}")
    refs = meta.get("refs")
    if not isinstance(refs, dict) or _ref(refs.get("base"), "base") is None:
        raise Failure("meta.json: refs.base is required")
    _ref(refs.get("index"), "index")
    _ref(refs.get("head"), "head")
    if meta.get("head") is not None:
        _sha(meta["head"], "meta.json head")
    branch = meta.get("branch")
    if branch is not None and (not isinstance(branch, str) or not branch or branch[0] == "-"):
        raise Failure(f"meta.json: bad branch {branch!r}")
    lfs = meta.get("lfs_paths", [])
    if not isinstance(lfs, list) or not all(isinstance(p, str) for p in lfs):
        raise Failure("meta.json: lfs_paths must be a list of paths")
    shallow = meta.get("shallow", [])
    if not isinstance(shallow, list):
        raise Failure("meta.json: shallow must be a list of commit ids")
    for sha in shallow:
        _sha(sha, "meta.json shallow commit")
    if not isinstance(meta.get("exclude", ""), str):
        raise Failure("meta.json: exclude must be a string")
    return meta


def import_repo(bundle: Path, meta_path: Path, ws: Path) -> dict[str, object]:
    """Recreate the host repo state in the empty workspace ``ws``."""
    meta = load_meta(meta_path)
    if not ws.is_dir():
        raise Failure(f"workspace {ws} is not a directory")
    if os.path.lexists(ws / ".git"):
        raise Failure(f"workspace {ws} is already a git repo")
    fmt = str(meta["object_format"])
    refs = meta["refs"]
    assert isinstance(refs, dict)
    git(ws, "init", "-q", f"--object-format={fmt}", ".")
    raw = bool(meta.get("raw_attrs", True))
    git(ws, "config", "agenteval.rawAttrs", "true" if raw else "false")
    git_dir = Path(out(ws, "rev-parse", "--absolute-git-dir"))
    (git_dir / "info").mkdir(exist_ok=True)
    (git_dir / "info" / "exclude").write_text(str(meta.get("exclude", "")), encoding="utf-8")
    shallow = meta.get("shallow", [])
    assert isinstance(shallow, list)
    if shallow:
        (git_dir / "shallow").write_text("".join(f"{sha}\n" for sha in shallow))

    specs = [f"+{refs['base']}:refs/agenteval/base"]
    if refs.get("index"):
        specs.append(f"+{refs['index']}:refs/agenteval/index")
    if refs.get("head"):
        specs.append(f"+{refs['head']}:refs/agenteval/head")
    git(
        ws,
        "fetch",
        "-q",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        str(bundle.resolve()),
        *specs,
    )
    base = rev(ws, "refs/agenteval/base^{commit}")
    base_tree = rev(ws, "refs/agenteval/base^{tree}")
    if base is None or base_tree is None:
        raise Failure("the bundle has no snapshot commit")
    for key, value in (("base_commit", base), ("base_tree", base_tree)):
        if meta.get(key) and meta[key] != value:
            raise Failure(f"{key} mismatch: bundle has {value}, meta.json says {meta[key]}")

    head = meta.get("head")
    branch = meta.get("branch")
    if not meta.get("git"):
        branch, head = "main", None
        git(ws, "update-ref", "refs/heads/main", base)
        git(ws, "symbolic-ref", "HEAD", "refs/heads/main")
    elif isinstance(head, str):
        if rev(ws, "refs/agenteval/head^{commit}") != head:
            raise Failure(f"the bundle does not carry HEAD {head}")
        if isinstance(branch, str):
            git(ws, "update-ref", f"refs/heads/{branch}", head)
            git(ws, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
        else:
            git(ws, "update-ref", "--no-deref", "HEAD", head)
    else:
        branch = branch if isinstance(branch, str) else "main"
        git(ws, "symbolic-ref", "HEAD", f"refs/heads/{branch}")

    attr = EMPTY_TREES[fmt] if raw else None
    git(ws, "read-tree", "refs/agenteval/base")
    git(ws, "checkout-index", "-a", "-f", attr_source=attr)
    git(ws, "read-tree", "refs/agenteval/index" if refs.get("index") else "refs/agenteval/base")
    lfs_paths = meta.get("lfs_paths", [])
    assert isinstance(lfs_paths, list)
    tracked = set(nul_split(git(ws, "ls-files", "-z").stdout))
    skipped = sorted(p for p in lfs_paths if p in tracked)
    if skipped:
        git(ws, "update-index", "--skip-worktree", "-z", "--stdin", stdin=nul_join(skipped))
    git(ws, "update-index", "-q", "--refresh", check=False)
    return {
        "base_tree": base_tree,
        "base_commit": base,
        "head": head,
        "branch": branch,
        "skip_worktree": skipped,
    }


def export_repo(
    ws: Path, base: str, head: str | None, out_dir: Path, max_bytes: int
) -> dict[str, object]:
    """Commit the final working tree on top of ``base`` and bundle everything new."""
    base = _sha(base, "--base")
    if head is not None:
        head = _sha(head, "--head")
    if rev(ws, f"{base}^{{commit}}") != base:
        raise Failure(f"base commit {base} is not in the workspace repo")
    git_dir = Path(out(ws, "rev-parse", "--absolute-git-dir"))
    raw = git(ws, "config", "--get", "agenteval.rawAttrs", check=False).stdout.strip() != b"false"
    attr = EMPTY_TREES.get(out(ws, "rev-parse", "--show-object-format")) if raw else None

    index = git_dir / "agenteval-export.index"
    index.unlink(missing_ok=True)
    excluded: list[str] = []
    try:
        git(ws, "read-tree", base, index=index)
        listing = git(ws, "ls-files", "-o", "--exclude-standard", "-z", index=index).stdout
        for path in nul_split(listing):
            if path.endswith("/"):  # a nested repo: its files are not exported
                excluded.append(path)
                continue
            try:
                st = os.lstat(ws / path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size > max_bytes:
                excluded.append(path)
        specs = [".", *(f":(exclude,literal){p.rstrip('/')}" for p in excluded)]
        git(
            ws,
            "add",
            "-A",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin=nul_join(specs),
            index=index,
            attr_source=attr,
        )
        tree = out(ws, "write-tree", index=index)
    finally:
        index.unlink(missing_ok=True)
    commit = out(ws, "commit-tree", tree, "-p", base, "-m", "agenteval dev: final working tree")
    git(ws, "update-ref", FINAL_REF, commit)

    branches: dict[str, str] = {}
    listing = git(ws, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/heads/").stdout
    for line in listing.splitlines():
        name, _, sha = line.partition(b"\0")
        branches[os.fsdecode(name).removeprefix("refs/heads/")] = sha.decode()
    vm_head = rev(ws, "HEAD^{commit}")
    symref = git(ws, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
    head_branch = symref.removeprefix("refs/heads/") if symref.startswith("refs/heads/") else None

    tips = [FINAL_REF, "--branches"]
    if vm_head is not None and head_branch is None and vm_head != head:
        git(ws, "update-ref", VM_HEAD_REF, vm_head)
        tips.append(VM_HEAD_REF)
    else:
        git(ws, "update-ref", "-d", VM_HEAD_REF, check=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / BUNDLE_NAME
    bundle.unlink(missing_ok=True)
    excludes = [base, *([head] if head else [])]
    git(ws, "bundle", "create", "-q", str(bundle.resolve()), *tips, "--not", *excludes)
    return {
        "final_commit": commit,
        "final_tree": tree,
        "head": vm_head,
        "head_branch": head_branch,
        "branches": branches,
        "excluded": sorted(excluded),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev_repo.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="recreate the host repo in the workspace")
    imp.add_argument("--bundle", type=Path, required=True)
    imp.add_argument("--meta", type=Path, required=True)
    imp.add_argument("--workspace", type=Path, required=True)
    exp = sub.add_parser("export", help="bundle the final working tree and new commits")
    exp.add_argument("--workspace", type=Path, required=True)
    exp.add_argument("--base", required=True)
    exp.add_argument("--head")
    exp.add_argument("--out", type=Path, required=True)
    exp.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_repo(args.bundle, args.meta, args.workspace)
        else:
            result = export_repo(
                args.workspace, args.base, args.head, args.out, args.max_file_bytes
            )
    except (Failure, OSError, ValueError) as ex:
        print(json.dumps({"error": str(ex)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
