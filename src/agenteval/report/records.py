"""Read Inspect eval logs into flat, JSON-friendly records.

Logs are grouped by the task metadata written by ``MatrixCell.metadata()``
(``metadata["agenteval"]`` -> suite/model/harness/environment). Logs without it
fall back to parsing the task name (``suite--harness--environment--model``).
"""

from __future__ import annotations

import hashlib
import logging
import shlex
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from inspect_ai.log import (
    EvalLog,
    EvalLogInfo,
    EvalSample,
    list_eval_logs,
    read_eval_log,
    read_eval_log_sample_summaries,
    read_eval_log_samples,
)
from inspect_ai.scorer import Score
from pydantic import BaseModel, Field, JsonValue

from agenteval.constants import METADATA_SANDBOX
from agenteval.report.economics import (
    EconomicsSummary,
    PricingLike,
    SampleEconomics,
    sample_economics,
    summarize,
    usage_economics,
)
from agenteval.report.scores import (
    CAPABILITY_SCORER,
    CONTAINMENT_SCORER,
    INJECTION_SCORER,
    MONITOR_SCORER,
    TEXT_CLIP,
    VULN_SCORER,
    CheckResult,
    InjectionResult,
    MonitorResult,
    as_dict,
    clip,
    parse_containment,
    parse_injection,
    parse_monitor,
    parse_vulns,
    score_float,
    str_or_none,
    tamper_flag,
    to_json,
)

logger = logging.getLogger(__name__)

SUITE_ORDER = ("containment", "injection", "capability")
AGENT_SUITES = ("injection", "capability")

_SCORER_SUITES = {
    CONTAINMENT_SCORER: "containment",
    INJECTION_SCORER: "injection",
    CAPABILITY_SCORER: "capability",
}

SampleStatus = Literal["ok", "error", "limit"]

TRACEBACK_CLIP = 12000


# --------------------------------------------------------------------------- records


class ConfigInfo(BaseModel):
    """One model x harness x environment combination (a column in the report)."""

    key: str
    label: str
    model: str
    harness: str
    environment: str
    inspect_model: str | None = None
    harness_version: str | None = None
    inner_sandbox: str | None = None
    sandbox_network: str | None = None
    provider: str | None = None
    egress: str | None = None
    pricing: dict[str, float | None] | None = None


class ScoreRecord(BaseModel):
    value: JsonValue
    answer: str | None = None
    explanation: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SampleRecord(BaseModel):
    uid: str
    task: str
    log_file: str
    suite: str
    config: str
    id: str
    epoch: int
    status: SampleStatus
    error: str | None = None
    traceback: str | None = None
    limit: str | None = None
    retries: int = 0
    scores: dict[str, ScoreRecord] = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    injection: InjectionResult | None = None
    resolved: bool | None = None
    """capability: reference tests pass; injection: the legitimate task was completed."""
    tampered: bool = False
    monitor: MonitorResult | None = None
    static_findings: int | None = None
    """New static-analysis findings in the agent's changes (None = not scanned)."""
    static_high: int | None = None
    economics: SampleEconomics = SampleEconomics()
    output: str | None = None
    view_command: str = ""
    sandbox: str | None = None
    """Container or VM name the sample ran in (joins it to the Resources readings)."""


class TaskRecord(BaseModel):
    """One eval log (one task = one suite for one config)."""

    task: str
    task_id: str
    log_file: str
    status: str
    error: str | None = None
    suite: str
    config: str
    run: str | None = None
    created: str
    started_at: str | None = None
    completed_at: str | None = None
    samples_expected: int | None = None
    samples_logged: int = 0
    packages: dict[str, str] = Field(default_factory=dict)
    discarded: EconomicsSummary | None = None
    """Usage of sample attempts in superseded logs of this task (eval_set retries)
    that are not in this log, so not in the sample totals; ``None`` when there are none."""


class DiscardedAttempt(BaseModel):
    """A sample attempt from a superseded log of a task that the newest log does not
    contain (an errored or cancelled attempt that eval_set re-ran). Its spend is real
    but is not part of any sample's numbers."""

    task: str
    task_id: str
    log_file: str
    """The superseded log the attempt is recorded in."""
    suite: str
    config: str
    id: str
    epoch: int
    error: str | None = None
    economics: SampleEconomics = SampleEconomics()
    """Tokens and cost only (read from the sample summary)."""


