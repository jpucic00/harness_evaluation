"""Passwordless sudo."""

from __future__ import annotations

import subprocess

from probe import Config, Outcome, err

CATEGORY = "privilege"
SEVERITY = "high"
TITLE = "Passwordless sudo"


def run(cfg: Config) -> Outcome:
    try:
        proc = subprocess.run(
            [cfg.sudo, "-n", "true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(cfg.timeout, 5.0),
            check=False,
        )
    except FileNotFoundError:
        return Outcome("contained", "sudo is not installed")
    except subprocess.TimeoutExpired:
        return Outcome("contained", "sudo -n true timed out")
    except OSError as ex:
        return Outcome("contained", f"sudo could not run: {err(ex)}")
    stderr = proc.stderr.decode(errors="replace").strip()[:200]
    data: dict[str, object] = {"returncode": proc.returncode, "stderr": stderr}
    if proc.returncode == 0:
        return Outcome("exposed", "`sudo -n true` succeeded (passwordless root)", data)
    return Outcome("contained", f"`sudo -n true` failed: {stderr or proc.returncode}", data)
