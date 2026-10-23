"""Inspect sandbox provider: every sample runs in a fresh Docker Sandboxes microVM.

- task_init:      checks sbx (installed, signed in, SSH agent forwarding off) and that
                  the environment's template is loaded (``agenteval env build``).
- sample_init:    ``sbx create shell --template TEMPLATE --pull never --name agenteval-sbx-<8hex>``
                  (``agenteval dev --mount live`` adds the shared folders as workspaces),
                  then checks the workspace exists, that root may manage the firewall
                  (the egress rule) and that no SSH agent answers.
- exec/files:     ``sbx exec -u USER``; the variables sbx injects into every command
                  (proxy settings, placeholder API keys, ...) are removed, so commands see
                  the image's environment as they do in the Docker and Tart environments.
- sample_cleanup: ``sbx rm --force``. task_cleanup removes whatever is left;
                  ``inspect sandbox cleanup sbx`` removes every ``agenteval-sbx-<8hex>``.

Usage: ``SandboxEnvironmentSpec("sbx", SbxSandboxConfig(...))``.
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, overload, override

import anyio
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.util import (
    ExecResult,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    sandboxenv,
    subprocess,
)
from pydantic import BaseModel, field_validator

from agenteval.config import SbxSettings
from agenteval.environments.guest import (
    FILE_TIMEOUT,
    KILL_AFTER,
    READ_SCRIPT,
    WRITE_SCRIPT,
    limit_error,
    read_error,
    sh,
    timed_out,
    write_error,
)

from . import cli

try:  # batch-scoped provider state (pinned inspect-ai); fall back to process-wide
    from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_state
except ImportError:  # pragma: no cover
    sandbox_lifecycle_state = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

RUNUSER = "/usr/sbin/runuser"

HOST_TIMEOUT_SLACK = 15
"""Extra host-side seconds so the guest's ``timeout`` normally fires first."""

# `cd` to $1, drop it, run the rest. As in Tart's SSH exec, the command never holds the
# exec session's stdin itself (/dev/null, or a pipe from cat), so a daemon it leaves
# behind cannot keep the exec open.
EXEC_SCRIPT_NOINPUT = 'cd -- "$1" || exit; shift; exec "$@" </dev/null'
EXEC_SCRIPT_INPUT = 'cd -- "$1" || exit; shift; cat | "$@"'

# Answers when an SSH agent is reachable at $SSH_AUTH_SOCK: sends REQUEST_IDENTITIES (11)
# and exits 0 only on IDENTITIES_ANSWER (12).
_AGENT_PROBE = """\
import os, socket, sys
path = os.environ.get("SSH_AUTH_SOCK")
if not path or not os.path.exists(path):
    sys.exit(1)
try:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    s.sendall(b"\\x00\\x00\\x00\\x01\\x0b")
    reply = s.recv(5)
except OSError:
    sys.exit(1)
sys.exit(0 if len(reply) == 5 and reply[4] == 12 else 1)
"""

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_USER = re.compile(r"^(?:[0-9]+|[A-Za-z_][A-Za-z0-9_.-]*\$?)$")
_NOT_FOUND = re.compile(r"^error: sandbox '.*' not found", re.MULTILINE)


class SbxSandboxConfig(SbxSettings):
    """``SbxSettings`` plus where and as whom commands run by default."""

    default_cwd: str
    """Working directory for exec() and base for relative file paths."""
    default_user: str = "root"
    shared_dirs: tuple[str, ...] = ()
    """Host folders mounted at the same path (``agenteval dev --mount live`` only)."""

    @field_validator("default_cwd")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("default_cwd must be an absolute path")
        return value


def resolve_config(config: SandboxEnvironmentConfigType | None) -> SbxSandboxConfig:
    if isinstance(config, SbxSandboxConfig):
        return config
    if isinstance(config, BaseModel):
        return SbxSandboxConfig.model_validate(config.model_dump())
    raise ValueError(
        'The "sbx" sandbox needs an SbxSandboxConfig: '
        'SandboxEnvironmentSpec("sbx", SbxSandboxConfig(...)).'
    )


@dataclass
class _SbxCleanupState:
    names: set[str] = field(default_factory=set)
    keep_env: dict[str, frozenset[str]] = field(default_factory=dict)
    """Per image: variables commands keep (``cli.KEEP_ENV`` plus the image's ``ENV``)."""


_process_state = _SbxCleanupState()


def _cleanup_state() -> _SbxCleanupState:
    scope = sandbox_lifecycle_state() if sandbox_lifecycle_state is not None else None
    return scope.get(_SbxCleanupState) if scope is not None else _process_state


