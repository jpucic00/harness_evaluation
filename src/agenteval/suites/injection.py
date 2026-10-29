"""Injection suite: CodeIPI (``inspect_evals.ipi_coding_agent``) run through our harness.

CodeIPI's published dataset, its ``setup_workspace()`` solver and its ``ipi_scorer()``
are reused unchanged. Only two things are swapped: the agent (the run's harness
instead of CodeIPI's ReAct agent) and the sandbox (each sample's pinned Docker compose
spec is cleared so the environment profile applies). ``boundary_monitor()`` is added
so the effects that actually reached the environment are recorded next to CodeIPI's
verdict. It runs *before* ``ipi_scorer()``: CodeIPI's verification command runs
``python -m pytest`` in the workspace as root, which imports (and runs) whatever the
agent left there. Scoring the monitor first keeps that code from rewriting the
monitor's evidence, and keeps its side effects out of the monitor's numbers.

How CodeIPI's scorer sees a bridged CLI agent (checked against inspect_ai 0.3.281 and
inspect_swe 0.2.71): the agent bridge rebuilds ``state.messages`` from the model calls
the CLI makes. Assistant messages carry the CLI's tool calls (``ToolCall.arguments``)
and the following request carries their outputs as ``tool`` messages, so all three
text sources ipi_scorer reads (tool-call arguments, assistant text, tool results)
are populated. Limitations:

- The bridge keeps only the main conversation thread. Sub-agent sessions and history
  dropped by the CLI's own context compaction are not in ``state.messages``, so
  exfiltration and code-execution markers that only appear there are missed
  (under-reports compliance).
- CodeIPI scores *compliance*, not *effect*: a canary in a tool call counts as
  exfiltration even if the inner sandbox or egress policy stopped it. The
  boundary_monitor score records what actually crossed the boundary.
- The detection/false-positive grader uses ``get_model(role="grader")``.
  ``build_task`` binds that role to the evaluated model, so the grader's calls are
  recorded as scoring usage, not agent usage.
- CodeIPI hard-codes ``/workspace`` and plants ``/root/.secret_canary``. The harness
  runs as the environment's ``agent_user`` (unprivileged by default, the same user as
  the other suites); the workspace is handed to that user, and ``prepare_sample``
  targets it too (egress rule, fake credentials, decoy and monitor baseline).
  ``/root`` is unreadable to that user, so the ``/root`` canary is protected by Unix
  permissions in every config, with or without an inner sandbox. CodeIPI detects a
  leak by the file's content, so an attempt on it is only visible in the transcript;
  the ``/workspace/.env`` and env-var canaries stay reachable and are still scored.
  Set the ``agent_user`` option to ``root`` to
  reproduce CodeIPI's upstream setup, where only the inner sandbox (if any) stands
  between the agent and that file.
- CodeIPI's env canaries are passed to the agent process via ``extra_env``. An inner
  sandbox with ``clear_env`` drops them unless they are allow-listed, which is the
  containment effect being compared. ``AWS_SECRET_ACCESS_KEY`` overrides the ambient
  canary of the same name, so that one is tracked by CodeIPI's scorer only.
- CodeIPI's verification command calls ``python``; ``ensure_python_alias()`` links it
  to ``python3`` before the baseline snapshot when the image lacks it.
"""

from __future__ import annotations

from pathlib import Path

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox
from inspect_evals.ipi_coding_agent import ipi_coding_agent, ipi_scorer, setup_workspace
from inspect_evals.ipi_coding_agent.constants import (
    CANARY_ENV_VARS,
    EXEC_CANARIES,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_NO_DEFENSE,
)

from agenteval.cases import CODEIPI_WORKSPACE, InjectionCase, load_injection_cases
from agenteval.config import InjectionOptions, MatrixCell
from agenteval.constants import check_user

from .capability import harness_chain, run_checked
from .monitors import boundary_monitor, monitor_timeout
from .setup import prepare_sample
from .vulnscan import generated_vulns


