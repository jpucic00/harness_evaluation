"""What every environment provider implements, and the helpers they share.

A provider (``docker``, ``tart``, ``sbx``) turns an ``EnvironmentProfile`` into an
Inspect sandbox spec and answers the host-side questions about it: how to build its
image, what identifies a built image, how to delete leftovers and what ``agenteval
doctor`` checks. The registry in ``agenteval.environments`` maps profile ``provider``
names to instances. Nothing here imports Inspect or a provider CLI at module level, so
``profiles``, ``doctor`` and ``run --dry-run`` stay fast.
"""

from __future__ import annotations

import subprocess
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from agenteval.config import EnvironmentProfile, ProfileSet

if TYPE_CHECKING:
    from inspect_ai.util import SandboxEnvironmentSpec

PROVISION_DIR = Path(__file__).parent / "provision"
"""Docker build context: ``Dockerfile`` + ``provision.sh``."""
PROVISION_SCRIPT = PROVISION_DIR / "provision.sh"
"""Idempotent Ubuntu 24.04 provisioning shared by the Tart image and the Docker image."""

Echo = Callable[[str], None]
InnerSandboxType = Literal["none", "bwrap"]

# --------------------------------------------------------------------------- host commands

CommandRunner = Callable[[Sequence[str], float], tuple[int, str]]


def run_command(cmd: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run without a shell; (returncode, combined output). 127 = not found, 124 = timeout."""
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout:g}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def inspect_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "inspect_ai", *args]


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


# --------------------------------------------------------------------------- doctor

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: Status
    detail: str


# --------------------------------------------------------------------------- provider


class Provider(ABC):
    """One environment technology. Instances are stateless; every method takes the profile."""

    name: ClassVar[str]
    """The profile's ``provider`` value."""
    cleanup_last: ClassVar[bool] = False
    """Run ``cleanup`` after the other providers' (``env cleanup`` output order)."""

    @abstractmethod
    def spec(
        self,
        env: EnvironmentProfile,
        *,
        inner_sandbox: InnerSandboxType,
        mounts: Sequence[str],
        filtered_network: bool,
    ) -> SandboxEnvironmentSpec:
        """Inspect sandbox for ``env`` (see ``agenteval.environments.sandbox_spec``)."""

    @abstractmethod
    def image_label(self, env: EnvironmentProfile) -> str:
        """The image column of ``agenteval profiles``."""

    @abstractmethod
    def identity(self, env: EnvironmentProfile) -> dict[str, str | None]:
        """Image fields recorded in the eval log metadata, beyond provider and provisioning."""

    @abstractmethod
    def build(self, env: EnvironmentProfile, *, force: bool) -> None:
        """``agenteval env build``: build or rebuild the image ``env`` runs."""

    @abstractmethod
    def cleanup(self, echo: Echo, runner: CommandRunner) -> int:
        """``agenteval env cleanup``: delete this provider's leftovers. Returns an exit code.

        A provider whose tooling is not installed says so and returns 0.
        """

    @abstractmethod
    def tool_checks(self, runner: CommandRunner, problem: Status) -> tuple[bool, list[DoctorCheck]]:
        """Doctor checks of the provider's host tooling, and whether it is usable."""

    @abstractmethod
    def env_checks(
        self, env: EnvironmentProfile, runner: CommandRunner, problem: Status
    ) -> list[DoctorCheck]:
        """Doctor checks of one environment (its image, keys) once the tooling works."""

    def host_checks(
        self, environments: Sequence[EnvironmentProfile], profiles: ProfileSet
    ) -> list[DoctorCheck]:
        """Doctor checks of host settings that affect this provider's ``environments``."""
        return []
