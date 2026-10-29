"""Inspect sandbox provider: every sample runs in a fresh Tart Linux VM.

- task_init:      checks Tart, the prebuilt image (``agenteval env build``) and the SSH key.
- sample_init:    ``tart clone`` the image to ``agenteval-<8hex>``, ``tart set`` cpus/memory,
                  ``tart run --no-graphics`` (no host folder sharing), ``tart ip --wait``,
                  then poll SSH until it answers.
- exec/files:     over ``ssh`` with a dedicated key; commands switch user with ``sudo -n``.
- sample_cleanup: stop and delete the VM. task_cleanup deletes whatever is left;
                  ``inspect sandbox cleanup tart`` deletes every ``agenteval-<8hex>`` VM.

Usage: ``SandboxEnvironmentSpec("tart", TartSandboxConfig(...))``.
"""

from __future__ import annotations

import logging
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, override

import anyio
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.util import (
    ExecResult,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    sandboxenv,
)
from pydantic import BaseModel, Field, field_validator

from agenteval.config import TartSettings, size_mb
from agenteval.environments import guest
from agenteval.environments.guest import ExecGuestSandbox, check_timeout

from . import ssh
from .image import (
    TART_MISSING_MESSAGE,
    TartVM,
    cleanup_vms,
    dhcp_lease_problem,
    image_exists,
    new_vm_name,
    tart_available,
)
from .ssh import SSHTarget, remote_command, ssh_argv, ssh_run, validate_user, wait_for_ssh

logger = logging.getLogger(__name__)

_DEFAULT_PASSWORD: str = TartSettings.model_fields["bootstrap_password"].default


class TartSandboxConfig(TartSettings):
    """``TartSettings`` plus where and as whom commands run by default."""

    bootstrap_password: str = Field(default=_DEFAULT_PASSWORD, exclude=True, repr=False)
    """Only used by ``agenteval env build``; excluded so it never reaches eval logs."""
    default_cwd: str
    """Working directory for exec() and base for relative file paths."""
    default_user: str = "root"
    """User for exec(user=None), write_file and read_file (needs ``use_sudo``)."""
    shared_dirs: tuple[str, ...] = ()
    """Host directories shared with ``tart run --dir`` (``agenteval dev --mount live``
    only); share ``i`` is named ``agenteval<i>``. Eval runs share nothing."""

    @field_validator("default_cwd")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("default_cwd must be an absolute path")
        return value


def resolve_config(config: SandboxEnvironmentConfigType | None) -> TartSandboxConfig:
    return guest.resolve_config(
        config,
        TartSandboxConfig,
        'The "tart" sandbox needs a TartSandboxConfig: '
        'SandboxEnvironmentSpec("tart", TartSandboxConfig(...)).',
        from_file=True,
    )


@dataclass
class _TartCleanupState:
    vms: dict[str, TartVM] = field(default_factory=dict)


_process_state = _TartCleanupState()


def _cleanup_state() -> _TartCleanupState:
    return guest.lifecycle_state(_TartCleanupState, _process_state)


_dhcp_warned = False


def _warn_dhcp_once() -> None:
    global _dhcp_warned
    if _dhcp_warned:
        return
    _dhcp_warned = True
    problem = dhcp_lease_problem()
    if problem is not None:
        logger.warning(problem)


