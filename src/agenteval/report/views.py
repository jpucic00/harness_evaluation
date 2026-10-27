"""Aggregate a run's records into the report's views (one per section)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from agenteval.report.economics import EconomicsSummary, rate, summarize
from agenteval.report.records import (
    AGENT_SUITES,
    RunData,
    SampleRecord,
    ordered_suites,
    summarize_discarded,
)
from agenteval.report.resources_view import ResourcesView
from agenteval.report.scores import MONITOR_FLAGS, CheckResult

CellStatus = Literal["contained", "exposed", "error", "info", "missing"]
_CELL_PRIORITY: tuple[CellStatus, ...] = ("exposed", "error", "contained", "info", "missing")

KNOWN_CHECKS = (
    "fs.secret_home",
    "fs.secret_system",
    "env.ambient_secret",
    "fs.write_home",
    "fs.write_tmp",
    "fs.write_system",
    "fs.workspace_write",
    "net.collector",
    "net.internet_tcp",
    "net.dns",
    "net.host_gateway",
    "priv.sudo",
    "priv.docker_socket",
    "priv.shadow_read",
    "proc.other_users_visible",
    "host.shared_mounts",
    "info.kernel",
)
CATEGORY_LABELS = {
    "fs": "Filesystem",
    "env": "Environment",
    "net": "Network",
    "priv": "Privilege",
    "proc": "Processes",
    "host": "Host",
    "info": "Informational",
}
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


# --------------------------------------------------------------------------- views


class CheckCell(BaseModel):
    status: CellStatus
    counts: dict[str, int] = Field(default_factory=dict)
    n: int = 0
    evidence: str = ""
    sample_uid: str | None = None


class CheckRow(BaseModel):
    id: str
    category: str
    severity: str
    scored: bool
    cells: dict[str, CheckCell]
    differs: bool


class CheckCategory(BaseModel):
    category: str
    label: str
    rows: list[CheckRow]


class ContainmentColumn(BaseModel):
    config: str
    exposed: int = 0
    contained: int = 0
    errors: int = 0
    scored: int = 0
    sample_error: str | None = None


class ContainmentView(BaseModel):
    columns: list[ContainmentColumn]
    categories: list[CheckCategory]
    differing_rows: int


class InjectionStats(BaseModel):
    samples: int = 0
    errors: int = 0
    injected: int = 0
    compromised: int = 0
    uncertain: int = 0
    resisted: int = 0
    detected: int = 0
    benign: int = 0
    false_positives: int = 0
    completed: int = 0
    scored: int = 0
    asr: float | None = None
    """Attack success rate: compromised / injected samples with a verdict."""
    detection_rate: float | None = None
    completion_rate: float | None = None
    false_positive_rate: float | None = None


class BreakdownRow(BaseModel):
    key: str
    cells: dict[str, InjectionStats]
    total: InjectionStats


class InjectionView(BaseModel):
    configs: list[str]
    per_config: dict[str, InjectionStats]
    overall: InjectionStats
    by_category: list[BreakdownRow]
    by_vector: list[BreakdownRow]


class CapabilityStats(BaseModel):
    samples: int = 0
    resolved: int = 0
    failed: int = 0
    errors: int = 0
    limits: int = 0
    tampered: int = 0
    rate: float | None = None
    """resolved / samples (errored samples count as unresolved)."""


class CapabilityCell(BaseModel):
    status: Literal["pass", "fail", "partial", "error", "missing"]
    passed: int = 0
    n: int = 0
    sample_uid: str | None = None


class CapabilityTaskRow(BaseModel):
    id: str
    cells: dict[str, CapabilityCell]
    differs: bool


class CapabilityView(BaseModel):
    configs: list[str]
    per_config: dict[str, CapabilityStats]
    overall: CapabilityStats
    tasks: list[CapabilityTaskRow]


class MonitorRow(BaseModel):
    suite: str
    config: str
    samples: int
    monitored: int
    counts: dict[str, int]
    any_violation: int


class MonitorView(BaseModel):
    flags: list[tuple[str, str]]
    rows: list[MonitorRow]
    monitored: int
    violations: int


class EconomicsRow(BaseModel):
    suite: str
    config: str
    summary: EconomicsSummary
    static_findings: int | None = None
    """New static-analysis findings summed over scanned samples (None = none scanned)."""
    static_high: int = 0
    static_scanned: int = 0
    discarded: EconomicsSummary | None = None
    """Superseded sample attempts of this group (not in ``summary``)."""


class EconomicsView(BaseModel):
    rows: list[EconomicsRow]
    static_scan: bool = False
    """Any row has generated-vulnerability scan results."""
    per_config: dict[str, EconomicsSummary]
    overall: EconomicsSummary
    scoring_excluded: bool
    discarded: EconomicsSummary | None = None
    """All superseded sample attempts of the run (not in ``overall``)."""
    run_total: EconomicsSummary | None = None
    """``overall`` plus ``discarded``: everything the run spent. Set when there are
    discarded attempts."""


class ScoreboardRow(BaseModel):
    config: str
    samples: int
    errors: int
    containment_exposed: int | None = None
    containment_scored: int | None = None
    asr: float | None = None
    injected: int = 0
    resolve_rate: float | None = None
    capability_samples: int = 0
    violations: int | None = None
    monitored: int = 0
    cost_usd: float | None = None
    cost_complete: bool = True
    tokens: int = 0


class Summary(BaseModel):
    """Headline numbers; also what the run index reads from ``data.json``."""

    title: str
    run_name: str | None
    started_at: str | None
    generated_at: str
    configs: list[str]
    suites: list[str]
    samples: int
    errors: int
    limits: int
    problems: int
    cost_usd: float | None
    cost_complete: bool
    total_tokens: int
    containment_exposed: int | None = None
    containment_scored: int | None = None
    asr: float | None = None
    injected: int = 0
    resolve_rate: float | None = None
    capability_samples: int = 0
    violations: int | None = None
    monitored: int = 0
    discarded_attempts: int = 0
    """Sample attempts in superseded logs (eval_set retries) not in the totals above."""
    discarded_tokens: int = 0
    discarded_cost_usd: float | None = None
    discarded_cost_complete: bool = True
    run_cost_usd: float | None = None
    """Cost including discarded attempts (equals ``cost_usd`` when there are none)."""
    run_cost_complete: bool = True
    host_cpu_seconds: float | None = None
    """CPU time the run's environments and agenteval used on the host (Resources)."""
    host_peak_mem: int | None = None
    """Their highest memory at one moment, in bytes."""


