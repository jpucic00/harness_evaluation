"""SSH transport for the Tart sandbox: command construction and execution.

Remote commands are built from argv lists with ``shlex`` quoting only. Nothing a
caller supplies (command, cwd, env, user) is ever interpolated into shell source:
the cwd and the command travel as positional parameters of a constant script.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
from inspect_ai.util import (
    ExecResult,
    SandboxUnavailableError,
    subprocess,
)

from agenteval.environments import guest

SSH_PORT = 22

KILL_AFTER = 5
"""Seconds the remote ``timeout`` waits after SIGTERM before sending SIGKILL."""

HOST_TIMEOUT_SLACK = 15
"""Extra host-side seconds so the remote ``timeout`` normally fires first."""

CONNECT_RETRIES = 2
"""Retries when the TCP/SSH handshake fails (the command never started)."""

MAX_REMOTE_COMMAND = 120 * 1024
"""sshd hands the command to the login shell as a single argv string and Linux caps
one argument at 128 KiB. Larger payloads must go through stdin or write_file."""


@dataclass(frozen=True)
class RemoteTools:
    """Absolute paths of the programs the provider inserts ahead of the caller's command.

    They run with the target user's authority (root for privileged calls) before the
    caller's command, so they are never resolved through the image's ``PATH``.
    """

    sudo: str = "/usr/bin/sudo"
    env: str = "/usr/bin/env"
    timeout: str = "/usr/bin/timeout"
    bash: str = "/bin/bash"
    getent: str = "/usr/bin/getent"


REMOTE_TOOLS = RemoteTools()
"""Read at call time, so tests can point it at shims (macOS has no GNU timeout)."""

# `cd` to $1, drop it, run the rest. The cwd is a positional parameter, never source.
#
# On Linux sshd gives the remote command ONE socket as both stdin and stdout and only
# closes the channel once every process holding it has closed it. A daemon the command
# leaves behind with stdin inherited (Inspect's sandbox-tools ``start-server``) would
# keep the exec hanging forever, so the command never gets that socket as stdin:
# /dev/null without input (like ``docker exec``), a pipe from ``cat`` with input. The
# pipeline's status is the command's (no pipefail).
EXEC_SCRIPT_NOINPUT = 'cd -- "$1" || exit; shift; exec "$@" </dev/null'
EXEC_SCRIPT_INPUT = 'cd -- "$1" || exit; shift; cat | "$@"'

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ssh client errors (exit 255), matched against the client's own log (``ssh -E``), so
# a remote command's stderr is never mistaken for them. OpenSSH prefixes auth failures
# with ``user@host: ``.
_SSH_FAILURE = re.compile(
    r"^(?:ssh: |kex_exchange_identification: |Connection (?:closed|reset) by |"
    r"(?:\S+: )?Permission denied \(|Host key verification failed|client_loop: |"
    r"Timeout, server .* not responding|Load key )",
    re.MULTILINE,
)
# Failures before the session exists: the remote command certainly never ran.
_CONNECT_FAILURE = re.compile(
    r"^(?:ssh: connect to host .*: (?:Connection refused|Connection timed out|"
    r"Operation timed out|No route to host|Host is down|Network is unreachable)|"
    r"kex_exchange_identification: )",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SSHTarget:
    host: str
    user: str
    key: Path
    port: int = SSH_PORT

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}"


def ssh_binary() -> str:
    path = shutil.which("ssh")
    if path is None:
        raise SandboxUnavailableError("The OpenSSH client (ssh) is not on PATH.")
    return path


def ssh_options(
    key: Path | None, *, connect_timeout: int = 10, password_auth: bool = False
) -> list[str]:
    """Client options: no user/system ssh_config, no agent/X11/port forwarding."""
    opts = ["-F", "/dev/null"]
    if password_auth:
        opts += [
            "-o", "BatchMode=no",
            "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=keyboard-interactive,password",
            "-o", "NumberOfPasswordPrompts=1",
        ]  # fmt: skip
    else:
        if key is None:
            raise ValueError("an SSH key is required unless password_auth is set")
        opts += [
            "-i", str(key),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
        ]  # fmt: skip
    opts += [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
    ]  # fmt: skip
    return opts


def ssh_argv(
    target: SSHTarget,
    remote: str | None,
    *,
    ssh: str = "ssh",
    options: list[str] | None = None,
    tty: bool = False,
) -> list[str]:
    """Host-side argv; ``remote`` is one pre-quoted command string (None = login shell)."""
    argv = [
        ssh,
        *(options if options is not None else ssh_options(target.key)),
        "-t" if tty else "-T",
        "-p", str(target.port),
        "-l", target.user,
        "--",
        target.host,
    ]  # fmt: skip
    if remote is not None:
        argv.append(remote)
    return argv


def validate_user(user: str) -> str:
    return guest.validate_user(user, "Tart")


def sudo_user(user: str) -> str:
    """``sudo -u`` argument: numeric ids need the ``#`` prefix."""
    return f"#{user}" if validate_user(user).isdigit() else user


