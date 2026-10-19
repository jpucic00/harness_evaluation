"""Harnesses: the agent CLI and the inner sandbox it runs in.

``harness_solvers(cell)`` returns the solver chain that installs the inner sandbox
wrapper, installs the agent and runs it for the cell's harness profile.
"""

from __future__ import annotations

from inspect_ai.solver import Solver

from agenteval.config import MatrixCell
from agenteval.harnesses import claude_code
from agenteval.harnesses.opencode import install_agent, run_agent
from agenteval.harnesses.sandbox_exec import install_inner_sandbox, render_sandbox_exec

__all__ = [
    "harness_solvers",
    "install_agent",
    "install_inner_sandbox",
    "render_sandbox_exec",
    "run_agent",
]


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
    if cell.harness.agent == "opencode":
        return [
            install_inner_sandbox(cell, verify_user=run_user),
            install_agent(cell, run_user),
            run_agent(cell, user=run_user, system_prompt=system_prompt, extra_env=extra_env),
        ]
    if cell.harness.agent == "claude_code":
        return [
            install_inner_sandbox(cell, verify_user=run_user),
            claude_code.install_agent(cell, run_user),
            claude_code.run_agent(
                cell, user=run_user, system_prompt=system_prompt, extra_env=extra_env
            ),
        ]
    raise ValueError(f"unsupported harness agent: {cell.harness.agent}")
