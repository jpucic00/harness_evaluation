"""Planning a run: run ids, run files, overrides and the expanded matrix."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from agenteval.config import (
    ConfigError,
    MatrixCell,
    ModelProfile,
    RunConfig,
    SuiteName,
    expand_matrix,
    load_profiles,
    load_run,
    missing_env,
    yaml_files,
)
from agenteval.profiles import table

DEFAULT_LOG_DIR = Path("logs")
DEFAULT_REPORT_DIR = Path("reports")
SUITE_NAMES: tuple[str, ...] = get_args(SuiteName)
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
LATEST = "latest"


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def load_dotenv_file() -> Path | None:
    """Load ``.env`` (searched upward from the cwd, like Inspect) into ``os.environ``.

    Variables already set in the shell win. Returns the file loaded, or None. Values
    are never read out or printed.
    """
    from dotenv import find_dotenv, load_dotenv

    found = find_dotenv(usecwd=True)
    if not found:
        return None
    load_dotenv(found, override=False)
    return Path(found)


# --------------------------------------------------------------------------- run ids


def make_run_id(run_name: str, now: datetime | None = None) -> str:
    """``<UTC timestamp>-<run name>``; sortable and safe as a directory name."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", run_name).strip("-.") or "run"
    return f"{stamp}-{name}"[:128]


def validate_run_id(run_id: str) -> str:
    """Run ids become directory names under the log/report roots: one safe component only."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ConfigError(
            f"invalid run id {run_id!r}: use letters, digits, '.', '_' or '-' "
            "(max 128 chars, must start with a letter or digit)"
        )
    return run_id


def resolve_run_dir(root: Path, run_id: str = LATEST, *, marker: str | None = None) -> Path:
    """Directory of one run under ``root``; ``latest`` = most recently modified.

    ``marker`` restricts ``latest`` to directories containing that file.
    """
    if run_id != LATEST:
        path = root / validate_run_id(run_id)
        if not path.is_dir():
            raise ConfigError(f"run '{run_id}' not found in {root}")
        return path
    candidates = (
        [p for p in root.iterdir() if p.is_dir() and (marker is None or (p / marker).is_file())]
        if root.is_dir()
        else []
    )
    if not candidates:
        raise ConfigError(f"no runs found in {root}")

    def modified(p: Path) -> tuple[float, str]:
        return ((p / marker) if marker else p).stat().st_mtime, p.name

    return max(candidates, key=modified)


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class RunPlan:
    run: RunConfig
    run_file: Path
    cells: list[MatrixCell]
    run_id: str
    log_root: Path
    report_root: Path
    limit: int | None
    max_samples: int
    max_sandboxes: int

    @property
    def log_dir(self) -> Path:
        return self.log_root / self.run_id

    @property
    def report_dir(self) -> Path:
        return self.report_root / self.run_id

    @property
    def models(self) -> list[ModelProfile]:
        models = {cell.model.name: cell.model for cell in self.cells}
        for cell in self.cells:
            if cell.subagent_model is not None:
                models.setdefault(cell.subagent_model.name, cell.subagent_model)
        return list(models.values())

    def missing_env(self) -> list[str]:
        return missing_env(self.models)


def resolve_run_file(run_file: str | Path, config_dir: Path) -> Path:
    """Accept a path or a bare run name (``smoke-docker`` -> ``config/runs/smoke-docker.yaml``)."""
    path = Path(run_file)
    if path.is_file():
        return path
    for suffix in (".yaml", ".yml"):
        candidate = config_dir / "runs" / f"{run_file}{suffix}"
        if candidate in yaml_files(config_dir / "runs"):
            return candidate
    raise ConfigError(f"run file not found: {run_file} (also looked in {config_dir / 'runs'})")


def apply_overrides(
    run: RunConfig,
    *,
    models: Sequence[str] = (),
    harnesses: Sequence[str] = (),
    environments: Sequence[str] = (),
    suites: Sequence[str] = (),
) -> RunConfig:
    """Replace axis lists; overridden suites keep the options the run file gives them."""
    data = run.model_dump()
    if models:
        data["models"] = _dedupe(models)
    if harnesses:
        data["harnesses"] = _dedupe(harnesses)
    if environments:
        data["environments"] = _dedupe(environments)
    if suites:
        configured: dict[str, dict[str, object]] = {s.name: s.model_dump() for s in run.suites}
        data["suites"] = [configured.get(name, {"name": name}) for name in _dedupe(suites)]
    try:
        return RunConfig.model_validate(data)
    except Exception as ex:
        raise ConfigError(f"run '{run.name}': invalid override: {ex}") from ex


def plan_run(
    run_file: str | Path,
    *,
    config_dir: Path,
    models: Sequence[str] = (),
    harnesses: Sequence[str] = (),
    environments: Sequence[str] = (),
    suites: Sequence[str] = (),
    limit: int | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> RunPlan:
    path = resolve_run_file(run_file, config_dir)
    run = apply_overrides(
        load_run(path),
        models=models,
        harnesses=harnesses,
        environments=environments,
        suites=suites,
    )
    if limit is not None and limit < 1:
        raise ConfigError("--limit must be at least 1")
    cells = expand_matrix(run, load_profiles(config_dir))
    if not cells:
        raise ConfigError(f"run '{run.name}': the matrix is empty")
    # eval_set takes one value for all tasks, so use the most constrained environment:
    # a Tart host that can boot 2 VMs must not be asked for 4 because Docker could.
    concurrency = min(cell.environment.max_concurrent for cell in cells)
    return RunPlan(
        run=run,
        run_file=path,
        cells=cells,
        run_id=validate_run_id(run_id) if run_id else make_run_id(run.name, now),
        log_root=Path(run.log_dir),
        report_root=Path(run.report_dir),
        limit=limit,
        max_samples=run.max_samples or concurrency,
        max_sandboxes=concurrency,
    )


def format_plan(plan: RunPlan) -> str:
    run = plan.run
    limits = ", ".join(f"{k}={v}" for k, v in run.limits.model_dump().items() if v is not None)
    pairs = [
        ("run", f"{run.name}  ({plan.run_file})"),
        ("run id", plan.run_id),
        ("logs", str(plan.log_dir)),
        ("report", str(plan.report_dir)),
        (
            "eval_set",
            f"tasks={len(plan.cells)} epochs={run.epochs} limit={plan.limit or '-'} "
            f"max_tasks={run.max_tasks} max_samples={plan.max_samples} "
            f"max_sandboxes={plan.max_sandboxes} retry_attempts={run.retry_attempts} "
            f"fail_on_error={run.fail_on_error}",
        ),
        ("limits", limits or "-"),
    ]
    header = "\n".join(f"{key:<10}{value}" for key, value in pairs)
    rows = [
        [
            str(i),
            cell.suite.name,
            json.dumps(cell.suite.options, sort_keys=True) if cell.suite.options else "-",
            cell.model.name
            + (f" (workers: {cell.subagent_model.name})" if cell.subagent_model else ""),
            cell.harness.name,
            cell.environment.name,
            cell.task_name,
        ]
        for i, cell in enumerate(plan.cells, start=1)
    ]
    matrix = table(["#", "suite", "options", "model", "harness", "environment", "task"], rows)
    return f"{header}\n\n{matrix}"
