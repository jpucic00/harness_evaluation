"""Suite registry: one Inspect Task per matrix cell.

Each suite factory builds the suite-specific parts of a Task (dataset, setup, solver,
scorers). ``build_task`` then applies what every task shares: name, model (also bound
to the ``grader`` role), sandbox, limits, epochs and the ``agenteval`` metadata used to
regroup results in reports.
"""

from __future__ import annotations

from collections.abc import Callable

from inspect_ai import Task, task_with
from inspect_ai.solver import TaskState

from agenteval.cases import get_capability_task
from agenteval.config import MatrixCell
from agenteval.environments import sandbox_spec
from agenteval.models import build_model

from .capability import META_TASK_ID, capability_task
from .containment import containment_task
from .injection import injection_task
from .vulnscan import generated_vulns


def capability_originals(state: TaskState) -> dict[str, bytes]:
    return get_capability_task(str(state.metadata[META_TASK_ID])).repo_files()


def capability_with_scan(cell: MatrixCell) -> Task:
    """The capability suite plus ``generated_vulns``, scored right after the monitor
    (before ``tests_pass`` runs workspace code)."""
    task = capability_task(cell)
    scorers = list(task.scorer or [])
    scan = generated_vulns(capability_originals, workspace=cell.environment.workspace)
    return task_with(task, scorer=[*scorers[:1], scan, *scorers[1:]])


SUITES: dict[str, Callable[[MatrixCell], Task]] = {
    "containment": containment_task,
    "capability": capability_with_scan,
    "injection": injection_task,
}


def build_task(cell: MatrixCell) -> Task:
    factory = SUITES.get(cell.suite.name)
    if factory is None:
        raise ValueError(f"unknown suite '{cell.suite.name}'; available: {sorted(SUITES)}")
    task = factory(cell)
    limits = cell.limits
    # One Model instance (keeps model_args such as provider routing). Binding the
    # "grader" role to it makes Inspect record grader calls (CodeIPI) as scoring
    # usage instead of charging them to the agent.
    model = build_model(cell.model)
    return task_with(
        task,
        name=cell.task_name,
        model=model,
        model_roles={"grader": model},
        sandbox=sandbox_spec(cell.environment, inner_sandbox=cell.harness.inner_sandbox.type),
        metadata={**(task.metadata or {}), **cell.metadata()},
        epochs=cell.epochs,
        time_limit=limits.time_limit,
        working_limit=limits.working_limit,
        token_limit=limits.token_limit,
        message_limit=limits.message_limit,
        cost_limit=limits.cost_limit,
    )


__all__ = ["SUITES", "build_task"]
