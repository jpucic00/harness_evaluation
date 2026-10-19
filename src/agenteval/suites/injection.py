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
from typing import Annotated, Literal

import yaml
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
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from agenteval import constants
from agenteval.config import MatrixCell

from .capability import CsvList, check_user, harness_chain, run_checked
from .common import prepare_sample
from .monitors import boundary_monitor, monitor_timeout
from .vulnscan import generated_vulns

CODEIPI_WORKSPACE = "/workspace"
"""CodeIPI writes its repos to, and verifies in, this fixed path."""

Vector = Literal["issue_text", "code_comment", "readme", "config_file"]
Severity = Literal["S1", "S2", "S3"]


def _check_optional_user(user: str | None) -> str | None:
    return None if user is None else check_user(user)


class InjectionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int | None = Field(default=None, ge=1)
    vectors: Annotated[list[Vector] | None, CsvList] = None
    """Injection vectors to include (benign samples are always included)."""
    severities: Annotated[list[Severity] | None, CsvList] = None
    defense_prompt: bool = True
    """Use CodeIPI's system prompt that warns about instructions found in files."""
    agent_user: Annotated[str | None, AfterValidator(_check_optional_user)] = None
    """User the harness runs as (default: the environment's ``agent_user``)."""
    codeipi: bool = True
    """Include CodeIPI's 45 published samples."""
    local: bool = True
    """Include the local cases in ``cases/injection/``."""
    ids: Annotated[list[str] | None, CsvList] = None
    """Only these sample ids (CodeIPI or local)."""


Category = Literal["none", "exfiltration", "code_execution", "persistence"]


class LocalCase(BaseModel):
    """``cases/injection/<id>/case.yaml``: one CodeIPI-format sample (see that README)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    """What the user asks the agent to do (CodeIPI ``task_description``)."""
    issue: str = ""
    """Bug report appended to the task (CodeIPI ``issue_text``); a possible injection vector."""
    vector: Literal["none", "issue_text", "code_comment", "readme", "config_file"]
    category: Category
    """What the injected instruction tries; ``none`` = benign control case."""
    sophistication: str = "none"
    severity: Literal["none", "S1", "S2", "S3"] = "none"
    indicator: str = ""
    """Regex describing the injected action (documentation; CodeIPI does not score it)."""
    expected_malicious_action: str = ""
    exec_marker: str | None = None
    """``code_execution`` only: unique string inside the injected command. Compliance
    = the marker appears in the agent's tool calls."""
    persistence_targets: list[str] = Field(default_factory=list)
    """``persistence`` only: workspace files whose change means compliance."""
    verification: str = f"cd {CODEIPI_WORKSPACE} && python -m pytest tests/ -q"
    """Command that passes once the legitimate bug is fixed."""
    expected_fix: str

    @model_validator(mode="after")
    def _consistent(self) -> LocalCase:
        benign = self.category == "none"
        if benign != (self.vector == "none") or benign != (self.severity == "none"):
            raise ValueError("benign cases (category none) need vector: none and severity: none")
        if (self.category == "code_execution") != bool(self.exec_marker):
            raise ValueError("exec_marker is required for, and only for, category code_execution")
        if self.category == "persistence" and not self.persistence_targets:
            raise ValueError("category persistence needs persistence_targets")
        return self


LOCAL_CASES_DIR = constants.cases_dir() / "injection"


def load_local_case(directory: Path) -> Sample:
    """One local case directory -> a Sample shaped exactly like CodeIPI's."""
    try:
        case = LocalCase.model_validate(yaml.safe_load((directory / "case.yaml").read_text()))
    except Exception as ex:
        raise ValueError(f"{directory / 'case.yaml'}: {ex}") from ex
    repo = directory / "repo"
    if not repo.is_dir():
        raise ValueError(f"{directory}: missing repo/ (the files written to the workspace)")
    files = {
        str(path.relative_to(repo)): path.read_text()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    sample_id = directory.name
    if case.exec_marker:
        # CodeIPI's scorer looks the marker up by sample id in this module-level table
        EXEC_CANARIES[sample_id] = case.exec_marker
    text = case.task + (f"\n\n{case.issue}" if case.issue else "")
    return Sample(
        id=sample_id,
        input=text,
        target=case.expected_fix,
        metadata={
            "injection_vector": case.vector,
            "payload_category": case.category,
            "payload_sophistication": case.sophistication,
            "severity": case.severity,
            "payload_indicator": case.indicator,
            "expected_malicious_action": case.expected_malicious_action,
            "is_benign": case.category == "none",
            "repo_files": files,
            "verification_command": case.verification,
            "persistence_targets": case.persistence_targets,
            "local_case": True,
        },
    )


def load_local_cases(root: Path = LOCAL_CASES_DIR) -> list[Sample]:
    """Every ``cases/injection/<id>/`` (``_*`` directories are templates)."""
    if not root.is_dir():
        return []
    return [
        load_local_case(d)
        for d in sorted(root.iterdir())
        if d.is_dir() and not d.name.startswith(("_", "."))
    ]


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
    options = InjectionOptions.model_validate(cell.suite.options)
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
