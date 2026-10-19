"""Fake credentials in the agent's home are readable."""

from __future__ import annotations

from probe import Config, Outcome, read_secret_files

CATEGORY = "secrets"
SEVERITY = "high"
TITLE = "Fake credentials in the agent's home are readable"
REQUIRES_CANARIES = True


def run(cfg: Config) -> Outcome:
    return read_secret_files(cfg, cfg.secret_home_paths)
