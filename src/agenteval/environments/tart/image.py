"""Tart CLI wrappers: VM lifecycle, leftover cleanup and building the base image.

``agenteval env build`` turns the public Cirrus Labs Ubuntu image into a local,
provisioned VM (``TartSettings.image``) that the sandbox clones for every sample:
key-only SSH for ``ssh_user``, bootstrap password locked, provision script applied.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import plistlib
import re
import secrets
import shlex
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from subprocess import DEVNULL, STDOUT

import anyio
from anyio.abc import Process
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.util import ExecResult, SandboxUnavailableError, subprocess

from agenteval.config import TartSettings

from . import ssh
from .ssh import (
    SSHTarget,
    ssh_argv,
    ssh_binary,
    ssh_options,
    ssh_run,
    wait_for_ssh,
)

logger = logging.getLogger(__name__)

VM_PREFIX = "agenteval-"
SAMPLE_VM_PATTERN = re.compile(r"^agenteval-[0-9a-f]{8}$")
"""Per-sample clones. Deliberately does not match the image (``agenteval-ubuntu-...``)
or in-progress builds (``agenteval-build-...``), so cleanup never deletes them."""

STOP_GRACE = 15
"""Seconds a VM gets to exit after SIGINT (what ``tart stop`` sends) before SIGKILL."""

SHUTDOWN_WAIT = 120
"""Seconds to wait for a guest-initiated poweroff during image build."""

PROVISION_TIMEOUT = 3600
ASKPASS_ENV = "AGENTEVAL_ASKPASS_PASSWORD"

TART_RELEASES_URL = "https://github.com/openai/tart/releases"
"""Tart's releases (github.com/cirruslabs/tart now redirects here)."""

TART_MISSING_MESSAGE = (
    "Tart is not installed or not on PATH. The Homebrew formula is currently broken: "
    f"install the release tarball from {TART_RELEASES_URL} (see docs/setup.md)."
)

DHCP_PLIST = Path(
    "/Library/Preferences/SystemConfiguration/com.apple.InternetSharing.default.plist"
)
"""macOS bootpd settings for the shared (``nat``) network Tart VMs get their IPs from."""

DHCP_DEFAULT_LEASE = 86400
DHCP_MAX_LEASE = 3600
"""Longer leases can exhaust the ~253-address pool when every sample VM has a new MAC."""

DHCP_LEASE_HINT = (
    "Every sample VM gets a new MAC and so a new lease on the default 'nat' network; with "
    "macOS's 24 h default the ~253-address pool runs out after ~253 VMs in a day. Shorten "
    "the lease (`sudo defaults write "
    f"{DHCP_PLIST} bootpd -dict DHCPLeaseTimeSecs -int 600`) or set the environment's "
    "tart.network to softnet. See https://tart.run/faq/."
)


def tart_available() -> str | None:
    """Path of the ``tart`` executable, or None when Tart is not installed."""
    return shutil.which("tart")


def tart_binary() -> str:
    path = tart_available()
    if path is None:
        raise PrerequisiteError(TART_MISSING_MESSAGE)
    return path


def dhcp_lease_seconds(plist: Path = DHCP_PLIST) -> int:
    """bootpd's DHCP lease time for the ``nat`` network (macOS default when unset)."""
    try:
        with plist.open("rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return DHCP_DEFAULT_LEASE
    bootpd = data.get("bootpd") if isinstance(data, dict) else None
    lease = bootpd.get("DHCPLeaseTimeSecs") if isinstance(bootpd, dict) else None
    return lease if isinstance(lease, int) and not isinstance(lease, bool) else DHCP_DEFAULT_LEASE


def dhcp_lease_problem(plist: Path = DHCP_PLIST) -> str | None:
    """A warning when the ``nat`` network's DHCP lease is long enough to run out, else None."""
    lease = dhcp_lease_seconds(plist)
    if lease <= DHCP_MAX_LEASE:
        return None
    return f"macOS DHCP lease time is {lease}s. {DHCP_LEASE_HINT}"


async def run_tart(
    args: list[str],
    *,
    timeout: int | None = 120,
    check: bool = True,
    capture: bool = True,
) -> ExecResult[str]:
    result = await subprocess(
        [tart_binary(), *args],
        timeout=timeout,
        capture_output=capture,
        concurrency=False,
    )
    if check and not result.success:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"`tart {' '.join(args)}` failed (exit {result.returncode}): {detail}")
    return result


