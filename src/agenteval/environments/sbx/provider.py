"""The ``sbx`` environment provider (see ``agenteval.environments.base.Provider``).

``sandbox`` imports Inspect, so it and the ``sbx`` CLI helpers are imported only when a
method needs them.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agenteval.config import ConfigError, DockerSettings, EnvironmentProfile
from agenteval.environments.base import (
    CommandRunner,
    DoctorCheck,
    Echo,
    InnerSandboxType,
    Provider,
    Status,
    inspect_cmd,
)

if TYPE_CHECKING:
    from inspect_ai.util import SandboxEnvironmentSpec


def status() -> tuple[list[str], list[str]]:
    """(problems, warnings) for Docker Sandboxes, without sending sbx telemetry."""
    from agenteval.environments.sbx.cli import SbxError, readiness_problems, setting_warnings

    try:
        problems = readiness_problems()
        return problems, [] if problems else setting_warnings()
    except SbxError as ex:
        return [str(ex)], []


def template_id(image: str) -> str | None:
    from agenteval.environments.sbx.cli import template_id as sbx_template_id

    return sbx_template_id(image)


class SbxProvider(Provider):
    name = "sbx"

    def spec(
        self,
        env: EnvironmentProfile,
        *,
        inner_sandbox: InnerSandboxType,
        mounts: Sequence[str],
        filtered_network: bool,
    ) -> SandboxEnvironmentSpec:
        from inspect_ai.util import SandboxEnvironmentSpec

        from agenteval.environments.sbx.sandbox import SbxSandboxConfig

        if env.sbx is None:  # filled in by EnvironmentProfile's validator
            raise ValueError(f"environment '{env.name}' has no sbx settings")
        if filtered_network:
            raise ValueError(
                f"environment '{env.name}': sbx environments do not support Claude subscription "
                "sessions yet (they need the egress proxy's network); use docker or tart"
            )
        sbx_config = SbxSandboxConfig(
            **env.sbx.model_dump(), default_cwd=env.workspace, shared_dirs=tuple(mounts)
        )
        return SandboxEnvironmentSpec("sbx", sbx_config)

    def image_label(self, env: EnvironmentProfile) -> str:
        return f"{env.sbx.template} (sbx template)" if env.sbx is not None else "-"

    def identity(self, env: EnvironmentProfile) -> dict[str, str | None]:
        from agenteval.environments.docker import image_id
        from agenteval.environments.sbx.cli import SbxError

        if env.sbx is None:
            return {}
        identity: dict[str, str | None] = {
            "image": env.sbx.image,
            "image_id": image_id(env.sbx.image),
            "template": env.sbx.template,
        }
        try:
            identity["template_id"] = template_id(env.sbx.template)
        except SbxError:
            identity["template_id"] = None
        return identity

    def build(self, env: EnvironmentProfile, *, force: bool) -> None:
        """Build the image with Docker, derive the sbx template, and load it into sbx."""
        from agenteval.environments.docker import build_image, image_id
        from agenteval.environments.sbx.cli import SbxError, build_template, load_template

        if env.sbx is None:
            raise ConfigError(f"environment '{env.name}' has no sbx settings")
        settings = env.sbx
        docker_env = env.model_copy(
            update={
                "provider": "docker",
                "docker": DockerSettings(image=settings.image),
                "sbx": None,
            }
        )
        build_image(docker_env, force=force)
        try:
            build_template(settings.image, settings.template)
            load_template(settings.template)
            loaded = template_id(settings.template)
        except SbxError as ex:
            raise ConfigError(str(ex)) from ex
        built = image_id(settings.template)
        if loaded is None or (built and not built.removeprefix("sha256:").startswith(loaded)):
            raise ConfigError(
                f"sbx template {settings.template} ({loaded or 'missing'}) does not match the "
                f"Docker image ({built or 'unknown'}) after loading it"
            )

    def cleanup(self, echo: Echo, runner: CommandRunner) -> int:
        if not shutil.which("sbx"):
            echo("sbx: not installed, skipped")
            return 0
        echo("sbx: removing leftover agenteval sandboxes")
        rc, out = runner(inspect_cmd("sandbox", "cleanup", "sbx"), 600)
        if out:
            echo(out)
        return 1 if rc else 0

    def tool_checks(self, runner: CommandRunner, problem: Status) -> tuple[bool, list[DoctorCheck]]:
        problems, warnings = status()
        checks = [DoctorCheck("sbx", problem, text) for text in problems]
        checks += [DoctorCheck("sbx", "warn", text) for text in warnings]
        if not problems:
            checks.append(DoctorCheck("sbx", "ok", "signed in; SSH agent forwarding off"))
        return not problems, checks

    def env_checks(
        self, env: EnvironmentProfile, runner: CommandRunner, problem: Status
    ) -> list[DoctorCheck]:
        if env.sbx is None:
            return []
        label = f"env {env.name}"
        try:
            loaded = template_id(env.sbx.template)
        except Exception as ex:
            return [DoctorCheck(label, problem, f"could not list sbx templates: {ex}")]
        return [
            DoctorCheck(
                label,
                "ok" if loaded else problem,
                f"sbx template {env.sbx.template} "
                + ("present" if loaded else f"missing (agenteval env build {env.name})"),
            )
        ]