class Report(BaseModel):
    schema_version: int = 1
    title: str
    generated_at: str
    summary: Summary
    run: RunData
    containment: ContainmentView | None = None
    injection: InjectionView | None = None
    capability: CapabilityView | None = None
    monitor: MonitorView | None = None
    economics: EconomicsView
    scoreboard: list[ScoreboardRow]
    resources: ResourcesView | None = None


def build_view(
    run: RunData, *, title: str, generated_at: str, resources: ResourcesView | None = None
) -> Report:
    containment = containment_view(run)
    injection = injection_view(run)
    capability = capability_view(run)
    monitor = monitor_view(run)
    economics = economics_view(run)
    scoreboard = scoreboard_rows(run, containment, injection, capability, monitor)
    labels = {c.key: c.label for c in run.configs}
    summary = Summary(
        title=title,
        run_name=run.run_name,
        started_at=run.started_at,
        generated_at=generated_at,
        configs=[labels[c.key] for c in run.configs],
        suites=run.suites,
        samples=len(run.samples),
        errors=sum(1 for s in run.samples if s.status == "error"),
        limits=sum(1 for s in run.samples if s.status == "limit"),
        problems=len(run.problems),
        cost_usd=economics.overall.cost_usd,
        cost_complete=economics.overall.cost_complete,
        total_tokens=economics.overall.usage.total,
    )
    run_total = economics.run_total or economics.overall
    summary.run_cost_usd = run_total.cost_usd
    summary.run_cost_complete = run_total.cost_complete
    if economics.discarded is not None:
        summary.discarded_attempts = economics.discarded.samples
        summary.discarded_tokens = economics.discarded.usage.total
        summary.discarded_cost_usd = economics.discarded.cost_usd
        summary.discarded_cost_complete = economics.discarded.cost_complete
    if containment is not None:
        summary.containment_exposed = sum(c.exposed for c in containment.columns)
        summary.containment_scored = sum(c.scored for c in containment.columns)
    if injection is not None:
        summary.asr = injection.overall.asr
        summary.injected = injection.overall.injected
    if capability is not None:
        summary.resolve_rate = capability.overall.rate
        summary.capability_samples = capability.overall.samples
    if monitor is not None:
        summary.violations = monitor.violations
        summary.monitored = monitor.monitored
    if resources is not None:
        summary.host_cpu_seconds = resources.total_cpu_seconds
        summary.host_peak_mem = resources.peak_mem
    return Report(
        title=title,
        generated_at=generated_at,
        summary=summary,
        run=run,
        containment=containment,
        injection=injection,
        capability=capability,
        monitor=monitor,
        economics=economics,
        scoreboard=scoreboard,
        resources=resources,
    )


