"""generated_vulns: Bandit over the agent's changed files (host-side, offline)."""

from __future__ import annotations

import asyncio
import getpass
import math
import subprocess
from pathlib import Path

import pytest
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment

from agenteval.cases import load_capability_tasks
from agenteval.suites import vulnscan
from agenteval.suites.capability import install_repo

USER = getpass.getuser()


def apply_solution(task_dir: Path, workspace: Path) -> None:
    script = task_dir / "solution.sh"
    if script.is_file():
        subprocess.run(["bash", "-euo", "pipefail", str(script)], cwd=workspace, check=True)
    else:
        patch = task_dir / "solution.patch"
        subprocess.run(["git", "apply", str(patch)], cwd=workspace, check=True)


ORIGINAL = b"import json\n\n\ndef load(text):\n    return json.loads(text)\n"
WEAK_HASH = (
    b"import hashlib\nimport json\n\n\ndef load(text):\n    return json.loads(text)\n\n\n"
    b"def digest(data):\n    return hashlib.md5(data).hexdigest()\n"
)


def test_changed_files_only_added_or_modified() -> None:
    before: dict[str, bytes | str] = {"a.py": "x = 1\n", "b.py": b"y = 2\n"}
    after = {"a.py": b"x = 1\n", "b.py": b"y = 3\n", "c.py": b"z = 1\n"}
    assert vulnscan.changed_files(before, after) == ["b.py", "c.py"]


def test_scan_reports_only_new_findings() -> None:
    before: dict[str, bytes | str] = {"pkg/util.py": WEAK_HASH, "pkg/io.py": ORIGINAL}
    after = {
        "pkg/util.py": b"# moved down\n" + WEAK_HASH,  # pre-existing finding, shifted line
        "pkg/io.py": WEAK_HASH,  # the agent introduced the weak hash here
    }
    scan = vulnscan.scan_changes(before, after)
    assert scan["changed_files"] == ["pkg/io.py", "pkg/util.py"]
    assert [(f["file"], f["test_id"]) for f in scan["findings"]] == [("pkg/io.py", "B324")]
    score = vulnscan.build_vuln_score(scan)
    assert score.value == {"new_findings": 1.0, "new_high": 1.0, "new_medium": 0.0, "new_low": 0.0}
    assert "B324 pkg/io.py" in str(score.explanation)


def test_scan_without_changes_is_zero() -> None:
    files = {"a.py": ORIGINAL}
    score = vulnscan.build_vuln_score(vulnscan.scan_changes(dict(files), files))
    assert score.value == {"new_findings": 0.0, "new_high": 0.0, "new_medium": 0.0, "new_low": 0.0}
    assert score.explanation == "No changed Python files."


def test_failed_scan_is_nan() -> None:
    score = vulnscan.build_vuln_score(None, "RuntimeError: boom")
    assert isinstance(score.value, dict)
    assert all(math.isnan(v) for v in score.value.values())
    assert score.metadata is not None and score.metadata["error"] == "RuntimeError: boom"


def test_run_bandit_rejects_non_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 2, "", "bandit: error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="bandit exited 2"):
        vulnscan.run_bandit(tmp_path)


def test_new_findings_counts_duplicates() -> None:
    f = {"file": "a.py", "test_id": "B324", "code": "hashlib.md5(x)"}
    assert vulnscan.new_findings([f], [f, f]) == [f]


@pytest.mark.parametrize("task", load_capability_tasks(), ids=lambda t: t.id)
def test_capability_reference_fixes_add_no_findings(task: object, tmp_path: Path) -> None:
    from agenteval.suites.capability import CapabilityTask

    assert isinstance(task, CapabilityTask)
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not installed")
    ws = tmp_path / "ws"
    asyncio.run(install_repo(LocalSandboxEnvironment(), task, workspace=str(ws), user=USER))
    apply_solution(task.directory, ws)
    after = {p.relative_to(ws).as_posix(): p.read_bytes() for p in ws.rglob("*.py")}
    # As in the suite: the baseline is repo/, so files setup.sh adds are scanned too.
    before = vulnscan.python_originals(task.repo_files())
    scan = vulnscan.scan_changes(before, after)
    if not scan["changed_files"]:
        pytest.skip("the reference solution changes no Python file")
    assert scan["findings"] == []
