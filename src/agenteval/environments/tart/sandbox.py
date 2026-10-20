"""Inspect sandbox provider: every sample runs in a fresh Tart Linux VM.

- task_init:      checks Tart, the prebuilt image (``agenteval env build``) and the SSH key.
- sample_init:    ``tart clone`` the image to ``agenteval-<8hex>``, ``tart set`` cpu/memory,
                  ``tart run --no-graphics`` (no host folder sharing), ``tart ip --wait``,
                  then poll SSH until it answers.
- exec/files:     over ``ssh`` with a dedicated key; commands switch user with ``sudo -n``.
- sample_cleanup: stop and delete the VM. task_cleanup deletes whatever is left;
                  ``inspect sandbox cleanup tart`` deletes every ``agenteval-<8hex>`` VM.

Usage: ``SandboxEnvironmentSpec("tart", TartSandboxConfig(...))``.
"""

from __future__ import annotations

import errno
import logging
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, overload, override

import anyio
import yaml
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    sandboxenv,
)
from pydantic import BaseModel, Field, field_validator

from agenteval.config import TartSettings

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

try:  # batch-scoped provider state (pinned inspect-ai); fall back to process-wide
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_state
except ImportError:  # pragma: no cover
    sandbox_lifecycle_state = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

FILE_TIMEOUT = 600
"""Seconds allowed for one write_file/read_file transfer."""

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

    @classmethod
    def from_settings(
        cls, settings: TartSettings, *, default_cwd: str, default_user: str = "root"
    ) -> TartSandboxConfig:
        return cls(**settings.model_dump(), default_cwd=default_cwd, default_user=default_user)


def resolve_config(config: SandboxEnvironmentConfigType | None) -> TartSandboxConfig:
    if isinstance(config, TartSandboxConfig):
        return config
    if isinstance(config, str):
        with Path(config).open() as f:
            return TartSandboxConfig.model_validate(yaml.safe_load(f) or {})
    if isinstance(config, BaseModel):
        return TartSandboxConfig.model_validate(config.model_dump())
    raise ValueError(
        'The "tart" sandbox needs a TartSandboxConfig: '
        'SandboxEnvironmentSpec("tart", TartSandboxConfig(...)).'
    )


@dataclass
class _TartCleanupState:
    vms: dict[str, TartVM] = field(default_factory=dict)


_process_state = _TartCleanupState()


def _cleanup_state() -> _TartCleanupState:
    scope = sandbox_lifecycle_state() if sandbox_lifecycle_state is not None else None
    return scope.get(_TartCleanupState) if scope is not None else _process_state


# Internal scripts: /bin/sh by absolute path with a pinned PATH, args as $1.., so the
# image's PATH (which the agent may influence) never picks what root runs.
_SH_PRELUDE = "PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexport PATH\nunset CDPATH\n"

_WRITE_SCRIPT = """\
f=$1
if [ -d "$f" ]; then echo "agenteval: $f: Is a directory" >&2; exit 21; fi
mkdir -p -- "$(dirname -- "$f")" || exit 1
cat > "$f"
"""

# Only regular files (after symlinks): a FIFO or device (e.g. a link to /dev/zero) at
# an agent-controlled path would otherwise block or stream until FILE_TIMEOUT. head
# bounds the transfer even if the file changes after the checks.
_READ_SCRIPT = """\
f=$1
limit=$2
if [ -d "$f" ]; then echo "agenteval: $f: Is a directory" >&2; exit 21; fi
if [ ! -e "$f" ]; then echo "agenteval: $f: No such file or directory" >&2; exit 2; fi
if [ ! -f "$f" ]; then echo "agenteval: $f: Not a regular file" >&2; exit 22; fi
if [ ! -r "$f" ]; then echo "agenteval: $f: Permission denied" >&2; exit 13; fi
size=$(stat -L -c %s -- "$f" 2>/dev/null) || size=$(wc -c < "$f") || exit 1
size=$(printf '%s' "$size" | tr -d ' \\t\\n')
if [ "$size" -gt "$limit" ]; then echo "agenteval: $f: file too large" >&2; exit 27; fi
exec head -c "$((limit + 1))" -- "$f"
"""