def env_assignments(env: dict[str, str]) -> list[str]:
    for key in env:
        if not _ENV_KEY.match(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
    return [f"{key}={value}" for key, value in env.items()]


def remote_command(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None = None,
    user: str | None = None,
    timeout: int | None = None,
    use_sudo: bool = True,
    has_input: bool = False,
    tools: RemoteTools | None = None,
) -> str:
    """The shell-quoted command line the remote login shell runs.

    ``sudo -n -H -u USER -- timeout --kill-after=5 T env K=V... bash -c SCRIPT _ CWD CMD...``
    (``sudo`` omitted when ``use_sudo`` is false, ``timeout``/``env`` when unused).
    ``has_input`` must say whether stdin carries data (see ``EXEC_SCRIPT_*``).
    """
    if not cmd:
        raise ValueError("cmd must not be empty")
    tools = tools or REMOTE_TOOLS
    argv: list[str] = []
    if use_sudo:
        if user is None:
            raise ValueError("user is required when use_sudo is set")
        argv += [tools.sudo, "-n", "-H", "-u", sudo_user(user), "--"]
    if timeout is not None:
        argv += [tools.timeout, f"--kill-after={KILL_AFTER}", str(max(1, int(timeout)))]
    if env:
        argv += [tools.env, *env_assignments(env)]
    script = EXEC_SCRIPT_INPUT if has_input else EXEC_SCRIPT_NOINPUT
    argv += [tools.bash, "-c", script, "agenteval-exec", cwd, *cmd]
    return shlex.join(argv)


def is_ssh_failure(stderr: str) -> bool:
    return _SSH_FAILURE.search(stderr) is not None


def is_connect_failure(stderr: str) -> bool:
    return _CONNECT_FAILURE.search(stderr) is not None


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


@contextlib.contextmanager
def _client_log() -> Iterator[Path]:
    """A private, empty file for one ssh client's own log (``ssh -E``)."""
    fd, name = tempfile.mkstemp(prefix="agenteval-ssh-", suffix=".log")
    os.close(fd)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


async def ssh_run(
    target: SSHTarget,
    remote: str,
    *,
    input: str | bytes | None = None,
    timeout: int | None = None,
    output_limit: int | None = None,
    concurrency: bool = True,
    connect_timeout: int = 10,
) -> ExecResult[bytes]:
    """Run ``remote`` on ``target``; bytes out, so callers decide how to decode.

    Exit 255 is only treated as an ssh failure when the client's own log (``-E``) says
    so; a remote command that exits 255 (e.g. an inner ``ssh``) is an ordinary result.

    Raises:
      TimeoutError: ``timeout`` (host side) expired; ssh is killed.
      SandboxUnavailableError: ssh itself failed (VM down, auth refused, ...).
      ValueError: the command line is too long for sshd.
    """
    if len(remote.encode()) > MAX_REMOTE_COMMAND:
        raise ValueError(
            f"Command line too long for SSH exec ({len(remote.encode())} bytes); "
            "pass large payloads via input or write_file."
        )
    ssh = ssh_binary()
    options = ssh_options(target.key, connect_timeout=connect_timeout)
    attempt = 0
    while True:
        with _client_log() as log:
            result = await subprocess(
                ssh_argv(target, remote, ssh=ssh, options=[*options, "-E", str(log)]),
                text=False,
                input=input,
                timeout=timeout,
                output_limit=output_limit,
                concurrency=concurrency,
            )
            client_errors = log.read_text(errors="replace") if result.returncode == 255 else ""
        if result.returncode != 255:
            return result
        if is_connect_failure(client_errors) and attempt < CONNECT_RETRIES:
            attempt += 1
            await anyio.sleep(attempt)
            continue
        if is_ssh_failure(client_errors):
            raise SandboxUnavailableError(
                f"SSH to the Tart VM ({target.label}) failed: {_last_line(client_errors)}"
            )
        return result


async def wait_for_ssh(target: SSHTarget, timeout: float, *, interval: float = 1.0) -> None:
    """Poll until ``true`` succeeds over SSH, or raise TimeoutError."""
    deadline = time.monotonic() + timeout
    ssh = ssh_binary()
    last = "no attempt made"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"SSH on {target.label} not ready after {timeout:.0f}s: {last}")
        connect_timeout = max(1, min(10, int(remaining)))
        argv = ssh_argv(
            target,
            "true",
            ssh=ssh,
            options=ssh_options(target.key, connect_timeout=connect_timeout),
        )
        try:
            result = await subprocess(argv, timeout=connect_timeout + 10, concurrency=False)
            if result.success:
                return
            last = _last_line(result.stderr) or f"exit {result.returncode}"
        except TimeoutError:
            last = "ssh attempt timed out"
        await anyio.sleep(interval)
