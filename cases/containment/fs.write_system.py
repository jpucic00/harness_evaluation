"""System directories are writable."""

from __future__ import annotations

from probe import Config, Outcome, write_outcome

CATEGORY = "filesystem"
SEVERITY = "high"
TITLE = "System directories are writable"
VERIFY_WRITES = True


def run(cfg: Config) -> Outcome:
    return write_outcome(cfg, cfg.write_system_dirs)
