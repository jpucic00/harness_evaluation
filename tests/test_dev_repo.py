"""Copy-mode repo transport of ``agenteval dev``: host git plus ``dev_repo.py`` run locally.

A temp directory stands in for the env workspace; the script runs with ``sys.executable``.
Git never sees the user's config (``GIT_CONFIG_GLOBAL=/dev/null``, temp ``HOME``).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agenteval.dev import repo as devrepo
from agenteval.dev.review import ReviewItem
from agenteval.dev.session import DevSession, ResultState
from agenteval.sandbox_scripts import dev_repo

SCRIPT = Path(dev_repo.__file__)
SID = "20261019T000000Z-test-abcd"


@pytest.fixture(autouse=True)
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test User")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
    # XDG_CONFIG_HOME would point git's default excludes file at the real user's
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def git(cwd: Path, *args: str | Path, stdin: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *map(str, args)], cwd=cwd, input=stdin, capture_output=True, text=True
    )
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc.stdout


def write(path: Path, content: str | bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    if mode is not None:
        path.chmod(mode)


def make_repo(path: Path, files: Mapping[str, str | bytes] | None = None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, content in (files or {"a.txt": "one\n", "b.txt": "two\n"}).items():
        write(path / name, content)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def script(*args: str | Path, ok: bool = True) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True
    )
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert isinstance(data, dict)
    assert (proc.returncode == 0) == ok, (data, proc.stderr)
    return data


@dataclass
class Run:
    session: DevSession
    session_dir: Path
    ws: Path
    snap: devrepo.Snapshot
    imported: dict[str, object]
    base: Path


def start(
    path: Path,
    base: Path,
    includes: Sequence[str] = (),
    confirm: Callable[[str], bool] | None = None,
    sid: str = SID,
) -> Run:
    """Snapshot ``path`` on the "host" and import it into ``base/ws`` like the env does."""
    info = devrepo.discover(path)
    session_dir = base / "session"
    session_dir.mkdir(parents=True)
    snap = devrepo.snapshot(info, session_dir, sid, includes, confirm=confirm)
    ws = base / "ws"
    ws.mkdir()
    imported = script("import", "--bundle", snap.bundle, "--meta", snap.meta, "--workspace", ws)
    session = DevSession(
        id=sid,
        created=datetime.now(UTC),
        harness="opencode-none",
        model="mockllm/model",
        environment="docker-ubuntu",
        vm_root=str(ws),
        vm_cwd=str(ws),
        repo=snap.state,
    )
    return Run(session, session_dir, ws, snap, imported, base)


def finish(run: Run, max_bytes: int | None = None) -> tuple[list[ReviewItem], list[str]]:
    """Export from the workspace, import on the host, and list the review items."""
    export = script(*export_args(run, max_bytes))
    run.session.result = devrepo.import_export(
        run.session, run.session_dir, run.base / "out" / dev_repo.BUNDLE_NAME, export
    )
    return devrepo.repo_items(run.session, run.session_dir)


def export_args(run: Run, max_bytes: int | None = None) -> list[str | Path]:
    """``dev_repo.py export`` arguments (bundle in ``run.base/out``)."""
    args: list[str | Path] = ["export", "--workspace", run.ws, "--out", run.base / "out"]
    args += ["--base", run.session.repo.base_commit]
    if run.session.repo.head:
        args += ["--head", run.session.repo.head]
    if max_bytes is not None:
        args += ["--max-file-bytes", str(max_bytes)]
    return args


def crafted_export(run: Run, tree: str) -> tuple[Path, dict[str, object]]:
    """A bundle and export JSON whose final commit has ``tree``, as a hostile env could send."""
    ws = run.ws
    commit = git(ws, "commit-tree", tree, "-p", run.session.repo.base_commit, "-m", "x").strip()
    git(ws, "update-ref", "refs/agenteval/final", commit)
    out = run.base / "out"
    out.mkdir(exist_ok=True)
    bundle = out / dev_repo.BUNDLE_NAME
    git(ws, "bundle", "create", "-q", bundle, "refs/agenteval/final", "--not", commit + "~1")
    export: dict[str, object] = {
        "final_commit": commit,
        "final_tree": tree,
        "head": run.session.repo.head,
        "head_branch": run.session.repo.branch,
        "branches": {},
        "excluded": [],
    }
    return bundle, export


def by_id(items: list[ReviewItem]) -> dict[str, ReviewItem]:
    return {item.id: item for item in items}


def apply_all(items: list[ReviewItem]) -> None:
    for item in items:
        item.apply(item.proposed)


def tree_files(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (``.git`` left out); a symlink as ``link:<target>``."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [*filenames, *dirnames]:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if name == ".git":
                continue
            if path.is_symlink():
                found[rel] = b"link:" + os.fsencode(os.readlink(path))
            elif path.is_file():
                found[rel] = path.read_bytes()
        dirnames[:] = [d for d in dirnames if d != ".git" and not (Path(dirpath) / d).is_symlink()]
    return found


def status(cwd: Path) -> str:
    return git(cwd, "status", "--porcelain", "--untracked-files=all")


def rev(cwd: Path, spec: str) -> str:
    return git(cwd, "rev-parse", spec).strip()


# --- snapshot and import -----------------------------------------------------------------


def test_dirty_tree_round_trip(tmp_path: Path) -> None:
    host = make_repo(
        tmp_path / "host",
        {"a.txt": "one\n", "b.txt": "two\n", "c.txt": "3\n", "d.txt": "4\n", "s/e.txt": "5\n"},
    )
    write(host / "a.txt", "one changed\n")
    write(host / "b.txt", "two staged\n")
    git(host, "add", "b.txt")
    write(host / "b.txt", "two staged, then edited\n")
    (host / "c.txt").unlink()
    git(host, "rm", "-q", "d.txt")
    write(host / "new.txt", "untracked\n")
    write(host / "s/added.txt", "staged new file\n")
    git(host, "add", "s/added.txt")
    write(host / "run.sh", "#!/bin/sh\n", 0o755)
    os.symlink("a.txt", host / "link")
    index_before = (host / ".git" / "index").read_bytes()

    run = start(host, tmp_path / "run")

    assert (host / ".git" / "index").read_bytes() == index_before
    assert run.imported["base_tree"] == run.snap.state.base_tree
    assert run.imported["base_commit"] == run.snap.state.base_commit
    assert status(run.ws) == status(host)
    assert tree_files(run.ws) == tree_files(host)
    assert rev(run.ws, "HEAD") == rev(host, "HEAD") == run.snap.state.head
    assert git(run.ws, "symbolic-ref", "HEAD") == "refs/heads/main\n"
    assert run.snap.state.branch == "main"
    assert rev(host, f"refs/agenteval/{SID}/base") == run.snap.state.base_commit
    meta = json.loads(run.snap.meta.read_text())
    assert meta["refs"] == {
        "base": f"refs/agenteval/{SID}/base",
        "index": f"refs/agenteval/{SID}/index",
        "head": "refs/heads/main",
    }

    items, notes = finish(run)
    assert items == []
    assert notes == []


def test_ignored_files_stay_out_unless_included(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host", {".gitignore": "*.log\nsecret/\n", "a.txt": "a\n"})
    write(host / "debug.log", "log\n")
    write(host / "secret/deep/key.txt", "k\n")
    write(host / ".env", "TOKEN=x\n")

    run = start(host, tmp_path / "plain")
    assert not (run.ws / "debug.log").exists()
    assert not (run.ws / "secret").exists()
    assert (run.ws / ".env").read_text() == "TOKEN=x\n"
    assert any("Secret-looking" in w and ".env" in w for w in run.snap.warnings)

    run = start(host, tmp_path / "included", includes=["*.log", "secret", "nothing-*"])
    assert (run.ws / "debug.log").read_text() == "log\n"
    assert (run.ws / "secret/deep/key.txt").read_text() == "k\n"
    assert status(run.ws) == status(host)
    assert run.snap.state.includes == ["*.log", "secret", "nothing-*"]
    assert any("'nothing-*' matched no ignored files" in w for w in run.snap.warnings)


def test_host_ignore_rules_outside_gitignore_apply_in_env(tmp_path: Path) -> None:
    global_ignore = tmp_path / "home" / ".config" / "git" / "ignore"
    write(global_ignore, "*.local")
    host = make_repo(tmp_path / "host")
    write(host / ".git" / "info" / "exclude", "scratch/\n")
    run = start(host, tmp_path / "run")
    assert (run.ws / ".git" / "info" / "exclude").read_text() == "*.local\nscratch/\n"

    global_ignore.unlink()  # the env has the patterns, not the user's global excludes file
    write(run.ws / "notes.local", "x\n")
    write(run.ws / "scratch/a.txt", "a\n")
    write(run.ws / "kept.txt", "k\n")
    items, _ = finish(run)
    assert set(by_id(items)) == {"repo:kept.txt"}


def test_unborn_repo_round_trip_and_first_commit(tmp_path: Path) -> None:
    host = tmp_path / "host"
    host.mkdir()
    git(host, "init", "-q", "-b", "main")
    write(host / "a.txt", "staged\n")
    git(host, "add", "a.txt")
    write(host / "b.txt", "untracked\n")

    run = start(host, tmp_path / "run")
    assert run.snap.state.head is None
    assert run.snap.state.branch == "main"
    assert status(run.ws) == status(host)
    assert tree_files(run.ws) == tree_files(host)

    git(run.ws, "add", "-A")
    git(run.ws, "commit", "-q", "-m", "first commit")
    items, _ = finish(run)
    ff = by_id(items)["commits:main"]
    ff.apply(None)
    assert rev(host, "main") == rev(run.ws, "HEAD")
    assert git(host, "write-tree").strip() == rev(host, "main^{tree}")
    assert status(host) == ""


def test_sha256_repo_round_trip(tmp_path: Path) -> None:
    host = tmp_path / "host"
    host.mkdir()
    git(host, "init", "-q", "-b", "main", "--object-format=sha256")
    write(host / "a.txt", "one\n")
    git(host, "add", "-A")
    git(host, "commit", "-q", "-m", "init")
    write(host / "a.txt", "dirty\n")
    run = start(host, tmp_path / "run")
    assert len(run.snap.state.base_tree) == 64
    assert status(run.ws) == status(host)
    git(run.ws, "commit", "-q", "-a", "-m", "agent")
    items, _ = finish(run)
    by_id(items)["commits:main"].apply(None)
    assert rev(host, "main") == rev(run.ws, "HEAD")
    assert status(host) == ""


def test_shallow_clone_round_trip(tmp_path: Path) -> None:
    upstream = make_repo(tmp_path / "upstream")
    for i in range(3):
        write(upstream / "a.txt", f"v{i}\n")
        git(upstream, "commit", "-q", "-a", "-m", f"c{i}")
    host = tmp_path / "host"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{upstream}", host)
    write(host / "a.txt", "dirty\n")

    run = start(host, tmp_path / "run")
    assert json.loads(run.snap.meta.read_text())["shallow"] == [rev(host, "HEAD")]
    assert status(run.ws) == status(host)
    git(run.ws, "commit", "-q", "-a", "-m", "agent")
    items, _ = finish(run)
    by_id(items)["commits:main"].apply(None)
    assert rev(host, "main") == rev(run.ws, "HEAD")


def test_detached_head(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    git(host, "commit", "-q", "--allow-empty", "-m", "second")
    git(host, "checkout", "-q", "--detach", "HEAD~1")
    head = rev(host, "HEAD")

    run = start(host, tmp_path / "run")
    assert run.snap.state.branch is None
    assert run.snap.state.head == head
    assert rev(run.ws, "HEAD") == head
    assert git(run.ws, "symbolic-ref", "-q", "HEAD", check=False) == ""

    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent on detached HEAD")
    items, notes = finish(run)
    assert run.session.result is not None
    assert run.session.result.head_branch is None
    assert rev(host, f"refs/agenteval/{SID}/vm-head") == rev(run.ws, "HEAD")
    assert not [i for i in items if i.group == "commits"]
    assert any("detached HEAD" in n for n in notes)
    assert rev(host, "HEAD") == head
    # the agent's detached commit is kept like its branches, until heads are dropped too
    agent_tip = rev(run.ws, "HEAD")
    devrepo.cleanup_refs(run.session)
    assert rev(host, f"refs/agenteval/{SID}/vm-head") == agent_tip
    devrepo.cleanup_refs(run.session, keep_heads=False)
    assert git(host, "for-each-ref", f"refs/agenteval/{SID}/") == ""


def test_non_git_directory_uses_shadow_repo_with_default_excludes(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    write(plain / "main.py", "print(1)\n")
    write(plain / "node_modules/x/index.js", "x\n")
    write(plain / ".venv/bin/python", "py\n")
    write(plain / ".env.local", "SECRET=1\n")
    write(plain / "id_rsa", "key\n")
    write(plain / "CLAUDE.local.md", "travels with the CLI config\n")

    run = start(plain, tmp_path / "run")
    assert not run.snap.state.git
    assert run.snap.state.shadow_git_dir == str(run.session_dir.resolve() / devrepo.SHADOW_DIR)
    assert set(tree_files(run.ws)) == {"main.py"}
    assert git(run.ws, "symbolic-ref", "HEAD") == "refs/heads/main\n"
    assert rev(run.ws, "main") == run.snap.state.base_commit
    assert status(run.ws) == ""

    write(run.ws / "main.py", "print(2)\n")
    write(run.ws / "util.py", "x = 1\n")
    git(run.ws, "add", "-A")
    git(run.ws, "commit", "-q", "-m", "agent commit")
    # the env ignores what the shadow repo ignores, so these do not come back
    write(run.ws / "node_modules/pkg/index.js", "x\n")
    write(run.ws / ".env", "TOKEN=1\n")
    write(run.ws / "CLAUDE.local.md", "agent edit\n")
    items, notes = finish(run)
    assert set(by_id(items)) == {"repo:main.py", "repo:util.py"}
    apply_all(items)
    assert (plain / "main.py").read_text() == "print(2)\n"
    assert (plain / "util.py").read_text() == "x = 1\n"
    assert not (plain / ".git").exists()
    assert any("not a git repo" in n for n in notes)


def test_submodule_and_nested_repo_are_flattened(tmp_path: Path) -> None:
    lib = make_repo(tmp_path / "lib", {"lib.txt": "lib\n"})
    host = make_repo(tmp_path / "host", {"a.txt": "a\n"})
    git(host, "-c", "protocol.file.allow=always", "submodule", "add", "-q", lib, "lib")
    git(host, "commit", "-q", "-m", "add submodule")
    write(host / "lib/untracked.txt", "u\n")
    make_repo(host / "vendor/nested", {"n.txt": "n\n"})

    run = start(host, tmp_path / "run")
    assert run.snap.state.flattened == ["lib", "vendor/nested"]
    assert (run.ws / "lib/lib.txt").read_text() == "lib\n"
    assert (run.ws / "lib/untracked.txt").read_text() == "u\n"
    assert not (run.ws / "lib/.git").exists()
    assert (run.ws / "vendor/nested/n.txt").read_text() == "n\n"

    write(run.ws / "lib/lib.txt", "lib changed\n")
    items, _ = finish(run)
    assert set(by_id(items)) == {"repo:lib/lib.txt"}
    apply_all(items)
    assert (host / "lib/lib.txt").read_text() == "lib changed\n"
    assert git(host / "lib", "status", "--porcelain") == " M lib.txt\n?? untracked.txt\n"


def test_lfs_paths_keep_raw_content_and_block_fast_forward(tmp_path: Path) -> None:
    pointer = "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 3\n"
    host = make_repo(
        tmp_path / "host",
        {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n", "big.bin": pointer},
    )
    write(host / "big.bin", b"RAW")  # the smudged file, as git-lfs leaves it

    run = start(host, tmp_path / "run")
    assert run.snap.state.lfs_paths == ["big.bin"]
    assert (run.ws / "big.bin").read_bytes() == b"RAW"
    assert git(run.ws, "ls-files", "-v", "big.bin") == "S big.bin\n"
    assert git(run.ws, "cat-file", "blob", ":big.bin") == pointer

    git(run.ws, "update-index", "--no-skip-worktree", "big.bin")
    write(run.ws / "big.bin", b"NEW")
    git(run.ws, "commit", "-q", "-a", "-m", "agent touches an LFS file")
    items, notes = finish(run)
    assert "commits:main" not in by_id(items)
    assert any("Git LFS files (big.bin)" in n for n in notes)
    assert by_id(items)["repo:big.bin"].proposed == b"NEW"


def test_new_files_matching_lfs_patterns_block_fast_forward(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host", {".gitattributes": "*.bin filter=lfs -text\n"})
    run = start(host, tmp_path / "run")
    assert run.snap.state.lfs_paths == []
    write(run.ws / "new.bin", b"RAW")
    git(run.ws, "add", "new.bin")
    git(run.ws, "commit", "-q", "-m", "agent adds a file git-lfs would track")
    items, notes = finish(run)
    assert "commits:main" not in by_id(items)
    assert any("Git LFS files (new.bin)" in n for n in notes)
    assert by_id(items)["repo:new.bin"].proposed == b"RAW"


def test_unmerged_index_warns_and_uses_head_tree(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host", {"c.txt": "base\n"})
    git(host, "checkout", "-q", "-b", "other")
    write(host / "c.txt", "other\n")
    git(host, "commit", "-q", "-a", "-m", "other")
    git(host, "checkout", "-q", "main")
    write(host / "c.txt", "main\n")
    git(host, "commit", "-q", "-a", "-m", "main")
    git(host, "merge", "-q", "other", check=False)

    run = start(host, tmp_path / "run")
    assert any(w.startswith("A merge is in progress") for w in run.snap.warnings)
    assert any("unmerged entries" in w for w in run.snap.warnings)
    index_commit = run.snap.state.index_commit
    assert index_commit is not None
    assert rev(host, f"{index_commit}^{{tree}}") == rev(host, "HEAD^{tree}")
    assert "<<<<<<<" in (run.ws / "c.txt").read_text()


def test_large_new_content_asks_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devrepo, "CONFIRM_BYTES", 10)
    host = make_repo(tmp_path / "host")
    write(host / "big.txt", "x" * 100)
    asked: list[str] = []

    def refuse(question: str) -> bool:
        asked.append(question)
        return False

    with pytest.raises(devrepo.RepoError, match="cancelled"):
        start(host, tmp_path / "no", confirm=refuse)
    assert "big.txt" in asked[0]
    assert git(host, "for-each-ref", f"refs/agenteval/{SID}/") == ""

    run = start(host, tmp_path / "yes", confirm=lambda _q: True)
    assert (run.ws / "big.txt").read_text() == "x" * 100
    run = start(host, tmp_path / "none")
    assert any("big.txt" in w for w in run.snap.warnings)


@pytest.mark.skipif(not devrepo.raw_attrs_supported(), reason="git < 2.42 has no --attr-source")
def test_files_are_copied_byte_for_byte_despite_eol_attributes(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host", {".gitattributes": "*.txt text eol=lf\n", "a.txt": "x\n"})
    write(host / "a.txt", b"one\r\ntwo\r\n")
    run = start(host, tmp_path / "run")
    assert (run.ws / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert status(run.ws) == status(host)

    write(run.ws / "a.txt", b"one\r\ntwo\r\nthree\r\n")
    items, _ = finish(run)
    item = by_id(items)["repo:a.txt"]
    assert not item.host_changed
    assert item.proposed == b"one\r\ntwo\r\nthree\r\n"


def test_script_uses_stdlib_only() -> None:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for line in SCRIPT.read_text().splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] in {"import", "from"} and not line.startswith(" "):
            assert words[1].split(".")[0] in stdlib, line


def test_import_refuses_an_existing_repo(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    data = script(
        "import",
        "--bundle",
        run.snap.bundle,
        "--meta",
        run.snap.meta,
        "--workspace",
        run.ws,
        ok=False,
    )
    assert "already a git repo" in str(data["error"])


# --- export, import and apply ------------------------------------------------------------


def test_agent_changes_apply_to_the_host(tmp_path: Path) -> None:
    host = make_repo(
        tmp_path / "host",
        {"edit.txt": "1\n2\n3\n", "gone.txt": "bye\n", "tool.sh": "echo hi\n", "keep.txt": "k\n"},
    )
    staged_before = git(host, "ls-files", "-s")
    run = start(host, tmp_path / "run")
    ws = run.ws
    write(ws / "edit.txt", "1\n2 changed\n3\n")
    write(ws / "new/dir/file.txt", "new\n")
    (ws / "gone.txt").unlink()
    (ws / "tool.sh").chmod(0o755)
    os.symlink("keep.txt", ws / "link")
    write(ws / "blob.bin", bytes(range(256)))

    items, notes = finish(run)
    found = by_id(items)
    assert set(found) == {
        "repo:edit.txt",
        "repo:new/dir/file.txt",
        "repo:gone.txt",
        "repo:tool.sh",
        "repo:link",
        "repo:blob.bin",
    }
    assert notes == []
    assert found["repo:gone.txt"].kind == "deleted"
    assert found["repo:gone.txt"].proposed is None
    assert found["repo:tool.sh"].kind == "mode"
    assert "made executable" in found["repo:tool.sh"].flags
    assert found["repo:new/dir/file.txt"].kind == "added"
    assert found["repo:new/dir/file.txt"].current is None
    assert found["repo:edit.txt"].current == b"1\n2\n3\n"
    assert found["repo:edit.txt"].proposed == b"1\n2 changed\n3\n"
    assert found["repo:blob.bin"].binary
    assert all(i.group == "repo" and not i.conflict for i in items)
    assert (run.session_dir / devrepo.PATCH_NAME).read_text().count("diff --git") == 6

    apply_all(items)
    assert tree_files(host) == tree_files(ws)
    assert os.access(host / "tool.sh", os.X_OK)
    assert os.readlink(host / "link") == "keep.txt"
    assert not (host / "gone.txt").exists()
    assert git(host, "ls-files", "-s") == staged_before
    assert "?? new/dir/file.txt\n" in status(host)


def test_edited_proposal_is_written(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(run.ws / "a.txt", "agent\n")
    items, _ = finish(run)
    by_id(items)["repo:a.txt"].apply(b"user edit\n")
    assert (host / "a.txt").read_text() == "user edit\n"


def test_names_that_look_like_diff_headers(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    fake = f":000000 100644 {'0' * 40} {'1' * 40} A"
    write(run.ws / ":x", "colon\n")
    write(run.ws / fake, "header lookalike\n")
    items, notes = finish(run)
    assert set(by_id(items)) == {"repo::x", f"repo:{fake}"}
    assert notes == []
    apply_all(items)
    assert (host / ":x").read_text() == "colon\n"
    assert (host / fake).read_text() == "header lookalike\n"


def test_apply_follows_the_umask(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(run.ws / "a.txt", "agent\n")
    write(run.ws / "tool.sh", "#!/bin/sh\n", 0o755)
    items, _ = finish(run)
    old = os.umask(0o077)
    try:
        apply_all(items)
    finally:
        os.umask(old)
    assert stat.S_IMODE((host / "a.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((host / "tool.sh").stat().st_mode) == 0o700


def test_apply_refuses_when_host_changed_during_review(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(run.ws / "a.txt", "agent\n")
    items, _ = finish(run)
    write(host / "a.txt", "user typed meanwhile\n")
    item = by_id(items)["repo:a.txt"]
    with pytest.raises(devrepo.RepoError, match="changed on the host"):
        item.apply(item.proposed)
    assert (host / "a.txt").read_text() == "user typed meanwhile\n"


def test_three_way_rules(tmp_path: Path) -> None:
    lines = "".join(f"line {i}\n" for i in range(20))
    host = make_repo(
        tmp_path / "host",
        {"merge.txt": lines, "clash.txt": lines, "same.txt": "s\n", "bin.dat": b"\0base"},
    )
    run = start(host, tmp_path / "run")
    write(run.ws / "merge.txt", lines.replace("line 1\n", "line 1 session\n"))
    write(host / "merge.txt", lines.replace("line 18\n", "line 18 host\n"))
    write(run.ws / "clash.txt", lines.replace("line 5\n", "line 5 session\n"))
    write(host / "clash.txt", lines.replace("line 5\n", "line 5 host\n"))
    write(run.ws / "same.txt", "both\n")
    write(host / "same.txt", "both\n")
    write(run.ws / "bin.dat", b"\0session")
    write(host / "bin.dat", b"\0host")

    items, _ = finish(run)
    found = by_id(items)
    assert set(found) == {"repo:merge.txt", "repo:clash.txt", "repo:bin.dat"}

    merged = found["repo:merge.txt"]
    assert merged.host_changed and not merged.conflict
    assert merged.proposed is not None
    assert b"line 1 session\n" in merged.proposed and b"line 18 host\n" in merged.proposed
    merged.apply(merged.proposed)
    assert (host / "merge.txt").read_bytes() == merged.proposed

    clash = found["repo:clash.txt"]
    assert clash.conflict and clash.host_changed
    assert clash.proposed is not None
    assert b"<<<<<<< host" in clash.proposed and b">>>>>>> session" in clash.proposed

    binary = found["repo:bin.dat"]
    assert binary.conflict and binary.binary
    assert binary.note is not None and "left alone" in binary.note
    binary.apply(binary.proposed)
    assert (host / "bin.dat").read_bytes() == b"\0host"
    conflict_copy = run.session_dir / devrepo.CONFLICTS_DIR / "bin.dat"
    assert conflict_copy.read_bytes() == b"\0session"


def test_deleted_on_host_and_in_session_conflicts(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host", {"a.txt": "a\n", "b.txt": "b\n"})
    run = start(host, tmp_path / "run")
    (host / "a.txt").unlink()
    write(run.ws / "a.txt", "session edit\n")
    write(host / "b.txt", "host edit\n")
    (run.ws / "b.txt").unlink()
    items, notes = finish(run)
    item = by_id(items)["repo:a.txt"]
    assert item.conflict and item.current is None
    item.apply(item.proposed)
    assert not (host / "a.txt").exists()
    assert (run.session_dir / devrepo.CONFLICTS_DIR / "a.txt").read_text() == "session edit\n"
    assert "repo:b.txt" not in by_id(items)
    assert any(n.startswith("b.txt: deleted in the session") for n in notes)


def test_big_untracked_file_is_left_out(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(run.ws / "huge.bin", b"x" * 2000)
    write(run.ws / "small.txt", "s\n")
    make_repo(run.ws / "cloned", {"c.txt": "c\n"})
    items, notes = finish(run, max_bytes=1000)
    assert run.session.result is not None
    assert run.session.result.excluded == ["cloned/", "huge.bin"]
    assert set(by_id(items)) == {"repo:small.txt"}
    assert any("huge.bin" in n and "cloned/" in n for n in notes)


# --- commits -----------------------------------------------------------------------------


def test_commits_fast_forward_when_branch_unmoved(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    write(host / "staged.txt", "staged on the host\n")
    git(host, "add", "staged.txt")
    run = start(host, tmp_path / "run")
    write(run.ws / "a.txt", "one\nmore\n")
    git(run.ws, "commit", "-q", "-m", "agent work", "a.txt")
    new = rev(run.ws, "HEAD")

    items, _ = finish(run)
    assert rev(host, "main") == run.snap.state.head
    ff = by_id(items)["commits:main"]
    assert ff.group == "commits" and ff.kind == "ff" and not ff.editable
    assert ff.proposed is not None and b"agent work" in ff.proposed
    assert items[0] is ff

    ff.apply(None)
    assert rev(host, "main") == new
    assert "agenteval dev: fast-forward" in git(host, "reflog", "-1", "main")
    staged = git(host, "diff", "--cached", "--name-only", "main")
    assert staged == "staged.txt\n"
    by_id(items)["repo:a.txt"].apply(b"one\nmore\n")
    assert status(host) == "A  staged.txt\n"


def test_commits_not_fast_forwarded_when_branch_moved(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(host / "b.txt", "host work\n")
    git(host, "commit", "-q", "-a", "-m", "host work")
    host_tip = rev(host, "main")
    write(run.ws / "a.txt", "agent\n")
    git(run.ws, "commit", "-q", "-a", "-m", "agent work")

    items, notes = finish(run)
    assert "commits:main" not in by_id(items)
    hint = f"merge refs/agenteval/{SID}/heads/main"
    assert any("moved on the host" in n and hint in n for n in notes)
    assert rev(host, "main") == host_tip
    assert rev(host, f"refs/agenteval/{SID}/heads/main") == rev(run.ws, "HEAD")
    item = by_id(items)["repo:a.txt"]
    item.apply(item.proposed)
    assert (host / "a.txt").read_text() == "agent\n"


def test_fast_forward_keeps_unstaged_host_edits(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(host / "a.txt", "host edit, not staged\n")
    write(run.ws / "a.txt", "agent\n")
    git(run.ws, "commit", "-q", "-a", "-m", "agent")
    items, _ = finish(run)
    by_id(items)["commits:main"].apply(None)
    assert rev(host, "main") == rev(run.ws, "HEAD")
    assert git(host, "diff", "--cached", "--name-only") == ""
    assert (host / "a.txt").read_text() == "host edit, not staged\n"


def test_no_fast_forward_over_conflicting_staged_changes(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    write(host / "a.txt", "host edit, staged\n")
    git(host, "add", "a.txt")
    write(run.ws / "a.txt", "agent\n")
    git(run.ws, "commit", "-q", "-a", "-m", "agent")
    items, notes = finish(run)
    assert "commits:main" not in by_id(items)
    assert any("staged changes that conflict" in n for n in notes)
    assert git(host, "diff", "--cached", "--name-only") == "a.txt\n"
    assert list((host / ".git").glob("index.agenteval-*")) == []


def test_fast_forward_rechecks_at_apply_time(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent")
    items, _ = finish(run)
    git(host, "commit", "-q", "--allow-empty", "-m", "host moved meanwhile")
    moved = rev(host, "main")
    with pytest.raises(devrepo.RepoError, match="moved on the host"):
        by_id(items)["commits:main"].apply(None)
    assert rev(host, "main") == moved


def test_root_commit_paths_are_checked_despite_log_show_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("[log]\n\tshowRoot = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    host = tmp_path / "host"
    host.mkdir()
    git(host, "init", "-q", "-b", "main")
    write(host / ".gitattributes", "*.bin filter=lfs -text\n")
    write(host / "big.bin", b"RAW")
    run = start(host, tmp_path / "run")
    assert run.snap.state.lfs_paths == ["big.bin"]
    git(run.ws, "add", "-A")
    git(run.ws, "commit", "-q", "-m", "first commit, with the LFS file")
    items, notes = finish(run)
    assert "commits:main" not in by_id(items)
    assert any("Git LFS files (big.bin)" in n for n in notes)


def test_session_id_that_is_not_a_valid_ref_component(tmp_path: Path) -> None:
    sid = "20261019T000000Z-a..b-abcd"  # a repo named "a..b"
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run", sid=sid)
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent")
    items, _ = finish(run)
    prefix = "refs/agenteval/20261019T000000Z-a._b-abcd"
    refs = git(host, "for-each-ref", "--format=%(refname)", "refs/agenteval/").split()
    assert refs == [f"{prefix}/{name}" for name in ("base", "final", "heads/main", "index")]
    by_id(items)["commits:main"].apply(None)
    assert rev(host, "main") == rev(run.ws, "HEAD")
    devrepo.cleanup_refs(run.session, keep_heads=False)
    assert git(host, "for-each-ref", "refs/agenteval/") == ""


def test_no_fast_forward_while_another_git_holds_the_index(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent")
    lock = host / ".git" / "index.lock"
    lock.write_text("")
    items, notes = finish(run)
    assert "commits:main" not in by_id(items)
    assert any("another git process is using the index" in n for n in notes)
    lock.unlink()
    items, _ = devrepo.repo_items(run.session, run.session_dir)
    assert "commits:main" in by_id(items)


def test_linked_worktree_round_trip(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    wt = tmp_path / "wt"
    git(host, "worktree", "add", "-q", "-b", "side", wt)
    write(wt / "a.txt", "edited in the linked worktree\n")
    run = start(wt, tmp_path / "run")
    assert run.snap.state.branch == "side"
    assert status(run.ws) == status(wt)
    git(run.ws, "commit", "-q", "-a", "-m", "agent")
    items, _ = finish(run)
    by_id(items)["commits:side"].apply(None)
    assert rev(wt, "side") == rev(run.ws, "HEAD")
    assert rev(host, "main") == run.snap.state.head
    assert status(wt) == "" and status(host) == ""


def test_fetch_never_touches_user_branches(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    git(host, "branch", "feature")
    heads_before = git(host, "for-each-ref", "refs/heads/")
    run = start(host, tmp_path / "run")
    git(run.ws, "checkout", "-q", "-b", "feature")
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent feature")
    git(run.ws, "checkout", "-q", "main")
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent main")

    items, notes = finish(run)
    assert git(host, "for-each-ref", "refs/heads/") == heads_before
    assert rev(host, f"refs/agenteval/{SID}/heads/feature") == rev(run.ws, "feature")
    assert any(f"branch feature refs/agenteval/{SID}/heads/feature" in n for n in notes)
    assert "commits:main" in by_id(items)


def test_bundle_with_dotgit_tree_entry_is_rejected(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    ws = run.ws
    blob = git(ws, "hash-object", "-w", "--stdin", stdin="evil\n").strip()
    inner = git(ws, "mktree", stdin=f"100644 blob {blob}\tconfig\n").strip()
    top = git(ws, "mktree", stdin=f"040000 tree {inner}\t.git\n100644 blob {blob}\tf.txt\n")
    bundle, export = crafted_export(run, top.strip())
    with pytest.raises(devrepo.RepoError, match="git fetch failed.*hasDotgit"):
        devrepo.import_export(run.session, run.session_dir, bundle, export)
    assert git(host, "for-each-ref", f"refs/agenteval/{SID}/final") == ""


def test_unsafe_paths_are_not_written_even_without_fsck(tmp_path: Path) -> None:
    """Old host git does not fsck bundle fetches: paths are checked again before writing."""
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    ws = run.ws
    outside = tmp_path / "outside"
    outside.mkdir()
    blob = git(ws, "hash-object", "-w", "--stdin", stdin="evil\n").strip()
    link = git(ws, "hash-object", "-w", "--stdin", stdin=str(outside)).strip()
    inner = git(ws, "mktree", stdin=f"100644 blob {blob}\tpwned\n").strip()
    entries = git(ws, "ls-tree", run.session.repo.base_tree)
    entries += f"120000 blob {link}\tx\n040000 tree {inner}\tx\n040000 tree {inner}\t.GIT\n"
    tree = git(ws, "mktree", stdin=entries).strip()
    commit = git(ws, "commit-tree", tree, "-p", run.session.repo.base_commit, "-m", "x").strip()
    git(ws, "update-ref", "refs/agenteval/final", commit)
    git(host, "fetch", "-q", ws, f"+refs/agenteval/final:refs/agenteval/{SID}/final")
    run.session.result = ResultState(final_commit=commit, final_tree=tree)

    items, notes = devrepo.repo_items(run.session, run.session_dir)
    assert any(".GIT/pwned" in n and ".git component" in n for n in notes)
    found = by_id(items)
    assert set(found) == {"repo:x", "repo:x/pwned"}
    assert "symlink pointing outside the repo" in found["repo:x"].flags
    found["repo:x"].apply(found["repo:x"].proposed)
    with pytest.raises(devrepo.UnsafePathError, match="symlink"):
        found["repo:x/pwned"].apply(found["repo:x/pwned"].proposed)
    assert list(outside.iterdir()) == []


def test_bundle_with_unsafe_branch_name_is_rejected(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    git(run.ws, "checkout", "-q", "-b", "agent-branch")
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent")
    export = script(*export_args(run))
    bundle = run.base / "out" / dev_repo.BUNDLE_NAME
    data = bundle.read_bytes()
    end = data.index(b"\n\n")  # the header lists the refs; the pack follows
    header = data[:end].replace(b"refs/heads/agent-branch", b"refs/heads/a/../../x")
    bundle.write_bytes(header + data[end:])
    with pytest.raises(devrepo.RepoError, match="unsafe branch name"):
        devrepo.import_export(run.session, run.session_dir, bundle, export)
    refs = git(host, "for-each-ref", "--format=%(refname)", f"refs/agenteval/{SID}/").split()
    assert refs == [f"refs/agenteval/{SID}/base", f"refs/agenteval/{SID}/index"]


def test_non_utf8_names_are_not_applied(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    blob = git(run.ws, "hash-object", "-w", "--stdin", stdin="x\n").strip()
    listing = git(run.ws, "ls-tree", run.session.repo.base_tree).encode()
    listing += f"100644 blob {blob}\t".encode() + b"\xff.txt\n"
    made = subprocess.run(["git", "mktree"], cwd=run.ws, input=listing, capture_output=True)
    bundle, export = crafted_export(run, made.stdout.decode().strip())
    run.session.result = devrepo.import_export(run.session, run.session_dir, bundle, export)
    items, notes = devrepo.repo_items(run.session, run.session_dir)
    assert items == []
    assert any("not UTF-8" in n for n in notes)


@pytest.mark.parametrize(
    "export",
    [
        {"final_commit": "HEAD", "final_tree": "0" * 40},
        {"final_commit": "0" * 40, "final_tree": "0" * 40, "branches": {"-x": "0" * 40}},
        {"final_commit": "0" * 40, "final_tree": "0" * 40, "head_branch": "a\x1b[2Jb"},
        {"final_commit": "0" * 40, "final_tree": "0" * 40, "excluded": "big"},
    ],
)
def test_export_json_is_validated(tmp_path: Path, export: dict[str, object]) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    with pytest.raises(devrepo.RepoError, match="export"):
        devrepo.import_export(run.session, run.session_dir, tmp_path / "missing", export)


def test_cleanup_refs(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    run = start(host, tmp_path / "run")
    git(run.ws, "checkout", "-q", "-b", "feature")
    git(run.ws, "commit", "-q", "--allow-empty", "-m", "agent")
    finish(run)

    def refs() -> set[str]:
        listing = git(host, "for-each-ref", "--format=%(refname)", f"refs/agenteval/{SID}/")
        return {ref.removeprefix(f"refs/agenteval/{SID}/") for ref in listing.split()}

    assert refs() == {"base", "index", "final", "heads/feature"}
    devrepo.cleanup_refs(run.session)
    assert refs() == {"heads/feature"}
    devrepo.cleanup_refs(run.session, keep_heads=False)
    assert refs() == set()


# --- paths -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../x",
        "a/../b",
        "a//b",
        "a/",
        "./a",
        ".git",
        ".git/config",
        ".GIT/hooks/pre-commit",
        "sub/.Git/config",
        ".git./x",
        ".git /x",
        "git~1/config",
        ".git‌/config",
        "a\0b",
        "a/\x1b[31mred",
        "x/‮evil",
        os.fsdecode(b"\xff.txt"),
    ],
)
def test_validate_path_rejects(tmp_path: Path, path: str) -> None:
    with pytest.raises(devrepo.UnsafePathError):
        devrepo.validate_path(tmp_path, path)


def test_validate_path_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", root / "link")
    with pytest.raises(devrepo.UnsafePathError, match="symlink"):
        devrepo.validate_path(root, "link/file")
    assert devrepo.validate_path(root, "link") == root / "link"
    for ok in ("a.txt", "dir/.gitignore", ".gitmodules", ".github/workflows/ci.yml", "a/.git-x"):
        assert devrepo.validate_path(root, ok) == root / ok


def test_safe_branch_name() -> None:
    for ok in ("main", "feature/x-1", "release-2.0", "fix_ü"):
        assert devrepo.safe_branch_name(ok)
    for bad in ("", "-x", "a..b", "a b", "a~1", ".hidden", "x.lock", "a/", "a@{1}", "a\x1bb"):
        assert not devrepo.safe_branch_name(bad)


def test_repo_path_flags() -> None:
    flags = devrepo.repo_path_flags
    pkg = b'{"name": "x", "scripts": {"test": "jest"}}'
    assert flags("package.json", "100644", "100644", pkg, pkg.replace(b'"x"', b'"y"')) == []
    assert flags("package.json", "100644", "100644", pkg, pkg.replace(b"jest", b"curl x|sh"))
    assert flags("web/package.json", "000000", "100644", None, pkg)
    assert flags("package.json", "100644", "100644", pkg, b"{not json")
    py = b'[project]\nname = "x"\nversion = "1"\n'
    assert flags("pyproject.toml", "100644", "100644", py, py.replace(b'"1"', b'"2"')) == []
    assert flags("pyproject.toml", "100644", "100644", py, py + b'[build-system]\nrequires=["e"]\n')
    assert flags("pyproject.toml", "100644", "100644", py, py + b'[project.scripts]\nx = "m:f"\n')
    for path in (
        "Makefile",
        "src/conftest.py",
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        "docs/CLAUDE.md",
        ".mcp.json",
        ".vscode/tasks.json",
        ".envrc",
        ".gitattributes",
        ".gitmodules",
        ".nvmrc",
        ".husky/pre-commit",
        ".pre-commit-config.yaml",
        "lefthook.yml",
        ".devcontainer/devcontainer.json",
        "setup.py",
        "justfile",
        # case variants: on a case-insensitive host they are the same files
        ".GitHub/workflows/ci.yml",
        "MAKEFILE",
        ".Claude/settings.json",
        "claude.md",
        ".VSCode/Tasks.json",
    ):
        assert flags(path, "100644", "100644", b"a", b"b"), path
    assert flags("Package.json", "000000", "100644", None, pkg)
    # unparsable is "changed"; nesting too deep for the parsers must not crash the review
    deep_json = b'{"scripts": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    assert flags("package.json", "100644", "100644", pkg, deep_json)
    deep_toml = b"x = " + b"[" * 100_000 + b"]" * 100_000 + b"\n"
    assert flags("pyproject.toml", "100644", "100644", py, deep_toml)
    assert flags("a/h", "000000", "120000", None, b"../.git/hooks") == [
        "symlink pointing into a .git directory (hooks and config run on your host)"
    ]
    assert flags("tool.sh", "000000", "100755", None, b"x") == ["new executable file"]
    assert flags("tool.sh", "100644", "100755", b"x", b"x") == ["made executable"]
    assert flags("README.md", "100644", "100644", b"a", b"b") == []
    assert flags(".vscode/extensions.json", "100644", "100644", b"a", b"b") == []
    assert flags("Makefile", "100644", "000000", b"x", None) == []
    assert flags("a/link", "000000", "120000", None, b"../../etc") == [
        "symlink pointing outside the repo"
    ]
    assert flags("a/link", "000000", "120000", None, b"/etc/passwd")
    assert flags("a/link", "000000", "120000", None, b"../b") == []


# --- discovery and live mode -------------------------------------------------------------


def test_discover(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    (host / "sub").mkdir()
    info = devrepo.discover(host / "sub")
    assert info.git and info.root == host.resolve() and info.cwd_rel == "sub"
    assert not info.linked_worktree

    git(host, "worktree", "add", "-q", tmp_path / "wt")
    info = devrepo.discover(tmp_path / "wt")
    assert info.git and info.linked_worktree
    assert info.common_dir == (host / ".git").resolve()

    plain = tmp_path / "plain"
    plain.mkdir()
    info = devrepo.discover(plain)
    assert not info.git and info.root == plain.resolve() and info.cwd_rel == ""


def test_live_hashes_and_report(tmp_path: Path) -> None:
    host = make_repo(tmp_path / "host")
    module = host / ".git" / "modules" / "vendor" / "lib"  # a submodule's git dir
    write(module / "HEAD", "ref: refs/heads/main\n")
    write(module / "config", "[core]\n")
    before = devrepo.live_hashes(host)
    assert any(key.endswith(".git/config") for key in before)
    assert any(key.endswith(".git/info/exclude") for key in before)
    assert str((module / "config").resolve()) in before

    write(host / ".git/hooks/pre-commit", "#!/bin/sh\n", 0o755)
    write(module / "hooks" / "post-checkout", "#!/bin/sh\n", 0o755)
    write(host / "Makefile", "all:\n\techo hi\n")
    write(host / "a.txt", "changed\n")
    report = devrepo.live_report(host, before)
    assert any("pre-commit was added" in line for line in report)
    assert any("lib/hooks/post-checkout was added" in line for line in report)
    assert any(line.startswith("Makefile: make runs") for line in report)
    assert not any(line.startswith("a.txt") for line in report)

    git(host, "config", "core.editor", "vim")
    report = devrepo.live_report(host, before)
    assert any(".git/config was changed" in line for line in report)
    assert any("git status was not run" in line for line in report)

    linked = tmp_path / "wt"
    git(host, "worktree", "add", "-q", linked)
    hashes = devrepo.live_hashes(linked)
    assert str((host / ".git" / "config").resolve()) in hashes
    assert str(linked / ".git") in hashes or str(linked.resolve() / ".git") in hashes
    # redirects to another git dir (and so to another config) are watched too
    worktree_dir = (host / ".git" / "worktrees" / "wt").resolve()
    assert {str(worktree_dir / "commondir"), str(worktree_dir / "gitdir")} <= set(hashes)