class Problem(BaseModel):
    level: Literal["error", "warning"]
    message: str
    log_file: str | None = None


class RunData(BaseModel):
    log_dir: str
    run_name: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    configs: list[ConfigInfo] = Field(default_factory=list)
    suites: list[str] = Field(default_factory=list)
    tasks: list[TaskRecord] = Field(default_factory=list)
    samples: list[SampleRecord] = Field(default_factory=list)
    discarded: list[DiscardedAttempt] = Field(default_factory=list)
    """Superseded sample attempts (see ``DiscardedAttempt``)."""
    problems: list[Problem] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- loading


def load_logs(log_dir: Path) -> RunData:
    """Read every eval log under ``log_dir`` (recursively) into a ``RunData``."""
    problems: list[Problem] = []
    infos: list[EvalLogInfo] = []
    if log_dir.is_dir():
        infos = list_eval_logs(str(log_dir), recursive=True, descending=False)

    headers: list[tuple[EvalLogInfo, EvalLog]] = []
    for info in infos:
        try:
            headers.append((info, read_eval_log(info, header_only=True)))
        except Exception as ex:
            message = f"unreadable log: {ex}"
            problems.append(Problem(level="error", message=message, log_file=_rel(info, log_dir)))
    latest = _latest_per_task(headers, problems, log_dir)

    identified = [(info, header, older, _identify(header)) for info, header, older in latest]
    models = {ident[1].model for _, _, _, ident in identified}
    configs: dict[str, ConfigInfo] = {}
    for _, _, _, (_, config, _) in identified:
        if len(models) > 1:
            config = config.model_copy(update={"label": f"{config.label} · {config.model}"})
        configs.setdefault(config.key, config)

    tasks: list[TaskRecord] = []
    samples: list[SampleRecord] = []
    discarded: list[DiscardedAttempt] = []
    versions: dict[str, str] = {}
    runs: list[str] = []
    for info, header, superseded, (suite, config, run) in identified:
        rel = _rel(info, log_dir)
        local = _local_path(info.name)
        view = f"inspect view --log-dir {shlex.quote(str(Path(local).parent))}"
        pricing = config.pricing
        task_samples: list[SampleRecord] = []
        kept_uuids: set[str] = set()
        kept_keys: set[tuple[str, int]] = set()
        samples_read = True
        try:
            for sample in _iter_samples(info):
                task_samples.append(
                    _sample_record(sample, header, rel, suite, config.key, pricing, view)
                )
                kept_keys.add((str(sample.id), sample.epoch))
                if sample.uuid:
                    kept_uuids.add(sample.uuid)
        except Exception as ex:
            samples_read = False
            problems.append(
                Problem(level="error", message=f"could not read samples: {ex}", log_file=rel)
            )
        task = _task_record(header, rel, suite, config.key, run, len(task_samples))
        if superseded and samples_read:
            attempts = _discarded_attempts(
                superseded, kept_uuids, kept_keys, task, pricing, log_dir=log_dir, problems=problems
            )
            discarded.extend(attempts)
            task.discarded = summarize_discarded(attempts)
            if task.discarded is not None and (
                task.discarded.usage.total or task.discarded.scoring_usage.total
            ):
                problems.append(
                    Problem(
                        level="warning",
                        message=(
                            f"task {header.eval.task}: {len(attempts)} sample attempt"
                            f"{'s' if len(attempts) != 1 else ''} in superseded logs "
                            f"(retried by eval_set) {'are' if len(attempts) != 1 else 'is'} "
                            "not in the newest log; their "
                            f"{task.discarded.usage.total:,} agent tokens "
                            f"({_usd_text(task.discarded)}) are reported as "
                            "discarded-attempt usage, not in the sample cost totals"
                        ),
                        log_file=rel,
                    )
                )
        tasks.append(task)
        samples.extend(task_samples)
        if run and run not in runs:
            runs.append(run)
        for name, version in (header.eval.packages or {}).items():
            versions.setdefault(name, str(version))
        if header.status != "success":
            message = f"task {header.eval.task} finished with status '{header.status}'"
            if header.error is not None:
                message += f": {header.error.message}"
            problems.append(Problem(level="error", message=message, log_file=rel))
        if task.samples_expected is not None and task.samples_logged < task.samples_expected:
            problems.append(
                Problem(
                    level="warning",
                    message=(
                        f"task {header.eval.task}: {task.samples_logged} of "
                        f"{task.samples_expected} samples are in the log; usage of "
                        "samples that were never written is not in the cost totals"
                    ),
                    log_file=rel,
                )
            )

    for config in configs.values():
        if config.harness_version:
            versions.setdefault(f"harness:{config.harness}", config.harness_version)

    started = [t.started_at or t.created for t in tasks]
    completed = [t.completed_at for t in tasks if t.completed_at]
    suites_present = {t.suite for t in tasks}
    return RunData(
        log_dir=str(log_dir),
        run_name=", ".join(runs) if runs else None,
        started_at=min(started) if started else None,
        completed_at=max(completed) if completed else None,
        configs=sorted(configs.values(), key=lambda c: (c.model, c.harness, c.environment)),
        suites=ordered_suites(suites_present),
        tasks=sorted(tasks, key=lambda t: (suite_rank(t.suite), t.config)),
        samples=sorted(samples, key=lambda s: (suite_rank(s.suite), s.config, s.id, s.epoch)),
        discarded=discarded,
        problems=problems,
        versions=versions,
    )


