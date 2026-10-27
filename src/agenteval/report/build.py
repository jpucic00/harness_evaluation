"""Render a run's eval logs into a self-contained HTML report (+ ``data.json``).

The HTML has no external resources (inline CSS/JS, system fonts), adapts to
light/dark mode and opens from ``file://``. All log content is HTML-escaped by
Jinja autoescaping: transcripts and evidence are untrusted agent output.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, JsonValue

from agenteval.report.records import SampleRecord, TaskRecord, load_logs
from agenteval.report.resources_view import load_resources, resources_view
from agenteval.report.scores import MONITOR_FLAGS
from agenteval.report.views import CATEGORY_LABELS, Report, Summary, build_view

REPORT_FILE = "index.html"
DATA_FILE = "data.json"
TEMPLATES = Path(__file__).parent / "templates"

_STATUS_CLASS = {
    # good
    "contained": "good",
    "pass": "good",
    "resisted": "good",
    "detected": "good",
    "ok": "good",
    "success": "good",
    "clean": "good",
    "resolved": "good",
    # bad
    "exposed": "bad",
    "fail": "bad",
    "compromised": "bad",
    "violation": "bad",
    "tampered": "bad",
    "unresolved": "bad",
    # warning
    "error": "warn",
    "uncertain": "warn",
    "limit": "warn",
    "partial": "warn",
    "started": "warn",
    "cancelled": "warn",
    "warning": "warn",
}
_STATUS_ICON = {"good": "✓", "bad": "✗", "warn": "!", "info": "•"}


def build_report(
    log_dir: Path, out_dir: Path, *, title: str | None = None, index_href: str | None = None
) -> Path:
    """Read all eval logs in ``log_dir`` and write ``index.html`` + ``data.json`` to ``out_dir``.

    ``index_href``: relative link to the all-runs index shown in the header (none if unset).
    Returns the path of the written ``index.html``.
    """
    run = load_logs(log_dir)
    generated = datetime.now(UTC).isoformat(timespec="seconds")
    if title is None:
        title = f"agenteval · {run.run_name or log_dir.name}"
    resources = None
    resource_log = load_resources(log_dir)
    if resource_log is not None:
        resources = resources_view(resource_log, run.samples, [c.key for c in run.configs])
    report = build_view(run, title=title, generated_at=generated, resources=resources)
    out_dir.mkdir(parents=True, exist_ok=True)
    html = render_report(report, index_href=index_href)
    _write_atomic(out_dir / DATA_FILE, report.model_dump_json(indent=1))
    path = out_dir / REPORT_FILE
    _write_atomic(path, html)
    return path


class RunEntry(BaseModel):
    """One run directory listed in the report index."""

    href: str
    name: str
    summary: Summary | None = None
    error: str | None = None
    sort_key: str = ""


def build_index(report_root: Path) -> Path:
    """Write ``report_root/index.html`` listing every run report below it (newest first)."""
    report_root.mkdir(parents=True, exist_ok=True)
    entries: list[RunEntry] = []
    for child in sorted(report_root.iterdir()):
        data = child / DATA_FILE
        if not child.is_dir() or not data.is_file():
            continue
        href = f"{quote(child.name)}/{REPORT_FILE}"
        try:
            raw = json.loads(data.read_text(encoding="utf-8"))
            summary = Summary.model_validate(raw["summary"])
        except Exception as ex:
            entries.append(
                RunEntry(href=href, name=child.name, error=f"unreadable data.json: {ex}")
            )
            continue
        entries.append(
            RunEntry(
                href=href,
                name=child.name,
                summary=summary,
                sort_key=summary.started_at or summary.generated_at,
            )
        )
    entries.sort(key=lambda e: (e.sort_key, e.name), reverse=True)
    html = (
        _env()
        .get_template("index.html.j2")
        .render(
            entries=entries,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            root=str(report_root),
        )
    )
    path = report_root / REPORT_FILE
    _write_atomic(path, html)
    return path


def render_report(report: Report, *, index_href: str | None = None) -> str:
    labels = {c.key: c.label for c in report.run.configs}
    configs = {c.key: c for c in report.run.configs}
    return (
        _env()
        .get_template("report.html.j2")
        .render(
            r=report,
            run=report.run,
            index_href=index_href,
            labels=labels,
            configs=configs,
            groups=sample_groups(report.run.tasks, report.run.samples),
            monitor_flags=MONITOR_FLAGS,
            category_labels=CATEGORY_LABELS,
            badges=sample_badges,
            flags=sample_flags,
        )
    )


def sample_groups(
    tasks: Sequence[TaskRecord], samples: Sequence[SampleRecord]
) -> list[tuple[TaskRecord, list[SampleRecord]]]:
    """Samples grouped under their task (log), in report order."""
    by_log: dict[str, list[SampleRecord]] = {}
    for sample in samples:
        by_log.setdefault(sample.log_file, []).append(sample)
    return [(task, by_log.get(task.log_file, [])) for task in tasks]


def sample_badges(sample: SampleRecord) -> list[tuple[str, str]]:
    """Headline ``(label, status)`` chips for a sample's summary line."""
    badges: list[tuple[str, str]] = []
    if sample.status == "error":
        badges.append(("error", "error"))
    elif sample.status == "limit":
        badges.append(("limit", "limit"))
    if sample.checks:
        exposed = sum(1 for c in sample.checks if c.scored and c.status == "exposed")
        errors = sum(1 for c in sample.checks if c.status == "error")
        if exposed:
            badges.append((f"{exposed} exposed", "exposed"))
        else:
            badges.append(("all contained", "contained"))
        if errors:
            badges.append((f"{errors} check errors", "error"))
    if sample.injection is not None:
        outcome = sample.injection.outcome
        badges.append((outcome if outcome != "benign" else "benign", outcome))
    if sample.suite in ("capability", "injection") and sample.resolved is not None:
        badges.append(
            (
                "resolved" if sample.resolved else "unresolved",
                "resolved" if sample.resolved else "unresolved",
            )
        )
    if sample.tampered:
        badges.append(("tests tampered", "tampered"))
    if sample.monitor is not None:
        if sample.monitor.any_violation:
            badges.append(("monitor violation", "violation"))
        else:
            badges.append(("monitor clean", "clean"))
    return badges


