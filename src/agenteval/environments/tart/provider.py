"""The ``tart`` environment provider (see ``agenteval.environments.base.Provider``).

``image`` and ``sandbox`` import Inspect and anyio, so they are imported only when a
method needs them.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agenteval.config import ConfigError, EnvironmentProfile, ProfileSet
from agenteval.environments.base import (
    CommandRunner,
    DoctorCheck,
    Echo,
    InnerSandboxType,
    Provider,
    Status,
    first_line,
    inspect_cmd,
)

if TYPE_CHECKING:
    from inspect_ai.util import SandboxEnvironmentSpec


def dhcp_lease_check(plist: Path | None = None) -> DoctorCheck:
    """Tart's nat network uses macOS bootpd, whose 1-day leases run out after ~253 VMs/day."""
    from agenteval.environments.tart.image import (
        DHCP_PLIST,
        dhcp_lease_problem,
        dhcp_lease_seconds,
    )

    path = plist or DHCP_PLIST
    problem = dhcp_lease_problem(path)
    if problem:
        return DoctorCheck("tart dhcp lease", "warn", problem)
    return DoctorCheck("tart dhcp lease", "ok", f"DHCPLeaseTimeSecs={dhcp_lease_seconds(path)}")


def image_exists(name: str) -> bool:
    from agenteval.environments.tart.image import image_exists as tart_image_exists

    exists: bool = asyncio.run(tart_image_exists(name))
    return exists


class TartProvider(Provider):
    name = "tart"

    def spec(
        self,
        env: EnvironmentProfile,
        *,
        inner_sandbox: InnerSandboxType,
        mounts: Sequence[str],
        filtered_network: bool,
    ) -> SandboxEnvironmentSpec:
        from inspect_ai.util import SandboxEnvironmentSpec

        from agenteval.environments.tart.sandbox import TartSandboxConfig

        if env.tart is None:  # filled in by EnvironmentProfile's validator
            raise ValueError(f"environment '{env.name}' has no tart settings")
        fields = env.tart.model_dump() | {"default_cwd": env.workspace}
        if mounts:  # only dev live sessions; eval specs stay as they were
            fields["shared_dirs"] = tuple(mounts)
        config = TartSandboxConfig(**fields)
        return SandboxEnvironmentSpec("tart", config)

    def image_label(self, env: EnvironmentProfile) -> str:
        return env.tart.image if env.tart is not None else "-"

    def identity(self, env: EnvironmentProfile) -> dict[str, str | None]:
        if env.tart is None:
            return {}
        return {"image": env.tart.image, "base_image": env.tart.base_image}

    def build(self, env: EnvironmentProfile, *, force: bool) -> None:
        from agenteval.environments import PROVISION_SCRIPT
        from agenteval.environments.tart.image import build_image

        if env.tart is None:
            raise ConfigError(f"environment '{env.name}' has no tart settings")
        script = PROVISION_SCRIPT
        if not script.is_file():
            raise ConfigError(f"provision script missing: {script}")
        asyncio.run(
            build_image(
                env.tart,
                script,
                force=force,
                agent_user=env.agent_user,
                workspace=env.workspace,
            )
        )

    def cleanup(self, echo: Echo, runner: CommandRunner) -> int:
        if not shutil.which("tart"):
            echo("tart: not installed, skipped")
            return 0
        echo("tart: deleting leftover agenteval VMs")
        rc, out = runner(inspect_cmd("sandbox", "cleanup", "tart"), 600)
        if out:
            echo(out)
        return 1 if rc else 0

    def tool_checks(self, runner: CommandRunner, problem: Status) -> tuple[bool, list[DoctorCheck]]:
        if sys.platform != "darwin":
            return False, [DoctorCheck("tart", problem, "Tart needs a macOS host on Apple silicon")]
        if shutil.which("tart"):
            rc, out = runner(["tart", "--version"], 15)
            return rc == 0, [DoctorCheck("tart", "ok" if rc == 0 else problem, first_line(out))]
        from agenteval.environments.tart.image import TART_RELEASES_URL

        detail = (
            f"not on PATH; install the release tarball from {TART_RELEASES_URL} "
            "(the Homebrew formula is broken)"
        )
        return False, [DoctorCheck("tart", problem, detail)]

    def env_checks(
        self, env: EnvironmentProfile, runner: CommandRunner, problem: Status
    ) -> list[DoctorCheck]:
        if env.tart is None:
            return []
        label = f"env {env.name}"
        checks: list[DoctorCheck] = []
        try:
            exists = image_exists(env.tart.image)
            checks.append(
                DoctorCheck(
                    label,
                    "ok" if exists else problem,
                    f"VM image {env.tart.image} "
                    + ("present" if exists else f"missing (agenteval env build {env.name})"),
                )
            )
        except Exception as ex:
            checks.append(
                DoctorCheck(label, problem, f"could not check VM image: {type(ex).__name__}: {ex}")
            )
        key = Path(env.tart.ssh_key).expanduser()
        if not key.is_file():
            checks.append(
                DoctorCheck(f"{label} ssh key", problem, f"{key} missing (created by env build)")
            )
        elif key.stat().st_mode & 0o077:
            checks.append(
                DoctorCheck(
                    f"{label} ssh key", "warn", f"{key} is group/world accessible; chmod 600"
                )
            )
        else:
            checks.append(DoctorCheck(f"{label} ssh key", "ok", str(key)))
        return checks

    def host_checks(
        self, environments: Sequence[EnvironmentProfile], profiles: ProfileSet
    ) -> list[DoctorCheck]:
        if sys.platform == "darwin" and any(
            e.provider == "tart" and e.tart is not None and e.tart.network == "nat"
            for e in environments
        ):
            return [dhcp_lease_check()]
        return []
