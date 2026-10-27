"""The report's Resources section: host CPU and memory per environment.

Reads ``resources.jsonl`` (written by ``agenteval.resource_sampler.ResourceSampler`` during
the run) into per-group totals, time series for the charts, and per-configuration
figures. Per-configuration figures join per-sandbox readings (a Tart VM process, a
Docker container) to samples through the sandbox name each sample records in its
metadata.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

from agenteval.resource_sampler import AGENTEVAL, DOCKER, GROUPS, RESOURCES_FILE, SBX, TART

GROUP_LABELS = {
    DOCKER: "Docker",
    TART: "Tart VMs",
    SBX: "Docker Sandboxes",
    AGENTEVAL: "agenteval",
}
GROUP_NOTES = {
    DOCKER: (
        "Docker Desktop's VM and helpers (macOS), or the daemon, containerd and containers (Linux)"
    ),
    TART: "one Apple Virtualization process per Tart VM",
    SBX: "Docker Sandboxes' daemon and microVMs",
    AGENTEVAL: "Inspect, the model bridge and the docker / ssh / sbx calls it makes",
}
UNIT_SOURCES = {
    DOCKER: "container, inside Docker's VM",
    TART: "VM process on the host",
    SBX: "microVM process on the host",
}

MAX_POINTS = 240
"""Chart points; longer runs are averaged into this many buckets."""
GIB = 1024**3


class HostInfo(BaseModel):
    os: str | None = None
    cpus: int | None = None
    memory: int | None = None


class GroupReading(BaseModel):
    cpu: float = 0.0
    mem: int = 0


class UnitReading(BaseModel):
    group: str
    cpu: float = 0.0
    mem: int = 0
    span: float = 0.0


class OtherReading(BaseModel):
    cpu: float = 0.0


class ResourceTick(BaseModel):
    t: float
    dt: float
    groups: dict[str, GroupReading] = Field(default_factory=dict)
    other: OtherReading = OtherReading()
    units: dict[str, UnitReading] = Field(default_factory=dict)


class ResourceLog(BaseModel):
    host: HostInfo = HostInfo()
    interval: float | None = None
    providers: list[str] | None = None
    """Environment providers the run used (None: not recorded, show every group)."""
    ticks: list[ResourceTick] = Field(default_factory=list)
    skipped: int = 0
    """Unreadable lines."""


def load_resources(log_dir: Path) -> ResourceLog | None:
    """``None`` when the run has no ``resources.jsonl`` (sampling off, or an older run)."""
    path = log_dir / RESOURCES_FILE
    if not path.is_file():
        return None
    log = ResourceLog()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            kind = record.get("kind") if isinstance(record, dict) else None
            if kind == "meta":
                log.host = HostInfo.model_validate(record.get("host") or {})
                log.interval = record.get("interval")
                providers = record.get("providers")
                if isinstance(providers, list):
                    log.providers = sorted({*(log.providers or []), *map(str, providers)})
            elif kind == "tick":
                tick = ResourceTick.model_validate(record)
                if tick.dt > 0:
                    log.ticks.append(tick)
            else:
                log.skipped += 1
        except (ValueError, ValidationError, AttributeError):
            log.skipped += 1
    log.ticks.sort(key=lambda t: t.t)
    return log


# --------------------------------------------------------------------------- view


class GroupStat(BaseModel):
    group: str
    label: str
    note: str = ""
    slot: int = 0
    """Categorical colour slot (1-based), fixed per group."""
    mean_cpu: float
    peak_cpu: float
    cpu_seconds: float
    mean_mem: float | None = None
    peak_mem: int | None = None


class Area(BaseModel):
    group: str
    label: str
    slot: int
    d: str
    """SVG path of the stacked band."""


class AxisTick(BaseModel):
    pos: float
    label: str


class ChartView(BaseModel):
    title: str
    unit: str
    width: int
    height: int
    left: int
    top: int
    plot_width: int
    plot_height: int
    areas: list[Area]
    x_ticks: list[AxisTick]
    y_ticks: list[AxisTick]
    x: list[float]
    """Pixel x of each point (for the hover crosshair)."""
    values: dict[str, list[float]]
    """Per group, in ``unit``, per point (for the hover tooltip)."""
    times: list[str]


class ConfigResources(BaseModel):
    config: str
    source: str
    samples: int
    """Samples with per-sandbox readings."""
    resolved: int
    cpu_seconds_per_sample: float
    mean_cpu: float
    peak_cpu: float
    peak_mem: int
    cpu_seconds_per_resolved: float | None = None


class ResourcesView(BaseModel):
    host: HostInfo
    interval: float | None
    duration: float
    groups: list[GroupStat]
    other: GroupStat
    cpu_chart: ChartView | None = None
    mem_chart: ChartView | None = None
    configs: list[ConfigResources] = Field(default_factory=list)
    unmatched_samples: int = 0
    """Samples whose sandbox has no readings (too short for a tick, or not recorded)."""
    skipped: int = 0
    total_cpu_seconds: float = 0.0
    """All groups (not ``other``)."""
    peak_mem: int = 0
    """Highest memory of all groups together at one tick."""


class SampleLike(Protocol):
    @property
    def config(self) -> str: ...
    @property
    def sandbox(self) -> str | None: ...
    @property
    def resolved(self) -> bool | None: ...


def resources_view(
    log: ResourceLog, samples: Iterable[SampleLike], config_order: Sequence[str]
) -> ResourcesView | None:
    ticks = [_fold_unused(t, log.providers) for t in log.ticks]
    if not ticks:
        return None
    total_time = sum(t.dt for t in ticks)
    present = [g for g in GROUPS if any(g in t.groups for t in ticks)]
    groups = [_group_stat(g, ticks, total_time) for g in present]
    other = GroupStat(
        group="other",
        label="Rest of the machine",
        note="every other process on the host",
        mean_cpu=sum(t.other.cpu * t.dt for t in ticks) / total_time,
        peak_cpu=max(t.other.cpu for t in ticks),
        cpu_seconds=sum(t.other.cpu * t.dt for t in ticks),
    )
    start = ticks[0].t - ticks[0].dt
    view = ResourcesView(
        host=log.host,
        interval=log.interval,
        duration=ticks[-1].t - start,
        groups=groups,
        other=other,
        skipped=log.skipped,
        total_cpu_seconds=sum(g.cpu_seconds for g in groups),
        peak_mem=max(sum(r.mem for r in t.groups.values()) for t in ticks),
    )
    if present:
        points = _buckets(ticks, present)
        times = [p[0] - start for p in points]
        cpu = {g: [p[1][g][0] for p in points] for g in present}
        mem = {g: [p[1][g][1] / GIB for p in points] for g in present}
        view.cpu_chart = stacked_chart("Host CPU", "cores", times, cpu, present)
        view.mem_chart = stacked_chart("Host memory", "GiB", times, mem, present)
    view.configs, view.unmatched_samples = _config_resources(ticks, samples, config_order)
    return view


def _fold_unused(tick: ResourceTick, providers: Sequence[str] | None) -> ResourceTick:
    """Count groups of environments the run did not use (an idle daemon) as ``other``."""
    if providers is None:
        return tick
    unused = [g for g in tick.groups if g != AGENTEVAL and g not in providers]
    if not unused:
        return tick
    return tick.model_copy(
        update={
            "groups": {g: r for g, r in tick.groups.items() if g not in unused},
            "other": OtherReading(cpu=tick.other.cpu + sum(tick.groups[g].cpu for g in unused)),
        }
    )


def _group_stat(group: str, ticks: Sequence[ResourceTick], total_time: float) -> GroupStat:
    readings = [(t.groups.get(group, GroupReading()), t.dt) for t in ticks]
    cpu_seconds = sum(r.cpu * dt for r, dt in readings)
    return GroupStat(
        group=group,
        label=GROUP_LABELS.get(group, group),
        note=GROUP_NOTES.get(group, ""),
        slot=GROUPS.index(group) + 1,
        mean_cpu=cpu_seconds / total_time,
        peak_cpu=max(r.cpu for r, _ in readings),
        cpu_seconds=cpu_seconds,
        mean_mem=sum(r.mem * dt for r, dt in readings) / total_time,
        peak_mem=max(r.mem for r, _ in readings),
    )


def _buckets(
    ticks: Sequence[ResourceTick], groups: Sequence[str]
) -> list[tuple[float, dict[str, tuple[float, float]]]]:
    """``(time, {group: (cpu, mem)})`` per point, time-weighted means within a bucket."""
    size = max(1, math.ceil(len(ticks) / MAX_POINTS))
    points: list[tuple[float, dict[str, tuple[float, float]]]] = []
    for i in range(0, len(ticks), size):
        chunk = ticks[i : i + size]
        span = sum(t.dt for t in chunk)
        values: dict[str, tuple[float, float]] = {}
        for g in groups:
            cpu = sum(t.groups.get(g, GroupReading()).cpu * t.dt for t in chunk) / span
            mem = sum(t.groups.get(g, GroupReading()).mem * t.dt for t in chunk) / span
            values[g] = (cpu, mem)
        points.append((chunk[-1].t, values))
    return points


def _config_resources(
    ticks: Sequence[ResourceTick], samples: Iterable[SampleLike], config_order: Sequence[str]
) -> tuple[list[ConfigResources], int]:
    units: dict[str, list[UnitReading]] = defaultdict(list)
    for tick in ticks:
        for name, reading in tick.units.items():
            units[name].append(reading)
    by_config: dict[str, list[tuple[list[UnitReading], bool]]] = defaultdict(list)
    unmatched = 0
    for sample in samples:
        if not sample.sandbox:
            continue
        readings = units.get(sample.sandbox)
        if not readings:
            unmatched += 1
            continue
        by_config[sample.config].append((readings, bool(sample.resolved)))
    rows: list[ConfigResources] = []
    order = {key: i for i, key in enumerate(config_order)}
    for config in sorted(by_config, key=lambda k: (order.get(k, len(order)), k)):
        group = by_config[config]
        cpu_seconds = [sum(r.cpu * r.span for r in readings) for readings, _ in group]
        active = sum(sum(r.span for r in readings) for readings, _ in group)
        resolved = sum(1 for _, ok in group if ok)
        total = sum(cpu_seconds)
        sources = {readings[0].group for readings, _ in group}
        rows.append(
            ConfigResources(
                config=config,
                source=", ".join(UNIT_SOURCES.get(s, s) for s in sorted(sources)),
                samples=len(group),
                resolved=resolved,
                cpu_seconds_per_sample=total / len(group),
                mean_cpu=total / active if active else 0.0,
                peak_cpu=max(r.cpu for readings, _ in group for r in readings),
                peak_mem=max(r.mem for readings, _ in group for r in readings),
                cpu_seconds_per_resolved=total / resolved if resolved else None,
            )
        )
    return rows, unmatched


# --------------------------------------------------------------------------- charts

WIDTH, HEIGHT = 720, 220
LEFT, RIGHT, TOP, BOTTOM = 48, 12, 10, 24


def stacked_chart(
    title: str,
    unit: str,
    times: Sequence[float],
    values: Mapping[str, Sequence[float]],
    order: Sequence[str],
) -> ChartView:
    """Stacked area chart (first group at the bottom) as SVG paths in a fixed viewBox."""
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    end = max(times[-1], 1e-9)
    totals = [sum(values[g][i] for g in order) for i in range(len(times))]
    step, y_max = nice_scale(max(totals) if totals else 0.0)

    def x(t: float) -> float:
        return round(LEFT + plot_w * t / end, 2)

    def y(v: float) -> float:
        return round(TOP + plot_h - plot_h * v / y_max, 2)

    xs = [x(t) for t in times]
    areas: list[Area] = []
    below = [0.0] * len(times)
    for g in order:
        above = [below[i] + values[g][i] for i in range(len(times))]
        top_pts: list[tuple[float, float]]
        bottom_pts: list[tuple[float, float]]
        if len(times) == 1:  # a single point: draw a band across the plot
            top_pts = [(LEFT, y(above[0])), (LEFT + plot_w, y(above[0]))]
            bottom_pts = [(LEFT + plot_w, y(below[0])), (LEFT, y(below[0]))]
        else:
            top_pts = [(xs[i], y(above[i])) for i in range(len(times))]
            bottom_pts = [(xs[i], y(below[i])) for i in reversed(range(len(times)))]
        d = "M" + " L".join(f"{px},{py}" for px, py in [*top_pts, *bottom_pts]) + " Z"
        slot = GROUPS.index(g) + 1 if g in GROUPS else 0
        areas.append(Area(group=g, label=GROUP_LABELS.get(g, g), slot=slot, d=d))
        below = above
    y_ticks = [
        AxisTick(pos=y(v), label=_fmt_value(v))
        for v in (step * i for i in range(int(round(y_max / step)) + 1))
    ]
    x_step = nice_time_step(end)
    x_ticks = [
        AxisTick(pos=x(s), label=fmt_elapsed(s))
        for s in (x_step * i for i in range(int(end // x_step) + 1))
    ]
    return ChartView(
        title=title,
        unit=unit,
        width=WIDTH,
        height=HEIGHT,
        left=LEFT,
        top=TOP,
        plot_width=plot_w,
        plot_height=plot_h,
        areas=areas,
        x_ticks=x_ticks,
        y_ticks=y_ticks,
        x=xs,
        values={g: [round(v, 3) for v in values[g]] for g in order},
        times=[fmt_elapsed(t) for t in times],
    )


def nice_scale(peak: float, ticks: int = 4) -> tuple[float, float]:
    """``(step, max)`` with a 1/2/2.5/5 x 10^n step and max a multiple of it, >= peak."""
    if peak <= 0:
        return 1.0, 1.0
    raw = peak / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return step, step * math.ceil(peak / step - 1e-9)


_TIME_STEPS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600)


def nice_time_step(duration: float, max_ticks: int = 6) -> float:
    return float(next((s for s in _TIME_STEPS if duration / s <= max_ticks), 43200))


def fmt_elapsed(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _fmt_value(value: float) -> str:
    return f"{value:g}"