def _sh(script: str, *args: str) -> list[str]:
    return ["/bin/sh", "-c", _SH_PRELUDE + script, "sh", *args]


def _timed_out(returncode: int, elapsed: float, timeout: int) -> bool:
    """GNU timeout: 124 after SIGTERM, 137 after the SIGKILL escalation; 143 = SIGTERM
    from elsewhere, so it only counts once the deadline has really passed."""
    return returncode in (124, 137) or (returncode == 143 and elapsed >= timeout)


_dhcp_warned = False


def _warn_dhcp_once() -> None:
    global _dhcp_warned
    if _dhcp_warned:
        return
    _dhcp_warned = True
    problem = dhcp_lease_problem()
    if problem is not None:
        logger.warning(problem)


def _limit_error() -> OutputLimitExceededError:
    return OutputLimitExceededError(
        limit_str=SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None
    )


@sandboxenv(name="tart")
class TartSandboxEnvironment(SandboxEnvironment):
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

    # ----------------------------------------------------------------- lifecycle

    @classmethod
    def config_files(cls) -> list[str]:
        return []

    @classmethod
    def default_concurrency(cls) -> int | None:
        return 2

    @override
    def default_polling_interval(self) -> float:
        return 0.5

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return TartSandboxConfig.model_validate(config)

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
            await vm.configure(cpu=cfg.cpu, memory_mb=cfg.memory_mb)
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
    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        # timeout_retry is advisory; only connection failures (command never started)
        # are retried, since a retried command may have had side effects.
        result = await self._run(
            cmd,
            input=input,
            cwd=cwd,
            env=env,
            user=user,
            timeout=timeout,
            concurrency=concurrency,
            output_limit=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE,
        )
        return ExecResult(
            success=result.success,
            returncode=result.returncode,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
        )

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
        if timeout is not None and _timed_out(
            result.returncode, time.monotonic() - started, timeout
        ):
            error = TimeoutError(f"Command timed out after {timeout} seconds")
            partial = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            if partial:
                setattr(error, "truncated_output", partial)  # noqa: B010
            raise error
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

    def _resolve_path(self, file: str) -> str:
        path = PurePosixPath(file)
        if not path.is_absolute():
            path = PurePosixPath(self._config.default_cwd) / path
        return path.as_posix()

    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        path = self._resolve_path(file)
        data = contents.encode("utf-8") if isinstance(contents, str) else contents
        result = await self._run(
            _sh(_WRITE_SCRIPT, path),
            input=data,
            cwd="/",
            env=None,
            user=None,
            timeout=FILE_TIMEOUT,
            concurrency=True,
            output_limit=64 * 1024,
        )
        if result.success:
            return
        stderr = result.stderr.decode(errors="replace").strip()
        lowered = stderr.casefold()
        if result.returncode == 21 or "is a directory" in lowered:
            raise IsADirectoryError(errno.EISDIR, "Is a directory.", file)
        if "permission denied" in lowered or "read-only file system" in lowered:
            raise PermissionError(errno.EACCES, f"Permission denied. {stderr}", file)
        raise RuntimeError(f"write_file({file!r}) failed in {self._vm.name}: {stderr}")

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        path = self._resolve_path(file)
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        result = await self._run(
            _sh(_READ_SCRIPT, path, str(limit)),
            input=None,
            cwd="/",
            env=None,
            user=None,
            timeout=FILE_TIMEOUT,
            concurrency=True,
            output_limit=limit + 1,
        )
        if not result.success:
            stderr = result.stderr.decode(errors="replace").strip()
            if result.returncode == 2:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory.", file)
            if result.returncode == 21:
                raise IsADirectoryError(errno.EISDIR, "Is a directory.", file)
            if result.returncode == 22:
                raise OSError(errno.EINVAL, "Not a regular file.", file)
            if result.returncode == 27:
                raise _limit_error()
            if result.returncode == 13 or "permission denied" in stderr.casefold():
                raise PermissionError(errno.EACCES, "Permission denied.", file)
            raise RuntimeError(f"read_file({file!r}) failed in {self._vm.name}: {stderr}")
        if len(result.stdout) > limit:  # grew between the size check and cat
            raise _limit_error()
        return result.stdout.decode("utf-8") if text else result.stdout

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
