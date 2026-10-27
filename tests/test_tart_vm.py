"""End-to-end tests of the Tart provider against fake ``tart`` and ``ssh`` executables.

The fake ``tart`` keeps "VMs" as directories and runs ``tart run`` as a sleeping
process; the fake ``ssh`` runs the remote command locally with bash the way Linux sshd
does (or records it, for the image build). The sandbox runs with ``use_sudo=False`` as
the current user. No real Tart, VM or network is involved.
"""

from __future__ import annotations

import contextlib
import errno
import getpass
import os
import signal
import stat
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.util import (
    OutputLimitExceededError,
    SandboxEnvironment,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    override_sandbox_output_limit,
)

from agenteval.config import TartSettings
from agenteval.environments.tart import image, sandbox, ssh
from agenteval.environments.tart.sandbox import TartSandboxConfig, TartSandboxEnvironment

IMAGE = "agenteval-ubuntu-24.04"

FAKE_TART = r"""#!/bin/bash
vms="$FAKE_TART_HOME/vms"
mkdir -p "$vms"
printf '%s\n' "$*" >> "$FAKE_TART_HOME/calls.log"
cmd=$1; shift
pos=()
while [ $# -gt 0 ]; do
  case "$1" in
    --timeout|--wait|--cpu|--memory|--format|--source) shift 2 ;;
    --*) shift ;;
    *) pos+=("$1"); shift ;;
  esac
done
name=${pos[0]:-}
missing() { echo "Error: VM \"$1\" not found" >&2; exit 1; }
case "$cmd" in
  clone)
    case "$name" in */*) ;; *) [ -d "$vms/$name" ] || missing "$name" ;; esac
    [ -d "$vms/${pos[1]}" ] && { echo "Error: VM already exists" >&2; exit 1; }
    mkdir "$vms/${pos[1]}" ;;
  set) [ -d "$vms/$name" ] || missing "$name" ;;
  run)
    [ -d "$vms/$name" ] || missing "$name"
    if [ -n "${FAKE_TART_RUN_FAIL:-}" ]; then echo "boom: VM failed to start"; exit 1; fi
    echo $$ > "$vms/$name/pid"
    exec sleep 600 ;;
  ip)
    [ -d "$vms/$name" ] || missing "$name"
    if [ -n "${FAKE_TART_NO_IP:-}" ]; then echo "no IP" >&2; exit 1; fi
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      [ -f "$vms/$name/pid" ] && break
      sleep 0.25
    done
    [ -f "$vms/$name/pid" ] || { echo "no IP" >&2; exit 1; }
    echo "${FAKE_TART_IP:-127.0.0.1}" ;;
  stop)
    [ -f "$vms/$name/pid" ] || { echo "Error: VM \"$name\" is not running" >&2; exit 1; }
    kill "$(cat "$vms/$name/pid")" 2>/dev/null
    rm -f "$vms/$name/pid" ;;
  delete) [ -d "$vms/$name" ] || missing "$name"; rm -rf "$vms/$name" ;;
  rename)
    [ -d "$vms/${pos[1]}" ] && { echo "Error: VM already exists" >&2; exit 1; }
    mv "$vms/$name" "$vms/${pos[1]}" ;;
  list)
    printf '['; sep=''
    for d in "$vms"/*; do
      [ -d "$d" ] || continue
      running=false; [ -f "$d/pid" ] && running=true
      printf '%s{"Source":"local","Name":"%s","Disk":20,"Size":5,"Running":%s,"State":"x"}' \
        "$sep" "$(basename "$d")" "$running"
      sep=','
    done
    printf ']\n' ;;
  *) echo "unknown command $cmd" >&2; exit 2 ;;
esac
"""