@sandboxenv(name="tart")
class TartSandboxEnvironment(ExecGuestSandbox):
    config_type: ClassVar[type[BaseModel]] = TartSandboxConfig
    _config: TartSandboxConfig

    def __init__(self, vm: TartVM, config: TartSandboxConfig, target: SSHTarget) -> None:
        super().__init__()
        self._vm = vm
        self._config = config
        self._target = target
        self._known_users: set[str] = set()

    @property
    def vm_name(self) -> str:
        return self._vm.name

    @property
    def target(self) -> SSHTarget:
        return self._target

    @property
    @override
    def _guest_name(self) -> str:
        return self._vm.name

    # ----------------------------------------------------------------- lifecycle

    @classmethod
    def default_concurrency(cls) -> int | None:
        return 2

    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        cfg = resolve_config(config)
        if tart_available() is None:
            raise PrerequisiteError(TART_MISSING_MESSAGE)
        if not await image_exists(cfg.image):
            raise PrerequisiteError(
                f"Tart image '{cfg.image}' not found. Build it with `agenteval env build <env>`."
            )
        key = Path(cfg.ssh_key).expanduser()
        if not key.is_file():
            raise PrerequisiteError(
                f"SSH key {key} not found. It is created by `agenteval env build <env>`."
            )
        if cfg.network == "nat" and sys.platform == "darwin":
            _warn_dhcp_once()

    @classmethod
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        cfg = resolve_config(config)
        state = _cleanup_state()
        vm = TartVM(new_vm_name())
        state.vms[vm.name] = vm
        try:
            started = time.monotonic()
            await vm.clone(cfg.image)
            await vm.configure(cpus=cfg.cpus, memory_mb=size_mb(cfg.memory))
            await vm.start(network=cfg.network, dirs=cfg.shared_dirs)
            ip = await vm.wait_ip(cfg.boot_timeout)
            target = SSHTarget(host=ip, user=cfg.ssh_user, key=Path(cfg.ssh_key).expanduser())
            remaining = max(10.0, cfg.boot_timeout - (time.monotonic() - started))
            await wait_for_ssh(target, remaining)
            if cfg.use_sudo:
                check = await ssh_run(
                    target, shlex.join([ssh.REMOTE_TOOLS.sudo, "-n", "true"]), timeout=60
                )
                if not check.success:
                    raise RuntimeError(
                        f"{cfg.ssh_user} cannot run sudo without a password in {vm.name}: "
                        f"{check.stderr.decode(errors='replace').strip()}"
                    )
            # Every exec without cwd starts with `cd default_cwd`; fail clearly if the
            # image was provisioned for another workspace.
            cwd = await ssh_run(target, shlex.join(["test", "-d", cfg.default_cwd]), timeout=60)
            if not cwd.success:
                raise RuntimeError(
                    f"Working directory {cfg.default_cwd} does not exist in Tart image "
                    f"'{cfg.image}'. Was it built for another workspace? Rebuild it with "
                    "`agenteval env build <env> --force`."
                )
        except BaseException:
            await vm.destroy()
            state.vms.pop(vm.name, None)
            raise
        return {"default": cls(vm, cfg, target)}

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        state = _cleanup_state()
        for environment in environments.values():
            vm = environment.as_type(TartSandboxEnvironment)._vm
            await vm.destroy()
            state.vms.pop(vm.name, None)

    @classmethod
    async def task_cleanup(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool
    ) -> None:
        state = _cleanup_state()
        vms = list(state.vms.values())
        state.vms.clear()
        if not vms:
            return
        if cleanup:
            with anyio.CancelScope(shield=True):
                for vm in vms:
                    await vm.destroy()
        else:
            print("\nTart VMs left running (--no-sandbox-cleanup):")
            for vm in vms:
                print(f"  {vm.name}  ip={vm.ip or '?'}")
            print("Delete them with `inspect sandbox cleanup tart [NAME]`.\n")

    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        deleted = await cleanup_vms(id)
        print(f"Deleted {len(deleted)} Tart VM(s){': ' + ', '.join(deleted) if deleted else ''}")

    # ----------------------------------------------------------------- exec / files

    @override
    async def _run(
        self,
        cmd: list[str],
        *,
        input: str | bytes | None,
        cwd: str | None,
        env: dict[str, str] | None,
        user: str | None,
        timeout: int | None,
        concurrency: bool,
        output_limit: int,
    ) -> ExecResult[bytes]:
        if self._vm.destroyed:
            raise SandboxUnavailableError(f"The Tart VM {self._vm.name} is no longer running.")
        run_as = await self._resolve_user(user)
        if timeout is not None:
            timeout = max(1, int(timeout))
        remote = remote_command(
            cmd,
            cwd=self._resolve_path(cwd) if cwd else self._config.default_cwd,
            env=env,
            user=run_as,
            timeout=timeout,
            use_sudo=self._config.use_sudo,
            has_input=input is not None,
        )
        host_timeout = (
            timeout + ssh.KILL_AFTER + ssh.HOST_TIMEOUT_SLACK if timeout is not None else None
        )
        started = time.monotonic()
        result = await ssh_run(
            self._target,
            remote,
            input=input,
            timeout=host_timeout,
            output_limit=output_limit,
            concurrency=concurrency,
        )
        check_timeout(result, started, timeout)
        return result

    async def _resolve_user(self, user: str | None) -> str | None:
        """The ``sudo -u`` target, or None when running as the SSH user (no sudo)."""
        cfg = self._config
        if not cfg.use_sudo:
            if user is not None and user != cfg.ssh_user:
                raise SandboxUserUnsupportedError(
                    f"This Tart sandbox runs without sudo; it can only execute as {cfg.ssh_user!r}."
                )
            return None
        run_as = validate_user(user if user is not None else cfg.default_user)
        if run_as in ("root", "0") or run_as in self._known_users:
            return run_as
        lookup = await ssh_run(
            self._target, shlex.join([ssh.REMOTE_TOOLS.getent, "passwd", run_as]), timeout=60
        )
        if lookup.returncode == 2:
            raise SandboxUserUnsupportedError(
                f"User {run_as!r} does not exist in Tart image '{self._config.image}'. Was it "
                "built for another agent_user? Rebuild it with `agenteval env build <env> --force`."
            )
        if not lookup.success:
            raise RuntimeError(
                f"Looking up user {run_as!r} failed: "
                f"{lookup.stderr.decode(errors='replace').strip()}"
            )
        self._known_users.add(run_as)
        return run_as

    @override
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        if self._vm.destroyed:
            raise ConnectionError(f"The Tart VM {self._vm.name} is not running.")
        remote: str | None = None
        if self._config.use_sudo:
            run_as = validate_user(user if user is not None else self._config.default_user)
            remote = shlex.join(["sudo", "-i", "-u", ssh.sudo_user(run_as)])
        elif user is not None and user != self._config.ssh_user:
            raise SandboxUserUnsupportedError(
                "This Tart sandbox runs without sudo; it can only connect as "
                f"{self._config.ssh_user!r}."
            )
        return SandboxConnection(
            type="tart",
            command=shlex.join(ssh_argv(self._target, remote, tty=True)),
            container=self._vm.name,
        )
