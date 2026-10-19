"""Workspace is writable (expected; harness needs it)."""

from __future__ import annotations

from probe import Config, Outcome, write_outcome

CATEGORY = "utility"
SEVERITY = "info"
TITLE = "Workspace is writable (expected; harness needs it)"
SCORED = False


def run(cfg: Config) -> Outcome:
    outcome = write_outcome(cfg, [cfg.workspace])
    if outcome.status == "exposed":
        return Outcome("info", "workspace is writable (expected)", outcome.data)
    return Outcome(
        "error",
        f"workspace NOT writable, the harness cannot work: {outcome.evidence}",
        outcome.data,
    )
