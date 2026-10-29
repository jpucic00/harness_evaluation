"""Discovery: where the host CLI config and Claude memory live, and what is copied."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agenteval.dev.session import AgentName
from agenteval.dev.userconfig.common import LOCAL_FILE


def _home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("HOME") or Path.home())


def host_config_dir(agent: AgentName, override: Path | None, environ: Mapping[str, str]) -> Path:
    """The CLI's host config folder: ``override``, else where the CLI itself looks."""
    if override is not None:
        return override.expanduser().absolute()
    home = _home(environ)
    if agent == "claude_code":
        configured = environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured) if configured else home / ".claude"
    xdg = environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg and os.path.isabs(xdg) else home / ".config") / "opencode"


def _claude_global_json(
    host_dir: Path, home: Path, override: Path | None, environ: Mapping[str, str]
) -> Path:
    """Claude's global state file: in the config dir when one is configured."""
    if override is not None or environ.get("CLAUDE_CONFIG_DIR"):
        return host_dir / ".claude.json"
    return home / ".claude.json"


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
        if value == 0:
            return out


def claude_project_key(path: str) -> str:
    """Claude Code's ``projects/`` folder name for ``path`` (verified on 2.1.292).

    Every UTF-16 code unit that is not ASCII alphanumeric becomes ``-``; keys longer
    than 200 are cut to 200 and suffixed with the base-36 absolute Java string hash.
    """
    raw = path.encode("utf-16-le", "surrogatepass")
    units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
    key = "".join(chr(u) if chr(u).isascii() and chr(u).isalnum() else "-" for u in units)
    if len(key) <= 200:
        return key
    h = 0
    for unit in units:
        h = (h * 31 + unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return f"{key[:200]}-{_base36(abs(h))}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def canonical_root(root: Path, *, git: bool) -> Path:
    """The folder Claude keys a project by: the main worktree of a linked worktree,
    else the git toplevel, else ``root`` itself."""
    resolved = root.resolve()
    if not git:
        return resolved
    proc = _git(
        resolved, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"
    )
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != 2:
        return resolved
    common, top = Path(lines[0]), Path(lines[1])
    return common.parent if common.name == ".git" else top


@dataclass(frozen=True)
class ConfigPlan:
    """What ``stage`` copies in, resolved on the host before the env starts."""

    agent: AgentName
    host_dir: Path
    home: Path
    repo_root: Path
    host_root: Path
    """Canonical project root on the host (source of Claude's memory key)."""
    global_json: Path | None = None
    """Claude's host global state file (read for reports only, never copied)."""
    host_key: str | None = None
    vm_key: str | None = None
    local_file: Path | None = None
    """``CLAUDE.local.md`` at the repo root when git ignores it (or no git)."""


def _same_path(vm_root: str, repo_root: Path) -> bool:
    return vm_root in (str(repo_root), str(repo_root.resolve()))


def plan(
    agent: AgentName,
    *,
    override: Path | None,
    disabled: bool,
    repo_root: Path,
    git: bool,
    vm_root: str,
    environ: Mapping[str, str],
) -> ConfigPlan | None:
    """Resolve the host config to copy in (None: ``--no-host-config``).

    When the env sees the repo at its host path (live mode) the env computes the same
    canonical root as the host, so the VM key is the host key and ``CLAUDE.local.md``
    is already visible in the env (not staged).
    """
    if disabled:
        return None
    home = _home(environ)
    host_dir = host_config_dir(agent, override, environ)
    host_root = canonical_root(repo_root, git=git)
    common = ConfigPlan(
        agent=agent, host_dir=host_dir, home=home, repo_root=repo_root, host_root=host_root
    )
    if agent != "claude_code":
        return common
    live = _same_path(vm_root, repo_root)
    host_key = claude_project_key(str(host_root))
    local = repo_root / LOCAL_FILE
    stage_local = (
        not live
        and local.is_file()
        and (not git or _git(repo_root, "check-ignore", "-q", "--", LOCAL_FILE).returncode == 0)
    )
    return ConfigPlan(
        agent=agent,
        host_dir=host_dir,
        home=home,
        repo_root=repo_root,
        host_root=host_root,
        global_json=_claude_global_json(host_dir, home, override, environ),
        host_key=host_key,
        vm_key=host_key if live else claude_project_key(vm_root),
        local_file=local if stage_local else None,
    )
