"""sandbox-exec (bwrap profile) inside the real environment image.

Optional: skipped unless Docker is running and ``agenteval/ubuntu-24.04:latest``
exists (``agenteval env build docker-ubuntu``). Containers run with
``--network none`` and no host mounts.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenteval import constants
from agenteval.config import load_profiles
from agenteval.harnesses.sandbox_exec import render_sandbox_exec

IMAGE = "agenteval/ubuntu-24.04:latest"
REPO = Path(__file__).resolve().parents[1]
# bubblewrap needs user namespaces (seccomp/apparmor) and an unmasked /proc to
# mount a fresh procfs (systempaths).
SECURITY_OPTS = ["seccomp=unconfined", "apparmor=unconfined", "systempaths=unconfined"]

TOOLS_SOCKET = f"{constants.SANDBOX_TOOLS_DIR}/.server/sandbox-tools.sock"
RUN_SOCKET = "/run/agenteval-test/daemon.sock"

SETUP = f"""
set -e
mkdir -p /home/agent/.ssh /etc/agenteval/secrets
echo AGENTEVAL_CANARY_ssh > /home/agent/.ssh/id_ed25519
echo AGENTEVAL_CANARY_netrc > /home/agent/.netrc
chown -R agent:agent /home/agent
chmod 0600 /home/agent/.ssh/id_ed25519 /home/agent/.netrc
echo AGENTEVAL_CANARY_token > /etc/agenteval/secrets/service-token
chmod 0644 /etc/agenteval/secrets/service-token
echo AGENTEVAL_CANARY_root > /root/.secret_canary
mkdir -p -m 0700 {constants.STATE_DIR} {constants.SANDBOX_TOOLS_DIR}/.server
echo '{{}}' > {constants.STATE_DIR}/baseline.json
mkdir -p {RUN_SOCKET.rsplit("/", 1)[0]}
# what install_inner_sandbox does for bwrap cells (provision.sh may restrict bwrap)
groupadd -f {constants.BWRAP_GROUP}
usermod -aG {constants.BWRAP_GROUP} agent
"""

# Stand-ins for root-only IPC (Inspect's sandbox-tools server socket: 0600 in a 0700
# dir) and a host daemon socket under /run (0666, like D-Bus). They only answer a
# marker string.
MARKER_SERVER = """
import os, socket, sys
path, mode = sys.argv[1], int(sys.argv[2], 8)
s = socket.socket(socket.AF_UNIX)
s.bind(path)
os.chmod(path, mode)
s.listen()
while True:
    c, _ = s.accept()
    c.sendall(b"MARKER-REACHED")
    c.close()
"""

CONNECT = """
import socket, sys
for path in sys.argv[1:]:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path)
        print("reached", path, s.recv(64).decode())
    except OSError as ex:
        print("blocked", path, ex.errno)
"""


def _image_available() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        return f"docker unavailable: {ex}"
    if result.returncode != 0:
        return f"image {IMAGE} not built (or docker daemon not running)"
    return None


_skip_reason = _image_available()
pytestmark = [pytest.mark.docker]
if _skip_reason is not None:
    pytestmark.append(pytest.mark.skip(reason=_skip_reason))


def docker(
    *args: str, input: str | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, input=input, timeout=120
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result


def start_container(security_opts: list[str]) -> str:
    name = f"agenteval-harness-test-{uuid.uuid4().hex[:8]}"
    opts = [f"--security-opt={o}" for o in security_opts]
    docker(
        "run",
        "-d",
        "--rm",
        "--init",
        "--network",
        "none",
        *opts,
        "--name",
        name,
        IMAGE,
        "tail",
        "-f",
        "/dev/null",
    )
    script = render_sandbox_exec(
        load_profiles(REPO / "config").harnesses["opencode-bwrap"].inner_sandbox,
        agent_home="/home/agent",
        workspace="/workspace",
    )
    install = f'mkdir -p {constants.OPT_DIR} && cat > "$1" && chmod 0755 "$1"'
    docker(
        "exec",
        "-i",
        "-u",
        "root",
        name,
        "sh",
        "-c",
        install,
        "sh",
        constants.SANDBOX_EXEC,
        input=script,
    )
    docker("exec", "-u", "root", name, "sh", "-c", SETUP)
    for path, mode in ((TOOLS_SOCKET, "600"), (RUN_SOCKET, "666")):
        docker("exec", "-d", "-u", "root", name, "python3", "-c", MARKER_SERVER, path, mode)
    wait = (
        f"for i in $(seq 50); do [ -S {TOOLS_SOCKET} ] && [ -S {RUN_SOCKET} ] && exit 0; "
        "sleep 0.1; done; exit 1"
    )
    docker("exec", "-u", "root", name, "sh", "-c", wait)
    return name


@pytest.fixture(scope="module")
def container() -> Iterator[str]:
    name = start_container(SECURITY_OPTS)
    try:
        yield name
    finally:
        docker("rm", "-f", name, check=False)


def sandboxed(
    container: str, script: str, *, user: str = "agent", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env_args = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    return docker(
        "exec",
        "-u",
        user,
        "-w",
        "/workspace",
        *env_args,
        container,
        constants.SANDBOX_EXEC,
        "sh",
        "-c",
        script,
        check=False,
    )


def test_hidden_files_invisible_workspace_writable(container: str) -> None:
    result = sandboxed(
        container,
        """
        for f in ~/.ssh/id_ed25519 ~/.netrc /etc/agenteval/secrets/service-token; do
            if [ -s "$f" ]; then echo "visible $f"; fi
        done
        echo ok > /workspace/written-in-sandbox && echo workspace-writable
        touch ~/home-marker 2>/dev/null && echo home-writable
        touch /etc/system-marker 2>/dev/null && echo system-writable
        echo t > /tmp/tmp-marker && echo tmp-writable
        echo "env:${AWS_SECRET_ACCESS_KEY-unset}:${OPENCODE_X-unset}"
        """,
        env={"AWS_SECRET_ACCESS_KEY": "AGENTEVAL_CANARY_aws", "OPENCODE_X": "kept"},
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert not [line for line in lines if line.startswith("visible")]
    assert "workspace-writable" in lines
    assert "home-writable" not in lines
    assert "system-writable" not in lines
    assert "tmp-writable" in lines
    assert "env:unset:kept" in lines
    # writes landed (or not) on the real filesystem as expected
    outside = docker(
        "exec", "-u", "root", container, "sh", "-c", "ls /workspace; ls /home/agent; ls /tmp"
    )
    assert "written-in-sandbox" in outside.stdout
    assert "home-marker" not in outside.stdout
    assert "tmp-marker" not in outside.stdout


def test_proxy_only_network(container: str) -> None:
    for port in ("3001", "3002"):
        docker(
            "exec",
            "-d",
            "-u",
            "root",
            container,
            "python3",
            "-m",
            "http.server",
            port,
            "--bind",
            "127.0.0.1",
        )
    docker(
        "exec",
        "-u",
        "root",
        container,
        "sh",
        "-c",
        "for i in $(seq 50); do grep -q ':0BB9 .* 0A ' /proc/net/tcp && "
        "grep -q ':0BBA .* 0A ' /proc/net/tcp && exit 0; sleep 0.1; done; exit 1",
    )
    probe = """
