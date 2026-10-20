#!/usr/bin/env python3
"""Terminal relay for TUIs inside the inner sandbox (stdlib only, Python 3.12).

``agenteval dev`` runs a bwrap-sandboxed CLI as
``sandbox-exec python3 /opt/agenteval/ptyrelay.py -- CMD [ARGS...]``. The bwrap wrapper
starts a new session (``--new-session``, against TIOCSTI injection into the user's
terminal), so the command would have no controlling terminal: ``/dev/tty`` cannot be
opened and resizes never arrive as SIGWINCH. This relay gives the command a fresh pty
of its own as controlling terminal and copies bytes between it and the terminal on fds
0/1. The protection holds: TIOCSTI on the command's own terminal only feeds the command,
and the outer terminal is still the controlling terminal of nothing inside the sandbox.
The command can reach the outer terminal through ``/proc/<relay pid>/fd``, just as it
holds bwrap's fds 0-2 without the relay, but TIOCSTI on it needs it as controlling
terminal, which it cannot take while the session outside the sandbox holds it.

Usage: ``ptyrelay.py [--] CMD [ARGS...]``

- When fd 0 or fd 1 is not a terminal, or no pty can be opened, CMD is exec'd directly
  (no relay).
- The inner pty starts with the outer terminal's modes and window size. The outer size
  is polled every 250 ms (and copied at once on SIGWINCH, should the relay get one);
  each change is set on the inner pty, and the kernel signals the command with SIGWINCH.
- The outer terminal is raw while the command runs; its modes are restored on exit.
- SIGHUP, SIGINT and SIGTERM sent to the relay are forwarded to the command. A hangup
  of the outer terminal sends the command SIGHUP.
- The exit status is the command's: its exit code, or 128 + N when killed by signal N;
  126/127 when it cannot be run, 2 on a usage error.
"""

from __future__ import annotations

import fcntl
import os
import select
import signal
import struct
import sys
import termios
import time
import tty
from contextlib import suppress
from types import FrameType

USAGE = "usage: ptyrelay.py [--] CMD [ARGS...]"
POLL_SECONDS = 0.25
CHUNK = 65536
MAX_PENDING = 4 * CHUNK  # input buffered for a command that is not reading
DRAIN_SECONDS = 2.0  # cap on copying output left behind when the command exits
DRAIN_QUIET = 0.1
WINSIZE = struct.Struct("HHHH")  # rows, cols, xpixel, ypixel
FORWARDED = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
# Ignored by the Python runtime itself rather than by our parent: reset for CMD, like
# subprocess's restore_signals.
RUNTIME_IGNORED = (signal.SIGPIPE, signal.SIGXFSZ)


def exec_command(argv: list[str]) -> int:
    """Replace this process with ``argv``; if that fails, report it and return 126/127."""
    for sig in RUNTIME_IGNORED:
        signal.signal(sig, signal.SIG_DFL)
    try:
        os.execvp(argv[0], argv)
    except OSError as ex:
        with suppress(OSError):
            os.write(2, f"ptyrelay: {argv[0]}: {ex.strerror}\n".encode(errors="replace"))
        return 127 if isinstance(ex, FileNotFoundError) else 126


def copy_winsize(src: int, dst: int) -> None:
    """Copy the window size of terminal ``src`` to ``dst``.

    Setting an unchanged size is a no-op for the kernel; a changed size on a pty sends
    SIGWINCH to the foreground process group of its terminal side.
    """
    with suppress(OSError):
        fcntl.ioctl(
            dst, termios.TIOCSWINSZ, fcntl.ioctl(src, termios.TIOCGWINSZ, bytes(WINSIZE.size))
        )


def read_chunk(fd: int) -> bytes | None:
    """Read what ``fd`` has: ``None`` when nothing yet, ``b""`` when closed (EOF or EIO)."""
    try:
        return os.read(fd, CHUNK)
    except BlockingIOError:
        return None
    except OSError:
        return b""


def write_all(fd: int, data: bytes) -> bool:
    """Write all of ``data`` to ``fd`` (partial writes, non-blocking fds); False on error.

    EINTR is retried by the os functions themselves (PEP 475).
    """
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view) :]
        except BlockingIOError:
            select.select([], [fd], [], POLL_SECONDS)
        except OSError:
            return False
    return True


def send(pid: int, sig: int) -> None:
    """Send ``sig`` to ``pid`` unless it is already gone."""
    with suppress(ProcessLookupError):
        os.kill(pid, sig)


