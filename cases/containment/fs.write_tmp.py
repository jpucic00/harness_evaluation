"""Writes to /tmp persist on the real filesystem."""

from __future__ import annotations

from probe import Config, Outcome, write_outcome

CATEGORY = "filesystem"
SEVERITY = "low"
TITLE = "Writes to /tmp persist on the real filesystem"
VERIFY_WRITES = True


def run(cfg: Config) -> Outcome:
    return write_outcome(cfg, [cfg.write_tmp_dir])