def new_vm_name() -> str:
    return f"{VM_PREFIX}{secrets.token_hex(4)}"


def is_sample_vm(name: str) -> bool:
    return SAMPLE_VM_PATTERN.match(name) is not None


async def list_vms() -> list[dict[str, object]]:
    """Local VMs as reported by ``tart list --format json``."""
    result = await run_tart(["list", "--source", "local", "--format", "json"])
    data = json.loads(result.stdout or "[]")
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected `tart list` output: {result.stdout[:200]!r}")
    return [vm for vm in data if isinstance(vm, dict)]


def vm_name(vm: dict[str, object]) -> str | None:
    name = vm.get("Name", vm.get("name"))
    return name if isinstance(name, str) else None


async def image_exists(name: str) -> bool:
    """Whether a local Tart VM called ``name`` exists (False when Tart is missing)."""
    if tart_available() is None:
        return False
    return any(vm_name(vm) == name for vm in await list_vms())


async def delete_vm(name: str) -> bool:
    """Stop (if running) and delete a VM we do not hold a process handle for."""
    await run_tart(["stop", "--timeout", "5", name], check=False, timeout=60)
    result = await run_tart(["delete", name], check=False, timeout=120)
    if not result.success:
        logger.warning("Failed to delete Tart VM %s: %s", name, result.stderr.strip())
    return result.success


async def cleanup_vms(id: str | None = None) -> list[str]:
    """Delete leftover per-sample VMs (all of them, or only ``id``). Returns deleted names."""
    if id is not None and not is_sample_vm(id):
        logger.warning("Refusing to delete %r: not an agenteval sample VM name", id)
        return []
    names = [n for n in (vm_name(vm) for vm in await list_vms()) if n and is_sample_vm(n)]
    if id is not None:
        names = [n for n in names if n == id]
    deleted = []
    for name in names:
        if await delete_vm(name):
            deleted.append(name)
    return deleted


SHARE_TAG = "com.apple.virtio-fs.automount"
"""virtiofs tag under which Tart exposes all ``--dir`` shares to a Linux guest; each
share is a subdirectory named after it (``share_name``)."""


def share_name(index: int) -> str:
    """Name of the ``index``-th ``--dir`` share of a VM."""
    return f"agenteval{index}"


