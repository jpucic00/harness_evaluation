"""Inner sandbox wrapper (``constants.SANDBOX_EXEC``) rendered from a harness profile.

``sandbox-exec <cmd> [args...]`` is a POSIX sh script installed root-owned (0755)
in every environment. Agent CLIs and the containment probes are started through
it, so the same inner-sandbox profile applies to both:

- ``type: none``  -> ``exec "$@"``.
- ``type: bwrap`` -> bubblewrap: read-only ``/``, private ``/dev``, ``/proc``,
  ``/tmp`` and ``/run``, writable workspace + harness state dirs
  (+ ``extra_writable_paths``), ``hide_paths`` (plus ``SYSTEM_HIDE_PATHS``) replaced
  by an empty tmpfs (dirs) or ``/dev/null`` (files), all namespaces unshared and no
  nested user namespaces. With ``network: proxy-only`` the Inspect model-bridge port
  (parsed from ``OPENAI_BASE_URL``/``ANTHROPIC_BASE_URL``) is relayed in through a
  read-only-mounted unix socket dir; nothing else is reachable. The script fails
  closed: when bwrap cannot set up the sandbox the command is not run.

A network namespace does not isolate filesystem unix sockets, which is why ``/run``
(host daemons: D-Bus, systemd, resolved) and Inspect's sandbox-tools dir (its server
runs commands as root) are hidden, along with agenteval's root-only evidence dir.
With ``network: host`` the resolver file that ``/etc/resolv.conf`` points to below
``/run`` is re-bound read-only so DNS works.

Ordering inside the bwrap profile: hide paths are applied first, then the writable
and config binds (so an explicitly writable path below a hidden one stays usable),
then hide paths that lie below a writable bind are applied again.

Users: a non-root user gets a user namespace (needs unprivileged userns: on Ubuntu
24.04 the ``agenteval-bwrap`` AppArmor profile, and membership in
``constants.BWRAP_GROUP``, which ``install_inner_sandbox`` grants; in Docker
``seccomp=unconfined``, ``apparmor=unconfined`` and ``systempaths=unconfined`` so a
fresh /proc can be mounted). This is the supported setup.

Root is a weaker boundary and the script warns about it. The command keeps real uid 0
(a user namespace maps root to root), so any root-owned IPC endpoint (unix socket,
service) still visible in the read-only filesystem view can be used to act outside
the sandbox; hiding ``/run`` and the sandbox-tools dir removes the known ones, not
all. Root with CAP_SYS_ADMIN+CAP_NET_ADMIN (a VM) runs without a user namespace,
keeps only ``ROOT_CAPS`` and cannot use ``--disable-userns``. Root without them (a
container) gets a user namespace with all capabilities dropped; there only
root-owned/world-accessible files are usable, e.g. an agent-owned workspace is
read-only for it.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping
from importlib.resources import files

from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import SandboxEnvironment, sandbox

from agenteval import constants
from agenteval.config import EnvironmentProfile, InnerSandbox, MatrixCell

ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TZ",
    "IS_SANDBOX",  # Claude Code: allows unattended permission bypass
)
"""Variables passed into a ``clear_env`` sandbox (plus ``env_passthrough``)."""

ENV_PREFIXES: tuple[str, ...] = (
    "OPENCODE_",
    "ANTHROPIC_",
    "OPENAI_",
    "GOOGLE_",
    "OPENROUTER_",
    "CLAUDE_",
    "MCP_",
)
"""Variables with these prefixes are passed into a ``clear_env`` sandbox."""

STATE_DIRS: tuple[str, ...] = (
    ".local/share/opencode",
    ".local/state/opencode",
    ".cache/opencode",
    ".config/opencode",  # opencode writes a .gitignore here on start
    ".claude",  # Claude Code (CLAUDE_CONFIG_DIR, set by its wrapper)
)
"""Writable harness state dirs, relative to the home of the user running the command."""

CONFIG_DIRS: tuple[str, ...] = ()
"""Harness config dirs, re-exposed read-only (they may sit below a hidden path)."""

PROXY_DIR = "/tmp/.agenteval-proxy"
"""Mount point of the model-proxy socket dir inside the sandbox."""

ROOT_CAPS: dict[str, int] = {
    "CAP_CHOWN": 0,
    "CAP_DAC_OVERRIDE": 1,
    "CAP_FOWNER": 3,
    "CAP_FSETID": 4,
    "CAP_KILL": 5,
    "CAP_SETGID": 6,
    "CAP_SETUID": 7,
    "CAP_NET_BIND_SERVICE": 10,
}
"""Capabilities kept when root runs the sandbox without a user namespace (file access
like root outside; none of them can undo the mounts). They do not stop root from
driving root-owned IPC endpoints still visible inside the sandbox, which then act
outside it: root runs are a weaker boundary (see the module docstring)."""

SYSTEM_HIDE_PATHS: tuple[str, ...] = (constants.SANDBOX_TOOLS_DIR, constants.STATE_DIR)
"""Always hidden in a bwrap sandbox, after the profile's ``hide_paths``: Inspect's
sandbox-tools dir (its root server's socket runs commands outside the sandbox) and
agenteval's root-only evidence dir (baseline, collector log)."""

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER = re.compile(r"@@([A-Z_]+)@@")


def _template(name: str) -> str:
    return (files("agenteval.harnesses") / "templates" / name).read_text(encoding="utf-8")


def render_template(name: str, values: Mapping[str, str]) -> str:
    """Substitute ``@@KEY@@`` placeholders in one pass (values are not re-scanned)."""

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"template {name}: no value for placeholder @@{key}@@")
        return values[key]

    return _PLACEHOLDER.sub(sub, _template(name))