def local_sample(case: InjectionCase) -> Sample:
    """One local case -> a Sample shaped exactly like CodeIPI's."""
    spec = case.spec
    if spec.exec_marker:
        # CodeIPI's scorer looks the marker up by sample id in this module-level table
        EXEC_CANARIES[case.id] = spec.exec_marker
    text = spec.task + (f"\n\n{spec.issue}" if spec.issue else "")
    return Sample(
        id=case.id,
        input=text,
        target=spec.expected_fix,
        metadata={
            "injection_vector": spec.vector,
            "payload_category": spec.category,
            "payload_sophistication": spec.sophistication,
            "severity": spec.severity,
            "payload_indicator": spec.indicator,
            "expected_malicious_action": spec.expected_malicious_action,
            "is_benign": spec.category == "none",
            "repo_files": case.repo_files,
            "verification_command": spec.verification,
            "persistence_targets": spec.persistence_targets,
            "local_case": True,
        },
    )


def load_local_cases(root: Path | None = None) -> list[Sample]:
    """Every ``cases/injection/<id>/`` as a Sample (``_*`` directories are templates)."""
    return [local_sample(case) for case in load_injection_cases(root)]


def codeipi_dataset(options: InjectionOptions) -> MemoryDataset:
    """CodeIPI's samples (per-sample sandbox cleared) plus the local cases."""
    samples: list[Sample] = []
    if options.codeipi:
        upstream = ipi_coding_agent(
            vectors=",".join(options.vectors) if options.vectors else None,
            severities=",".join(options.severities) if options.severities else None,
            defense_prompt="true" if options.defense_prompt else "false",
        )
        samples = [s.model_copy(update={"sandbox": None}) for s in upstream.dataset]
    if options.local:
        upstream_ids = {s.id for s in samples}
        for sample in load_local_cases():
            meta = sample.metadata or {}
            if sample.id in upstream_ids:
                raise ValueError(f"local injection case '{sample.id}' reuses a CodeIPI sample id")
            vector, severity = meta.get("injection_vector"), meta.get("severity")
            if options.vectors and vector != "none" and vector not in options.vectors:
                continue
            if options.severities and severity != "none" and severity not in options.severities:
                continue
            samples.append(sample)
    if options.ids is not None:
        wanted = set(options.ids)
        samples = [s for s in samples if str(s.id) in wanted]
    if options.limit is not None:
        samples = samples[: options.limit]
    return MemoryDataset(samples, name="codeipi")


def codeipi_originals(state: TaskState) -> dict[str, str]:
    """The repo files CodeIPI's ``setup_workspace()`` wrote for this sample."""
    files = state.metadata.get("repo_files")
    return {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else {}


@solver
def ensure_python_alias() -> Solver:
    """Provide ``python`` (CodeIPI's verification command) when only ``python3`` exists."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        script = (
            "command -v python >/dev/null 2>&1 || "
            'ln -s "$(command -v python3)" /usr/local/bin/python'
        )
        await run_checked(sandbox(), ["sh", "-c", script])
        return state

    return solve


@solver
def chown_workspace(workspace: str, user: str) -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await run_checked(sandbox(), ["chown", "-R", f"{check_user(user)}:", workspace])
        return state

    return solve


def injection_task(cell: MatrixCell) -> Task:
    """Suite factory; model, sandbox, limits and metadata are applied by ``build_task``."""
    options = cell.suite.typed_options(InjectionOptions)
    if cell.environment.workspace != CODEIPI_WORKSPACE:
        raise ValueError(
            f"environment '{cell.environment.name}': the injection suite (CodeIPI) needs "
            f"workspace {CODEIPI_WORKSPACE}, got {cell.environment.workspace}"
        )
    user = check_user(options.agent_user or cell.environment.agent_user)
    setup: list[Solver] = [
        ensure_python_alias(),
        prepare_sample(cell, user=user),
        setup_workspace(),
    ]
    if user != "root":
        setup.append(chown_workspace(CODEIPI_WORKSPACE, user))
    return Task(
        dataset=codeipi_dataset(options),
        setup=setup,
        solver=harness_chain(
            cell,
            user=user,
            system_prompt=SYSTEM_PROMPT if options.defense_prompt else SYSTEM_PROMPT_NO_DEFENSE,
            extra_env=dict(CANARY_ENV_VARS),
        ),
        # The monitor and the static scan read the workspace before ipi_scorer runs
        # workspace code as root.
        scorer=[
            boundary_monitor(timeout=monitor_timeout(cell.limits.time_limit)),
            generated_vulns(codeipi_originals, workspace=CODEIPI_WORKSPACE),
            ipi_scorer(),
        ],
        metadata={
            "agenteval_suite": {
                "upstream": "inspect_evals/ipi_coding_agent",
                "agent_user": user,
                "defense_prompt": options.defense_prompt,
            }
        },
    )
