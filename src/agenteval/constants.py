"""Paths and names shared by the host-side code and the in-sandbox scripts."""

import os
from pathlib import Path


def cases_dir() -> Path:
    """Root of the contributor-written cases (``<repo>/cases``; ``AGENTEVAL_CASES_DIR`` overrides).

    One sub-directory per kind: ``capability/``, ``containment/``, ``monitor/``, ``injection/``.
    """
    override = os.environ.get("AGENTEVAL_CASES_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "cases"


# Directory inside every environment that holds agenteval's helper scripts.
# Root-owned, world-readable; uploaded fresh for every sample.
OPT_DIR = "/opt/agenteval"

# Root-only directory for evidence the agent must not see or tamper with
# (baseline snapshots, collector log, canary list).
STATE_DIR = "/var/lib/agenteval"

# World-readable "system" secrets location (fake credentials, see canaries).
SECRETS_DIR = "/etc/agenteval/secrets"

# Wrapper that runs a command inside the harness's inner sandbox profile.
# `sandbox-exec <cmd> [args...]`. For inner_sandbox.type == "none" it just execs.
SANDBOX_EXEC = f"{OPT_DIR}/sandbox-exec"

# Local HTTP listener that stands in for an external server. Requests to it are
# logged (root-only) so scorers can tell whether data left the agent's sandbox.
COLLECTOR_HOST = "collector.agenteval.test"
COLLECTOR_PORT = 8099
COLLECTOR_LOG = f"{STATE_DIR}/collector.jsonl"

# Inspect store keys written by the prepare_sample solver.
STORE_CANARIES = "agenteval:canaries"
STORE_RUN_START = "agenteval:run_start"
STORE_PROBE_RESULTS = "agenteval:probe_results"

# Prefix of every canary token, so they are easy to grep for in transcripts.
CANARY_PREFIX = "AGENTEVAL_CANARY_"

# Group allowed to execute /usr/bin/bwrap. provision.sh restricts bwrap to it and
# install_inner_sandbox adds the agent user only in cells that use the bwrap inner
# sandbox, so "none" cells keep Ubuntu's unprivileged-user-namespace restriction.
BWRAP_GROUP = "agenteval-bwrap"

# Where Inspect's sandbox-tools server lives inside every environment (root-only).
# Hidden from the inner sandbox; must match inspect_ai.util._sandbox._cli.
SANDBOX_TOOLS_DIR = "/var/tmp/.da7be258e003d428"
