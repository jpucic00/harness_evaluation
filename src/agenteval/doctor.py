"""``agenteval doctor``: check the host, the profiles and the environments' tooling.

Provider-specific checks (tool versions, images, SSH keys, host settings) come from
each provider in ``agenteval.environments``; this module runs them in order and adds
the provider-independent ones.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

from agenteval.config import (
    ConfigError,
    EnvironmentProfile,
    ProfileSet,
    expand_matrix,
    load_profiles,
    load_run,
    missing_env,
)
from agenteval.environments import PROVIDERS
from agenteval.environments.base import (
    CommandRunner,
    DoctorCheck,
    Status,
    first_line,
    run_command,
)
from agenteval.profiles import run_files, table
from agenteval.run.plan import load_dotenv_file

__all__ = ["DoctorCheck", "Status", "doctor", "format_checks"]


def _tool_checks(runner: CommandRunner) -> list[DoctorCheck]:
    """Python, uv and the installed packages."""
    checks: list[DoctorCheck] = []
    py = sys.version_info
    checks.append(
        DoctorCheck(
            "python",
            "ok" if py >= (3, 12) else "fail",
            f"{py.major}.{py.minor}.{py.micro} ({sys.executable})",
        )
    )
    if shutil.which("uv"):
        rc, out = runner(["uv", "--version"], 15)
        checks.append(DoctorCheck("uv", "ok" if rc == 0 else "warn", first_line(out)))
    else:
        checks.append(DoctorCheck("uv", "warn", "not on PATH (needed to install/sync the project)"))

    for dist in ("agenteval", "inspect-ai", "inspect-swe", "inspect-evals"):
        try:
            checks.append(DoctorCheck(dist, "ok", metadata.version(dist)))
        except metadata.PackageNotFoundError:
            checks.append(DoctorCheck(dist, "fail", "not installed (run `uv sync`)"))
    registered = any(ep.name == "agenteval" for ep in metadata.entry_points(group="inspect_ai"))
    checks.append(
        DoctorCheck(
            "inspect entry point",
            "ok" if registered else "warn",
            "agenteval sandboxes registered"
            if registered
            else "agenteval not registered with Inspect (reinstall with `uv sync`)",
        )
    )
    return checks


def _run_checks(config_dir: Path, profiles: ProfileSet) -> list[DoctorCheck]:
    """Every run file expands against the profiles."""
    checks: list[DoctorCheck] = []
    for path in run_files(config_dir):
        try:
            cells = expand_matrix(load_run(path), profiles)
            checks.append(DoctorCheck(f"run {path.stem}", "ok", f"{len(cells)} tasks"))
        except ConfigError as ex:
            checks.append(DoctorCheck(f"run {path.stem}", "fail", str(ex)))
    return checks


def _environment_checks(
    environments: Sequence[EnvironmentProfile],
    profiles: ProfileSet,
    runner: CommandRunner,
    problem: Status,
) -> list[DoctorCheck]:
    """Provider tooling first, then each environment, then host settings."""
    checks: list[DoctorCheck] = []
    used = {e.provider for e in environments}
    providers = [p for name, p in PROVIDERS.items() if name in used]
    available: dict[str, bool] = {}
    for provider in providers:
        available[provider.name], found = provider.tool_checks(runner, problem)
        checks += found
    for env in environments:
        if not available.get(env.provider, False):
            checks.append(
                DoctorCheck(
                    f"env {env.name}", problem, f"{env.provider} unavailable; image not checked"
                )
            )
            continue
        checks += PROVIDERS[env.provider].env_checks(env, runner, problem)
    for provider in providers:
        checks += provider.host_checks(environments, profiles)
    return checks


def _env_var_checks(profiles: ProfileSet) -> list[DoctorCheck]:
    """``.env`` and each model's required variables, by name only."""
    checks: list[DoctorCheck] = []
    dotenv = load_dotenv_file()
    if dotenv is not None:
        checks.append(
            DoctorCheck(".env", "ok", f"loaded {dotenv} (shell variables take precedence)")
        )
    for model in profiles.models.values():
        for var in model.required_env:
            unset = var in missing_env([model])
            checks.append(
                DoctorCheck(
                    f"env var {var}",
                    "warn" if unset else "ok",
                    f"{'not set' if unset else 'set'} (model {model.name})",
                )
            )
    return checks


def doctor(
    config_dir: Path, env_name: str | None = None, *, runner: CommandRunner = run_command
) -> list[DoctorCheck]:
    """Check the host. Problems with the selected ``--env`` are failures, others warnings.

    Required env vars are reported by name only; their values are never read out.
    """
    checks = _tool_checks(runner)
    try:
        profiles = load_profiles(config_dir)
    except ConfigError as ex:
        checks.append(DoctorCheck("config", "fail", str(ex)))
        return checks
    checks.append(
        DoctorCheck(
            "config",
            "ok",
            f"{config_dir}: {len(profiles.models)} models, {len(profiles.harnesses)} harnesses, "
            f"{len(profiles.environments)} environments",
        )
    )
    checks += _run_checks(config_dir, profiles)

    if env_name is not None:
        if env_name not in profiles.environments:
            checks.append(
                DoctorCheck(
                    f"env {env_name}",
                    "fail",
                    f"unknown; available: {sorted(profiles.environments)}",
                )
            )
            return checks
        environments = [profiles.environments[env_name]]
    else:
        environments = list(profiles.environments.values())
    problem: Status = "fail" if env_name is not None else "warn"
    checks += _environment_checks(environments, profiles, runner, problem)
    checks += _env_var_checks(profiles)
    return checks


def format_checks(checks: Sequence[DoctorCheck]) -> str:
    return table(
        ["status", "check", "detail"], [[c.status.upper(), c.name, c.detail] for c in checks]
    )
