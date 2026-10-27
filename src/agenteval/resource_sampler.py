"""Host CPU and memory used by a run's environments, sampled while it runs.

``ResourceSampler`` runs next to ``eval_set`` in a thread and appends one JSON line per
tick to ``<log_dir>/resources.jsonl``. Each tick reads every host process with ``ps``
and sorts the ones the run causes into groups:

- ``agenteval``: this process and everything it starts (Inspect, the model bridge's
  ``docker exec`` / ``ssh`` / ``sbx exec`` calls, ``tart run``). Children that exit
  between ticks are counted through ``getrusage``, so short exec calls are not missed.
  The sampler's own CPU (its thread and its ``ps`` / ``lsof`` / ``docker stats`` calls)
  is left out.
- ``docker``: Docker Desktop's VM and helper processes (macOS), or the Docker daemon,
  containerd and everything under the container shims (Linux).
- ``tart``: Tart's Apple Virtualization VM processes, matched by the disk image each
  holds open.
- ``sbx``: Docker Sandboxes' daemon and VMs.

The CPU of every other process is summed as ``other`` (how busy the rest of the
machine was). Units are per-sandbox readings that the report joins to samples: each
Tart VM process, and each Docker container from ``docker stats`` (what the container
used inside Docker's VM).

CPU is in cores (1.0 = one core busy for the whole interval); memory is resident set
size in bytes. A VM process's RSS is the guest memory it has touched, which Apple's
virtualization does not hand back when the guest frees it.
"""

from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RESOURCES_FILE = "resources.jsonl"
FORMAT_VERSION = 1

AGENTEVAL = "agenteval"
DOCKER = "docker"
TART = "tart"
SBX = "sbx"
GROUPS = (DOCKER, TART, SBX, AGENTEVAL)
"""Fixed order: the report stacks and colours groups by it."""

PS_ARGV = ["ps", "-A", "-o", "pid=,ppid=,rss=,time=,command="]
DOCKER_STATS_ARGV = ["docker", "stats", "--no-stream", "--format", "{{json .}}"]
DOCKER_CONTAINER_PREFIX = "inspect-"
"""Inspect's compose projects (and so their containers) are named ``inspect-<task>-i<id>``."""

VZ_MARKER = "com.apple.Virtualization.VirtualMachine"
_TART_DISK = re.compile(r"/\.tart/vms/([^/]+)/")
_CPUTIME = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")
_SIZE = re.compile(r"^\s*([\d.]+)\s*([A-Za-z]*)\s*$")
_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    rss: int
    """Bytes."""
    cpu: float
    """Cumulative CPU seconds (user + system) of the process itself."""
    command: str


def parse_cputime(text: str) -> float:
    """``ps`` cumulative CPU time: macOS ``MMM:SS.ss``, Linux ``[DD-]HH:MM:SS``."""
    match = _CPUTIME.match(text.strip())
    if match is None:
        raise ValueError(f"unrecognised CPU time: {text!r}")
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def parse_ps(text: str) -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss_kb = int(parts[0]), int(parts[1]), int(parts[2])
            cpu = parse_cputime(parts[3])
        except ValueError:
            continue
        procs[pid] = Proc(pid, ppid, rss_kb * 1024, cpu, parts[4] if len(parts) > 4 else "")
    return procs


def parse_size(text: str) -> int | None:
    """``docker stats`` sizes such as ``12.3MiB`` or ``1.5GB``."""
    match = _SIZE.match(text)
    if match is None:
        return None
    factor = _SIZE_UNITS.get(match.group(2).lower())
    if factor is None:
        return None
    return int(float(match.group(1)) * factor)


def parse_docker_stats(text: str) -> dict[str, dict[str, float]]:
    """Per-container ``{"cpu": cores, "mem": bytes}`` for Inspect's containers."""
    units: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "")
        if not name.startswith(DOCKER_CONTAINER_PREFIX):
            continue
        try:
            cpu = float(str(row.get("CPUPerc", "")).rstrip("%")) / 100
        except ValueError:
            continue
        mem = parse_size(str(row.get("MemUsage", "")).split("/")[0])
        units[name] = {"cpu": cpu, "mem": float(mem or 0)}
    return units