_EXCLUDE_FIELDS = {"store", "attachments"}


def _iter_samples(info: EvalLogInfo) -> Iterator[EvalSample]:
    """Stream samples one at a time; fully read logs whose header lacks sample ids."""
    try:
        stream = read_eval_log_samples(
            info, all_samples_required=False, exclude_fields=_EXCLUDE_FIELDS
        )
        first = next(stream, None)
    except RuntimeError:
        yield from read_eval_log(info, exclude_fields=_EXCLUDE_FIELDS).samples or []
        return
    if first is not None:
        yield first
        yield from stream


def _latest_per_task(
    headers: list[tuple[EvalLogInfo, EvalLog]], problems: list[Problem], log_dir: Path
) -> list[tuple[EvalLogInfo, EvalLog, list[EvalLogInfo]]]:
    """Keep the newest log per task id, with its superseded (retried) logs, oldest first."""
    by_task: dict[str, list[tuple[EvalLogInfo, EvalLog]]] = defaultdict(list)
    for info, header in headers:
        by_task[header.eval.task_id or info.name].append((info, header))
    kept: list[tuple[EvalLogInfo, EvalLog, list[EvalLogInfo]]] = []
    for entries in by_task.values():
        entries.sort(key=lambda e: e[1].eval.created)
        newest_info, newest = entries[-1]
        kept.append((newest_info, newest, [info for info, _ in entries[:-1]]))
        for info, header in entries[:-1]:
            problems.append(
                Problem(
                    level="warning",
                    message=f"superseded log for task {header.eval.task} ignored for results",
                    log_file=_rel(info, log_dir),
                )
            )
    return sorted(kept, key=lambda e: e[1].eval.created)


def _discarded_attempts(
    superseded: Sequence[EvalLogInfo],
    kept_uuids: set[str],
    kept_keys: set[tuple[str, int]],
    task: TaskRecord,
    pricing: PricingLike | None,
    *,
    log_dir: Path,
    problems: list[Problem],
) -> list[DiscardedAttempt]:
    """Sample attempts in ``superseded`` logs that the newest log does not contain.

    eval_set retries copy clean samples into the next attempt with their uuid and
    re-run the rest under a new uuid, so a superseded sample whose uuid is not in
    the newest log is spend that no reported sample accounts for. A uuid seen in
    several superseded logs (reused, then dropped) is counted once. Samples without
    a uuid (Inspect < 0.3.70) match on ``(id, epoch)`` unless they errored.
    """
    seen: set[str] = set()
    attempts: list[DiscardedAttempt] = []
    for info in superseded:
        rel = _rel(info, log_dir)
        try:
            summaries = read_eval_log_sample_summaries(info)
        except Exception as ex:
            message = f"could not read superseded log samples (usage not counted): {ex}"
            problems.append(Problem(level="warning", message=message, log_file=rel))
            continue
        for summary in summaries:
            if summary.uuid is not None:
                if summary.uuid in kept_uuids or summary.uuid in seen:
                    continue
                seen.add(summary.uuid)
            elif summary.error is None and (str(summary.id), summary.epoch) in kept_keys:
                continue
            attempts.append(
                DiscardedAttempt(
                    task=task.task,
                    task_id=task.task_id,
                    log_file=rel,
                    suite=task.suite,
                    config=task.config,
                    id=str(summary.id),
                    epoch=summary.epoch,
                    error=clip(summary.error, TEXT_CLIP) if summary.error else None,
                    economics=usage_economics(summary.model_usage, summary.role_usage, pricing),
                )
            )
    return attempts