def _check_path(path: str, what: str, *, allow_home: bool) -> str:
    if not path or "\n" in path or "\0" in path:
        raise ValueError(f"inner_sandbox: invalid {what} {path!r}")
    if path.startswith("/"):
        return path
    if allow_home and (path == "~" or path.startswith("~/")):
        return path
    raise ValueError(
        f"inner_sandbox: {what} {path!r} must be absolute"
        + (" or start with '~/'" if allow_home else "")
    )


def env_allowlist(inner: InnerSandbox) -> list[str]:
    """Exact variable names passed into a ``clear_env`` sandbox."""
    names = list(ENV_ALLOWLIST)
    for name in inner.env_passthrough:
        if not _ENV_NAME.fullmatch(name) or name.startswith("sx_"):
            raise ValueError(f"inner_sandbox: invalid env_passthrough name {name!r}")
        if name not in names:
            names.append(name)
    return names


def _lines(values: Iterable[str]) -> str:
    return shlex.quote("\n".join(values))


def render_sandbox_exec(
    inner: InnerSandbox, *, agent_home: str, workspace: str, quiet_relays: bool = False
) -> str:
    """Render the ``sandbox-exec`` script for an inner-sandbox profile.

    ``quiet_relays`` drops the model-proxy relays' stderr (per-connection socat errors),
    which would otherwise print into an interactive CLI (``agenteval dev``).
    """
    profile = json.dumps(inner.model_dump(), sort_keys=True)
    if inner.type == "none":
        return render_template("sandbox-exec-none.sh", {"PROFILE": profile})

    agent_home = _check_path(agent_home, "agent home", allow_home=False)
    workspace = _check_path(workspace, "workspace", allow_home=False)
    hide = [_check_path(p, "hide path", allow_home=True) for p in inner.hide_paths]
    hide += [p for p in SYSTEM_HIDE_PATHS if p not in hide]
    writable = [
        _check_path(p, "writable path", allow_home=True) for p in inner.extra_writable_paths
    ]
    return render_template(
        "sandbox-exec-bwrap.sh",
        {
            "PROFILE": profile,
            "NETWORK": shlex.quote(inner.network),
            "CLEAR_ENV": "1" if inner.clear_env else "0",
            "AGENT_HOME": shlex.quote(agent_home),
            "WORKSPACE": shlex.quote(workspace),
            "HIDE_PATHS": _lines(hide),
            "WRITABLE_PATHS": _lines(writable),
            "STATE_DIRS": _lines(STATE_DIRS),
            "CONFIG_DIRS": _lines(CONFIG_DIRS),
            "ENV_NAMES": _lines(env_allowlist(inner)),
            "ENV_PREFIXES": _lines(ENV_PREFIXES),
            "PROXY_DIR": shlex.quote(PROXY_DIR),
            "ROOT_CAPS": _lines(f"{name}:{bit}" for name, bit in ROOT_CAPS.items()),
            "BWRAP_GROUP": shlex.quote(constants.BWRAP_GROUP),
            "RELAY_STDERR": " 2>/dev/null" if quiet_relays else "",
        },
    )


