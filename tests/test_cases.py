"""The case loaders in agenteval.cases resolve cases/ when called, not at import."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agenteval import cases, constants
from agenteval.suites import capability, containment, injection


def test_cases_dir_set_after_import_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = constants.cases_dir()
    shutil.copytree(
        shipped / "capability" / "cap-01-pagination", tmp_path / "capability" / "cap-01-pagination"
    )
    (tmp_path / "containment").mkdir()
    shutil.copy(shipped / "containment" / "info.kernel.py", tmp_path / "containment")
    (tmp_path / "monitor").mkdir()
    shutil.copy(shipped / "monitor" / "crontabs.yaml", tmp_path / "monitor")
    shutil.copytree(
        shipped / "injection" / "local-benign-01", tmp_path / "injection" / "local-benign-01"
    )

    monkeypatch.setenv("AGENTEVAL_CASES_DIR", str(tmp_path))

    assert [t.id for t in cases.load_capability_tasks()] == ["cap-01-pagination"]
    assert [c.id for c in cases.load_containment_checks()] == ["info.kernel"]
    assert list(cases.containment_check_files()) == ["info.kernel.py"]
    assert [g["name"] for g in cases.load_monitor_groups()] == ["crontabs"]
    assert [c.id for c in cases.load_injection_cases()] == ["local-benign-01"]

    # and through the suites that use them
    assert [t.id for t in capability.load_capability_tasks()] == ["cap-01-pagination"]
    with pytest.raises(KeyError):
        capability.get_capability_task("cap-02-word-frequency")
    assert [c.id for c in containment.containment_checks()] == ["info.kernel"]
    assert [s.id for s in injection.load_local_cases()] == ["local-benign-01"]


def test_monitor_group_rejects_bad_values(tmp_path: Path) -> None:
    bad = tmp_path / "x.yaml"
    for text, message in [
        ("title: 3\npaths: [a]\n", "'title' is required"),
        ("title: t\npriority: true\npaths: [a]\n", "'priority' must be an integer"),
        ("title: t\npaths: a\n", "'paths' must be a list of paths"),
        ("title: t\nfiles: ['']\n", "'files' must be a list of paths"),
        ("title: t\npaths: null\nfiles: null\n", "needs 'paths' or 'files'"),
        ("title: t\npath: [a]\nextra: 1\n", "unknown keys ['extra', 'path']; allowed:"),
        ("- a\n", "expected a mapping"),
    ]:
        bad.write_text(text)
        with pytest.raises(ValueError, match="x.yaml") as info:
            cases.load_monitor_groups(tmp_path)
        assert message in str(info.value), text
    bad.write_text("title: t\npaths: null\nfiles: [/etc/x]\n")
    assert cases.load_monitor_groups(tmp_path) == [
        {"name": "x", "priority": 50, "paths": [], "files": ["/etc/x"]}
    ]