def containment_view(run: RunData) -> ContainmentView | None:
    samples = [s for s in run.samples if s.suite == "containment"]
    if not samples:
        return None
    configs = _configs_in(run, samples)
    by_config: dict[str, list[SampleRecord]] = defaultdict(list)
    for sample in samples:
        by_config[sample.config].append(sample)

    meta: dict[str, CheckResult] = {}
    for sample in samples:
        for check in sample.checks:
            known = meta.get(check.id)
            if known is None or (known.severity == "unknown" and check.severity != "unknown"):
                meta[check.id] = check

    columns: list[ContainmentColumn] = []
    rows: list[CheckRow] = []
    for config in configs:
        errored = [s for s in by_config[config] if not s.checks]
        columns.append(
            ContainmentColumn(
                config=config,
                sample_error=(errored[0].error or "no containment score") if errored else None,
            )
        )

    for check_id in sorted(meta, key=_check_sort_key(meta)):
        info = meta[check_id]
        cells: dict[str, CheckCell] = {}
        for column in columns:
            cell = _check_cell(check_id, by_config[column.config])
            cells[column.config] = cell
            if info.scored and cell.status in ("exposed", "contained"):
                column.scored += 1
                if cell.status == "exposed":
                    column.exposed += 1
                else:
                    column.contained += 1
            elif cell.status == "error":
                column.errors += 1
        # an errored or missing cell is not a containment difference
        present = {c.status for c in cells.values() if c.status not in ("missing", "error")}
        rows.append(
            CheckRow(
                id=check_id,
                category=info.category,
                severity=info.severity,
                scored=info.scored,
                cells=cells,
                differs=len(present) > 1,
            )
        )

    categories: list[CheckCategory] = []
    for row in rows:
        if not categories or categories[-1].category != row.category:
            categories.append(
                CheckCategory(
                    category=row.category,
                    label=CATEGORY_LABELS.get(row.category, row.category.title()),
                    rows=[],
                )
            )
        categories[-1].rows.append(row)
    return ContainmentView(
        columns=columns, categories=categories, differing_rows=sum(r.differs for r in rows)
    )


def _check_cell(check_id: str, samples: Sequence[SampleRecord]) -> CheckCell:
    counts: Counter[str] = Counter()
    evidence = ""
    uid: str | None = None
    for sample in samples:
        match = next((c for c in sample.checks if c.id == check_id), None)
        if match is None:
            counts["error" if sample.status == "error" else "missing"] += 1
            if sample.status == "error":
                evidence = evidence or (sample.error or "")
                uid = uid or sample.uid
            continue
        counts[match.status] += 1
        if match.status == "exposed" or not evidence:
            evidence = match.evidence or evidence
            uid = sample.uid
    status: CellStatus = "missing"
    for candidate in _CELL_PRIORITY:
        if counts.get(candidate):
            status = candidate
            break
    present = {k: v for k, v in counts.items() if k != "missing"}
    return CheckCell(
        status=status, counts=present, n=sum(present.values()), evidence=evidence, sample_uid=uid
    )


def _check_sort_key(
    meta: Mapping[str, CheckResult],
) -> Callable[[str], tuple[int, str, int, int, str]]:
    categories = list(CATEGORY_LABELS)

    def key(check_id: str) -> tuple[int, str, int, int, str]:
        info = meta[check_id]
        cat_rank = categories.index(info.category) if info.category in categories else 99
        known = KNOWN_CHECKS.index(check_id) if check_id in KNOWN_CHECKS else 999
        sev = SEVERITY_ORDER.index(info.severity) if info.severity in SEVERITY_ORDER else 99
        return (cat_rank, info.category, known, sev, check_id)

    return key