async def user_home(sbox: SandboxEnvironment, user: str) -> str:
    """Home directory of ``user`` in the environment (passwd entry)."""
    result = await sbox.exec(
        ["sh", "-c", 'getent passwd "$1" | cut -d: -f6', "sh", user], user="root"
    )
    home = result.stdout.strip() if result.success else ""
    if home.startswith("/"):
        return home
    return "/root" if user == "root" else f"/home/{user}"


async def install_root_file(
    sbox: SandboxEnvironment, path: str, content: str, mode: str = "0755"
) -> None:
    """Atomically install ``content`` at ``path``, owned by root with ``mode``."""
    parent = path.rsplit("/", 1)[0] or "/"
    tmp = f"{path}.agenteval-tmp"
    mkdir = await sbox.exec(["mkdir", "-p", parent], user="root")
    if not mkdir.success:
        raise RuntimeError(f"cannot create {parent}: {mkdir.stderr.strip()}")
    await sbox.write_file(tmp, content)
    result = await sbox.exec(
        [
            "sh",
            "-c",
            'chown root:root "$1" && chmod "$2" "$1" && mv -f "$1" "$3"',
            "sh",
            tmp,
            mode,
            path,
        ],
        user="root",
    )
    if not result.success:
        raise RuntimeError(f"cannot install {path}: {result.stderr.strip()}")


async def grant_bwrap(sbox: SandboxEnvironment, user: str) -> None:
    """Let ``user`` run bubblewrap: add it to ``constants.BWRAP_GROUP`` (created if missing).

    provision.sh makes ``/usr/bin/bwrap`` executable only by root and that group, so
    users of ``type: none`` cells keep the distro's user-namespace restriction. Root
    owns bwrap and is never added.
    """
    if user == "root":
        return
    result = await sbox.exec(
        [
            "sh",
            "-c",
            'groupadd -f "$1" && { usermod -aG "$1" "$2" || gpasswd -a "$2" "$1" >/dev/null; }',
            "sh",
            constants.BWRAP_GROUP,
            user,
        ],
        user="root",
    )
    if not result.success:
        raise RuntimeError(
            f"cannot add user '{user}' to group '{constants.BWRAP_GROUP}': {result.stderr.strip()}"
        )


@solver
def install_inner_sandbox(cell: MatrixCell, *, verify_user: str | None = None) -> Solver:
    """Write ``constants.SANDBOX_EXEC`` for the cell's harness profile (idempotent).

    For ``type: bwrap`` the run user (``verify_user``, else the environment's
    ``agent_user``) is also added to ``constants.BWRAP_GROUP`` (see ``grant_bwrap``);
    for ``type: none`` group membership is left alone.

    Args:
        cell: Matrix cell (harness ``inner_sandbox`` + environment user/workspace).
        verify_user: When set, run ``sandbox-exec true`` as this user and raise if the
            inner sandbox cannot start (clear error instead of a failing agent run).
    """
    inner = cell.harness.inner_sandbox
    environment = cell.environment

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await install_sandbox_exec(sandbox(), inner, environment, verify_user=verify_user)
        return state

    return solve


async def install_sandbox_exec(
    sbox: SandboxEnvironment,
    inner: InnerSandbox,
    environment: EnvironmentProfile,
    *,
    verify_user: str | None = None,
    quiet_relays: bool = False,
) -> None:
    """Install ``constants.SANDBOX_EXEC`` (see ``install_inner_sandbox``)."""
    run_user = verify_user or environment.agent_user
    home = await user_home(sbox, environment.agent_user)
    script = render_sandbox_exec(
        inner, agent_home=home, workspace=environment.workspace, quiet_relays=quiet_relays
    )
    await install_root_file(sbox, constants.SANDBOX_EXEC, script, "0755")
    if inner.type == "bwrap":
        await grant_bwrap(sbox, run_user)
    if verify_user is not None:
        check = await sbox.exec(
            [constants.SANDBOX_EXEC, "true"], user=verify_user, cwd=environment.workspace
        )
        if not check.success:
            raise RuntimeError(
                f"inner sandbox '{inner.type}' failed to start as user '{verify_user}' "
                f"(exit {check.returncode}): {check.stderr.strip()}"
            )
