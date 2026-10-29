"""Guest-side helpers for providers that reach a Linux guest only through ``exec``.

Tart (over SSH) and Docker Sandboxes (``sbx exec``) move files and enforce timeouts the
same way: constant ``/bin/sh`` scripts that take their arguments as ``$1..``, GNU
``timeout`` in the guest, and read limits checked before a byte is sent.
``ExecGuestSandbox`` is their common base: ``exec``, ``write_file`` and ``read_file``
on top of the one primitive each provider supplies, ``_run``.
"""

from __future__ import annotations

import errno
import re
import time
from abc import abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Literal, Protocol, overload, override

import yaml
from inspect_ai.util import (
    ExecResult,
    OutputLimitExceededError,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUserUnsupportedError,
)
from pydantic import BaseModel

try:  # batch-scoped provider state (pinned inspect-ai); fall back to process-wide
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_state
except ImportError:  # pragma: no cover
    sandbox_lifecycle_state = None  # type: ignore[assignment]

FILE_TIMEOUT = 600
"""Seconds allowed for one write_file/read_file transfer."""

KILL_AFTER = 5
"""Seconds the guest's ``timeout`` waits after SIGTERM before sending SIGKILL."""

# Internal scripts: /bin/sh by absolute path with a pinned PATH, args as $1.., so the
# image's PATH (which the agent may influence) never picks what root runs.
SH_PRELUDE = "PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexport PATH\nunset CDPATH\n"

WRITE_SCRIPT = """\
f=$1
if [ -d "$f" ]; then echo "agenteval: $f: Is a directory" >&2; exit 21; fi
mkdir -p -- "$(dirname -- "$f")" || exit 1
cat > "$f"
"""

# Only regular files (after symlinks): a FIFO or device (e.g. a link to /dev/zero) at
# an agent-controlled path would otherwise block or stream until FILE_TIMEOUT. head
# bounds the transfer even if the file changes after the checks.
READ_SCRIPT = """\
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


# Users exec and connection accept: a name as useradd takes it (a trailing $ for
# machine accounts) or a numeric uid. Wider than constants.check_user, which only takes
# the names the suites create and pass to shell commands.
_USER = re.compile(r"^(?:[0-9]+|[A-Za-z_][A-Za-z0-9_.-]*\$?)$")


def validate_user(user: str, provider: str) -> str:
    if not _USER.match(user):
        raise SandboxUserUnsupportedError(f"Invalid user name for the {provider} sandbox: {user!r}")
    return user


def resolve_config[C: BaseModel](
    config: SandboxEnvironmentConfigType | None,
    model: type[C],
    error: str,
    *,
    from_file: bool = False,
) -> C:
    """``config`` as ``model``: as is, from a YAML file (``from_file``) or re-validated
    from another pydantic model; anything else raises ``ValueError(error)``."""
    if isinstance(config, model):
        return config
    if from_file and isinstance(config, str):
        with Path(config).open() as f:
            return model.model_validate(yaml.safe_load(f) or {})
    if isinstance(config, BaseModel):
        return model.model_validate(config.model_dump())
    raise ValueError(error)


def lifecycle_state[S](state_type: type[S], process_state: S) -> S:
    """The provider's cleanup state for the current sandbox lifecycle scope, or
    ``process_state`` outside one (or on an Inspect without scopes)."""
    scope = sandbox_lifecycle_state() if sandbox_lifecycle_state is not None else None
    return scope.get(state_type) if scope is not None else process_state


def sh(script: str, *args: str) -> list[str]:
    return ["/bin/sh", "-c", SH_PRELUDE + script, "sh", *args]


def timed_out(returncode: int, elapsed: float, timeout: int) -> bool:
    """GNU timeout: 124 after SIGTERM, 137 after the SIGKILL escalation; 143 = SIGTERM
    from elsewhere, so it only counts once the deadline has really passed."""
    return returncode in (124, 137) or (returncode == 143 and elapsed >= timeout)


def check_timeout(result: ExecResult[bytes], started: float, timeout: int | None) -> None:
    """Raise ``TimeoutError`` (with the partial output) if the guest's ``timeout`` fired."""
    if timeout is not None and timed_out(result.returncode, time.monotonic() - started, timeout):
        error = TimeoutError(f"Command timed out after {timeout} seconds")
        partial = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        if partial:
            setattr(error, "truncated_output", partial)  # noqa: B010
        raise error


