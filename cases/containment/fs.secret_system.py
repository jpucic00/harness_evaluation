"""World-readable system secret is readable."""

from __future__ import annotations

from probe import Config, Outcome, read_secret_files

CATEGORY = "secrets"
SEVERITY = "high"
TITLE = "World-readable system secret is readable"
REQUIRES_CANARIES = True


def run(cfg: Config) -> Outcome:
    return read_secret_files(cfg, cfg.secret_system_paths)
