"""Pieces the OpenCode and Claude Code harnesses share.

The agent env (harness env, ambient canaries, extra env), the keyword arguments for
the inspect_swe agent factory, the sandbox-wrapper install and the check of a harness
profile's ``options`` against the factory's signature.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Unpack, get_args, get_origin

from inspect_ai.util import SandboxEnvironment

from agenteval.config import ConfigError, HarnessProfile, MatrixCell
from agenteval.harnesses.sandbox_exec import install_root_file


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


def check_reserved(harness: HarnessProfile, reserved: frozenset[str]) -> None:
    clash = reserved & set(harness.options)
    if clash:
        raise ConfigError(f"harness '{harness.name}': options {sorted(clash)} are set by agenteval")


def factory_kwargs(
    cell: MatrixCell,
    *,
    prompts: Iterable[str | None],
    extra: Mapping[str, Any],
    user: str,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Factory arguments: workspace cwd, the non-empty ``prompts`` joined, ``extra``, then
    the profile's ``options`` (which may override those), then the arguments agenteval
    owns. Check the reserved options first (``check_reserved``)."""
    joined = [p for p in prompts if p]
    kwargs: dict[str, Any] = {"cwd": cell.environment.workspace}
    if joined:
        kwargs["system_prompt"] = "\n\n".join(joined)
    kwargs |= extra
    kwargs |= cell.harness.options
    kwargs |= {"version": "sandbox", "user": user, "env": dict(env)}
    return kwargs


async def install_wrapper(
    sbox: SandboxEnvironment,
    *,
    command: str,
    binary: str,
    wrapper: str,
    marker: str,
    render: Callable[[str], str],
    run_user: str,
) -> None:
    """Put the root-owned ``wrapper`` (``render(binary)``) at ``wrapper`` and check that
    ``command`` resolves to it for ``run_user``.

    ``binary == wrapper`` means inspect_swe found the command via ``which`` ("auto" and
    "sandbox" versions): our own wrapper (a re-run) is accepted, a real binary there is
    refused rather than wrapped in place.
    """
    if binary == wrapper:
        current = await sbox.read_file(wrapper)
        if marker not in current:
            raise RuntimeError(
                f"{command} is installed at {wrapper}; agenteval needs that path "
                "for its sandbox wrapper. Pin a version in the harness profile instead."
            )
    else:
        await install_root_file(sbox, wrapper, render(binary), "0755")

    found = await sbox.exec(["bash", "-c", f"command -v {command}"], user=run_user)
    if not found.success or found.stdout.strip() != wrapper:
        raise RuntimeError(
            f"`command -v {command}` as '{run_user}' resolves to "
            f"{found.stdout.strip() or '<nothing>'}, expected {wrapper}"
        )


def factory_options(factory: Callable[..., object]) -> frozenset[str] | None:
    """Keyword arguments ``factory`` accepts, or None if it takes arbitrary ``**kwargs``.

    ``**kwargs: Unpack[SomeTypedDict]`` (claude_code's deprecated ``auto_mode``) only
    takes that TypedDict's keys, so they count as named arguments.
    """
    params = inspect.signature(factory).parameters.values()
    named = {
        p.name
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    var_kw = next((p for p in params if p.kind is inspect.Parameter.VAR_KEYWORD), None)
    if var_kw is None:
        return frozenset(named)
    if get_origin(var_kw.annotation) is Unpack:
        keys = getattr(get_args(var_kw.annotation)[0], "__annotations__", None)
        if isinstance(keys, dict):
            return frozenset(named | set(keys))
    return None


def check_options(
    harness: HarnessProfile, factory: Callable[..., object], reserved: frozenset[str]
) -> None:
    """Reject ``harness.options`` keys that agenteval sets itself or that ``factory``
    does not take, before a run starts instead of inside a sample."""
    check_reserved(harness, reserved)
    accepted = factory_options(factory)
    if accepted is None:  # open **kwargs: only the reserved keys can be checked
        return
    unknown = sorted(set(harness.options) - accepted)
    if unknown:
        raise ConfigError(
            f"harness '{harness.name}': unknown options {unknown} for inspect_swe "
            f"{factory.__name__}(); accepted: {', '.join(sorted(accepted - reserved))}"
        )
