"""OpenCode harness: pinned install via inspect_swe, wrapped by the inner sandbox.

``install_agent`` installs the pinned OpenCode release with inspect_swe's
installer and puts a root-owned wrapper at ``/usr/local/bin/opencode`` that runs
the real binary through ``constants.SANDBOX_EXEC``. ``run_agent`` then starts
``inspect_swe.opencode(version="sandbox")``, whose ``which opencode`` lookup
resolves to that wrapper, so every OpenCode process (and everything it spawns)
lives inside the inner sandbox while the Inspect model bridge stays outside.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from inspect_ai.agent import Agent, as_solver
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import SandboxEnvironment, sandbox
from inspect_swe import opencode
from inspect_swe._opencode.agentbinary import ensure_opencode_setup

from agenteval import constants
from agenteval.config import HarnessProfile, MatrixCell
from agenteval.harnesses.common import agent_env, check_reserved, factory_kwargs, install_wrapper
from agenteval.harnesses.common import check_options as _check_options
from agenteval.harnesses.sandbox_exec import render_template

OPENCODE_WRAPPER = "/usr/local/bin/opencode"
WRAPPER_MARKER = "agenteval opencode wrapper"

RESERVED_OPTIONS = frozenset({"version", "user", "env", "sandbox"})
"""inspect_swe.opencode arguments owned by the harness (not settable via ``options``)."""


def render_wrapper(binary: str) -> str:
    return render_template(
        "opencode-wrapper.sh",
        {"SANDBOX_EXEC": shlex.quote(constants.SANDBOX_EXEC), "BINARY": shlex.quote(binary)},
    )


def check_options(harness: HarnessProfile) -> None:
    """Reject ``options`` that ``inspect_swe.opencode`` does not take or agenteval sets."""
    _check_options(harness, opencode, RESERVED_OPTIONS)


def agent_kwargs(
    cell: MatrixCell, *, user: str, system_prompt: str | None, env: Mapping[str, str]
) -> dict[str, Any]:
    """Keyword arguments for ``inspect_swe.opencode``."""
    harness = cell.harness
    check_reserved(harness, RESERVED_OPTIONS)
    extra: dict[str, Any] = {}
    agent_env = dict(env)
    cli_model = harness.agent_model or cell.model.agent_models.get(harness.agent)
    if cli_model:
        extra["opencode_model"] = cli_model
        # OpenCode refuses to start a provider without its key variable. Requests go
        # to the Inspect bridge, so a placeholder is enough; real keys stay on the host.
        provider = cli_model.split("/", 1)[0] if "/" in cli_model else "anthropic"
        agent_env.setdefault(f"{provider.upper().replace('-', '_')}_API_KEY", "sk-none")
    return factory_kwargs(
        cell,
        prompts=(harness.system_prompt, system_prompt),
        extra=extra,
        user=user,
        env=agent_env,
    )


def opencode_agent(
    cell: MatrixCell, *, user: str, system_prompt: str | None, env: Mapping[str, str]
) -> Agent:
    return opencode(**agent_kwargs(cell, user=user, system_prompt=system_prompt, env=env))


@solver
def install_agent(cell: MatrixCell, user: str | None = None) -> Solver:
    """Install the pinned OpenCode and the ``/usr/local/bin/opencode`` sandbox wrapper."""
    run_user = user or cell.environment.agent_user
    version = cell.harness.version

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await install_opencode(sandbox(), version, run_user)
        return state

    return solve


async def install_opencode(
    sbox: SandboxEnvironment, version: str, run_user: str
) -> tuple[str, list[str]]:
    """Install OpenCode and its wrapper (see ``install_agent``).

    Returns the binary inspect_swe resolved (the real binary for pinned versions)
    and the dependency bin directories (node, ripgrep).
    """
    binary, dep_dirs = await ensure_opencode_setup(sbox, version, run_user)
    await install_wrapper(
        sbox,
        command="opencode",
        binary=binary,
        wrapper=OPENCODE_WRAPPER,
        marker=WRAPPER_MARKER,
        render=render_wrapper,
        run_user=run_user,
    )
    return binary, list(dep_dirs)


@solver
def run_agent(
    cell: MatrixCell,
    user: str | None = None,
    system_prompt: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> Solver:
    """Run OpenCode; the agent is built per sample so ambient canaries reach its env."""
    run_user = user or cell.environment.agent_user

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        env = agent_env(cell, state.store.get(constants.STORE_CANARIES), extra_env)
        agent = opencode_agent(cell, user=run_user, system_prompt=system_prompt, env=env)
        return await as_solver(agent)(state, generate)

    return solve