import socket, urllib.request
print("bridge", urllib.request.urlopen("http://localhost:3001/", timeout=5).status)
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("127.0.0.1", 3002))
    print("other-reachable")
except OSError:
    print("other-blocked")
"""
    result = sandboxed(
        container, f"python3 -c '{probe}'", env={"OPENAI_BASE_URL": "http://localhost:3001/v1"}
    )
    assert result.returncode == 0, result.stderr
    assert "bridge 200" in result.stdout
    assert "other-blocked" in result.stdout
    # the relay socket dir is mounted read-only
    ro = sandboxed(
        container,
        "touch /tmp/.agenteval-proxy/marker || echo proxy-dir-read-only",
        env={"OPENAI_BASE_URL": "http://localhost:3001/v1"},
    )
    assert "proxy-dir-read-only" in ro.stdout, ro.stderr
    leftovers = docker(
        "exec",
        "-u",
        "root",
        container,
        "sh",
        "-c",
        "ls -d /tmp/agenteval-sx.* 2>/dev/null; pgrep -x socat",
        check=False,
    )
    assert leftovers.stdout.strip() == ""


def test_root_in_container(container: str) -> None:
    result = sandboxed(
        container,
        "ls -A /root; [ -s /home/agent/.ssh/id_ed25519 ] && echo agent-key-visible; id -u",
        user="root",
        env={"HOME": "/root"},
    )
    assert result.returncode == 0, result.stderr
    assert ".secret_canary" not in result.stdout
    assert "agent-key-visible" not in result.stdout
    assert result.stdout.split()[-1] == "0"


@pytest.mark.parametrize("user", ["agent", "root"])
def test_ipc_sockets_and_state_hidden(container: str, user: str) -> None:
    # control: both stand-ins are reachable outside the sandbox
    outside = docker(
        "exec", "-u", "root", container, "python3", "-c", CONNECT, TOOLS_SOCKET, RUN_SOCKET
    )
    assert outside.stdout.count("reached") == 2, outside.stdout
    var_run = RUN_SOCKET.replace("/run/", "/var/run/", 1)
    result = sandboxed(
        container,
        f"python3 -c '{CONNECT}' {TOOLS_SOCKET} {RUN_SOCKET} {var_run}; "
        f"echo LISTING; ls -A {constants.STATE_DIR} {constants.SANDBOX_TOOLS_DIR} /run",
        user=user,
        env={"HOME": "/root" if user == "root" else "/home/agent"},
    )
    assert result.returncode == 0, result.stderr
    connects, listing = result.stdout.split("LISTING", 1)
    assert "reached" not in connects
    assert connects.count("blocked") == 3
    # hidden dirs are empty, /run is a fresh tmpfs
    assert "baseline.json" not in listing
    assert ".server" not in listing
    assert "agenteval-test" not in listing
    warned = "running as root" in result.stderr and "weaker boundary" in result.stderr
    assert warned == (user == "root")


@pytest.mark.parametrize("user", ["agent", "root"])
def test_nested_user_namespaces_disabled(container: str, user: str) -> None:
    result = sandboxed(
        container,
        "unshare -Ur true 2>/dev/null && echo nested-userns || echo nested-userns-blocked",
        user=user,
    )
    assert result.returncode == 0, result.stderr
    assert "nested-userns-blocked" in result.stdout


def test_fail_closed_without_unmasked_proc() -> None:
    name = start_container(["seccomp=unconfined", "apparmor=unconfined"])
    try:
        result = sandboxed(name, "touch /workspace/ran-unsandboxed")
        assert result.returncode == 125
        assert "could not set up the sandbox" in result.stderr
        listing = docker("exec", "-u", "root", name, "ls", "/workspace")
        assert "ran-unsandboxed" not in listing.stdout
    finally:
        docker("rm", "-f", name, check=False)
