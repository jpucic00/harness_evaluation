"""Executing a planned run with ``inspect_ai.eval_set``, and building its report."""

from __future__ import annotations

import shlex
import traceback
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenteval.config import ConfigError, MatrixCell, ModelProfile
from agenteval.environments import environment_identity
from agenteval.run.plan import DEFAULT_LOG_DIR, DEFAULT_REPORT_DIR, RunPlan, load_dotenv_file

if TYPE_CHECKING:
    from inspect_ai import Task
    from inspect_ai.log import EvalLog

Echo = Callable[[str], None]


def _echo(message: str) -> None:
    print(message)


def register_model_prices(models: Sequence[ModelProfile]) -> list[str]:
    """Teach Inspect each model's price so samples carry a cost. Returns notes to print."""
    from inspect_ai.model import ModelCost, ModelInfo, get_model_info, set_model_info

    notes: list[str] = []
    for model in models:
        if model.pricing is None:
            notes.append(f"model '{model.name}': no pricing configured; cost is not tracked")
            continue
        # Unset cache prices fall back to the input price: cached tokens are never free
        # unless the profile says so.
        cost = ModelCost(
            input=model.pricing.input,
            output=model.pricing.output,
            input_cache_read=model.pricing.cache_read_rate(),
            input_cache_write=model.pricing.cache_write_rate(),
        )
        try:
            existing = get_model_info(model.inspect_model)
        except Exception:
            existing = None
        info = existing.model_copy(update={"cost": cost}) if existing else ModelInfo(cost=cost)
        try:
            set_model_info(model.inspect_model, info)
        except Exception:
            if model.inspect_model.startswith("mockllm/"):
                continue
            raise
    return notes


def build_tasks(cells: Sequence[MatrixCell]) -> list[Task]:
    from agenteval.suites import build_task

    return [build_task(cell) for cell in cells]


def _log_summary(log: EvalLog) -> str:
    samples = log.results.completed_samples if log.results else 0
    total = log.results.total_samples if log.results else 0
    line = f"  {log.status:<9} {log.eval.task}  samples {samples}/{total}"
    if log.error:
        line += f"  error: {log.error.message.splitlines()[0] if log.error.message else ''}"
    return line


INDEX_HREF = "../index.html"
"""Link from a run report (``<report-root>/<run-id>/``) back to the all-runs index."""


def build_run_report(plan: RunPlan, *, echo: Echo = _echo) -> Path | None:
    from agenteval.report import build_index, build_report

    if not plan.log_dir.is_dir():
        echo(f"no logs in {plan.log_dir}; skipping the report")
        return None
    path: Path = build_report(
        plan.log_dir,
        plan.report_dir,
        title=f"{plan.run.name} ({plan.run_id})",
        index_href=INDEX_HREF,
    )
    build_index(plan.report_root)
    return path


def report_from_logs(
    log_dir: Path,
    *,
    out: Path | None = None,
    report_root: Path = DEFAULT_REPORT_DIR,
    title: str | None = None,
) -> Path:
    """Build a report for ``log_dir``; refresh the index when it lands under ``report_root``."""
    from agenteval.report import build_index, build_report

    if not log_dir.is_dir():
        raise ConfigError(f"log directory not found: {log_dir}")
    target = out if out is not None else report_root / log_dir.resolve().name
    in_root = target.resolve().parent == report_root.resolve()
    path: Path = build_report(
        log_dir, target, title=title, index_href=INDEX_HREF if in_root else None
    )
    if in_root:
        build_index(report_root)
    return path


def run_metadata(plan: RunPlan) -> dict[str, Any]:
    environments = {cell.environment.name: cell.environment for cell in plan.cells}
    return {
        "agenteval_run_id": plan.run_id,
        "agenteval_run": plan.run.name,
        "agenteval_environments": {
            name: environment_identity(env) for name, env in environments.items()
        },
    }


def view_hint(plan: RunPlan) -> str:
    hint = f"agenteval view {plan.run_id}"
    if plan.log_root != DEFAULT_LOG_DIR:
        hint += f" --log-dir {shlex.quote(str(plan.log_root))}"
    return hint


def risk_warnings(cells: Sequence[MatrixCell]) -> list[str]:
    """Warn where an upstream scorer runs agent-written code as root in a relaxed container.

    CodeIPI's ipi_scorer runs the workspace tests as root. In a Docker cell with the bwrap
    relaxations (no gVisor runtime) container root can write global /proc/sys entries, so a
    real model there is only safe on a disposable Docker host.
    """
    warnings: list[str] = []
    for cell in cells:
        env = cell.environment
        if (
            cell.suite.name == "injection"
            and env.provider == "docker"
            and env.docker is not None
            and env.docker.runtime is None
            and cell.harness.inner_sandbox.type == "bwrap"
            and not cell.model.inspect_model.startswith("mockllm/")
        ):
            warnings.append(
                f"WARNING {cell.task_name}: CodeIPI's scorer runs agent-written tests as root in a "
                "Docker container relaxed for bubblewrap. Use a Tart environment, a gVisor runtime "
                "(docker.runtime: runsc) or a disposable Docker host for real models."
            )
    return warnings


def resource_sampler(plan: RunPlan) -> AbstractContextManager[object]:
    """Host CPU/memory sampling for the run (``resource_interval: 0`` turns it off)."""
    if plan.run.resource_interval <= 0:
        return nullcontext()
    from agenteval.resource_sampler import RESOURCES_FILE, ResourceSampler

    return ResourceSampler(
        plan.log_dir / RESOURCES_FILE,
        interval=plan.run.resource_interval,
        providers=[cell.environment.provider for cell in plan.cells],
    )


def execute_run(plan: RunPlan, *, report: bool = True, echo: Echo = _echo) -> int:
    """Run the matrix with ``inspect_ai.eval_set`` and build the report. Returns an exit code."""
    load_dotenv_file()
    missing = plan.missing_env()
    if missing:
        raise ConfigError(
            "missing required environment variables: "
            + ", ".join(missing)
            + " (set them in the shell that runs agenteval or in a .env file)"
        )

    from inspect_ai import eval_set

    for warning in risk_warnings(plan.cells):
        echo(warning)
    for note in register_model_prices(plan.models):
        echo(note)
    tasks = build_tasks(plan.cells)
    plan.log_dir.mkdir(parents=True, exist_ok=True)

    success = False
    try:
        with resource_sampler(plan):
            success, logs = eval_set(
                tasks,
                log_dir=str(plan.log_dir),
                max_tasks=plan.run.max_tasks,
                max_samples=plan.max_samples,
                max_sandboxes=plan.max_sandboxes,
                retry_attempts=plan.run.retry_attempts,
                fail_on_error=plan.run.fail_on_error,
                limit=plan.limit,
                # Keep logs of failed attempts: the report adds up their usage, which the
                # retried log does not carry.
                retry_cleanup=False,
                tags=["agenteval", plan.run.name],
                metadata=run_metadata(plan),
            )
        echo(f"eval set {'completed' if success else 'did not complete'}: {plan.log_dir}")
        for log in logs:
            echo(_log_summary(log))
    except Exception as ex:
        echo(f"eval_set failed: {type(ex).__name__}: {ex}")
        echo(traceback.format_exc())

    code = 0 if success else 1
    if report:
        try:
            path = build_run_report(plan, echo=echo)
            if path is not None:
                echo(f"report: {path}")
        except Exception as ex:
            echo(f"report build failed: {type(ex).__name__}: {ex}")
            echo(traceback.format_exc())
            code = 1
    echo(f"view transcripts: {view_hint(plan)}")
    return code
