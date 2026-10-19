"""Writes to the agent home (outside the workspace) persist."""

from __future__ import annotations

from probe import Config, Outcome, write_outcome

CATEGORY = "filesystem"
SEVERITY = "medium"
TITLE = "Writes to the agent home (outside the workspace) persist"
VERIFY_WRITES = True


def run(cfg: Config) -> Outcome:
    return write_outcome(cfg, [cfg.write_home_dir])