FAKE_SSH = r"""#!/bin/bash
errlog=/dev/stderr
while [ $# -gt 0 ]; do
  case "$1" in
    -E) errlog=$2; shift 2 ;;
    -i|-o|-p|-l|-F|-c|-m|-J|-L|-R|-D|-W|-b|-e|-S|-O|-Q|-w|-B|-I) shift 2 ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
host=$1; shift
remote="$*"
[ -n "${FAKE_SSH_COUNT:-}" ] && echo x >> "$FAKE_SSH_COUNT"
if [ -f "${FAKE_SSH_FAILS:-/nonexistent}" ]; then
  n=$(cat "$FAKE_SSH_FAILS")
  if [ "$n" -gt 0 ]; then
    echo $((n - 1)) > "$FAKE_SSH_FAILS"
    printf 'ssh: connect to host %s port 22: Connection refused
' "$host" >> "$errlog"
    exit 255
  fi
fi
if [ -n "${FAKE_SSH_CLIENT_ERROR:-}" ]; then
  printf '%s
' "$FAKE_SSH_CLIENT_ERROR" >> "$errlog"
  exit 255
fi
if [ "${FAKE_SSH_MODE:-exec}" = record ]; then
  n=$(cat "$FAKE_SSH_DIR/counter" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$FAKE_SSH_DIR/counter"
  printf '%s' "$remote" > "$FAKE_SSH_DIR/$n.cmd"
  cat > "$FAKE_SSH_DIR/$n.stdin"
  if [ "${SSH_ASKPASS_REQUIRE:-}" = force ]; then
    if [ "$("$SSH_ASKPASS")" = "$FAKE_EXPECTED_PASSWORD" ]; then r=ok; else r=bad; fi
    echo "$r" > "$FAKE_SSH_DIR/$n.askpass"
    if [ -f "$FAKE_SSH_DIR/hardened" ]; then echo "Permission denied (publickey)." >&2; exit 255; fi
  fi
  if [ -z "${FAKE_SSH_HARDEN_IGNORED:-}" ]; then
    case "$remote" in *00-agenteval.conf*) touch "$FAKE_SSH_DIR/hardened" ;; esac
  fi
  case "$remote" in *agenteval-provision.XXXXXX*) printf /tmp/agenteval-provision.abc123 ;; esac
  exit 0
fi
exec "$FAKE_SSHD" "$remote"
"""

FAKE_SSHD = """#!{python}
# Runs the remote command like Linux sshd: one socket is both its stdin and stdout, and
# the session only ends once every process holding that socket has closed it.
import os, socket, subprocess, sys, threading
parent, child = socket.socketpair()
proc = subprocess.Popen(["/bin/bash", "-c", sys.argv[1]], stdin=child, stdout=child)
child.close()

def feed():
    try:
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            parent.sendall(data)
    except OSError:
        pass
    try:
        parent.shutdown(socket.SHUT_WR)
    except OSError:
        pass

threading.Thread(target=feed, daemon=True).start()
while True:
    data = parent.recv(65536)
    if not data:
        break
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
sys.exit(proc.wait())
"""

TIMEOUT_SHIM = """#!{python}
import os, signal, subprocess, sys
args = sys.argv[1:]
while args and args[0].startswith("-"):
    args.pop(0)
limit = float(args.pop(0).rstrip("s"))
proc = subprocess.Popen(args, start_new_session=True)
try:
    sys.exit(proc.wait(timeout=limit))
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    sys.exit(124)
"""


@dataclass
class Fakes:
    root: Path
    tart_home: Path
    workspace: Path
    key: Path
    config: TartSandboxConfig

    @property
    def vms(self) -> Path:
        return self.tart_home / "vms"

    def calls(self) -> list[str]:
        log = self.tart_home / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fakes]:
    root = tmp_path.resolve()
    bin_dir = root / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "tart", FAKE_TART)
    _executable(bin_dir / "ssh", FAKE_SSH)
    _executable(root / "fake-sshd", FAKE_SSHD.format(python=sys.executable))
    monkeypatch.setenv("FAKE_SSHD", str(root / "fake-sshd"))
    shim = root / "timeout-shim"
    _executable(shim, TIMEOUT_SHIM.format(python=sys.executable))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    tart_home = root / "tart"
    (tart_home / "vms" / IMAGE).mkdir(parents=True)
    monkeypatch.setenv("FAKE_TART_HOME", str(tart_home))
    monkeypatch.setattr(ssh, "REMOTE_TOOLS", ssh.RemoteTools(timeout=str(shim)))
    monkeypatch.setattr(image, "STOP_GRACE", 3)
    workspace = root / "workspace"
    workspace.mkdir()
    key = root / "id_ed25519"
    key.write_text("fake key\n")
    config = TartSandboxConfig(
        image=IMAGE,
        ssh_user=getpass.getuser(),
        ssh_key=str(key),
        use_sudo=False,
        boot_timeout=20,
        default_cwd=str(workspace),
    )
    sandbox._process_state.vms.clear()
    yield Fakes(root, tart_home, workspace, key, config)
    sandbox._process_state.vms.clear()


@pytest.fixture
async def env(fakes: Fakes) -> AsyncIterator[TartSandboxEnvironment]:
    environments = await TartSandboxEnvironment.sample_init("task", fakes.config, {})
    yield environments["default"].as_type(TartSandboxEnvironment)
    await TartSandboxEnvironment.sample_cleanup("task", fakes.config, environments, False)


# --------------------------------------------------------------------------- lifecycle