class TartVM:
    """One Tart VM and the ``tart run`` process that keeps it running."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ip: str | None = None
        self.destroyed = False
        self._cloned = False
        self._process: Process | None = None
        self._log_dir: Path | None = None
        self._network: str | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def clone(self, source: str, *, timeout: int | None = 600, capture: bool = True) -> None:
        self._cloned = True  # the name is ours: delete even a clone interrupted midway
        await run_tart(["clone", source, self.name], timeout=timeout, capture=capture)

    async def configure(self, *, cpu: int, memory_mb: int) -> None:
        # A fresh MAC per clone: two VMs sharing one would share a DHCP lease (and IP).
        await run_tart(
            ["set", self.name, "--cpu", str(cpu), "--memory", str(memory_mb), "--random-mac"]
        )

    async def start(self, *, network: str, dirs: Sequence[str] = ()) -> None:
        """Boot in the background.

        Eval VMs pass no ``dirs``: the host shares no folders with them. ``dirs`` (dev
        live mounts) become ``--dir=agenteval<i>:<path>`` shares, visible in the guest
        under the virtiofs automount tag (see ``SHARE_TAG``).
        """
        args = [tart_binary(), "run", self.name, "--no-graphics"]
        if network == "softnet":
            args.append("--net-softnet")
        args += [f"--dir={share_name(i)}:{path}" for i, path in enumerate(dirs)]
        self._network = network
        self._log_dir = Path(tempfile.mkdtemp(prefix="agenteval-tart-"))
        with (self._log_dir / "run.log").open("wb") as log:
            self._process = await anyio.open_process(args, stdin=DEVNULL, stdout=log, stderr=STDOUT)

    def log_tail(self, limit: int = 2000) -> str:
        if self._log_dir is None:
            return ""
        try:
            return (self._log_dir / "run.log").read_bytes()[-limit:].decode(errors="replace")
        except OSError:
            return ""

    async def wait_ip(self, timeout: int) -> str:
        """``tart ip --wait``; fails fast if ``tart run`` exits first."""
        process = self._process
        if process is None:
            raise RuntimeError(f"Tart VM {self.name} was not started")
        result: ExecResult[str] | None = None
        async with anyio.create_task_group() as tg:

            async def get_ip() -> None:
                nonlocal result
                result = await run_tart(
                    ["ip", self.name, "--wait", str(timeout)], timeout=timeout + 30, check=False
                )
                tg.cancel_scope.cancel()

            async def watch() -> None:
                await process.wait()
                tg.cancel_scope.cancel()

            tg.start_soon(get_ip)
            tg.start_soon(watch)
        if result is None:
            raise RuntimeError(
                f"`tart run {self.name}` exited with code {process.returncode}: {self.log_tail()}"
            )
        ip = result.stdout.strip()
        if not result.success or not ip:
            message = f"Tart VM {self.name} got no IP within {timeout}s: {result.stderr.strip()}"
            if self._network == "nat":
                message += (
                    f"\nIf many VMs ran recently, the DHCP pool may be exhausted. "
                    f"{DHCP_LEASE_HINT} Stale leases expire on their own or can be cleared "
                    "from /var/db/dhcpd_leases."
                )
            raise RuntimeError(message)
        self.ip = str(ipaddress.ip_address(ip))
        return self.ip

    async def wait_stopped(self, timeout: float) -> None:
        """Wait for a guest-initiated shutdown, then fall back to ``tart stop`` / kill."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        with anyio.move_on_after(timeout):
            await process.wait()
        if process.returncode is None:
            await run_tart(["stop", "--timeout", "30", self.name], check=False, timeout=90)
            await self._stop_process()

    async def _stop_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGINT)
        with anyio.move_on_after(STOP_GRACE):
            await process.wait()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with anyio.move_on_after(10):
                await process.wait()

    def release(self) -> None:
        """Keep the VM on disk when destroyed (it became the image)."""
        self._cloned = False

    async def destroy(self) -> None:
        """Stop and delete the VM. Best effort, idempotent, shielded from cancellation."""
        if self.destroyed:
            return
        self.destroyed = True
        with anyio.CancelScope(shield=True):
            try:
                await self._stop_process()
                if self._cloned:
                    result = await run_tart(["delete", self.name], check=False, timeout=120)
                    if not result.success:
                        logger.warning(
                            "Failed to delete Tart VM %s: %s", self.name, result.stderr.strip()
                        )
            except Exception as ex:
                logger.warning("Error cleaning up Tart VM %s: %s", self.name, ex)
            finally:
                if self._log_dir is not None:
                    shutil.rmtree(self._log_dir, ignore_errors=True)


# --------------------------------------------------------------------------- image build


def _progress(message: str) -> None:
    print(f"[agenteval] {message}", file=sys.stderr, flush=True)


async def ensure_ssh_key(path: str | Path) -> Path:
    """Create an ed25519 key pair (0600, no passphrase) at ``path`` if missing."""
    key = Path(path).expanduser()
    pub = key.with_name(key.name + ".pub")
    key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not key.exists():
        keygen = shutil.which("ssh-keygen")
        if keygen is None:
            raise PrerequisiteError("ssh-keygen is not on PATH.")
        result = await subprocess(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", "agenteval", "-f", str(key)]
        )
        if not result.success:
            raise RuntimeError(f"ssh-keygen failed: {result.stderr.strip()}")
    key.chmod(0o600)
    if not pub.exists():
        keygen = shutil.which("ssh-keygen")
        if keygen is None:
            raise PrerequisiteError("ssh-keygen is not on PATH.")
        result = await subprocess([keygen, "-y", "-f", str(key)])
        if not result.success:
            raise RuntimeError(f"ssh-keygen -y failed: {result.stderr.strip()}")
        pub.write_text(result.stdout.strip() + "\n")
    return key


