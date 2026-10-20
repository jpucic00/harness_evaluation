"""pty relay (sandbox_scripts/ptyrelay.py) on a test pty, as under bwrap ``--new-session``.

Every relay runs in a new session (``start_new_session``: setsid before exec) with fds
0-2 inherited from the test's pty slave. Inherited descriptors never make a terminal the
controlling one, so the relay has none, like a command started by the bwrap wrapper.
A ``prelude`` runs the relay through ``python -c`` with that code before ``main()``.
"""

from __future__ import annotations

import os
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("needs POSIX ptys", allow_module_level=True)

import fcntl  # noqa: E402
import termios  # noqa: E402

from agenteval.sandbox_scripts import ptyrelay  # noqa: E402

RELAY = Path(ptyrelay.__file__)
TIMEOUT = 10.0

# The command under the relay: reports its terminal, prints its size on SIGWINCH, then
# follows commands typed one per line.
CHILD = r"""
import os, signal, sys

for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, signal.SIG_DFL)

def say(text):
    os.write(1, text.encode() + b"\n")

def size(*_):
    s = os.get_terminal_size(tty)
    say(f"SIZE {s.lines}x{s.columns}")

tty = os.open("/dev/tty", os.O_RDWR)
signal.signal(signal.SIGWINCH, size)
say(f"TTY {os.isatty(tty)}")
size()
while line := sys.stdin.readline():
    cmd, _, arg = line.strip().partition(" ")
    say(f"GOT {cmd} {arg}")
    if cmd == "exit":
        sys.exit(int(arg))
    if cmd == "kill":
        os.kill(os.getpid(), int(arg))
"""
LAUNCH = (
    "import sys\nfrom agenteval.sandbox_scripts import ptyrelay\n{}\nsys.exit(ptyrelay.main())\n"
)
# The outer pty becomes the relay's controlling terminal, so the relay gets SIGWINCH
# itself; with polling off only its handler can pass a resize on.
OWN_TERMINAL = """
import fcntl, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
ptyrelay.POLL_SECONDS = 3600.0
"""
NO_PTYS = """
import errno, os
def openpty():
    raise OSError(errno.ENXIO, os.strerror(errno.ENXIO))
os.openpty = openpty
"""
PID_EXIT_5 = "import os, sys; print(f'PID={os.getpid()};'); sys.exit(5)"


@dataclass
class Outer:
    """The test's pty: the user's terminal as the relay sees it."""

    master: int
    slave: int
    output: bytearray = field(default_factory=bytearray)
    seen: int = 0

    def set_size(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def modes(self) -> list[object]:
        """Terminal modes, without the PENDIN state flag macOS sets on entering ICANON."""
        attrs: list[object] = termios.tcgetattr(self.slave)
        lflag = attrs[3]
        assert isinstance(lflag, int)
        attrs[3] = lflag & ~termios.PENDIN
        return attrs

    def type(self, text: str) -> None:
        os.write(self.master, text.encode())

    def expect(self, text: str) -> None:
        """Wait for ``text`` in the output after the previous match."""
        needle = text.encode()
        deadline = time.monotonic() + TIMEOUT
        while (at := self.output.find(needle, self.seen)) < 0:
            ready, _, _ = select.select([self.master], [], [], max(deadline - time.monotonic(), 0))
            if not ready:
                pytest.fail(f"no {text!r} within {TIMEOUT}s; output: {bytes(self.output)!r}")
            self.output += os.read(self.master, 4096)
        self.seen = at + len(needle)

    def hang_up(self) -> None:
        os.close(self.master)
        self.master = -1

    def close(self) -> None:
        for fd in (self.master, self.slave):
            if fd >= 0:
                os.close(fd)


@pytest.fixture
def outer() -> Iterator[Outer]:
    master, slave = os.openpty()
    term = Outer(master, slave)
    term.set_size(24, 80)
    yield term
    term.close()


@pytest.fixture
def start(outer: Outer) -> Iterator[Callable[..., subprocess.Popen[bytes]]]:
    """Start ``ptyrelay.py -- CMD...`` on the outer pty, in a session of its own."""
    procs: list[subprocess.Popen[bytes]] = []

    def spawn(*cmd: str, prelude: str = "") -> subprocess.Popen[bytes]:
        relay = ["-c", LAUNCH.format(prelude)] if prelude else [str(RELAY)]
        proc = subprocess.Popen(
            [sys.executable, *relay, "--", *cmd],
            stdin=outer.slave,
            stdout=outer.slave,
            stderr=outer.slave,
            start_new_session=True,
        )
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()  # closes the inner pty, which hangs up the command
            proc.wait(TIMEOUT)


def test_setup_leaves_no_controlling_terminal(outer: Outer) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "import os; os.open('/dev/tty', os.O_RDWR)"],
        stdin=outer.slave,
        stdout=outer.slave,
        stderr=subprocess.PIPE,
        start_new_session=True,
        timeout=TIMEOUT,
    )
    assert proc.returncode != 0
    assert b"/dev/tty" in proc.stderr