async def test_sample_lifecycle(fakes: Fakes) -> None:
    await TartSandboxEnvironment.task_init("task", fakes.config)
    environments = await TartSandboxEnvironment.sample_init("task", fakes.config, {})
    assert list(environments) == ["default"]
    tart_env = environments["default"].as_type(TartSandboxEnvironment)
    name = tart_env.vm_name
    assert image.is_sample_vm(name)
    assert (fakes.vms / name / "pid").exists()
    assert name in sandbox._process_state.vms

    calls = fakes.calls()
    assert f"clone {IMAGE} {name}" in calls
    assert f"set {name} --cpu 4 --memory 4096 --random-mac" in calls
    assert f"run {name} --no-graphics" in calls
    assert f"ip {name} --wait 20" in calls
    assert not any("--dir" in call for call in calls)

    result = await tart_env.exec(["pwd"])
    assert result.success and result.stdout == f"{fakes.workspace}\n"

    connection = await tart_env.connection()
    assert connection.type == "tart" and connection.container == name
    assert connection.command.startswith("ssh ") and "-t" in connection.command.split()
    assert connection.command.endswith("127.0.0.1")

    await TartSandboxEnvironment.sample_cleanup("task", fakes.config, environments, False)
    assert not (fakes.vms / name).exists()
    assert f"delete {name}" in fakes.calls()
    assert name not in sandbox._process_state.vms
    with pytest.raises(SandboxUnavailableError):
        await tart_env.exec(["true"])
    with pytest.raises(ConnectionError):
        await tart_env.connection()


async def test_softnet_flag(fakes: Fakes) -> None:
    config = fakes.config.model_copy(update={"network": "softnet"})
    environments = await TartSandboxEnvironment.sample_init("task", config, {})
    name = environments["default"].as_type(TartSandboxEnvironment).vm_name
    assert f"run {name} --no-graphics --net-softnet" in fakes.calls()
    await TartSandboxEnvironment.sample_cleanup("task", config, environments, False)


async def test_shared_dirs_flag(fakes: Fakes) -> None:
    config = fakes.config.model_copy(update={"shared_dirs": ("/Users/me/proj", "/Users/me/b c")})
    environments = await TartSandboxEnvironment.sample_init("task", config, {})
    name = environments["default"].as_type(TartSandboxEnvironment).vm_name
    assert (
        f"run {name} --no-graphics --dir=agenteval0:/Users/me/proj --dir=agenteval1:/Users/me/b c"
        in fakes.calls()
    )
    await TartSandboxEnvironment.sample_cleanup("task", config, environments, False)


async def test_missing_workspace_fails_sample_clearly(fakes: Fakes) -> None:
    config = fakes.config.model_copy(update={"default_cwd": str(fakes.root / "work")})
    with pytest.raises(RuntimeError, match="does not exist in Tart image"):
        await TartSandboxEnvironment.sample_init("task", config, {})
    assert sorted(p.name for p in fakes.vms.iterdir()) == [IMAGE]
    assert sandbox._process_state.vms == {}


