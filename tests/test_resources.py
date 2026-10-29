"""Host resource sampling (agenteval.resource_sampler) and the report's Resources section."""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agenteval.report.build import build_report
from agenteval.report.resources_view import (
    load_resources,
    nice_scale,
    resources_view,
    stacked_chart,
)
from agenteval.resource_sampler import (
    AGENTEVAL,
    DOCKER,
    RESOURCES_FILE,
    SBX,
    TART,
    Classifier,
    Measurement,
    Proc,
    ResourceSampler,
    own_group,
    parse_cputime,
    parse_docker_stats,
    parse_ps,
    parse_size,
    vm_owner,
)

VZ = (
    "/System/Library/Frameworks/Virtualization.framework/Versions/A/XPCServices/"
    "com.apple.Virtualization.VirtualMachine.xpc/Contents/MacOS/"
    "com.apple.Virtualization.VirtualMachine"
)


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("0:00.79", 0.79),
        ("771:51.41", 771 * 60 + 51.41),
        ("00:01:23", 83),
        ("1-02:03:04", 86400 + 2 * 3600 + 3 * 60 + 4),
    ],
)
def test_parse_cputime_macos_and_linux(text: str, seconds: float) -> None:
    assert parse_cputime(text) == pytest.approx(seconds)


def test_parse_cputime_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_cputime("n/a")


def test_parse_ps_keeps_spaces_in_commands_and_skips_bad_lines() -> None:
    text = (
        "  335     1    800   0:00.79 /Library/PrivilegedHelperTools/com.docker.vmnetd\n"
        "53712 53689 150512   7:42.03 /Applications/Docker.app/Contents/MacOS/Docker Desktop.app\n"
        "garbage line\n"
    )
    procs = parse_ps(text)
    assert set(procs) == {335, 53712}
    assert procs[53712].rss == 150512 * 1024
    assert procs[53712].cpu == pytest.approx(462.03)
    assert procs[53712].command.endswith("MacOS/Docker Desktop.app")


def test_parse_size_units() -> None:
    assert parse_size("12.5MiB") == int(12.5 * 1024**2)
    assert parse_size("1.5GB") == int(1.5e9)
    assert parse_size("0B") == 0
    assert parse_size("lots") is None


def test_parse_docker_stats_keeps_inspect_containers_only() -> None:
    rows = [
        {
            "Name": "inspect-capability-iabc123-default-1",
            "CPUPerc": "150.5%",
            "MemUsage": "512MiB / 7.6GiB",
        },
        {"Name": "my-postgres", "CPUPerc": "1%", "MemUsage": "100MiB / 7.6GiB"},
    ]
    units = parse_docker_stats("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert units == {
        "inspect-capability-iabc123-default-1": {"cpu": pytest.approx(1.505), "mem": 512 * 1024**2}
    }


def test_vm_owner_from_open_files() -> None:
    assert vm_owner("p1\nn/Users/me/.tart/vms/agenteval-1a2b3c4d/disk.img\n") == (
        TART,
        "agenteval-1a2b3c4d",
    )
    assert vm_owner(
        "n/Users/me/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw"
    ) == (
        DOCKER,
        None,
    )
    assert vm_owner("n/Users/me/Library/Application Support/com.docker.sandboxes/vm/disk") == (
        SBX,
        None,
    )
    assert vm_owner("n/Applications/UTM.app/disk.qcow2") is None


def test_own_group_by_command() -> None:
    def group(command: str) -> str | None:
        return own_group(Proc(10, 1, 0, 0.0, command))

    assert group("/Applications/Docker.app/Contents/MacOS/com.docker.backend") == DOCKER
    assert group("/usr/bin/dockerd -H fd://") == DOCKER
    assert group("/usr/bin/containerd-shim-runc-v2 -namespace moby -id abc") == DOCKER
    assert group("/opt/homebrew/bin/sbx daemon start") == SBX
    assert group("/opt/homebrew/Caskroom/sbx/0.49.2/Sbx.app/Contents/MacOS/llmman serve") == SBX
    assert group("/opt/sbx/Sbx.app/Contents/Helpers/containerd-shim-nerdbox-v1 -id x") == SBX
    assert group("/usr/libexec/sandboxd") is None  # macOS's own App Sandbox daemon
    assert group("/usr/local/bin/docker exec inspect-x") is None  # the CLI is the caller's cost
    assert group("/Applications/Safari.app/Contents/MacOS/Safari") is None


# --------------------------------------------------------------------------- classification


def _procs(*rows: tuple[int, int, str, float, int]) -> dict[int, Proc]:
    return {pid: Proc(pid, ppid, rss, cpu, cmd) for pid, ppid, cmd, cpu, rss in rows}


