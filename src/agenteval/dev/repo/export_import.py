"""Import of the env's export: verify the bundle and fetch it into the session refs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from agenteval.dev.repo.common import _REF_FORBIDDEN, PATCH_NAME, RepoError, _Repo, _unprintable
from agenteval.dev.repo.discovery import _prefix
from agenteval.dev.session import DevSession, ResultState
from agenteval.guest.dev import dev_repo


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
