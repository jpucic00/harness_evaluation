#!/usr/bin/env python3
"""Benign containment probe runner (stdlib only, Python 3.12).

Runs inside the environment as the agent user, wrapped by the harness's inner
sandbox (``/opt/agenteval/sandbox-exec``), and prints exactly one JSON object
on stdout. Every check is a read, a TCP/UNIX connect with a short timeout, or a
write of a harmless marker file. Nothing is exploited, modified or deleted.

The checks themselves are one file each in ``cases/containment/<check-id>.py``
(see ``cases/containment/README.md``). This module is their runtime: ``Config``,
``Outcome`` and the shared helpers they import with ``from probe import ...``.
The host uploads the check files to ``checks/`` next to this script.

Usage: ``probe.py --config '<json>' [--checks DIR]`` (or ``--config /path/to/config.json``).
All config keys are optional; paths are overridable so the probe can be tested
on a developer machine against temp dirs.

Statuses:
    contained  the action was blocked (or had no effect outside the sandbox)
    exposed    the action succeeded
    error      the check could not be performed
    info       informational, never scored

Write checks report ``exposed`` when the write succeeded *inside* the sandbox;
the host then verifies from outside (as root) whether the marker reached the
real filesystem and downgrades to ``contained`` if it did not.
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# Kept in sync with agenteval.constants (asserted by the test suite); this file
# is uploaded on its own and cannot import the agenteval package.
CANARY_PREFIX = "AGENTEVAL_CANARY_"
COLLECTOR_HOST = "collector.agenteval.test"
COLLECTOR_PORT = 8099
OPT_DIR = "/opt/agenteval"
SECRETS_DIR = "/etc/agenteval/secrets"

PROBE_VERSION = 1
MARKER_PREFIX = "agenteval-probe-"

STATUSES = ("contained", "exposed", "error", "info")
SEVERITIES = ("high", "medium", "low", "info")
CATEGORY_ORDER = (
    "secrets",
    "filesystem",
    "utility",
    "network",
    "privilege",
    "process",
    "host",
    "info",
)
"""Report order of the known categories; new categories sort after them."""
CHECK_ID = re.compile(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+")
"""``<area>.<name>``, e.g. ``fs.secret_home``; the check file is ``<id>.py``."""
DEFAULT_CHECKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks")

SHARED_FS_TYPES = frozenset(
    {"virtiofs", "9p", "vboxsf", "vmhgfs", "fuse", "fakeowner", "osxfs", "grpcfuse", "prl_fs"}
)
NOT_SHARED_FS_TYPES = frozenset({"fuse.lxcfs", "fusectl", "fuse.portal", "fuse.gvfsd-fuse"})


class Config:
    """Typed view over the JSON config with defaults."""

    def __init__(self, raw: Mapping[str, object]) -> None:
        self.raw = raw
        self.marker = self._str("marker", f"{os.getpid()}{int(time.time())}")
        self.home = self._str("home", os.path.expanduser("~"))
        self.workspace = self._str("workspace", os.getcwd())
        self.canaries_planted = self._bool("canaries_planted", True)
        self.secret_home_paths = self._strs(
            "secret_home_paths", ["~/.ssh/id_ed25519", "~/.aws/credentials", "~/.netrc"]
        )
        self.secret_system_paths = self._strs(
            "secret_system_paths", [f"{SECRETS_DIR}/service-token"]
        )
        self.write_home_dir = self._str("write_home_dir", "~")
        self.write_tmp_dir = self._str("write_tmp_dir", "/tmp")
        self.write_system_dirs = self._strs(
            "write_system_dirs", ["/etc", "/usr/local/bin", OPT_DIR]
        )
        self.collector_host = self._str("collector_host", COLLECTOR_HOST)
        self.collector_port = self._int("collector_port", COLLECTOR_PORT)
        self.collector_fallback_host = self._str("collector_fallback_host", "127.0.0.1")
        self.internet_target = self._str("internet_target", "1.1.1.1:443")
        self.dns_name = self._str("dns_name", "example.com")
        self.host_ports = self._ints("host_ports", [22, 5000, 7000])
        self.gateway = self._opt_str("gateway")
        self.route_file = self._str("route_file", "/proc/net/route")
        uid = os.getuid()
        self.docker_sockets = self._strs(
            "docker_sockets",
            [
                "/var/run/docker.sock",
                "/run/docker.sock",
                "/run/containerd/containerd.sock",
                "/run/podman/podman.sock",
                f"/run/user/{uid}/docker.sock",
                f"/run/user/{uid}/podman/podman.sock",
            ],
        )
        self.shadow_path = self._str("shadow_path", "/etc/shadow")
        self.proc_dir = self._str("proc_dir", "/proc")
        self.mounts_file = self._str("mounts_file", "/proc/self/mounts")
        self.sudo = self._str("sudo", "sudo")
        self.timeout = self._float("timeout", 3.0)
        only = self.raw.get("only")
        self.only: set[str] | None = {str(x) for x in only} if isinstance(only, list) else None

    def expand(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            return self.home + path[1:]
        return path

    def _str(self, key: str, default: str) -> str:
        value = self.raw.get(key)
        return value if isinstance(value, str) else default

    def _opt_str(self, key: str) -> str | None:
        value = self.raw.get(key)
        return value if isinstance(value, str) and value else None

    def _bool(self, key: str, default: bool) -> bool:
        value = self.raw.get(key)
        return value if isinstance(value, bool) else default

    def _int(self, key: str, default: int) -> int:
        value = self.raw.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    def _float(self, key: str, default: float) -> float:
        value = self.raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return default

    def _strs(self, key: str, default: list[str]) -> list[str]:
        value = self.raw.get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        return default

    def _ints(self, key: str, default: list[int]) -> list[int]:
        value = self.raw.get(key)
        if isinstance(value, list):
            return [int(v) for v in value if isinstance(v, int | str) and str(v).isdigit()]
        return default


class Outcome:
    """Result of one check (before the CheckSpec metadata is attached)."""

    def __init__(self, status: str, evidence: str, data: dict[str, object] | None = None):
        if status not in STATUSES:
            raise ValueError(f"bad status {status}")
        self.status = status
        self.evidence = evidence
        self.data: dict[str, object] = data or {}


def err(ex: BaseException) -> str:
    return f"{type(ex).__name__}: {ex}"


# --------------------------------------------------------------------------- secrets


def read_secret_files(cfg: Config, paths: list[str]) -> Outcome:
    if not cfg.canaries_planted:
        return Outcome("info", "no canaries planted (ambient_secrets is off)")
    results: list[dict[str, object]] = []
    exposed: list[str] = []
    for raw_path in paths:
        path = cfg.expand(raw_path)
        entry: dict[str, object] = {"path": path}
        try:
            with open(path, "rb") as f:
                content = f.read(65536)
            has_canary = CANARY_PREFIX.encode() in content
            entry.update(readable=True, bytes=len(content), canary=has_canary)
            if has_canary:
                exposed.append(path)
        except OSError as ex:
            entry.update(readable=False, error=type(ex).__name__)
        results.append(entry)
    if exposed:
        return Outcome(
            "exposed", f"canary content readable: {', '.join(exposed)}", {"files": results}
        )
    summary = "; ".join(
        f"{r['path']}: {'masked/empty' if r.get('readable') else r.get('error')}" for r in results
    )
    return Outcome("contained", f"no canary readable ({summary})", {"files": results})


# --------------------------------------------------------------------------- writes


def marker_content(cfg: Config) -> bytes:
    return f"agenteval probe marker {cfg.marker}\n".encode()


def try_write(cfg: Config, directory: str) -> dict[str, object]:
    path = os.path.join(cfg.expand(directory), f".{MARKER_PREFIX}{cfg.marker}")
    entry: dict[str, object] = {"path": path}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, marker_content(cfg))
        finally:
            os.close(fd)
        entry["written"] = True
    except OSError as ex:
        entry.update(written=False, error=type(ex).__name__)
    return entry


def write_outcome(cfg: Config, directories: list[str]) -> Outcome:
    attempts = [try_write(cfg, d) for d in directories]
    written = [str(a["path"]) for a in attempts if a.get("written")]
    data: dict[str, object] = {"attempts": attempts}
    if written:
        return Outcome("exposed", f"marker written: {', '.join(written)}", data)
    errors = ", ".join(f"{a['path']} ({a.get('error')})" for a in attempts)
    return Outcome("contained", f"write blocked: {errors}", data)


# --------------------------------------------------------------------------- network


def resolve_with_timeout(name: str, port: int, timeout: float) -> list[str] | BaseException:
    """getaddrinfo has no timeout of its own; run it in a daemon thread."""
    box: list[list[str] | BaseException] = []

    def run() -> None:
        try:
            infos = socket.getaddrinfo(name, port, proto=socket.IPPROTO_TCP)
            box.append(sorted({str(info[4][0]) for info in infos}))
        except BaseException as ex:  # noqa: BLE001 - reported as evidence
            box.append(ex)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if not box:
        return TimeoutError(f"resolution of {name} took longer than {timeout}s")
    return box[0]


def tcp_connect(host: str, port: int, timeout: float) -> str | None:
    """Return None when the connection was established, else an error string."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as ex:
        return err(ex)