def vm_owner(open_files: str) -> tuple[str, str | None] | None:
    """Which environment an Apple Virtualization VM process belongs to, from ``lsof -Fn``."""
    for line in open_files.splitlines():
        path = line[1:] if line.startswith("n") else line
        tart = _TART_DISK.search(path)
        if tart is not None:
            return TART, tart.group(1)
        if "com.docker.sandboxes" in path:
            return SBX, None
        if "com.docker.docker" in path or path.endswith("/Docker.raw"):
            return DOCKER, None
    return None


def own_group(proc: Proc) -> str | None:
    """Group from the process's own command line (not its ancestors)."""
    command = proc.command
    executable = command.split(None, 1)[0] if command else ""
    name = executable.rsplit("/", 1)[-1]
    # "sbx daemon start" plus helpers from the Sbx.app bundle; each microVM runs inside
    # a containerd-shim-nerdbox-v1 process. (/usr/libexec/sandboxd is macOS's own.)
    if "/Sbx.app/" in command or name == "sbx" or "nerdbox" in name:
        return SBX
    if "/Docker.app/" in command or name.startswith("com.docker."):
        return DOCKER
    if name in ("dockerd", "containerd", "docker-proxy") or name.startswith("containerd-shim"):
        return DOCKER
    return None


class Classifier:
    """Assign every process of one ``ps`` snapshot to a group (or None = other)."""

    def __init__(
        self,
        procs: Mapping[int, Proc],
        self_pid: int,
        vm_lookup: Callable[[Proc], tuple[str, str | None] | None],
    ) -> None:
        self._procs = procs
        self._self_pid = self_pid
        self._vm_lookup = vm_lookup
        self._groups: dict[int, tuple[str, str | None] | None] = {}

    def ours(self, pid: int) -> bool:
        """``pid`` is this process or one of its descendants."""
        seen: set[int] = set()
        while pid not in seen:
            if pid == self._self_pid:
                return True
            seen.add(pid)
            proc = self._procs.get(pid)
            if proc is None or proc.ppid in (0, pid):
                return False
            pid = proc.ppid
        return False

    def group(
        self, pid: int, _visiting: frozenset[int] = frozenset()
    ) -> tuple[str, str | None] | None:
        """``(group, unit)``: unit is the Tart VM name for VM processes, else None."""
        if pid not in self._groups:
            self._groups[pid] = self._resolve(pid, _visiting | {pid})
        return self._groups[pid]

    def _resolve(self, pid: int, visiting: frozenset[int]) -> tuple[str, str | None] | None:
        proc = self._procs.get(pid)
        if proc is None:
            return None
        if self.ours(pid):
            return AGENTEVAL, None
        if VZ_MARKER in proc.command:
            return self._vm_lookup(proc)
        own = own_group(proc)
        if own is not None:
            return own, None
        if proc.ppid in (0, 1) or proc.ppid in visiting:
            return None
        parent = self.group(proc.ppid, visiting)
        # a process under a container shim or VM helper belongs to that environment
        return (parent[0], None) if parent is not None else None


@dataclass
class _Totals:
    cpu: float = 0.0
    mem: int = 0