def stop_forwarding() -> None:
    """Ignore the forwarded signals from now on: the command is reaped, its pid may be reused."""
    for sig in FORWARDED:
        signal.signal(sig, signal.SIG_IGN)


def drain(master: int) -> None:
    """Copy output the command left on ``master`` to fd 1.

    Bounded in time, because processes the command started may hold the pty open and
    keep writing.
    """
    deadline = time.monotonic() + DRAIN_SECONDS
    while time.monotonic() < deadline:
        readable, _, _ = select.select([master], [], [], DRAIN_QUIET)
        data = read_chunk(master) if readable else None
        if not data or not write_all(1, data):
            return


def relay(pid: int, master: int) -> int:
    """Copy fd 0 to ``master`` and ``master`` to fd 1 until ``pid`` exits; return its status."""
    os.set_blocking(master, False)
    pending = bytearray()
    stdin_open = output_open = True
    next_poll = 0.0
    while True:
        now = time.monotonic()
        if now >= next_poll:
            copy_winsize(0, master)
            next_poll = now + POLL_SECONDS
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            stop_forwarding()
            if output_open:
                drain(master)
            return status
        readers = [master]
        if stdin_open and len(pending) < MAX_PENDING:
            readers.append(0)
        writers = [master] if pending else []
        readable, writable, _ = select.select(readers, writers, [], POLL_SECONDS)
        if writable:
            try:
                del pending[: os.write(master, pending)]
            except BlockingIOError:
                pass
            except OSError:
                pending.clear()
        if master in readable:
            data = read_chunk(master)
            if data == b"":  # nothing holds the pty any more: just wait for the command
                status = os.waitpid(pid, 0)[1]
                stop_forwarding()
                return status
            if data and output_open:
                output_open = write_all(1, data)
        if 0 in readable:
            data = read_chunk(0)
            if data == b"":  # a raw-mode terminal only reads EOF/EIO on hangup
                stdin_open = False
                send(pid, signal.SIGHUP)
            elif data:
                pending += data


def run(argv: list[str]) -> int:
    """Run ``argv`` on a new pty relayed to the terminal on fds 0/1; return its exit code."""
    try:
        modes = termios.tcgetattr(0)
    except termios.error:
        return exec_command(argv)
    try:
        master, slave = os.openpty()
    except OSError as ex:
        with suppress(OSError):
            os.write(2, f"ptyrelay: no pty ({ex.strerror}), running without relay\n".encode())
        return exec_command(argv)
    # Configure the pty before the command starts, so it never sees a 0x0 or default tty.
    with suppress(termios.error):
        termios.tcsetattr(slave, termios.TCSANOW, modes)
    copy_winsize(0, slave)
    inherited = {sig: signal.getsignal(sig) for sig in FORWARDED}
    # An ignored SIGCHLD (inherited) would let the kernel reap the command: waitpid fails.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    # Held until the handlers are in place; the child resets them before unblocking.
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, (*FORWARDED, signal.SIGWINCH))
    pid = os.fork()
    if pid == 0:  # child: never returns into the relay
        code = 127
        try:
            os.close(master)
            os.login_tty(slave)  # new session, the pty as controlling terminal and fds 0-2
            for sig in FORWARDED:
                ignored = inherited[sig] == signal.SIG_IGN
                signal.signal(sig, signal.SIG_IGN if ignored else signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            code = exec_command(argv)
        finally:
            os._exit(code)
    os.close(slave)

    def forward(signum: int, _frame: FrameType | None) -> None:
        send(pid, signum)

    def resized(_signum: int, _frame: FrameType | None) -> None:
        copy_winsize(0, master)

    for sig in FORWARDED:
        signal.signal(sig, forward)
    signal.signal(signal.SIGWINCH, resized)
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    try:
        tty.setraw(0, termios.TCSANOW)
        status = relay(pid, master)
    finally:
        # TCSANOW: waiting for output to drain could hang on a terminal nobody reads.
        with suppress(termios.error):
            termios.tcsetattr(0, termios.TCSANOW, modes)
    os.close(master)
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def main(argv: list[str] | None = None) -> int:
    """Parse ``[--] CMD [ARGS...]`` and relay CMD, or exec it when there is no terminal."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    if os.isatty(0) and os.isatty(1):
        return run(args)
    return exec_command(args)


if __name__ == "__main__":
    sys.exit(main())
