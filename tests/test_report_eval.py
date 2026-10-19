"""End-to-end: a real (offline) mockllm Inspect eval rendered into a report."""

from __future__ import annotations

import json
from pathlib import Path

from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import TaskState, generate

from agenteval.config import (
    EnvironmentProfile,
    HarnessProfile,
    Limits,
    MatrixCell,
    ModelProfile,
    Pricing,
    SuiteSelection,
)
from agenteval.report import build_index, build_report


@scorer(metrics=[], name="tests_pass")
def fake_tests_pass() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        ok = str(state.sample_id).endswith("ok")
        return Score(value="C" if ok else "I", explanation="pytest exit 0" if ok else "1 failed")

    return score


def test_mockllm_eval_report(tmp_path: Path) -> None:
    cell = MatrixCell(
        run="mock-run",
        suite=SuiteSelection(name="capability"),
        model=ModelProfile(
            name="mock", inspect_model="mockllm/model", pricing=Pricing(input=1.0, output=2.0)
        ),
        harness=HarnessProfile(name="opencode-none", agent="opencode"),
        environment=EnvironmentProfile(name="docker-ubuntu", provider="docker"),
        limits=Limits(),
    )
    task = Task(
        name=cell.task_name,
        dataset=[Sample(id="t1-ok", input="fix it"), Sample(id="t2-bad", input="fix it")],
        solver=generate(),
        scorer=fake_tests_pass(),
        metadata=cell.metadata(),
    )
    log_dir = tmp_path / "logs" / "run1"
    logs = eval(task, model="mockllm/model", log_dir=str(log_dir), display="none")
    assert logs[0].status == "success"

    out = tmp_path / "reports" / "run1"
    html = build_report(log_dir, out).read_text()
    data = json.loads((out / "data.json").read_text())

    assert data["summary"]["run_name"] == "mock-run"
    assert data["summary"]["samples"] == 2
    assert data["summary"]["resolve_rate"] == 0.5
    samples = {s["id"]: s for s in data["run"]["samples"]}
    assert samples["t1-ok"]["resolved"] is True
    assert samples["t2-bad"]["resolved"] is False
    assert samples["t1-ok"]["economics"]["model_calls"] == 1
    assert samples["t1-ok"]["economics"]["cost_source"] in {"pricing", "log", "no-usage"}
    assert "opencode-none@docker-ubuntu" in html
    assert "pytest exit 0" in html

    index = build_index(tmp_path / "reports").read_text()
    assert 'href="run1/index.html"' in index