def test_command_gets_a_terminal(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    outer.set_size(30, 100)
    start(sys.executable, "-c", CHILD)
    outer.expect("TTY True")
    outer.expect("SIZE 30x100")


def test_resize_reaches_command(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    start(sys.executable, "-c", CHILD)
    outer.expect("SIZE 24x80")
    # The outer pty is nobody's controlling terminal, so no SIGWINCH reaches the relay:
    # the new size gets through the relay's poll, then as SIGWINCH from the inner pty.
    outer.set_size(41, 123)
    outer.expect("SIZE 41x123")
    outer.set_size(50, 132)
    outer.expect("SIZE 50x132")


def test_resize_signal_on_relay(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    start(sys.executable, "-c", CHILD, prelude=OWN_TERMINAL)
    outer.expect("SIZE 24x80")
    outer.set_size(41, 123)
    outer.expect("SIZE 41x123")


def test_input_reaches_command(outer: Outer, start: Callable[..., subprocess.Popen[bytes]]) -> None:
    start(sys.executable, "-c", CHILD)
    outer.expect("TTY True")
    outer.type("echo hello\r")  # Enter; the inner pty's ICRNL makes it a newline
    outer.expect("GOT echo hello")
    outer.type("echo " + "x" * 300 + "\r")
    outer.expect("GOT echo " + "x" * 300)


@pytest.mark.parametrize(
    ("command", "code"),
    [("exit 0", 0), ("exit 7", 7), ("kill 9", 128 + 9), ("kill 15", 128 + 15)],
)
def test_exit_status_passes_through(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]], command: str, code: int
) -> None:
    proc = start(sys.executable, "-c", CHILD)
    outer.expect("TTY True")
    outer.type(command + "\r")
    assert proc.wait(TIMEOUT) == code


def test_inherited_ignored_sigchld(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    # With SIGCHLD ignored the kernel would reap the command and waitpid fail (on Linux).
    ignore = "import signal\nsignal.signal(signal.SIGCHLD, signal.SIG_IGN)"
    proc = start(sys.executable, "-c", CHILD, prelude=ignore)
    outer.expect("TTY True")
    outer.type("exit 7\r")
    assert proc.wait(TIMEOUT) == 7


def test_output_at_exit_reaches_terminal(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    size = 300_000  # more than the pty buffers: the relay copies while the command writes
    script = f"import sys; sys.stdout.write('x' * {size} + 'END'); sys.exit(4)"
    proc = start(sys.executable, "-c", script)
    outer.expect("END")
    assert outer.output.count(b"x") == size
    assert proc.wait(TIMEOUT) == 4


def test_background_job_does_not_hold_relay(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    # The job keeps the pty open and writing after the command exits: the relay copies its
    # output for at most DRAIN_SECONDS, then exits with the command's status. The job
    # ignores the SIGHUP of its session leader's exit (macOS also revokes the terminal
    # then); the loop ends once writes fail, at the latest after 5 s.
    job = "i=0; while [ $i -lt 100 ] && echo job; do i=$((i + 1)); sleep 0.05; done"
    started = time.monotonic()
    proc = start("sh", "-c", f"trap '' HUP; ({job}) & sleep 0.3; exit 3")
    assert proc.wait(TIMEOUT) == 3
    assert time.monotonic() - started < ptyrelay.DRAIN_SECONDS + 2
    outer.expect("job")


def test_terminal_modes_restored(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    before = outer.modes()
    proc = start(sys.executable, "-c", CHILD)
    outer.expect("TTY True")
    assert not termios.tcgetattr(outer.slave)[3] & termios.ICANON  # raw while relaying
    outer.type("exit 3\r")
    assert proc.wait(TIMEOUT) == 3
    assert outer.modes() == before


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGINT, signal.SIGTERM])
def test_signals_forwarded(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]], sig: signal.Signals
) -> None:
    before = outer.modes()
    proc = start(sys.executable, "-c", CHILD)
    outer.expect("TTY True")
    proc.send_signal(sig)
    assert proc.wait(TIMEOUT) == 128 + sig
    assert outer.modes() == before


def test_outer_hangup_hangs_up_command(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    proc = start(sys.executable, "-c", CHILD)
    outer.expect("TTY True")
    outer.hang_up()
    assert proc.wait(TIMEOUT) == 128 + signal.SIGHUP


def test_missing_command_on_terminal(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    before = outer.modes()
    proc = start("/nonexistent/agenteval-cmd")
    assert proc.wait(TIMEOUT) == 127
    outer.expect("ptyrelay: /nonexistent/agenteval-cmd")
    assert outer.modes() == before


@pytest.mark.parametrize("stdin_tty", [False, True], ids=["stdin-pipe", "stdout-pipe"])
def test_without_terminal_execs_in_place(outer: Outer, stdin_tty: bool) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(RELAY), "--", sys.executable, "-c", PID_EXIT_5],
        stdin=outer.slave if stdin_tty else subprocess.DEVNULL,
        stdout=subprocess.PIPE if stdin_tty else outer.slave,
        start_new_session=True,
    )
    out, _ = proc.communicate(timeout=TIMEOUT)
    assert proc.returncode == 5
    if stdin_tty:
        assert f"PID={proc.pid};".encode() in out  # same process: exec'd, no fork
    else:
        outer.expect(f"PID={proc.pid};")


def test_without_pty_execs_in_place(
    outer: Outer, start: Callable[..., subprocess.Popen[bytes]]
) -> None:
    before = outer.modes()
    proc = start(sys.executable, "-c", PID_EXIT_5, prelude=NO_PTYS)
    assert proc.wait(TIMEOUT) == 5
    outer.expect("ptyrelay: no pty")
    outer.expect(f"PID={proc.pid};")
    assert outer.modes() == before


def test_without_terminal_missing_command() -> None:
    proc = subprocess.run(
        [sys.executable, str(RELAY), "--", "/nonexistent/agenteval-cmd"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT,
    )
    assert proc.returncode == 127
    assert b"ptyrelay: /nonexistent/agenteval-cmd" in proc.stderr


@pytest.mark.parametrize("args", [[], ["--"]])
def test_usage(args: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, str(RELAY), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT,
    )
    assert proc.returncode == 2
    assert b"usage" in proc.stderr