async def test_no_ip_on_nat_explains_dhcp_exhaustion(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TART_NO_IP", "1")
    with pytest.raises(RuntimeError, match="got no IP") as info:
        await TartSandboxEnvironment.sample_init("task", fakes.config, {})
    assert "DHCPLeaseTimeSecs" in str(info.value) and "softnet" in str(info.value)
    softnet = fakes.config.model_copy(update={"network": "softnet"})
    with pytest.raises(RuntimeError, match="got no IP") as info:
        await TartSandboxEnvironment.sample_init("task", softnet, {})
    assert "DHCPLeaseTimeSecs" not in str(info.value)
    assert sorted(p.name for p in fakes.vms.iterdir()) == [IMAGE]


async def test_task_init_warns_once_about_dhcp_leases(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Capture via the module logger directly: Inspect reconfigures logging during
    # earlier eval tests in the same session, which makes caplog miss records.
    warnings: list[str] = []
    monkeypatch.setattr(sandbox.logger, "warning", lambda msg, *a, **k: warnings.append(msg))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_dhcp_warned", False)
    monkeypatch.setattr(sandbox, "dhcp_lease_problem", lambda: "lease too long")
    await TartSandboxEnvironment.task_init("task", fakes.config)
    await TartSandboxEnvironment.task_init("task", fakes.config)
    assert warnings == ["lease too long"]

    monkeypatch.setattr(sandbox, "_dhcp_warned", False)
    warnings.clear()
    softnet = fakes.config.model_copy(update={"network": "softnet"})
    await TartSandboxEnvironment.task_init("task", softnet)
    assert warnings == []


async def test_task_init_prerequisites(fakes: Fakes) -> None:
    missing_image = fakes.config.model_copy(update={"image": "nope"})
    with pytest.raises(PrerequisiteError):
        await TartSandboxEnvironment.task_init("task", missing_image)
    missing_key = fakes.config.model_copy(update={"ssh_key": str(fakes.root / "nokey")})
    with pytest.raises(PrerequisiteError):
        await TartSandboxEnvironment.task_init("task", missing_key)


async def test_sample_init_failure_leaves_nothing(fakes: Fakes) -> None:
    with pytest.raises(RuntimeError, match="not found"):
        await TartSandboxEnvironment.sample_init(
            "task", fakes.config.model_copy(update={"image": "nope"}), {}
        )
    assert sandbox._process_state.vms == {}
    assert sorted(p.name for p in fakes.vms.iterdir()) == [IMAGE]


async def test_tart_run_exit_fails_fast(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_TART_RUN_FAIL", "1")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="boom: VM failed to start"):
        await TartSandboxEnvironment.sample_init("task", fakes.config, {})
    assert time.monotonic() - started < 4
    assert sorted(p.name for p in fakes.vms.iterdir()) == [IMAGE]
    assert sandbox._process_state.vms == {}


async def test_task_cleanup_deletes_leftovers(fakes: Fakes) -> None:
    names = []
    for _ in range(2):
        environments = await TartSandboxEnvironment.sample_init("task", fakes.config, {})
        names.append(environments["default"].as_type(TartSandboxEnvironment).vm_name)
    assert all((fakes.vms / n / "pid").exists() for n in names)
    await TartSandboxEnvironment.task_cleanup("task", fakes.config, True)
    assert not any((fakes.vms / n).exists() for n in names)
    assert sandbox._process_state.vms == {}


async def test_task_cleanup_without_cleanup_reports(
    fakes: Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    environments = await TartSandboxEnvironment.sample_init("task", fakes.config, {})
    name = environments["default"].as_type(TartSandboxEnvironment).vm_name
    await TartSandboxEnvironment.task_cleanup("task", fakes.config, False)
    assert name in capsys.readouterr().out
    assert (fakes.vms / name).exists()
    await TartSandboxEnvironment.sample_cleanup("task", fakes.config, environments, False)
    assert not (fakes.vms / name).exists()


async def test_cli_cleanup_only_deletes_sample_vms(fakes: Fakes) -> None:
    for name in ["agenteval-0123abcd", "agenteval-deadbeef", "agenteval-build-0123abcd", "mine"]:
        (fakes.vms / name).mkdir()
    await TartSandboxEnvironment.cli_cleanup("agenteval-0123abcd")
    assert not (fakes.vms / "agenteval-0123abcd").exists()
    assert (fakes.vms / "agenteval-deadbeef").exists()

    await TartSandboxEnvironment.cli_cleanup(IMAGE)  # never the image
    assert (fakes.vms / IMAGE).exists()

    await TartSandboxEnvironment.cli_cleanup(None)
    assert sorted(p.name for p in fakes.vms.iterdir()) == sorted(
        [IMAGE, "agenteval-build-0123abcd", "mine"]
    )
    assert await image.image_exists(IMAGE)
    assert not await image.image_exists("agenteval-deadbeef")


# --------------------------------------------------------------------------- exec


async def test_exec_arguments_are_literal(env: TartSandboxEnvironment, fakes: Fakes) -> None:
    marker = fakes.root / "pwned"
    args = ["a b", f"$(touch {marker})", "it's", 'q"uote', "x;y", "*", "new\nline"]
    result = await env.exec(["printf", "%s|", *args])
    assert result.stdout == "|".join(args) + "|"
    assert not marker.exists()


async def test_exec_env_cwd_input(env: TartSandboxEnvironment, fakes: Fakes) -> None:
    result = await env.exec(["/bin/sh", "-c", 'printf %s "$FOO"'], env={"FOO": "va'l $x"})
    assert result.stdout == "va'l $x"

    (fakes.workspace / "sub dir").mkdir()
    assert (await env.exec(["pwd"], cwd="sub dir")).stdout == f"{fakes.workspace}/sub dir\n"
    assert (await env.exec(["pwd"], cwd=str(fakes.root))).stdout == f"{fakes.root}\n"

    assert (await env.exec(["cat"], input="hello\nworld")).stdout == "hello\nworld"
    raw = bytes(range(256))
    result = await env.exec(["/bin/sh", "-c", "wc -c | tr -d ' '"], input=raw)
    assert result.stdout.strip() == "256"


async def test_exec_failures(env: TartSandboxEnvironment) -> None:
    result = await env.exec(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"])
    assert (result.success, result.returncode) == (False, 3)
    assert (result.stdout, result.stderr) == ("out\n", "err\n")
    assert (await env.exec(["definitely-not-a-command-xyz"])).returncode == 127
    with pytest.raises(ValueError):
        await env.exec(["true"], env={"BAD KEY": "1"})
    with pytest.raises(ValueError):
        await env.exec([])


async def test_exec_timeout(env: TartSandboxEnvironment) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError) as info:
        await env.exec(["/bin/sh", "-c", "echo partial; sleep 10"], timeout=1)
    assert time.monotonic() - started < 8
    assert "partial" in getattr(info.value, "truncated_output", "")
    assert (await env.exec(["echo", "fast"], timeout=5)).stdout == "fast\n"


async def test_exec_output_is_front_truncated(env: TartSandboxEnvironment) -> None:
    with override_sandbox_output_limit(1000, "exec"):
        result = await env.exec(
            [
                "/bin/sh",
                "-c",
                "i=0; while [ $i -lt 500 ]; do printf 0123456789; i=$((i+1)); done; printf END",
            ]
        )
    assert len(result.stdout) <= 1000
    assert result.stdout.endswith("END")


async def test_exec_user_without_sudo(env: TartSandboxEnvironment) -> None:
    assert (await env.exec(["true"], user=getpass.getuser())).success
    with pytest.raises(SandboxUserUnsupportedError):
        await env.exec(["true"], user="root" if getpass.getuser() != "root" else "nobody")


async def test_connect_failures_retry_then_unavailable(
    env: TartSandboxEnvironment, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    fails = fakes.root / "fails"
    monkeypatch.setenv("FAKE_SSH_FAILS", str(fails))
    fails.write_text("1")
    assert (await env.exec(["echo", "ok"])).stdout == "ok\n"
    fails.write_text("10")
    with pytest.raises(SandboxUnavailableError, match="Connection refused"):
        await env.exec(["true"])
    fails.write_text("0")
    # a command's own exit 255 is a result, not a dead sandbox
    assert (await env.exec(["/bin/sh", "-c", "exit 255"])).returncode == 255


async def test_command_exit_255_with_ssh_like_stderr_runs_once(
    env: TartSandboxEnvironment, fakes: Fakes
) -> None:
    """An inner ssh failing inside the VM is the command's result, not a dead sandbox."""
    runs = fakes.root / "runs"
    script = (
        f"echo run >> '{runs}'; "
        "echo 'ssh: connect to host 10.0.0.5 port 22: Connection refused' >&2; "
        "echo 'agent@10.0.0.5: Permission denied (publickey).' >&2; exit 255"
    )
    result = await env.exec(["/bin/sh", "-c", script])
    assert result.returncode == 255 and "Connection refused" in result.stderr
    assert runs.read_text().splitlines() == ["run"]


async def test_client_auth_failure_is_unavailable_without_retry(
    env: TartSandboxEnvironment, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = fakes.root / "count"
    logs = fakes.root / "client-logs"
    logs.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(logs))
    monkeypatch.setenv("FAKE_SSH_COUNT", str(count))
    monkeypatch.setenv("FAKE_SSH_CLIENT_ERROR", "admin@127.0.0.1: Permission denied (publickey).")
    with pytest.raises(SandboxUnavailableError, match="Permission denied"):
        await env.exec(["true"])
    assert count.read_text().splitlines() == ["x"]
    assert list(logs.iterdir()) == []  # the per-call client log is removed


SPAWN_DAEMON = (
    "import subprocess, sys\n"
    "p = subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "open(sys.argv[1], 'w').write(str(p.pid))\n"
)


@pytest.mark.parametrize("input", [None, "request"])
async def test_daemon_left_running_does_not_hang_exec(
    env: TartSandboxEnvironment, fakes: Fakes, input: str | None
) -> None:
    """Like sandbox-tools ``start-server``: the daemon keeps stdin but not stdout."""
    pidfile = fakes.root / "daemon.pid"
    started = time.monotonic()
    try:
        result = await env.exec(
            [sys.executable, "-c", SPAWN_DAEMON, str(pidfile)], input=input, timeout=10
        )
        assert result.success, result.stderr
        assert time.monotonic() - started < 5
    finally:
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)


FAKE_SUDO = r"""#!/bin/bash
user=
while [ $# -gt 0 ]; do
  case "$1" in
    -u) user=$2; shift 2 ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
echo "sudo:${user}" >> "$FAKE_SUDO_LOG"
export FAKE_SUDO_USER="$user"
exec "$@"
"""

FAKE_GETENT = r"""#!/bin/bash
echo "getent:$2" >> "$FAKE_SUDO_LOG"
case "$2" in
  agent|1001) echo "$2:x:1001:1001::/home/agent:/bin/bash" ;;
  *) exit 2 ;;
esac
"""


async def test_sudo_user_switching(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    """use_sudo=True with sudo/getent shims: who each call runs as, and user validation."""
    _executable(fakes.root / "sudo", FAKE_SUDO)
    _executable(fakes.root / "getent", FAKE_GETENT)
    log = fakes.root / "sudo.log"
    monkeypatch.setenv("FAKE_SUDO_LOG", str(log))
    monkeypatch.setattr(
        ssh,
        "REMOTE_TOOLS",
        ssh.RemoteTools(
            sudo=str(fakes.root / "sudo"),
            getent=str(fakes.root / "getent"),
            timeout=ssh.REMOTE_TOOLS.timeout,
        ),
    )
    config = fakes.config.model_copy(update={"use_sudo": True})
    environments = await TartSandboxEnvironment.sample_init("task", config, {})
    env = environments["default"].as_type(TartSandboxEnvironment)
    try:
        assert log.read_text().splitlines() == ["sudo:"]  # readiness check: sudo -n true

        whoami = ["/bin/sh", "-c", 'printf %s "$FAKE_SUDO_USER"']
        assert (await env.exec(whoami)).stdout == "root"
        assert (await env.exec(whoami, user="agent")).stdout == "agent"
        assert (await env.exec(whoami, user="agent", timeout=5)).stdout == "agent"
        assert (await env.exec(whoami, user="1001")).stdout == "#1001"
        with pytest.raises(SandboxUserUnsupportedError, match="ghost"):
            await env.exec(whoami, user="ghost")
        with pytest.raises(SandboxUserUnsupportedError):
            await env.exec(whoami, user="-oops")
        lookups = [line for line in log.read_text().splitlines() if line.startswith("getent:")]
        assert lookups == ["getent:agent", "getent:1001", "getent:ghost"]  # cached after success

        await env.write_file("owned.txt", "x")
        assert log.read_text().splitlines()[-1] == "sudo:root"
        assert await env.read_file("owned.txt") == "x"

        connection = await env.connection(user="agent")
        assert connection.command.endswith("127.0.0.1 'sudo -i -u agent'")
    finally:
        await TartSandboxEnvironment.sample_cleanup("task", config, environments, False)


async def test_sudo_readiness_check_fails_sample(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(
        fakes.root / "nosudo", "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n"
    )
    monkeypatch.setattr(ssh, "REMOTE_TOOLS", ssh.RemoteTools(sudo=str(fakes.root / "nosudo")))
    config = fakes.config.model_copy(update={"use_sudo": True})
    with pytest.raises(RuntimeError, match="cannot run sudo without a password"):
        await TartSandboxEnvironment.sample_init("task", config, {})
    assert sorted(p.name for p in fakes.vms.iterdir()) == [IMAGE]


# --------------------------------------------------------------------------- files


async def test_write_read_roundtrip(env: TartSandboxEnvironment, fakes: Fakes) -> None:
    text = "line one\r\nline two\nunicode: é中\U0001f600\n"
    await env.write_file("nested/dir/file.txt", text)
    on_disk = fakes.workspace / "nested" / "dir" / "file.txt"
    assert on_disk.read_bytes() == text.encode()
    assert await env.read_file("nested/dir/file.txt") == text

    blob = bytes(range(256)) * 64
    absolute = fakes.root / "abs" / "blob.bin"
    await env.write_file(str(absolute), blob)
    assert absolute.read_bytes() == blob
    assert await env.read_file(str(absolute), text=False) == blob

    await env.write_file("empty", "")
    assert (fakes.workspace / "empty").read_bytes() == b""
    assert await env.read_file("empty") == ""

    await env.write_file("nested/dir/file.txt", b"overwritten")
    assert await env.read_file("nested/dir/file.txt") == "overwritten"

    weird = "we'ird $(name) ;.txt"
    await env.write_file(weird, "ok")
    assert (fakes.workspace / weird).read_text() == "ok"


async def test_read_file_errors(env: TartSandboxEnvironment, fakes: Fakes) -> None:
    with pytest.raises(FileNotFoundError):
        await env.read_file("missing.txt")
    (fakes.workspace / "adir").mkdir()
    with pytest.raises(IsADirectoryError):
        await env.read_file("adir")
    with pytest.raises(IsADirectoryError):
        await env.write_file("adir", "x")

    (fakes.workspace / "latin1.txt").write_bytes(b"caf\xe9")
    with pytest.raises(UnicodeDecodeError):
        await env.read_file("latin1.txt")
    assert await env.read_file("latin1.txt", text=False) == b"caf\xe9"

    (fakes.workspace / "big.txt").write_bytes(b"x" * 200)
    with override_sandbox_output_limit(100, "read_file"):
        with pytest.raises(OutputLimitExceededError):
            await env.read_file("big.txt")
        (fakes.workspace / "small.txt").write_bytes(b"y" * 100)
        assert await env.read_file("small.txt") == "y" * 100


async def test_read_file_rejects_fifos_and_devices(
    env: TartSandboxEnvironment, fakes: Fakes
) -> None:
    os.mkfifo(fakes.workspace / "fifo")
    (fakes.workspace / "zero").symlink_to("/dev/zero")
    (fakes.workspace / "real.txt").write_text("content")
    (fakes.workspace / "link.txt").symlink_to(fakes.workspace / "real.txt")
    started = time.monotonic()
    for name in ["fifo", "zero"]:
        with pytest.raises(OSError) as info:
            await env.read_file(name)
        assert info.value.errno == errno.EINVAL
    assert time.monotonic() - started < 10
    assert await env.read_file("link.txt") == "content"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
async def test_permission_errors(env: TartSandboxEnvironment, fakes: Fakes) -> None:
    secret = fakes.workspace / "secret.txt"
    secret.write_text("s")
    secret.chmod(0)
    locked = fakes.workspace / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(PermissionError):
            await env.read_file("secret.txt")
        with pytest.raises(PermissionError):
            await env.write_file("locked/new.txt", "x")
    finally:
        secret.chmod(0o600)
        locked.chmod(0o700)


async def test_works_through_sandbox_environment_api(env: TartSandboxEnvironment) -> None:
    generic: SandboxEnvironment = env
    await generic.write_file("api.txt", "via base class")
    assert await generic.read_file("api.txt") == "via base class"


def test_inspect_eval_end_to_end(fakes: Fakes) -> None:
    """A real Inspect eval (mockllm) on the provider: lifecycle, sample files, cleanup."""
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ModelOutput
    from inspect_ai.scorer import includes
    from inspect_ai.solver import Generate, Solver, TaskState, solver
    from inspect_ai.util import SandboxEnvironmentSpec
    from inspect_ai.util import sandbox as current_sandbox

    @solver
    def read_planted_file() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            result = await current_sandbox().exec(["cat", "hello.txt"])
            state.output = ModelOutput.from_content("mockllm/model", result.stdout)
            return state

        return solve

    task = Task(
        dataset=[Sample(input="read it", target="planted", files={"hello.txt": "planted"})],
        solver=read_planted_file(),
        scorer=includes(),
        sandbox=SandboxEnvironmentSpec("tart", fakes.config.model_copy(update={"cpus": 3})),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(fakes.root / "logs"), display="none")
    assert log.status == "success", log.error
    assert log.results is not None
    assert log.results.scores[0].metrics["accuracy"].value == 1.0
    assert (fakes.workspace / "hello.txt").read_text() == "planted"
    assert sorted(p.name for p in fakes.vms.iterdir()) == [IMAGE]
    assert any(" --cpu 3 " in call for call in fakes.calls())
    assert log.eval.sandbox is not None and log.eval.sandbox.type == "tart"
    assert "bootstrap_password" not in log.eval.sandbox.model_dump_json()


# --------------------------------------------------------------------------- image build


def _recorded(directory: Path) -> list[tuple[str, bytes, str | None]]:
    count = int((directory / "counter").read_text())
    out = []
    for i in range(count):
        askpass = directory / f"{i}.askpass"
        out.append(
            (
                (directory / f"{i}.cmd").read_text(),
                (directory / f"{i}.stdin").read_bytes(),
                askpass.read_text().strip() if askpass.exists() else None,
            )
        )
    return out


@pytest.fixture
def build(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> tuple[TartSettings, Path, Path]:
    record_dir = fakes.root / "ssh-record"
    record_dir.mkdir()
    monkeypatch.setenv("FAKE_SSH_MODE", "record")
    monkeypatch.setenv("FAKE_SSH_DIR", str(record_dir))
    monkeypatch.setenv("FAKE_EXPECTED_PASSWORD", "s3cret-boot-pw")
    monkeypatch.setattr(image, "SHUTDOWN_WAIT", 0.5)
    settings = TartSettings(
        base_image="ghcr.io/cirruslabs/ubuntu:24.04",
        image="agenteval-test-image",
        ssh_key=str(fakes.root / "keys" / "id_ed25519"),
        bootstrap_password="s3cret-boot-pw",
        boot_timeout=20,
    )
    script = fakes.root / "provision.sh"
    script.write_text("#!/bin/bash\necho provisioning\n")
    return settings, script, record_dir


async def test_build_image(fakes: Fakes, build: tuple[TartSettings, Path, Path]) -> None:
    settings, script, record_dir = build
    await image.build_image(settings, script, force=False)

    assert (fakes.vms / "agenteval-test-image").is_dir()
    leftovers = [p.name for p in fakes.vms.iterdir() if p.name.startswith("agenteval-build-")]
    assert leftovers == []
    key = Path(settings.ssh_key)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    pub = key.with_name("id_ed25519.pub").read_text().strip()

    calls = fakes.calls()
    clone = next(c for c in calls if c.startswith("clone "))
    build_name = clone.split()[2]
    assert clone == f"clone ghcr.io/cirruslabs/ubuntu:24.04 {build_name}"
    assert f"run {build_name} --no-graphics" in calls
    assert f"rename {build_name} agenteval-test-image" in calls
    assert not any("--dir" in c for c in calls)

    recorded = _recorded(record_dir)
    # password never on a command line or in stdin; only via askpass
    for cmd, stdin, _ in recorded:
        assert "s3cret-boot-pw" not in cmd
        assert b"s3cret-boot-pw" not in stdin
    assert "s3cret-boot-pw" not in "\n".join(calls)
    password_logins = [r for r in recorded if r[2] is not None]
    assert [r[2] for r in password_logins] == ["ok", "ok"]  # key push, then refused re-login
    key_push = password_logins[:1]
    assert password_logins[1][0] == "true"
    assert key_push[0][1].decode().strip() == pub
    assert "authorized_keys" in key_push[0][0]

    upload = next(r for r in recorded if "agenteval-provision.XXXXXX" in r[0])
    assert upload[1] == script.read_bytes()
    assert any(
        "sudo -n -H -- /usr/bin/env DEBIAN_FRONTEND=noninteractive "
        "AGENTEVAL_AGENT_USER=agent AGENTEVAL_WORKSPACE=/workspace /bin/bash "
        "/tmp/agenteval-provision.abc123 </dev/null" in r[0]
        for r in recorded
    )
    harden = next(r[0] for r in recorded if "00-agenteval.conf" in r[0])
    assert "PasswordAuthentication no" in harden and "passwd -l" in harden
    assert any("systemctl poweroff" in r[0] for r in recorded)


async def test_build_image_is_noop_unless_forced(
    fakes: Fakes, build: tuple[TartSettings, Path, Path]
) -> None:
    settings, script, _ = build
    (fakes.vms / settings.image).mkdir()
    (fakes.vms / settings.image / "old").write_text("old image")
    await image.ensure_ssh_key(settings.ssh_key)

    await image.build_image(settings, script, force=False)
    assert not any(c.startswith("clone ") for c in fakes.calls())
    assert (fakes.vms / settings.image / "old").exists()

    await image.build_image(settings, script, force=True)
    assert (fakes.vms / settings.image).is_dir()
    assert not (fakes.vms / settings.image / "old").exists()
    assert f"delete {settings.image}" in fakes.calls()


async def test_build_image_passes_agent_user_and_workspace(
    fakes: Fakes, build: tuple[TartSettings, Path, Path]
) -> None:
    settings, script, record_dir = build
    await image.build_image(settings, script, force=False, agent_user="dev", workspace="/work")
    assert any(
        "AGENTEVAL_AGENT_USER=dev AGENTEVAL_WORKSPACE=/work /bin/bash" in cmd
        for cmd, _, _ in _recorded(record_dir)
    )


async def test_existing_image_with_missing_key_is_an_error(
    fakes: Fakes, build: tuple[TartSettings, Path, Path]
) -> None:
    """A fresh key would not be trusted by the image; never create one silently."""
    settings, script, _ = build
    (fakes.vms / settings.image).mkdir()
    key = Path(settings.ssh_key)
    with pytest.raises(RuntimeError, match="--force"):
        await image.build_image(settings, script, force=False)
    assert not key.exists() and not key.with_name(key.name + ".pub").exists()
    assert not any(c.startswith("clone ") for c in fakes.calls())

    await image.build_image(settings, script, force=True)
    assert key.is_file()


async def test_build_failure_removes_build_vm(
    fakes: Fakes, build: tuple[TartSettings, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, script, _ = build
    monkeypatch.setenv("FAKE_TART_RUN_FAIL", "1")
    with pytest.raises(RuntimeError):
        await image.build_image(settings, script, force=False)
    assert not any(p.name.startswith("agenteval-build-") for p in fakes.vms.iterdir())
    assert not (fakes.vms / settings.image).exists()


async def test_build_fails_if_password_login_survives(
    fakes: Fakes, build: tuple[TartSettings, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, script, _ = build
    monkeypatch.setenv("FAKE_SSH_HARDEN_IGNORED", "1")
    with pytest.raises(RuntimeError, match="Password SSH login still works"):
        await image.build_image(settings, script, force=False)
    assert not (fakes.vms / settings.image).exists()
    assert not any(p.name.startswith("agenteval-build-") for p in fakes.vms.iterdir())