@dataclass
class Measurement:
    """State carried between ticks, and the arithmetic of one tick."""

    self_pid: int
    prev_wall: float | None = None
    prev_cpu: dict[int, float] = field(default_factory=dict)
    prev_ours: set[int] = field(default_factory=set)
    prev_base: float | None = None

    def tick(
        self,
        procs: Mapping[int, Proc],
        *,
        wall: float,
        base_cpu: float,
        vm_lookup: Callable[[Proc], tuple[str, str | None] | None],
        exclude: frozenset[int] = frozenset(),
        docker_units: Mapping[str, Mapping[str, float]] | None = None,
        docker_span: float | None = None,
    ) -> dict[str, Any] | None:
        """One record, or None for the first (baseline) tick.

        ``base_cpu``: cumulative CPU of this process plus its reaped children, without
        the sampler's own share. ``exclude``: the sampler's ``ps`` process.
        """
        classifier = Classifier(procs, self.self_pid, vm_lookup)
        groups = {name: _Totals() for name in GROUPS}
        other = 0.0
        units: dict[str, dict[str, Any]] = {}
        ours: set[int] = set()
        cpu_now: dict[int, float] = {}
        dt = wall - self.prev_wall if self.prev_wall is not None else None

        for pid, proc in procs.items():
            if pid in exclude:
                continue
            cpu_now[pid] = proc.cpu
            assigned = classifier.group(pid)
            name = assigned[0] if assigned else None
            if name == AGENTEVAL:
                ours.add(pid)
                groups[AGENTEVAL].mem += proc.rss
                continue  # CPU comes from base_cpu and the live-descendant deltas below
            delta = self._delta(pid, proc.cpu)
            if name is None:
                other += delta
                continue
            groups[name].cpu += delta
            groups[name].mem += proc.rss
            if assigned is not None and assigned[1] is not None and dt:
                unit = units.setdefault(assigned[1], {"group": name, "cpu": 0.0, "mem": 0})
                unit["cpu"] += delta / dt
                unit["mem"] += proc.rss
                unit["span"] = dt

        # this process's own CPU is in base_cpu; children while alive by their deltas,
        # and once reaped by base_cpu (minus what their live deltas already counted)
        descendants = ours - {self.self_pid}
        live = sum(self._delta(pid, cpu_now[pid]) for pid in descendants)
        vanished = sum(
            self.prev_cpu.get(pid, 0.0) for pid in self.prev_ours - ours - {self.self_pid}
        )
        if self.prev_base is not None:
            groups[AGENTEVAL].cpu = max(0.0, base_cpu - self.prev_base + live - vanished)

        first = self.prev_wall is None
        self.prev_wall = wall
        self.prev_cpu = cpu_now
        self.prev_ours = ours
        self.prev_base = base_cpu
        if first or not dt or dt <= 0:
            return None

        for name, reading in (docker_units or {}).items():
            units[name] = {"group": DOCKER, **reading, "span": docker_span or dt}
        return {
            "kind": "tick",
            "t": round(wall, 3),
            "dt": round(dt, 3),
            "groups": {
                name: {"cpu": round(g.cpu / dt, 4), "mem": g.mem}
                for name, g in groups.items()
                if g.cpu or g.mem
            },
            "other": {"cpu": round(other / dt, 4)},
            "units": {
                name: {
                    "group": u["group"],
                    "cpu": round(float(u["cpu"]), 4),
                    "mem": int(u["mem"]),
                    "span": round(float(u["span"]), 3),
                }
                for name, u in units.items()
            },
        }

    def _delta(self, pid: int, cpu: float) -> float:
        """CPU since the previous tick; a process new since then counts from its start."""
        if self.prev_wall is None:
            return 0.0
        return max(0.0, cpu - self.prev_cpu.get(pid, 0.0))


def host_info() -> dict[str, Any]:
    memory: int | None = None
    try:
        memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        pass
    return {"os": sys.platform, "cpus": os.cpu_count(), "memory": memory}


