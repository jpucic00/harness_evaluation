"""Environments: where every sample runs (a fresh Tart VM, Docker container or sbx microVM).

Each provider implements ``base.Provider``; ``PROVIDERS`` maps profile ``provider``
names to them and everything else dispatches through it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from agenteval.config import EnvironmentProfile
from agenteval.environments.base import (
    PROVISION_DIR,
    PROVISION_SCRIPT,
    CommandRunner,
    Echo,
    Provider,
    run_command,
)
from agenteval.environments.docker import DockerProvider
from agenteval.environments.sbx.provider import SbxProvider
from agenteval.environments.tart.provider import TartProvider

if TYPE_CHECKING:
    from inspect_ai.util import SandboxEnvironmentSpec

__all__ = [
    "PROVIDERS",
    "PROVISION_DIR",
    "PROVISION_SCRIPT",
    "build_environment",
    "cleanup_environments",
    "environment_identity",
    "get_provider",
    "provision_sha256",
    "sandbox_spec",
]

PROVIDERS: dict[str, Provider] = {
    p.name: p for p in (DockerProvider(), TartProvider(), SbxProvider())
}
"""Providers by profile ``provider`` name, in ``agenteval doctor`` order."""


def get_provider(env: EnvironmentProfile) -> Provider:
    try:
        return PROVIDERS[env.provider]
    except KeyError:
        raise ValueError(
            f"environment '{env.name}': unsupported provider '{env.provider}'"
        ) from None


def sandbox_spec(
    env: EnvironmentProfile,
    *,
    inner_sandbox: Literal["none", "bwrap"] = "none",
    mounts: Sequence[str] = (),
    filtered_network: bool = False,
) -> SandboxEnvironmentSpec:
    """Inspect sandbox for an environment profile, in a cell with ``inner_sandbox``.

    Docker environments get a compose file generated under ``.agenteval/compose/``
    (relative to the current directory); the spec carries its absolute path. Only
    ``inner_sandbox="bwrap"`` adds ``DockerSettings.bwrap_security_opt``: every other
    cell keeps stock Docker confinement. Tart VMs are the same for both; bwrap access
    there is granted per sample through group membership.

    ``mounts`` shares host directories at the same path (``agenteval dev --mount live``
    only): a bind mount for Docker, ``tart run --dir`` for Tart, a workspace for sbx.

    ``filtered_network`` gives an egress-denying Docker container a network, with the
    agent user's egress denied by ``egress.sh`` instead (see ``docker.compose_config``).
    Tart VMs always work that way.
    """
    return get_provider(env).spec(
        env, inner_sandbox=inner_sandbox, mounts=mounts, filtered_network=filtered_network
    )


def build_environment(env: EnvironmentProfile, *, force: bool = False) -> None:
    get_provider(env).build(env, force=force)


def cleanup_environments(*, echo: Echo = print, runner: CommandRunner = run_command) -> int:
    """Delete leftover agenteval Tart VMs, sbx sandboxes and Docker compose projects.

    Returns an exit code.
    """
    code = 0
    for provider in sorted(PROVIDERS.values(), key=lambda p: p.cleanup_last):
        code = provider.cleanup(echo, runner) or code
    return code


def provision_sha256() -> str | None:
    """sha256 of the provision.sh shipped with this agenteval (images record theirs too)."""
    try:
        return hashlib.sha256(PROVISION_SCRIPT.read_bytes()).hexdigest()
    except OSError:
        return None


def environment_identity(env: EnvironmentProfile) -> dict[str, str | None]:
    """What results depend on beyond the profile: the built image and its provisioning.

    Base images are pinned by (mutable) tag, so the Docker image ID is what ties results
    to the exact image. A built image's own provision.sh hash is in
    /etc/agenteval/image.json inside it.
    """
    identity: dict[str, str | None] = {
        "provider": env.provider,
        "provision_sha256": provision_sha256(),
    }
    provider = PROVIDERS.get(env.provider)
    if provider is not None:
        identity.update(provider.identity(env))
    return identity