def injection_view(run: RunData) -> InjectionView | None:
    samples = [s for s in run.samples if s.suite == "injection"]
    if not samples:
        return None
    configs = _configs_in(run, samples)
    per_config = {c: injection_stats([s for s in samples if s.config == c]) for c in configs}

    def breakdown(attr: Literal["category", "vector"]) -> list[BreakdownRow]:
        injected = [s for s in samples if s.injection and not s.injection.is_benign]
        keys = sorted({str(getattr(s.injection, attr)) for s in injected})
        rows: list[BreakdownRow] = []
        for key in keys:
            group = [s for s in injected if str(getattr(s.injection, attr)) == key]
            cells = {c: injection_stats([s for s in group if s.config == c]) for c in configs}
            rows.append(BreakdownRow(key=key, cells=cells, total=injection_stats(group)))
        return rows

    return InjectionView(
        configs=configs,
        per_config=per_config,
        overall=injection_stats(samples),
        by_category=breakdown("category"),
        by_vector=breakdown("vector"),
    )


def injection_stats(samples: Iterable[SampleRecord]) -> InjectionStats:
    stats = InjectionStats()
    for sample in samples:
        stats.samples += 1
        if sample.status == "error":
            stats.errors += 1
        result = sample.injection
        if result is None:
            continue
        stats.scored += 1
        stats.completed += result.task_completed
        if result.is_benign:
            stats.benign += 1
            stats.false_positives += result.false_positive
            continue
        stats.injected += 1
        if result.outcome == "compromised":
            stats.compromised += 1
        elif result.outcome == "uncertain":
            stats.uncertain += 1
        else:
            stats.resisted += 1
            stats.detected += result.outcome == "detected"
    verdicts = stats.injected - stats.uncertain
    stats.asr = rate(stats.compromised, verdicts)
    stats.detection_rate = rate(stats.detected, stats.injected)
    stats.completion_rate = rate(stats.completed, stats.scored)
    stats.false_positive_rate = rate(stats.false_positives, stats.benign)
    return stats


def capability_view(run: RunData) -> CapabilityView | None:
    samples = [s for s in run.samples if s.suite == "capability"]
    if not samples:
        return None
    configs = _configs_in(run, samples)
    per_config = {c: capability_stats([s for s in samples if s.config == c]) for c in configs}
    rows: list[CapabilityTaskRow] = []
    for task_id in sorted({s.id for s in samples}, key=_natural_key):
        cells: dict[str, CapabilityCell] = {}
        for config in configs:
            group = [s for s in samples if s.config == config and s.id == task_id]
            cells[config] = _capability_cell(group)
        present = {c.status for c in cells.values() if c.status != "missing"}
        rows.append(CapabilityTaskRow(id=task_id, cells=cells, differs=len(present) > 1))
    return CapabilityView(
        configs=configs, per_config=per_config, overall=capability_stats(samples), tasks=rows
    )


def capability_stats(samples: Sequence[SampleRecord]) -> CapabilityStats:
    stats = CapabilityStats(samples=len(samples))
    for sample in samples:
        if sample.resolved:
            stats.resolved += 1
        elif sample.status == "error":
            stats.errors += 1
        else:
            stats.failed += 1
        stats.limits += sample.status == "limit"
        stats.tampered += sample.tampered
    stats.rate = rate(stats.resolved, stats.samples)
    return stats


def _capability_cell(group: Sequence[SampleRecord]) -> CapabilityCell:
    if not group:
        return CapabilityCell(status="missing")
    passed = sum(1 for s in group if s.resolved)
    errors = sum(1 for s in group if s.status == "error" and not s.resolved)
    status: Literal["pass", "fail", "partial", "error", "missing"]
    if passed == len(group):
        status = "pass"
    elif passed:
        status = "partial"
    elif errors == len(group):
        status = "error"
    else:
        status = "fail"
    return CapabilityCell(status=status, passed=passed, n=len(group), sample_uid=group[0].uid)


def monitor_view(run: RunData) -> MonitorView | None:
    samples = [s for s in run.samples if s.monitor is not None or s.suite in AGENT_SUITES]
    if not any(s.monitor is not None for s in samples):
        return None
    rows: list[MonitorRow] = []
    for suite in ordered_suites({s.suite for s in samples}):
        suite_samples = [s for s in samples if s.suite == suite]
        for config in _configs_in(run, suite_samples):
            group = [s for s in suite_samples if s.config == config]
            monitored = [s.monitor for s in group if s.monitor is not None]
            rows.append(
                MonitorRow(
                    suite=suite,
                    config=config,
                    samples=len(group),
                    monitored=len(monitored),
                    counts={
                        k: sum(1 for m in monitored if m.flags.get(k)) for k, _ in MONITOR_FLAGS
                    },
                    any_violation=sum(1 for m in monitored if m.any_violation),
                )
            )
    return MonitorView(
        flags=list(MONITOR_FLAGS),
        rows=rows,
        monitored=sum(r.monitored for r in rows),
        violations=sum(r.any_violation for r in rows),
    )


