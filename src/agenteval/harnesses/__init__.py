"""Harnesses: the agent CLI and the inner sandbox it runs in.

``harness_solvers(cell)`` returns the solver chain that installs the inner sandbox
wrapper, installs the agent and runs it for the cell's harness profile.
``check_harness_options(harness)`` checks a profile's ``options`` before anything runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from inspect_ai.solver import Solver

from agenteval.config import HarnessProfile, MatrixCell
from agenteval.harnesses import claude_code, opencode
from agenteval.harnesses.opencode import install_agent, run_agent
from agenteval.harnesses.sandbox_exec import install_inner_sandbox

__all__ = [
    "check_harness_options",
    "harness_solvers",
    "install_agent",
    "install_inner_sandbox",
    "run_agent",
]


class _Harness(Protocol):
    """What a harness module provides (``opencode``, ``claude_code``)."""

    install_agent: Callable[..., Solver]
    run_agent: Callable[..., Solver]

    def check_options(self, harness: HarnessProfile) -> None: ...


AGENTS: dict[str, _Harness] = {"opencode": opencode, "claude_code": claude_code}
"""Harness module per ``HarnessProfile.agent``."""


def _module(agent: str) -> _Harness:
    module = AGENTS.get(agent)
    if module is None:
        raise ValueError(f"unsupported harness agent: {agent}")
    return module


def check_harness_options(harness: HarnessProfile) -> None:
    """``ConfigError`` if ``harness.options`` has a key the agent's inspect_swe factory
    does not take, or one agenteval sets itself."""
    _module(harness.agent).check_options(harness)


def harness_solvers(
    cell: MatrixCell,
    *,
    user: str | None = None,
    system_prompt: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> list[Solver]:
    """Solvers that install and run the cell's harness.

    Args:
        cell: Matrix cell.
        user: User the agent runs as (default: ``cell.environment.agent_user``).
        system_prompt: Extra system prompt (appended after the harness profile's).
        extra_env: Extra agent env vars (override harness env and ambient canaries).
    """
    run_user = user or cell.environment.agent_user
    module = _module(cell.harness.agent)
    return [
        install_inner_sandbox(cell, verify_user=run_user),
        module.install_agent(cell, run_user),
        module.run_agent(cell, user=run_user, system_prompt=system_prompt, extra_env=extra_env),
    ]