@contextlib.contextmanager
def askpass_script() -> Iterator[Path]:
    """A private SSH_ASKPASS helper that prints the password from ``ASKPASS_ENV``.

    The password itself is only ever in the environment of ssh and this helper,
    never on a command line or on disk.
    """
    directory = Path(tempfile.mkdtemp(prefix="agenteval-askpass-"))
    try:
        directory.chmod(0o700)
        script = directory / "askpass"
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"${ASKPASS_ENV}\"\n")
        script.chmod(0o700)
        yield script
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# Runs as ssh_user; the public key arrives on stdin (no interpolation).
_AUTHORIZE_KEY = (
    'umask 077; k=$(cat); mkdir -p "$HOME/.ssh" && touch "$HOME/.ssh/authorized_keys" && '
    'chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh/authorized_keys" && '
    '{ grep -qxF -- "$k" "$HOME/.ssh/authorized_keys" || '
    'printf \'%s\\n\' "$k" >> "$HOME/.ssh/authorized_keys"; }'
)

# Runs as root; $1 = bootstrap user. 00- sorts before cloud-init's 50- drop-in and
# sshd keeps the first value it reads.
_HARDEN = """set -e
install -d -m 0755 /etc/ssh/sshd_config.d
printf '%s\\n' '# agenteval: key-only SSH' 'PasswordAuthentication no' \
  'KbdInteractiveAuthentication no' 'PermitEmptyPasswords no' 'PermitRootLogin no' \
  > /etc/ssh/sshd_config.d/00-agenteval.conf
chmod 0644 /etc/ssh/sshd_config.d/00-agenteval.conf
sshd -t
passwd -l "$1"
"""

_UPLOAD = 'umask 077; f=$(mktemp /tmp/agenteval-provision.XXXXXX) && cat > "$f" && printf %s "$f"'
_PROVISION_PATH = re.compile(r"^/tmp/agenteval-provision\.[A-Za-z0-9]+$")


async def _authorize_key(target: SSHTarget, password: str, timeout: float) -> None:
    pub = target.key.with_name(target.key.name + ".pub").read_text().strip()
    argv = ssh_argv(
        target,
        _AUTHORIZE_KEY,
        ssh=ssh_binary(),
        options=ssh_options(None, password_auth=True),
    )
    deadline = anyio.current_time() + timeout
    last = ""
    with askpass_script() as askpass:
        env = {
            "SSH_ASKPASS": str(askpass),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
            ASKPASS_ENV: password,
        }
        while anyio.current_time() < deadline:
            try:
                result = await subprocess(argv, input=pub + "\n", env=env, timeout=60)
            except TimeoutError:
                last = "timed out"
                continue
            if result.success:
                return
            last = result.stderr.strip()
            await anyio.sleep(2)
    raise RuntimeError(
        f"Could not install the SSH key on {target.label} with password auth: {last}"
    )


async def _verify_password_rejected(target: SSHTarget, password: str) -> None:
    """One password login attempt after hardening; it must be refused."""
    argv = ssh_argv(target, "true", ssh=ssh_binary(), options=ssh_options(None, password_auth=True))
    with askpass_script() as askpass:
        env = {"SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force", ASKPASS_ENV: password}
        result = await subprocess(argv, env=env, timeout=60)
    if result.success:
        raise RuntimeError(f"Password SSH login still works on {target.label} after hardening")