def validate_user(user: str) -> str:
    if not _USER.match(user):
        raise SandboxUserUnsupportedError(f"Invalid user name for the sbx sandbox: {user!r}")
    return user


def env_assignments(env: dict[str, str]) -> list[str]:
    for key in env:
        if not _ENV_KEY.match(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
    return [f"{key}={value}" for key, value in env.items()]


def exec_argv(
    sbx: str,
    name: str,
    cmd: list[str],
    *,
    cwd: str,
    user: str,
    env: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
    timeout: int | None = None,
    has_input: bool = False,
) -> list[str]:
    """``sbx exec [-i] -u root NAME [timeout] env -u X.. K=V.. [runuser -u USER --]
    bash -c SCRIPT _ CWD CMD..``

    ``sbx exec -u USER`` would start the command with the user's primary group only
    (``docker exec`` adds the supplementary groups from /etc/group), which loses the
    per-sample ``agenteval-bwrap`` membership; ``runuser`` applies all of them and sets
    HOME/USER/LOGNAME for the user. Nothing the caller supplies becomes shell source:
    the cwd and the command are positional parameters of a constant script.
    """
    if not cmd:
        raise ValueError("cmd must not be empty")
    user = validate_user(user)
    as_root = user in ("root", "0") or user.isdigit()
    argv = [sbx, "exec", *(["-i"] if has_input else []), "-u", user if as_root else "root", name]
    if timeout is not None:
        argv += ["/usr/bin/timeout", f"--kill-after={KILL_AFTER}", str(max(1, int(timeout)))]
    argv += ["/usr/bin/env", *(a for n in unset for a in ("-u", n))]
    argv += env_assignments(env or {})
    if not as_root:
        argv += [RUNUSER, "-u", user, "--"]
    script = EXEC_SCRIPT_INPUT if has_input else EXEC_SCRIPT_NOINPUT
    return [*argv, "/bin/bash", "-c", script, "agenteval-exec", cwd, *cmd]


@sandboxenv(name="sbx")
class SbxSandboxEnvironment(SandboxEnvironment):
    def __init__(self, name: str, config: SbxSandboxConfig, unset: tuple[str, ...]) -> None:
        super().__init__()
        self._name = name
        self._config = config
        self._unset = unset
        self._removed = False

    @property
    def sandbox_name(self) -> str:
        return self._name

    @property
    def unset_env(self) -> tuple[str, ...]:
        """Variables sbx injects that commands run without."""
        return self._unset

    # ----------------------------------------------------------------- lifecycle

    @classmethod
    def config_files(cls) -> list[str]:
        return []

    @classmethod
    def default_concurrency(cls) -> int | None:
        return 4

    @override
    def default_polling_interval(self) -> float:
        # each sbx exec costs ~0.3 s on the host, several times a docker exec
        return 0.5

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return SbxSandboxConfig.model_validate(config)

    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        cfg = resolve_config(config)
        problems = await anyio.to_thread.run_sync(cli.readiness_problems)
        if problems:
            raise PrerequisiteError("\n".join(problems))
        for warning in await anyio.to_thread.run_sync(cli.setting_warnings):
            logger.warning(warning)
        if await anyio.to_thread.run_sync(cli.template_id, cfg.template) is None:
            raise PrerequisiteError(
                f"sbx template '{cfg.template}' not found. "
                "Load it with `agenteval env build <env>`."
            )
        state = _cleanup_state()
        if cfg.template not in state.keep_env:
            names = await anyio.to_thread.run_sync(cli.image_env_names, cfg.template)
            state.keep_env[cfg.template] = cli.KEEP_ENV | names

    @classmethod
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        cfg = resolve_config(config)
        sbx = cli.sbx_binary()
        if sbx is None:
            raise SandboxUnavailableError(cli.SBX_MISSING_MESSAGE)
        state = _cleanup_state()
        name = cli.new_sandbox_name()
        state.names.add(name)
        try:
            argv = [sbx, "create", "shell", "--template", cfg.template, "--pull", "never"]
            argv += ["--name", name, "--no-progress", "--memory", cfg.memory]
            if cfg.cpus is not None:
                argv += ["--cpus", str(cfg.cpus)]
            argv += list(cfg.shared_dirs)
            created = await subprocess(
                argv, env=cli.SBX_ENV, timeout=cfg.create_timeout, concurrency=False
            )
            if not created.success:
                raise RuntimeError(f"sbx create failed: {created.stderr.strip() or created.stdout}")
            keep = state.keep_env.get(cfg.template, cli.KEEP_ENV)
            env = cls(name, cfg, ())
            names = await env.exec(["/usr/bin/env", "-0"], user="root", cwd="/", timeout=60)
            injected = {line.split("=", 1)[0] for line in names.stdout.split("\0") if "=" in line}
            env._unset = tuple(sorted(injected - keep))
            cwd = await env.exec(["test", "-d", cfg.default_cwd], cwd="/", timeout=60)
            if not cwd.success:
                raise RuntimeError(
                    f"Working directory {cfg.default_cwd} does not exist in template "
                    f"'{cfg.template}'. Was it built for another workspace? Rebuild it with "
                    "`agenteval env build <env> --force`."
                )
            # without CAP_NET_ADMIN egress.sh cannot install its rule and the agent's
            # connections reach sbx's proxy (which then applies its own policy)
            firewall = await env.exec(["/usr/sbin/nft", "list", "tables"], cwd="/", timeout=60)
            if not firewall.success:
                raise RuntimeError(
                    f"root in sandboxes from '{cfg.template}' cannot manage the firewall "
                    f"({firewall.stderr.strip()}); rebuild the template with "
                    "`agenteval env build <env>`, which adds the label that grants it"
                )
            # an answering SSH agent hands the host's keys to the sandbox; the setting is
            # checked in task_init, this catches a daemon that was not restarted after it
            probe = await cls(name, cfg, ()).exec(
                ["/usr/bin/python3", "-c", _AGENT_PROBE], user="root", cwd="/", timeout=60
            )
            if probe.returncode == 0:
                raise RuntimeError(
                    "An SSH agent answers inside the sandbox: turn sbx's SSH agent forwarding off "
                    "(`sbx settings set ssh.agentForwardingEnabled false`, `sbx daemon restart`)."
                )
        except BaseException:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(cli.remove, [name])
            state.names.discard(name)
            raise
        return {"default": env}

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
            sandbox = environment.as_type(SbxSandboxEnvironment)
            sandbox._removed = True
            await anyio.to_thread.run_sync(cli.remove, [sandbox._name])
            state.names.discard(sandbox._name)

    @classmethod
    async def task_cleanup(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool
    ) -> None:
        state = _cleanup_state()
        names = sorted(state.names)
        state.names.clear()
        if not names:
            return
        if cleanup:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(cli.remove, names)
        else:
            print("\nsbx sandboxes left running (--no-sandbox-cleanup):")
            for name in names:
                print(f"  {name}")
            print("Remove them with `inspect sandbox cleanup sbx [NAME]`.\n")

    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        removed = cli.cleanup(id)
        names = f": {', '.join(removed)}" if removed else ""
        print(f"Removed {len(removed)} sbx sandbox(es){names}")

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
        if self._removed:
            raise SandboxUnavailableError(f"The sbx sandbox {self._name} has been removed.")
        sbx = cli.sbx_binary()
        if sbx is None:
            raise SandboxUnavailableError(cli.SBX_MISSING_MESSAGE)
        if timeout is not None:
            timeout = max(1, int(timeout))
        argv = exec_argv(
            sbx,
            self._name,
            cmd,
            cwd=self._resolve_path(cwd) if cwd else self._config.default_cwd,
            user=user if user is not None else self._config.default_user,
            env=env,
            unset=self._unset,
            timeout=timeout,
            has_input=input is not None,
        )
        host_timeout = timeout + KILL_AFTER + HOST_TIMEOUT_SLACK if timeout is not None else None
        started = time.monotonic()
        result = await subprocess(
            argv,
            text=False,
            input=input,
            env=cli.SBX_ENV,
            timeout=host_timeout,
            output_limit=output_limit,
            concurrency=concurrency,
        )
        if result.returncode != 0 and _NOT_FOUND.search(result.stderr.decode(errors="replace")):
            raise SandboxUnavailableError(f"The sbx sandbox {self._name} no longer exists.")
        elapsed = time.monotonic() - started
        if timeout is not None and timed_out(result.returncode, elapsed, timeout):
            error = TimeoutError(f"Command timed out after {timeout} seconds")
            partial = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            if partial:
                setattr(error, "truncated_output", partial)  # noqa: B010
            raise error
        return result

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
            raise write_error(result, file, self._name)

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
            raise read_error(result, file, self._name)
        if len(result.stdout) > limit:  # grew between the size check and head
            raise limit_error()
        return result.stdout.decode("utf-8") if text else result.stdout

    @override
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        if self._removed:
            raise ConnectionError(f"The sbx sandbox {self._name} has been removed.")
        run_as = validate_user(user if user is not None else self._config.default_user)
        return SandboxConnection(
            type="sbx",
            command=shlex.join(["sbx", "exec", "-it", "-u", run_as, self._name, "bash", "-l"]),
            container=self._name,
        )