class ResourceSampler:
    """Context manager: samples in a background thread between ``__enter__`` and ``__exit__``.

    Never raises into the run: a failing tick is skipped and the sampler keeps going.
    """

    def __init__(
        self,
        path: Path,
        *,
        interval: float,
        providers: Sequence[str] = (),
        docker_interval: float = 10.0,
    ) -> None:
        self.path = path
        self.interval = interval
        self.providers = sorted(set(providers))
        """Environment providers the run uses; the report folds other groups into ``other``."""
        self.docker_units = DOCKER in self.providers
        self.docker_interval = docker_interval
        self._measure = Measurement(self_pid=os.getpid())
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._own_cpu = 0.0
        self._own_pid: int | None = None
        self._vm_cache: dict[tuple[int, str], tuple[str, str | None] | None] = {}
        self._docker: tuple[dict[str, dict[str, float]], float] | None = None
        """Latest unconsumed ``docker stats`` reading and the seconds it covers."""
        self._file: Any = None

    def __enter__(self) -> ResourceSampler:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._write(
            {
                "kind": "meta",
                "version": FORMAT_VERSION,
                "started": datetime.now(UTC).isoformat(timespec="seconds"),
                "interval": self.interval,
                "providers": self.providers,
                "host": host_info(),
            }
        )
        self._start(self._loop, "agenteval-resources")
        if self.docker_units:
            # docker stats takes seconds per call; its own thread keeps the ticks even
            self._start(self._docker_loop, "agenteval-resources-docker")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=60)
        if self._file is not None:
            self._file.close()

    def _start(self, target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _loop(self) -> None:
        deadline = time.monotonic()
        while True:
            try:
                self._sample()
            except Exception:  # never take the run down; the gap shows in the charts
                pass
            if self._stop.is_set():
                return
            deadline += self.interval
            if self._stop.wait(max(0.0, deadline - time.monotonic())):
                try:  # a last reading so the tail of the run is in the file
                    self._sample()
                except Exception:
                    pass
                return

    def _docker_loop(self) -> None:
        last: float | None = None
        while not self._stop.is_set():
            try:
                units = parse_docker_stats(self._own_command(DOCKER_STATS_ARGV, timeout=30))
                now = time.monotonic()
                if last is not None:
                    with self._lock:
                        self._docker = (units, now - last)
                last = now
            except Exception:
                pass
            self._stop.wait(self.docker_interval)

    def _sample(self) -> None:
        with self._lock:
            docker, self._docker = self._docker, None
        text = self._own_command(PS_ARGV, timeout=30, remember_pid=True)
        record = self._measure.tick(
            parse_ps(text),
            wall=time.time(),
            base_cpu=self._base_cpu(),
            vm_lookup=self._vm_lookup,
            exclude=frozenset({self._own_pid} if self._own_pid else ()),
            docker_units=docker[0] if docker else None,
            docker_span=docker[1] if docker else None,
        )
        if record is not None:
            self._write(record)

    def _base_cpu(self) -> float:
        # time.thread_time() is only this thread's; the docker thread's Python work is tiny
        with self._lock:
            me = resource.getrusage(resource.RUSAGE_SELF)
            children = resource.getrusage(resource.RUSAGE_CHILDREN)
            own = self._own_cpu
        total = me.ru_utime + me.ru_stime + children.ru_utime + children.ru_stime
        return total - time.thread_time() - own

    def _vm_lookup(self, proc: Proc) -> tuple[str, str | None] | None:
        key = (proc.pid, proc.command)
        if key not in self._vm_cache:
            files = self._own_command(["lsof", "-Fn", "-p", str(proc.pid)], timeout=30)
            self._vm_cache[key] = vm_owner(files)
        return self._vm_cache[key]

    def _own_command(self, argv: list[str], *, timeout: float, remember_pid: bool = False) -> str:
        """Run one of the sampler's own commands and keep its CPU out of agenteval's share.

        Reaped with ``wait4`` for the child's exact CPU usage; a reaped child also lands
        in ``RUSAGE_CHILDREN``, which ``_base_cpu`` takes it back out of.
        """
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError:
            return ""
        if remember_pid:
            self._own_pid = proc.pid
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        out = b""
        try:
            assert proc.stdout is not None
            out = proc.stdout.read()
        finally:
            timer.cancel()
            if proc.stdout is not None:
                proc.stdout.close()
            try:
                # under the lock so _base_cpu never sees the child in RUSAGE_CHILDREN
                # without its share in _own_cpu
                with self._lock:
                    _, status, usage = os.wait4(proc.pid, 0)
                    self._own_cpu += usage.ru_utime + usage.ru_stime
                proc.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                proc.returncode = -1
        return out.decode(errors="replace") if proc.returncode == 0 else ""

    def _write(self, record: Mapping[str, Any]) -> None:
        if self._file is None:
            return
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._file.flush()