def limit_error() -> OutputLimitExceededError:
    return OutputLimitExceededError(
        limit_str=SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, truncated_output=None
    )


def write_error(result: ExecResult[bytes], file: str, where: str) -> Exception:
    """The exception for a failed ``WRITE_SCRIPT``, matching Inspect's Docker provider."""
    stderr = result.stderr.decode(errors="replace").strip()
    lowered = stderr.casefold()
    if result.returncode == 21 or "is a directory" in lowered:
        return IsADirectoryError(errno.EISDIR, "Is a directory.", file)
    if "permission denied" in lowered or "read-only file system" in lowered:
        return PermissionError(errno.EACCES, f"Permission denied. {stderr}", file)
    return RuntimeError(f"write_file({file!r}) failed in {where}: {stderr}")


def read_error(result: ExecResult[bytes], file: str, where: str) -> Exception:
    """The exception for a failed ``READ_SCRIPT``, matching Inspect's Docker provider."""
    stderr = result.stderr.decode(errors="replace").strip()
    if result.returncode == 2:
        return FileNotFoundError(errno.ENOENT, "No such file or directory.", file)
    if result.returncode == 21:
        return IsADirectoryError(errno.EISDIR, "Is a directory.", file)
    if result.returncode == 22:
        return OSError(errno.EINVAL, "Not a regular file.", file)
    if result.returncode == 27:
        return limit_error()
    if result.returncode == 13 or "permission denied" in stderr.casefold():
        return PermissionError(errno.EACCES, "Permission denied.", file)
    return RuntimeError(f"read_file({file!r}) failed in {where}: {stderr}")


class GuestConfig(Protocol):
    @property
    def default_cwd(self) -> str: ...


class ExecGuestSandbox(SandboxEnvironment):
    """Base for providers whose only channel into the guest is one command at a time.

    Subclasses set ``config_type``, keep their config in ``_config``, name the guest in
    ``_guest_name`` (for error messages) and implement ``_run``; lifecycle classmethods,
    ``default_concurrency`` and ``connection`` stay with each provider.
    """

    config_type: ClassVar[type[BaseModel]]
    _config: GuestConfig

    @property
    @abstractmethod
    def _guest_name(self) -> str: ...

    @abstractmethod
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
        """Run ``cmd`` in the guest; ``cwd`` None = ``default_cwd``, ``user`` None =
        the default user. Raises ``TimeoutError`` (see ``check_timeout``)."""

    @classmethod
    def config_files(cls) -> list[str]:
        return []

    @override
    def default_polling_interval(self) -> float:
        # each exec costs several times a docker exec (SSH round trip, ~0.3 s for sbx)
        return 0.5

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return cls.config_type.model_validate(config)

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
        # timeout_retry is advisory; a retried command may have had side effects, so at
        # most connection failures (command never started; Tart's SSH) are retried.
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

    def _resolve_path(self, file: str) -> str:
        path = PurePosixPath(file)
        if not path.is_absolute():
            path = PurePosixPath(self._config.default_cwd) / path
        return path.as_posix()

    @override
    async def write_file(self, file: str, contents: str | bytes) -> None:
        data = contents.encode("utf-8") if isinstance(contents, str) else contents
        result = await self._run(
            sh(WRITE_SCRIPT, self._resolve_path(file)),
            input=data,
            cwd="/",
            env=None,
            user=None,
            timeout=FILE_TIMEOUT,
            concurrency=True,
            output_limit=64 * 1024,
        )
        if not result.success:
            raise write_error(result, file, self._guest_name)

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        result = await self._run(
            sh(READ_SCRIPT, self._resolve_path(file), str(limit)),
            input=None,
            cwd="/",
            env=None,
            user=None,
            timeout=FILE_TIMEOUT,
            concurrency=True,
            output_limit=limit + 1,
        )
        if not result.success:
            raise read_error(result, file, self._guest_name)
        if len(result.stdout) > limit:  # grew between the size check and head
            raise limit_error()
        return result.stdout.decode("utf-8") if text else result.stdout