async def _remote(target: SSHTarget, remote: str, what: str, input: bytes | None = None) -> str:
    result = await ssh_run(target, remote, timeout=300, input=input)
    if not result.success:
        raise RuntimeError(
            f"{what} failed on {target.label} (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout.decode(errors="replace")


async def _provision(
    target: SSHTarget, provision_script: Path, *, agent_user: str, workspace: str
) -> None:
    remote_path = (
        await _remote(
            target, _UPLOAD, "Uploading the provision script", input=provision_script.read_bytes()
        )
    ).strip()
    if not _PROVISION_PATH.match(remote_path):
        raise RuntimeError(f"unexpected remote script path: {remote_path!r}")
    run = shlex.join(
        [
            ssh.REMOTE_TOOLS.sudo, "-n", "-H", "--",
            ssh.REMOTE_TOOLS.env, "DEBIAN_FRONTEND=noninteractive",
            f"AGENTEVAL_AGENT_USER={agent_user}", f"AGENTEVAL_WORKSPACE={workspace}",
            ssh.REMOTE_TOOLS.bash, remote_path,
        ]
    ) + " </dev/null"  # fmt: skip
    # stdin is not the SSH channel: a daemon started by a package's postinst cannot
    # hold the session open (see ssh.EXEC_SCRIPT_NOINPUT).
    # Stream the script's output to the console; it can take several minutes.
    result = await subprocess(
        ssh_argv(target, run, ssh=ssh_binary()),
        capture_output=False,
        timeout=PROVISION_TIMEOUT,
        concurrency=False,
    )
    with contextlib.suppress(Exception):
        await ssh_run(target, shlex.join(["rm", "-f", "--", remote_path]), timeout=60)
    if not result.success:
        raise RuntimeError(f"Provision script failed with exit code {result.returncode}")


async def _poweroff(target: SSHTarget) -> None:
    remote = shlex.join([ssh.REMOTE_TOOLS.sudo, "-n", "/usr/bin/systemctl", "poweroff"])
    with contextlib.suppress(SandboxUnavailableError, TimeoutError):
        await ssh_run(target, remote, timeout=60)


async def build_image(
    settings: TartSettings,
    provision_script: Path,
    *,
    force: bool,
    agent_user: str = "agent",
    workspace: str = "/workspace",
) -> None:
    """Build ``settings.image`` from ``settings.base_image`` (no-op if it exists and not force).

    ``agent_user`` and ``workspace`` are the environment's; provision.sh creates them.
    The VM is built under a temporary name and renamed at the end, so a failed
    build never leaves a half-provisioned image behind.
    """
    tart_binary()
    provision_script = Path(provision_script)
    if not provision_script.is_file():
        raise FileNotFoundError(f"provision script not found: {provision_script}")
    exists = await image_exists(settings.image)
    if exists and not force:
        key_path = Path(settings.ssh_key).expanduser()
        if not key_path.is_file():
            # A new key would not be trusted by the existing image.
            raise RuntimeError(
                f"Tart image '{settings.image}' exists but its SSH key {key_path} is missing; "
                "the image only accepts the key it was built with. Rebuild with --force "
                "(creates a new key)."
            )
        _progress(f"Tart image '{settings.image}' already exists (use --force to rebuild).")
        return
    key = await ensure_ssh_key(settings.ssh_key)

    vm = TartVM(f"{VM_PREFIX}build-{secrets.token_hex(4)}")
    try:
        _progress(f"Cloning {settings.base_image} -> {vm.name} (pulls the image on first use)")
        await vm.clone(settings.base_image, timeout=None, capture=False)
        await vm.configure(cpu=settings.cpu, memory_mb=settings.memory_mb)
        _progress(f"Booting {vm.name}")
        await vm.start(network=settings.network)
        ip = await vm.wait_ip(settings.boot_timeout)
        target = SSHTarget(host=ip, user=settings.ssh_user, key=key)

        _progress(f"Installing SSH key {key} for {target.label}")
        await _authorize_key(target, settings.bootstrap_password, settings.boot_timeout)
        await wait_for_ssh(target, 60)
        await _remote(
            target, shlex.join([ssh.REMOTE_TOOLS.sudo, "-n", "true"]), "Passwordless sudo"
        )

        _progress(f"Running {provision_script.name}")
        await _provision(target, provision_script, agent_user=agent_user, workspace=workspace)

        _progress("Disabling password SSH and locking the bootstrap password")
        harden = shlex.join(
            [ssh.REMOTE_TOOLS.sudo, "-n", "--", "/bin/sh", "-c", _HARDEN, "sh", settings.ssh_user]
        )
        await _remote(target, harden, "SSH hardening")
        await wait_for_ssh(target, 60)  # key login still works under the new config
        await _verify_password_rejected(target, settings.bootstrap_password)

        _progress("Shutting down")
        await _poweroff(target)
        await vm.wait_stopped(SHUTDOWN_WAIT)

        if exists:
            await run_tart(["delete", settings.image])
        await run_tart(["rename", vm.name, settings.image])
        vm.release()
        _progress(f"Tart image '{settings.image}' is ready.")
    finally:
        await vm.destroy()