def economics_view(run: RunData) -> EconomicsView:
    rows: list[EconomicsRow] = []
    agent_samples = [s for s in run.samples if s.suite != "containment"]
    agent_discarded = [d for d in run.discarded if d.suite != "containment"]
    suites = {s.suite for s in agent_samples} | {d.suite for d in agent_discarded}
    for suite in ordered_suites(suites):
        suite_samples = [s for s in agent_samples if s.suite == suite]
        suite_discarded = [d for d in agent_discarded if d.suite == suite]
        keys = {s.config for s in suite_samples} | {d.config for d in suite_discarded}
        for config in _config_order(run, keys):
            group = [s for s in suite_samples if s.config == config]
            scanned = [s for s in group if s.static_findings is not None]
            rows.append(
                EconomicsRow(
                    suite=suite,
                    config=config,
                    summary=_summarize(group),
                    static_findings=(
                        sum(s.static_findings or 0 for s in scanned) if scanned else None
                    ),
                    static_high=sum(s.static_high or 0 for s in scanned),
                    static_scanned=len(scanned),
                    discarded=summarize_discarded(
                        [d for d in suite_discarded if d.config == config]
                    ),
                )
            )
    per_config = {
        c.key: _summarize([s for s in agent_samples if s.config == c.key])
        for c in run.configs
        if any(s.config == c.key for s in agent_samples)
    }
    overall = _summarize(run.samples)
    run_total: EconomicsSummary | None = None
    if run.discarded:
        run_total = summarize(
            [s.economics for s in run.samples] + [d.economics for d in run.discarded],
            resolved=overall.resolved,
            errors=overall.errors + sum(1 for d in run.discarded if d.error is not None),
        )
    return EconomicsView(
        rows=rows,
        static_scan=any(r.static_scanned for r in rows),
        per_config=per_config,
        overall=overall,
        scoring_excluded=overall.scoring_usage.total > 0,
        discarded=summarize_discarded(run.discarded),
        run_total=run_total,
    )


def _summarize(samples: Sequence[SampleRecord]) -> EconomicsSummary:
    return summarize(
        [s.economics for s in samples],
        resolved=sum(1 for s in samples if s.resolved),
        errors=sum(1 for s in samples if s.status == "error"),
    )


def scoreboard_rows(
    run: RunData,
    containment: ContainmentView | None,
    injection: InjectionView | None,
    capability: CapabilityView | None,
    monitor: MonitorView | None,
) -> list[ScoreboardRow]:
    rows: list[ScoreboardRow] = []
    for config in run.configs:
        samples = [s for s in run.samples if s.config == config.key]
        econ = _summarize(samples)
        row = ScoreboardRow(
            config=config.key,
            samples=len(samples),
            errors=sum(1 for s in samples if s.status == "error"),
            cost_usd=econ.cost_usd,
            cost_complete=econ.cost_complete,
            tokens=econ.usage.total,
        )
        if containment is not None:
            column = next((c for c in containment.columns if c.config == config.key), None)
            if column is not None:
                row.containment_exposed = column.exposed
                row.containment_scored = column.scored
        if injection is not None and config.key in injection.per_config:
            stats = injection.per_config[config.key]
            row.asr = stats.asr
            row.injected = stats.injected
        if capability is not None and config.key in capability.per_config:
            cap = capability.per_config[config.key]
            row.resolve_rate = cap.rate
            row.capability_samples = cap.samples
        if monitor is not None:
            mine = [r for r in monitor.rows if r.config == config.key]
            if mine:
                row.violations = sum(r.any_violation for r in mine)
                row.monitored = sum(r.monitored for r in mine)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- helpers


def _configs_in(run: RunData, samples: Sequence[SampleRecord]) -> list[str]:
    return _config_order(run, {s.config for s in samples})


def _config_order(run: RunData, keys: set[str]) -> list[str]:
    return [c.key for c in run.configs if c.key in keys]


def _natural_key(text: str) -> tuple[str, ...]:
    return tuple(part.zfill(8) if part.isdigit() else part for part in _split_digits(text))


def _split_digits(text: str) -> list[str]:
    parts: list[str] = []
    current = ""
    for ch in text:
        if current and ch.isdigit() != current[-1].isdigit():
            parts.append(current)
            current = ""
        current += ch
    if current:
        parts.append(current)
    return parts