def test_classifier_groups_descendants_vms_and_shims() -> None:
    procs = _procs(
        (100, 1, "python agenteval run", 0.0, 1000),
        (101, 100, "/usr/local/bin/docker exec inspect-x", 0.0, 10),
        (102, 101, "/opt/homebrew/bin/sbx exec y", 0.0, 10),  # ours, despite sbx in the name
        (200, 1, VZ, 0.0, 5000),
        (300, 1, "/usr/bin/containerd-shim-runc-v2 -id abc", 0.0, 10),
        (301, 300, "node /usr/local/bin/opencode", 0.0, 400),
        (400, 1, "/usr/bin/zsh", 0.0, 10),
    )
    classifier = Classifier(procs, 100, lambda p: (TART, "agenteval-1a2b3c4d"))
    assert classifier.group(100) == (AGENTEVAL, None)
    assert classifier.group(101) == (AGENTEVAL, None)
    assert classifier.group(102) == (AGENTEVAL, None)
    assert classifier.group(200) == (TART, "agenteval-1a2b3c4d")
    assert classifier.group(301) == (DOCKER, None)
    assert classifier.group(400) is None


def test_classifier_survives_parent_cycles() -> None:
    procs = _procs((5, 6, "a", 0.0, 0), (6, 5, "b", 0.0, 0))
    assert Classifier(procs, 1, lambda p: None).group(5) is None


# --------------------------------------------------------------------------- ticks


def _tick(
    m: Measurement,
    procs: dict[int, Proc],
    wall: float,
    base: float,
    *,
    exclude: frozenset[int] = frozenset(),
    docker_units: dict[str, dict[str, float]] | None = None,
    docker_span: float | None = None,
) -> dict[str, Any] | None:
    return m.tick(
        procs,
        wall=wall,
        base_cpu=base,
        vm_lookup=lambda p: (TART, "vm-a"),
        exclude=exclude,
        docker_units=docker_units,
        docker_span=docker_span,
    )


def test_first_tick_is_a_baseline() -> None:
    m = Measurement(self_pid=100)
    assert _tick(m, _procs((100, 1, "python", 1.0, 1000)), 10.0, 1.0) is None


def test_tick_rates_groups_units_and_other() -> None:
    m = Measurement(self_pid=100)
    first = _procs(
        (100, 1, "python", 1.0, 1000),
        (200, 1, VZ, 50.0, 4096),
        (400, 1, "/usr/bin/zsh", 3.0, 10),
    )
    second = _procs(
        (100, 1, "python", 1.5, 2000),
        (200, 1, VZ, 54.0, 8192),
        (400, 1, "/usr/bin/zsh", 4.0, 10),
    )
    _tick(m, first, 10.0, 1.0)
    record = _tick(m, second, 12.0, 2.0)
    assert record is not None
    assert record["dt"] == 2.0
    assert record["groups"][TART] == {"cpu": 2.0, "mem": 8192}
    assert record["groups"][AGENTEVAL] == {"cpu": 0.5, "mem": 2000}
    assert record["other"] == {"cpu": 0.5}
    assert record["units"] == {"vm-a": {"group": TART, "cpu": 2.0, "mem": 8192, "span": 2.0}}


def test_tick_does_not_double_count_a_child_that_exited() -> None:
    m = Measurement(self_pid=100)
    _tick(m, _procs((100, 1, "python", 0.0, 0), (101, 100, "tart run vm", 1.0, 0)), 0.0, 0.0)
    # the child used 1 s more while alive
    rec = _tick(m, _procs((100, 1, "python", 0.0, 0), (101, 100, "tart run vm", 2.0, 0)), 1.0, 0.0)
    assert rec is not None and rec["groups"][AGENTEVAL]["cpu"] == 1.0
    # it exited after 0.5 s more and was reaped: its whole 2.5 s lands in RUSAGE_CHILDREN
    rec = _tick(m, _procs((100, 1, "python", 0.0, 0)), 2.0, 2.5)
    assert rec is not None and rec["groups"][AGENTEVAL]["cpu"] == pytest.approx(0.5)


def test_tick_skips_the_samplers_ps_and_passes_docker_units() -> None:
    m = Measurement(self_pid=100)
    procs = _procs((100, 1, "python", 0.0, 0), (150, 100, "ps -A", 0.0, 99999))
    _tick(m, procs, 0.0, 0.0, exclude=frozenset({150}))
    rec = _tick(
        m,
        procs,
        5.0,
        0.0,
        exclude=frozenset({150}),
        docker_units={"inspect-a-i1-default-1": {"cpu": 0.25, "mem": 1024.0}},
        docker_span=10.0,
    )
    assert rec is not None
    assert AGENTEVAL not in rec["groups"] or rec["groups"][AGENTEVAL]["mem"] == 0
    assert rec["units"]["inspect-a-i1-default-1"] == {
        "group": DOCKER,
        "cpu": 0.25,
        "mem": 1024,
        "span": 10.0,
    }


def test_sampler_writes_meta_and_ticks(tmp_path: Path) -> None:
    path = tmp_path / RESOURCES_FILE
    with ResourceSampler(path, interval=0.2, providers=["tart"]):
        time.sleep(0.7)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["kind"] == "meta"
    assert records[0]["providers"] == ["tart"]
    assert records[0]["host"]["cpus"]
    ticks = [r for r in records if r["kind"] == "tick"]
    assert ticks and all(t["dt"] > 0 for t in ticks)
    assert any(AGENTEVAL in t["groups"] for t in ticks)


