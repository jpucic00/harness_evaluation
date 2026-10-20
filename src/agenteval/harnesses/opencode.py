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
from agenteval.config import MatrixCell
from agenteval.harnesses.sandbox_exec import install_root_file, render_template

OPENCODE_WRAPPER = "/usr/local/bin/opencode"
WRAPPER_MARKER = "agenteval opencode wrapper"

RESERVED_OPTIONS = frozenset({"version", "user", "env", "sandbox"})
"""inspect_swe.opencode arguments owned by the harness (not settable via ``options``)."""


def render_wrapper(binary: str) -> str:
    return render_template(
        "opencode-wrapper.sh",
        {"SANDBOX_EXEC": shlex.quote(constants.SANDBOX_EXEC), "BINARY": shlex.quote(binary)},
    )


def ambient_env(canaries: object) -> dict[str, str]:
    """Canary env vars planted by ``prepare_sample`` (``store[STORE_CANARIES]["env"]``)."""
    if not isinstance(canaries, Mapping):
        return {}
    env = canaries.get("env")
    if not isinstance(env, Mapping):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def agent_env(
    cell: MatrixCell, canaries: object = None, extra_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Agent env: harness env, then ambient canaries (if enabled), then ``extra_env``."""
    env = dict(cell.harness.env)
    if cell.environment.ambient_secrets:
        env |= ambient_env(canaries)
    env |= dict(extra_env or {})
    return env


def agent_kwargs(
    cell: MatrixCell, *, user: str, system_prompt: str | None, env: Mapping[str, str]
) -> dict[str, Any]:
    """Keyword arguments for ``inspect_swe.opencode``."""
    harness = cell.harness
    reserved = RESERVED_OPTIONS & set(harness.options)
    if reserved:
        raise ValueError(
            f"harness '{harness.name}': options {sorted(reserved)} are set by agenteval"
        )
    prompts = [p for p in (harness.system_prompt, system_prompt) if p]
    kwargs: dict[str, Any] = {"cwd": cell.environment.workspace}
    if prompts:
        kwargs["system_prompt"] = "\n\n".join(prompts)
    agent_env = dict(env)
    cli_model = harness.agent_model or cell.model.agent_models.get(harness.agent)
    if cli_model:
        kwargs["opencode_model"] = cli_model
        # OpenCode refuses to start a provider without its key variable. Requests go
        # to the Inspect bridge, so a placeholder is enough; real keys stay on the host.
        provider = cli_model.split("/", 1)[0] if "/" in cli_model else "anthropic"
        agent_env.setdefault(f"{provider.upper().replace('-', '_')}_API_KEY", "sk-none")
    kwargs |= harness.options
    kwargs |= {"version": "sandbox", "user": user, "env": agent_env}
    return kwargs


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
    if binary == OPENCODE_WRAPPER:
        # "auto"/"sandbox" versions resolve via `which`; accept our own wrapper
        # (re-run), refuse to wrap a real binary in place.
        current = await sbox.read_file(OPENCODE_WRAPPER)
        if WRAPPER_MARKER not in current:
            raise RuntimeError(
                f"opencode is installed at {OPENCODE_WRAPPER}; agenteval needs that path "
                "for its sandbox wrapper. Pin a version in the harness profile instead."
            )
    else:
        await install_root_file(sbox, OPENCODE_WRAPPER, render_wrapper(binary), "0755")

    found = await sbox.exec(["bash", "-c", "command -v opencode"], user=run_user)
    if not found.success or found.stdout.strip() != OPENCODE_WRAPPER:
        raise RuntimeError(
            f"`command -v opencode` as '{run_user}' resolves to "
            f"{found.stdout.strip() or '<nothing>'}, expected {OPENCODE_WRAPPER}"
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