def summarize_discarded(attempts: Sequence[DiscardedAttempt]) -> EconomicsSummary | None:
    if not attempts:
        return None
    return summarize(
        [a.economics for a in attempts], errors=sum(1 for a in attempts if a.error is not None)
    )


def _usd_text(summary: EconomicsSummary) -> str:
    if summary.cost_usd is None:
        return "cost unknown"
    text = f"${summary.cost_usd:,.4f}"
    return text if summary.cost_complete else f"at least {text}"


def _identify(header: EvalLog) -> tuple[str, ConfigInfo, str | None]:
    """Return ``(suite, config, run)`` for a log."""
    meta = header.metadata.get("agenteval") if isinstance(header.metadata, dict) else None
    task_name = header.eval.task.rsplit("/", 1)[-1]
    parts = task_name.split("--")
    if isinstance(meta, dict):
        suite_meta = as_dict(meta.get("suite"))
        model = as_dict(meta.get("model"))
        harness = as_dict(meta.get("harness"))
        env = as_dict(meta.get("environment"))
        inner = as_dict(harness.get("inner_sandbox"))
        suite = str_or_none(suite_meta.get("name")) or _suite_from_scorers(header) or "other"
        model_name = str_or_none(model.get("name")) or header.eval.model
        harness_name = str_or_none(harness.get("name")) or "unknown"
        env_name = str_or_none(env.get("name")) or "unknown"
        pricing = _pricing(model.get("pricing"))
        config = ConfigInfo(
            key=f"{harness_name}@{env_name}/{model_name}",
            label=f"{harness_name}@{env_name}",
            model=model_name,
            harness=harness_name,
            environment=env_name,
            inspect_model=str_or_none(model.get("inspect_model")) or header.eval.model,
            harness_version=str_or_none(harness.get("version")),
            inner_sandbox=str_or_none(inner.get("type")),
            sandbox_network=str_or_none(inner.get("network")),
            provider=str_or_none(env.get("provider")),
            egress=str_or_none(env.get("egress")),
            pricing=pricing,
        )
        return suite, config, str_or_none(meta.get("run"))
    if len(parts) == 4:
        suite, harness_name, env_name, model_name = parts
    else:
        suite = _suite_from_scorers(header) or task_name
        harness_name, env_name, model_name = task_name, "unknown", header.eval.model
    config = ConfigInfo(
        key=f"{harness_name}@{env_name}/{model_name}",
        label=f"{harness_name}@{env_name}",
        model=model_name,
        harness=harness_name,
        environment=env_name,
        inspect_model=header.eval.model,
    )
    return suite, config, None


def _suite_from_scorers(header: EvalLog) -> str | None:
    names = [s.name for s in header.eval.scorers or []]
    if header.results is not None:
        names += [s.name for s in header.results.scores]
    for name in names:
        if name in _SCORER_SUITES:
            return _SCORER_SUITES[name]
    return None