def test_resource_sampler_off_when_interval_is_zero() -> None:
    from agenteval.run.execute import resource_sampler
    from agenteval.run.plan import RunPlan

    plan = cast(RunPlan, SimpleNamespace(run=SimpleNamespace(resource_interval=0.0)))
    assert isinstance(resource_sampler(plan), nullcontext)


# --------------------------------------------------------------------------- report


def _write_log(path: Path, ticks: list[dict[str, object]], providers: list[str] | None) -> None:
    meta: dict[str, object] = {"kind": "meta", "version": 1, "interval": 2.0}
    meta["host"] = {"os": "darwin", "cpus": 10, "memory": 24 * 1024**3}
    if providers is not None:
        meta["providers"] = providers
    lines = [json.dumps(meta), *(json.dumps(t) for t in ticks), "{broken"]
    path.write_text("\n".join(lines) + "\n")


def _ticks() -> list[dict[str, object]]:
    unit = {"group": TART, "cpu": 1.0, "mem": 2 * 1024**3, "span": 2.0}
    return [
        {
            "kind": "tick",
            "t": 100.0 + 2 * i,
            "dt": 2.0,
            "groups": {
                TART: {"cpu": 1.0 + i, "mem": 2 * 1024**3},
                SBX: {"cpu": 0.5, "mem": 1024},
                AGENTEVAL: {"cpu": 0.5, "mem": 1024**3},
            },
            "other": {"cpu": 1.0},
            "units": {"agenteval-1a2b3c4d": unit},
        }
        for i in range(3)
    ]


@dataclass
class _Sample:
    config: str
    sandbox: str | None
    resolved: bool | None


def test_resources_view_totals_folding_and_per_config(tmp_path: Path) -> None:
    _write_log(tmp_path / RESOURCES_FILE, _ticks(), providers=["tart"])
    log = load_resources(tmp_path)
    assert log is not None and log.skipped == 1 and len(log.ticks) == 3
    samples = [
        _Sample("h@tart-ubuntu/m", "agenteval-1a2b3c4d", True),
        _Sample("h@tart-ubuntu/m", "agenteval-gone0000", False),
        _Sample("h@tart-ubuntu/m", None, None),
    ]
    view = resources_view(log, samples, ["h@tart-ubuntu/m"])
    assert view is not None
    # sbx was not part of the run: its CPU is "rest of the machine"
    assert [g.group for g in view.groups] == [TART, AGENTEVAL]
    assert view.other.mean_cpu == pytest.approx(1.5)
    tart = view.groups[0]
    assert tart.cpu_seconds == pytest.approx((1 + 2 + 3) * 2.0)
    assert tart.peak_cpu == 3.0
    assert view.duration == pytest.approx(6.0)
    assert view.peak_mem == 3 * 1024**3
    assert view.unmatched_samples == 1
    (row,) = view.configs
    assert row.samples == 1 and row.resolved == 1
    assert row.cpu_seconds_per_sample == pytest.approx(6.0)
    assert row.cpu_seconds_per_resolved == pytest.approx(6.0)
    assert row.peak_mem == 2 * 1024**3
    assert row.source == "VM process on the host"
    assert view.cpu_chart is not None and [a.group for a in view.cpu_chart.areas] == [
        TART,
        AGENTEVAL,
    ]


def test_resources_view_without_providers_shows_every_group(tmp_path: Path) -> None:
    _write_log(tmp_path / RESOURCES_FILE, _ticks(), providers=None)
    log = load_resources(tmp_path)
    assert log is not None
    view = resources_view(log, [], [])
    assert view is not None and [g.group for g in view.groups] == [TART, SBX, AGENTEVAL]


def test_load_resources_none_without_file(tmp_path: Path) -> None:
    assert load_resources(tmp_path) is None


def test_nice_scale() -> None:
    assert nice_scale(6.28) == (2.0, 8.0)
    assert nice_scale(0.0) == (1.0, 1.0)
    assert nice_scale(10.0) == (2.5, 10.0)


def test_stacked_chart_bands_stack_in_order() -> None:
    chart = stacked_chart(
        "CPU", "cores", [2.0, 4.0], {TART: [1.0, 1.0], AGENTEVAL: [1.0, 1.0]}, [TART, AGENTEVAL]
    )
    assert [a.slot for a in chart.areas] == [2, 4]
    assert len(chart.x) == 2 and chart.times == ["0:02", "0:04"]
    assert chart.y_ticks[-1].label == "2"


def test_report_renders_resources_section(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_log(logs / RESOURCES_FILE, _ticks(), providers=["tart"])
    page = build_report(logs, tmp_path / "out", title="t")
    html = page.read_text()
    assert 'id="resources"' in html and "Host CPU" in html and "Tart VMs" in html
    data = json.loads((tmp_path / "out" / "data.json").read_text())
    assert data["summary"]["host_cpu_seconds"] == pytest.approx(12.0 + 3.0)


def test_report_without_resources_has_no_section(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    html = build_report(logs, tmp_path / "out", title="t").read_text()
    assert 'id="resources"' not in html
