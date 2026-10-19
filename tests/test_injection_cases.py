"""Local injection cases in cases/injection/ (CodeIPI format)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from inspect_evals.ipi_coding_agent.constants import EXEC_CANARIES

from agenteval import constants
from agenteval.suites import injection

CASES = constants.cases_dir() / "injection"


def test_shipped_cases_load_like_codeipi_samples() -> None:
    samples = injection.load_local_cases()
    ids = [s.id for s in samples]
    assert "local-benign-01" in ids and "_template" not in ids
    benign = next(s for s in samples if s.id == "local-benign-01")
    meta = benign.metadata or {}
    assert meta["is_benign"] is True and meta["payload_category"] == "none"
    assert "src/stats.py" in meta["repo_files"]
    assert meta["verification_command"].startswith("cd /workspace")
    assert "pytest tests/" in str(benign.input)


def test_dataset_mixes_codeipi_and_local_cases() -> None:
    both = injection.codeipi_dataset(injection.InjectionOptions())
    local = injection.codeipi_dataset(injection.InjectionOptions(codeipi=False))
    upstream = injection.codeipi_dataset(injection.InjectionOptions(local=False))
    assert len(both) == len(local) + len(upstream)
    only = injection.codeipi_dataset(injection.InjectionOptions(ids=["local-benign-01"]))
    assert [s.id for s in only] == ["local-benign-01"]


def _copy_template(tmp_path: Path, name: str, **changes: object) -> Path:
    root = tmp_path / "injection"
    case = root / name
    shutil.copytree(CASES / "_template", case)
    data = yaml.safe_load((case / "case.yaml").read_text())
    data.update(changes)
    (case / "case.yaml").write_text(yaml.safe_dump(data))
    return root


def test_template_loads_and_code_execution_marker_is_registered(tmp_path: Path) -> None:
    root = _copy_template(
        tmp_path, "local-exec-01", category="code_execution", exec_marker="CANARY_EXEC_local01"
    )
    try:
        (sample,) = injection.load_local_cases(root)
        assert sample.id == "local-exec-01"
        assert EXEC_CANARIES["local-exec-01"] == "CANARY_EXEC_local01"
    finally:
        EXEC_CANARIES.pop("local-exec-01", None)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"category": "none"}, "benign cases"),
        ({"category": "code_execution"}, "exec_marker is required"),
        ({"category": "persistence"}, "persistence_targets"),
        ({"vector": "email"}, "vector"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_cases_name_the_file(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    root = _copy_template(tmp_path, "local-bad-01", **changes)
    with pytest.raises(ValueError, match="case.yaml") as info:
        injection.load_local_cases(root)
    assert message in str(info.value)


def test_case_without_repo_is_rejected(tmp_path: Path) -> None:
    root = _copy_template(tmp_path, "local-norepo-01")
    shutil.rmtree(root / "local-norepo-01" / "repo")
    with pytest.raises(ValueError, match="missing repo/"):
        injection.load_local_cases(root)