def split_host_port(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {target!r}")
    return host, int(port)


def default_gateway(route_file: str) -> str | None:
    """IPv4 default gateway from /proc/net/route (None when there is no default route)."""
    with open(route_file) as f:
        lines = f.read().splitlines()[1:]
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        flags = int(fields[3], 16)
        if not flags & 0x2:  # RTF_GATEWAY
            continue
        raw = bytes.fromhex(fields[2])
        return socket.inet_ntoa(raw[::-1])
    return None


HOST_ANSWERED_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET})
"""connect() errors that mean the host itself answered (RST): reachable, port closed.

A policy block (nft ``reject with icmpx type admin-prohibited``) fails with
EHOSTUNREACH/EACCES/EPERM, a silent drop with a timeout; both count as contained.
"""


def gateway_port(host: str, port: int, timeout: float) -> tuple[str, str]:
    """('open' | 'answered' | 'blocked', detail) for one TCP connect to the gateway."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open", "open"
    except OSError as ex:
        if ex.errno in HOST_ANSWERED_ERRNOS:
            return "answered", f"host answered: {err(ex)}"
        return "blocked", err(ex)


# --------------------------------------------------------------------------- privilege


def read_small(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read(4096).strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- checks


@dataclass(frozen=True)
class Check:
    """One containment check, loaded from ``<id>.py``."""

    id: str
    category: str
    severity: str
    title: str
    run: Callable[[Config], Outcome]
    scored: bool = True
    requires_canaries: bool = False
    verify_writes: bool = False
    """The host checks from outside (as root) whether the markers in ``data.attempts``
    (from ``write_outcome``) reached the real filesystem, and downgrades to contained if not."""
    path: str = ""


def load_check(path: str) -> Check:
    """Load one check file; raise ``ValueError`` naming the file when it is malformed."""
    check_id = os.path.basename(path)[: -len(".py")]
    if not CHECK_ID.fullmatch(check_id):
        raise ValueError(
            f"{path}: file name must be '<area>.<name>.py' (lowercase), e.g. fs.secret_home.py"
        )
    # check files import the runtime as `probe`, whether this module runs as a script or is imported
    sys.modules.setdefault("probe", sys.modules[__name__])
    spec = importlib.util.spec_from_file_location(
        f"agenteval_check_{check_id.replace('.', '_')}", path
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"{path}: cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = {
        name: getattr(module, name, None) for name in ("CATEGORY", "SEVERITY", "TITLE", "run")
    }
    problems = [
        name for name in ("CATEGORY", "SEVERITY", "TITLE") if not isinstance(values[name], str)
    ]
    if not callable(values["run"]):
        problems.append("run(cfg)")
    if problems:
        raise ValueError(f"{path}: missing {', '.join(problems)}")
    if values["SEVERITY"] not in SEVERITIES:
        raise ValueError(f"{path}: SEVERITY must be one of {SEVERITIES}")
    scored = getattr(module, "SCORED", True)
    requires = getattr(module, "REQUIRES_CANARIES", False)
    verify = getattr(module, "VERIFY_WRITES", False)
    if not all(isinstance(v, bool) for v in (scored, requires, verify)):
        raise ValueError(f"{path}: SCORED, REQUIRES_CANARIES, VERIFY_WRITES must be True/False")
    return Check(
        id=check_id,
        category=str(values["CATEGORY"]),
        severity=str(values["SEVERITY"]),
        title=str(values["TITLE"]),
        run=values["run"],  # type: ignore[arg-type]
        scored=scored,
        requires_canaries=requires,
        verify_writes=verify,
        path=path,
    )


def load_checks(directory: str = DEFAULT_CHECKS_DIR) -> list[Check]:
    """Every ``<id>.py`` in ``directory`` (``_*`` files are templates), in report order."""
    names = sorted(n for n in os.listdir(directory) if n.endswith(".py") and not n.startswith("_"))
    checks = [load_check(os.path.join(directory, n)) for n in names]

    def order(check: Check) -> tuple[int, str]:
        known = check.category in CATEGORY_ORDER
        return (CATEGORY_ORDER.index(check.category) if known else len(CATEGORY_ORDER), check.id)

    return sorted(checks, key=order)


def run_checks(cfg: Config, checks: list[Check]) -> dict[str, object]:
    started = time.time()
    results: list[dict[str, object]] = []
    for check in checks:
        if cfg.only is not None and check.id not in cfg.only:
            continue
        t0 = time.monotonic()
        try:
            outcome = check.run(cfg)
            if not isinstance(outcome, Outcome) and not hasattr(outcome, "status"):
                raise TypeError("run() must return an Outcome")
        except Exception as ex:  # noqa: BLE001 - one broken check must not hide the rest
            outcome = Outcome("error", f"check crashed: {err(ex)}")
        results.append(
            {
                "id": check.id,
                "category": check.category,
                "severity": check.severity,
                "title": check.title,
                "scored": check.scored,
                "status": outcome.status,
                "evidence": outcome.evidence,
                "data": outcome.data,
                "duration_ms": round((time.monotonic() - t0) * 1000),
            }
        )
    return {
        "probe": "agenteval",
        "version": PROBE_VERSION,
        "marker": cfg.marker,
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "home": cfg.home,
        "workspace": cfg.workspace,
        "canaries_planted": cfg.canaries_planted,
        "started": started,
        "duration_s": round(time.time() - started, 3),
        "checks": results,
    }


def load_config(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    text = value
    if not value.lstrip().startswith("{"):
        with open(value) as f:
            text = f.read()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--config must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--config", help="JSON object or path to a JSON file")
    parser.add_argument("--checks", default=DEFAULT_CHECKS_DIR, help="directory of check files")
    args = parser.parse_args(argv)
    try:
        cfg = Config(load_config(args.config))
    except (OSError, ValueError) as ex:
        print(json.dumps({"probe": "agenteval", "error": f"bad config: {err(ex)}"}))
        return 2
    try:
        checks = load_checks(args.checks)
    except Exception as ex:  # noqa: BLE001 - reported as the probe's error
        print(json.dumps({"probe": "agenteval", "error": f"bad checks: {err(ex)}"}))
        return 2
    print(json.dumps(run_checks(cfg, checks), sort_keys=True))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
