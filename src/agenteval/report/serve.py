"""Opening and serving built reports, and Inspect's log viewer for a run's logs."""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agenteval.config import ConfigError
from agenteval.run.plan import LATEST, resolve_run_dir


def report_page(report_root: Path, run_id: str = LATEST) -> Path:
    if run_id == "index":
        page = report_root / "index.html"
    else:
        page = resolve_run_dir(report_root, run_id, marker="index.html") / "index.html"
    if not page.is_file():
        raise ConfigError(f"report not found: {page}")
    return page


def open_path(path: Path) -> bool:
    """Open with the macOS ``open`` command; False when not available (caller prints the path)."""
    if sys.platform != "darwin" or shutil.which("open") is None:
        return False
    return subprocess.run(["open", str(path)], check=False).returncode == 0


def make_report_server(report_root: Path, port: int) -> ThreadingHTTPServer:
    """Static file server for ``report_root`` bound to 127.0.0.1 only."""
    if not report_root.is_dir():
        raise ConfigError(f"report directory not found: {report_root}")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(report_root.resolve()))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def view_command(log_root: Path, run_id: str = LATEST) -> list[str]:
    run_dir = resolve_run_dir(log_root, run_id)
    return [sys.executable, "-m", "inspect_ai", "view", "--log-dir", str(run_dir)]
