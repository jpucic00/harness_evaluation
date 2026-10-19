"""Environments: where every sample runs (a fresh Tart VM or Docker container)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from inspect_ai.util import SandboxEnvironmentSpec

from agenteval.config import EnvironmentProfile

PROVISION_DIR = Path(__file__).parent / "provision"
"""Docker build context: ``Dockerfile`` + ``provision.sh``."""
PROVISION_SCRIPT = PROVISION_DIR / "provision.sh"
"""Idempotent Ubuntu 24.04 provisioning shared by the Tart image and the Docker image."""


def sandbox_spec(
    env: EnvironmentProfile, *, inner_sandbox: Literal["none", "bwrap"] = "none"
) -> SandboxEnvironmentSpec:
    """Inspect sandbox for an environment profile, in a cell with ``inner_sandbox``.

    Docker environments get a compose file generated under ``.agenteval/compose/``
    (relative to the current directory); the spec carries its absolute path. Only
    ``inner_sandbox="bwrap"`` adds ``DockerSettings.bwrap_security_opt``: every other
    cell keeps stock Docker confinement. Tart VMs are the same for both; bwrap access
    there is granted per sample through group membership.
    """
    if env.provider == "tart":
        from .tart.sandbox import TartSandboxConfig

        if env.tart is None:  # filled in by EnvironmentProfile's validator
            raise ValueError(f"environment '{env.name}' has no tart settings")
        config = TartSandboxConfig(**env.tart.model_dump(), default_cwd=env.workspace)
        return SandboxEnvironmentSpec("tart", config)
    if env.provider == "docker":
        from .docker import write_compose_file

        path = write_compose_file(env, inner_sandbox=inner_sandbox)
        return SandboxEnvironmentSpec("docker", path.as_posix())
    raise ValueError(f"environment '{env.name}': unsupported provider '{env.provider}'")