def _task_record(
    header: EvalLog, rel: str, suite: str, config: str, run: str | None, logged: int
) -> TaskRecord:
    expected: int | None = None
    if header.results is not None and header.results.total_samples:
        expected = header.results.total_samples
    elif header.eval.dataset.samples is not None:
        expected = header.eval.dataset.samples * (header.eval.config.epochs or 1)
    return TaskRecord(
        task=header.eval.task,
        task_id=header.eval.task_id,
        log_file=rel,
        status=header.status,
        error=header.error.message if header.error is not None else None,
        suite=suite,
        config=config,
        run=run,
        created=header.eval.created,
        started_at=header.stats.started_at or None,
        completed_at=header.stats.completed_at or None,
        samples_expected=expected,
        samples_logged=logged,
        packages={k: str(v) for k, v in (header.eval.packages or {}).items()},
    )


def _sample_record(
    sample: EvalSample,
    header: EvalLog,
    rel: str,
    suite: str,
    config: str,
    pricing: Mapping[str, float | None] | None,
    view: str,
) -> SampleRecord:
    scores = sample.scores or {}
    status: SampleStatus = "ok"
    if sample.error is not None:
        status = "error"
    elif sample.limit is not None:
        status = "limit"

    limit: str | None = None
    if sample.limit is not None:
        limit = f"{sample.limit.type} limit ({sample.limit.limit:g})"
        if sample.limit.reason:
            limit += f": {sample.limit.reason}"

    error = sample.error
    record = SampleRecord(
        uid="s" + hashlib.sha1(f"{rel}|{sample.id}|{sample.epoch}".encode()).hexdigest()[:12],
        task=header.eval.task,
        log_file=rel,
        suite=suite,
        config=config,
        id=str(sample.id),
        epoch=sample.epoch,
        status=status,
        error=clip(error.message, TEXT_CLIP) if error is not None else None,
        traceback=clip(error.traceback, TRACEBACK_CLIP) if error is not None else None,
        limit=limit,
        retries=len(sample.error_retries or []),
        scores={name: _score_record(score) for name, score in scores.items()},
        economics=sample_economics(sample, pricing),
        output=clip(sample.output.completion, TEXT_CLIP) if sample.output.choices else None,
        view_command=view,
        sandbox=str_or_none((sample.metadata or {}).get(METADATA_SANDBOX)),
    )

    if CONTAINMENT_SCORER in scores:
        record.checks = parse_containment(scores[CONTAINMENT_SCORER])
    if INJECTION_SCORER in scores:
        record.injection = parse_injection(scores[INJECTION_SCORER], sample.metadata)
        record.resolved = record.injection.task_completed
    if CAPABILITY_SCORER in scores:
        cap = scores[CAPABILITY_SCORER]
        value = score_float(cap.value)
        record.resolved = value is not None and value >= 1.0
        record.tampered = tamper_flag(cap.metadata or {})
    if MONITOR_SCORER in scores:
        record.monitor = parse_monitor(scores[MONITOR_SCORER])
    if VULN_SCORER in scores:
        record.static_findings, record.static_high = parse_vulns(scores[VULN_SCORER])
    if record.resolved is None and status == "error" and suite in AGENT_SUITES:
        record.resolved = False
    return record


def _score_record(score: Score) -> ScoreRecord:
    metadata = to_json(score.metadata or {})
    return ScoreRecord(
        value=to_json(score.value),
        answer=clip(score.answer, TEXT_CLIP) if score.answer is not None else None,
        explanation=clip(score.explanation, TEXT_CLIP) if score.explanation is not None else None,
        metadata=metadata if isinstance(metadata, dict) else {},
    )


# --------------------------------------------------------------------------- helpers


def ordered_suites(suites: Iterable[str]) -> list[str]:
    return sorted(set(suites), key=lambda s: (suite_rank(s), s))


def suite_rank(suite: str) -> int:
    return SUITE_ORDER.index(suite) if suite in SUITE_ORDER else len(SUITE_ORDER)


def _pricing(raw: object) -> dict[str, float | None] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, float | None] = {}
    for key, value in raw.items():
        out[str(key)] = float(value) if isinstance(value, int | float) else None
    return out


def _local_path(name: str) -> str:
    if name.startswith("file://"):
        return unquote(urlparse(name).path)
    return name


def _rel(info: EvalLogInfo, log_dir: Path) -> str:
    local = Path(_local_path(info.name))
    try:
        return str(local.resolve().relative_to(log_dir.resolve()))
    except ValueError:
        return str(local)