def sample_flags(sample: SampleRecord) -> str:
    """Space-separated filter tags for the drill-down filter."""
    flags: set[str] = {sample.status, sample.suite}
    if sample.status != "ok":
        flags.add("problem")
    if any(c.scored and c.status == "exposed" for c in sample.checks):
        flags.update({"exposed", "problem"})
    if sample.injection is not None and sample.injection.outcome == "compromised":
        flags.update({"compromised", "problem"})
    if sample.monitor is not None and sample.monitor.any_violation:
        flags.update({"violation", "problem"})
    if sample.resolved is False:
        flags.add("unresolved")
    return " ".join(sorted(flags))


# --------------------------------------------------------------------------- jinja


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        num=fmt_num,
        tokens=fmt_tokens,
        usd=fmt_usd,
        pct=fmt_pct,
        duration=fmt_duration,
        ts=fmt_ts,
        st=status_class,
        icon=status_icon,
        pretty=fmt_json,
        meter=meter_width,
        cores=fmt_cores,
        gib=fmt_gib,
    )
    return env


def status_class(status: str | None) -> str:
    return _STATUS_CLASS.get((status or "").lower(), "info")


def status_icon(status: str | None) -> str:
    return _STATUS_ICON[status_class(status)]


def fmt_num(value: float | int | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def fmt_tokens(value: float | int | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(v) >= limit:
            scaled = v / limit
            return f"{scaled:.1f}{suffix}" if scaled < 100 else f"{scaled:.0f}{suffix}"
    return f"{v:,.0f}"


def fmt_usd(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    if abs(value) < 1:
        return f"${value:.3f}"
    return f"${value:,.2f}"


def fmt_pct(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    pct = value * 100
    if pct == 0 or pct == 100 or pct >= 10:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def fmt_cores(value: float | None) -> str:
    """CPU in cores (1.0 = one core fully busy)."""
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.2f}" if value < 10 else f"{value:.1f}"


def fmt_gib(value: float | int | None) -> str:
    """Bytes as GiB (MiB below 1 GiB)."""
    if value is None or not math.isfinite(value):
        return "—"
    if value < 1024**3:
        return f"{value / 1024**2:,.0f} MiB"
    return f"{value / 1024**3:.2f} GiB"


def fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def fmt_json(value: JsonValue | Mapping[str, JsonValue]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def meter_width(value: float | None) -> str:
    """CSS percentage for a 0..1 rate (clamped)."""
    if value is None or not math.isfinite(value):
        return "0%"
    return f"{max(0.0, min(1.0, value)) * 100:.1f}%"


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
