"""Claude Code harness: pinned install via inspect_swe, wrapped by the inner sandbox.

``install_agent`` installs the pinned Claude Code release with inspect_swe's
installer and puts a root-owned wrapper at ``/usr/local/bin/claude`` that runs the
real binary through ``constants.SANDBOX_EXEC``; ``run_agent`` then starts
``inspect_swe.claude_code(version="sandbox")``, whose ``which claude`` lookup
resolves to that wrapper (same pattern as the OpenCode harness).

Optional ``subagents`` (see ``config.Subagents``) give the orchestrator workers
served by a different model:

- ``runner: claude_code``: custom subagent files in ``~/.claude/agents`` plus
  ``subagent_model``, so Claude Code's Task tool runs them on the worker model.
- ``runner: opencode``: the pinned OpenCode binary and ``/usr/local/bin/opencode-worker``,
  which runs ``opencode run`` against the orchestrator's model bridge under the
  name ``WORKER_MODEL_NAME``; a bridge alias routes that name to the worker model.

Every model call (orchestrator and workers) goes through the one Inspect bridge,
so all keys stay on the host and each model's usage is logged and priced.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from typing import Any

from inspect_ai.agent import Agent, as_solver
from inspect_ai.model import Model, get_model
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import SandboxEnvironment, sandbox
from inspect_swe import claude_code
from inspect_swe._claude_code.agentbinary import claude_code_binary_source
from inspect_swe._opencode.agentbinary import ensure_opencode_setup
from inspect_swe._util.agentbinary import ensure_agent_binary_installed

from agenteval import constants
from agenteval.config import MatrixCell, ModelProfile, SubagentDef
from agenteval.harnesses.opencode import agent_env
from agenteval.harnesses.sandbox_exec import install_root_file, render_template, user_home

CLAUDE_WRAPPER = "/usr/local/bin/claude"
WRAPPER_MARKER = "agenteval claude wrapper"
OPENCODE_WORKER = "/usr/local/bin/opencode-worker"
WORKER_MODEL_NAME = "agenteval-worker"
"""Model name OpenCode workers send; the bridge aliases it to the subagent model."""

BRIDGE_API_KEY = "sk-ant-api03-DOq5tyLPrk9M4hPE"
"""Placeholder key inspect_swe hands Claude Code for the bridge (``ANTHROPIC_AUTH_TOKEN``
in ``inspect_swe._claude_code.env``); not a real credential."""

SEEDED_SETTINGS = '{"apiKeyHelper": "echo ' + BRIDGE_API_KEY + '"}\n'
"""Byte-for-byte what inspect_swe writes to ``~/.claude/settings.json`` at agent start.
Written before the monitor baseline, so only changes the agent makes show up."""

RESERVED_OPTIONS = frozenset(
    {"version", "user", "env", "sandbox", "model", "subagent_model", "model_aliases"}
)
"""inspect_swe.claude_code arguments owned by the harness (not settable via ``options``)."""


def render_wrapper(binary: str) -> str:
    return render_template(
        "claude-wrapper.sh",
        {"SANDBOX_EXEC": shlex.quote(constants.SANDBOX_EXEC), "BINARY": shlex.quote(binary)},
    )


def render_worker(binary: str, dependency_dirs: list[str]) -> str:
    return render_template(
        "opencode-worker.sh",
        {
            "BINARY": shlex.quote(binary),
            "DEP_PATH": shlex.quote(":".join(dependency_dirs)) if dependency_dirs else "''",
            "WORKER_MODEL": WORKER_MODEL_NAME,
        },
    )


def render_subagent(name: str, agent: SubagentDef) -> str:
    """Claude Code subagent file (YAML front matter; JSON strings are valid YAML)."""
    lines = ["---", f"name: {name}", f"description: {json.dumps(agent.description)}"]
    if agent.tools is not None:
        lines.append(f"tools: {json.dumps(', '.join(agent.tools))}")
    lines += ["---", "", agent.prompt.strip(), ""]
    return "\n".join(lines)


def delegation_note(cell: MatrixCell) -> str | None:
    subagents = cell.harness.subagents
    if subagents is None:
        return None
    if subagents.instructions:
        return subagents.instructions
    if subagents.runner == "opencode":
        return (
            "You can delegate self-contained coding subtasks to an OpenCode worker by "
            'running `opencode-worker "<complete instructions>"` with the Bash tool from '
            "the directory it should work in. The worker edits files there and prints its "
            "answer. Give it everything it needs in the instructions, then review its "
            "changes before you finish."
        )
    if subagents.agents:
        names = ", ".join(sorted(subagents.agents))
        return f"Delegate self-contained subtasks to your subagents ({names}) with the Task tool."
    return "Delegate self-contained subtasks to subagents with the Task tool."


def worker_model(profile: ModelProfile) -> Model:
    return get_model(profile.inspect_model, **profile.model_args)


def agent_kwargs(
    cell: MatrixCell,
    *,
    user: str,
    system_prompt: str | None,
    env: Mapping[str, str],
    worker: Model | None = None,
) -> dict[str, Any]:
    """Keyword arguments for ``inspect_swe.claude_code``.

    ``worker`` is the subagent model (``cell.subagent_model``), resolved by the caller
    inside the eval so its provider arguments apply.
    """
    harness = cell.harness
    reserved = RESERVED_OPTIONS & set(harness.options)
    if reserved:
        raise ValueError(
            f"harness '{harness.name}': options {sorted(reserved)} are set by agenteval"
        )
    prompts = [p for p in (harness.system_prompt, system_prompt, delegation_note(cell)) if p]
    kwargs: dict[str, Any] = {"cwd": cell.environment.workspace}
    if prompts:
        kwargs["system_prompt"] = "\n\n".join(prompts)
    subagents = harness.subagents
    if subagents is not None:
        if worker is None or cell.subagent_model is None:
            raise ValueError(f"harness '{harness.name}': subagent model not resolved")
        if subagents.runner == "claude_code":
            # inspect_swe derives the routing alias from this name; the alias below
            # replaces its argument-less model with ours (provider args included)
            kwargs["subagent_model"] = cell.subagent_model.inspect_model
            kwargs["model_aliases"] = {worker.name: worker}
        else:
            kwargs["model_aliases"] = {WORKER_MODEL_NAME: worker}
    kwargs |= harness.options
    kwargs |= {"version": "sandbox", "user": user, "env": dict(env)}
    return kwargs


async def _write_user_file(sbox: SandboxEnvironment, user: str, path: str, content: str) -> None:
    result = await sbox.exec(
        ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat >"$1"', "sh", path],
        input=content,
        user=user,
    )
    if not result.success:
        raise RuntimeError(f"cannot write {path} as '{user}': {result.stderr.strip()}")


@solver
def install_agent(cell: MatrixCell, user: str | None = None) -> Solver:
    """Install pinned Claude Code + its sandbox wrapper, and the configured subagents."""
    run_user = user or cell.environment.agent_user
    harness = cell.harness

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sbox = sandbox()
        binary = await ensure_agent_binary_installed(
            claude_code_binary_source(), harness.version, run_user, sbox
        )
        if binary == CLAUDE_WRAPPER:
            current = await sbox.read_file(CLAUDE_WRAPPER)
            if WRAPPER_MARKER not in current:
                raise RuntimeError(
                    f"claude is installed at {CLAUDE_WRAPPER}; agenteval needs that path "
                    "for its sandbox wrapper. Pin a version in the harness profile instead."
                )
        else:
            await install_root_file(sbox, CLAUDE_WRAPPER, render_wrapper(binary), "0755")

        found = await sbox.exec(["bash", "-c", "command -v claude"], user=run_user)
        if not found.success or found.stdout.strip() != CLAUDE_WRAPPER:
            raise RuntimeError(
                f"`command -v claude` as '{run_user}' resolves to "
                f"{found.stdout.strip() or '<nothing>'}, expected {CLAUDE_WRAPPER}"
            )

        home = await user_home(sbox, run_user)
        await _write_user_file(sbox, run_user, f"{home}/.claude/settings.json", SEEDED_SETTINGS)
        subagents = harness.subagents
        if subagents is not None and subagents.runner == "claude_code":
            for name, agent in subagents.agents.items():
                path = f"{home}/.claude/agents/{name}.md"
                await _write_user_file(sbox, run_user, path, render_subagent(name, agent))
        elif subagents is not None and subagents.runner == "opencode":
            oc_binary, dep_dirs = await ensure_opencode_setup(
                sbox, subagents.opencode_version, run_user
            )
            await install_root_file(sbox, OPENCODE_WORKER, render_worker(oc_binary, dep_dirs))
        return state

    return solve


@solver
def run_agent(
    cell: MatrixCell,
    user: str | None = None,
    system_prompt: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> Solver:
    """Run Claude Code; built per sample so ambient canaries reach its env."""
    run_user = user or cell.environment.agent_user

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        env = agent_env(cell, state.store.get(constants.STORE_CANARIES), extra_env)
        worker = worker_model(cell.subagent_model) if cell.subagent_model else None
        agent: Agent = claude_code(
            **agent_kwargs(cell, user=run_user, system_prompt=system_prompt, env=env, worker=worker)
        )
        return await as_solver(agent)(state, generate)

    return solve
