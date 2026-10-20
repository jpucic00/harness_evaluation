"""Host CLI config and Claude memory for dev sessions.

Copy-in (``stage``): an allow-listed, sanitized copy of the user's Claude Code
(``~/.claude``) or OpenCode (``~/.config/opencode``) config, plus this repo's Claude
memory, goes into the env as ``<session>/config.tar``:

- ``home/<path>``: relative to the agent's HOME (``.claude/settings.json``,
  ``.claude/projects/<vm key>/memory/...``, ``.config/opencode/opencode.json``);
- ``workspace/CLAUDE.local.md``: relative to the repo root in the env.

Members are regular files owned by 0:0 (mode 0644/0755); the env extracts them with
``--no-same-owner`` as the agent. Each file also lands in
``<session>/config/base/<logical name>`` (``claude/settings.json``, ``memory/x.md``,
``opencode/AGENTS.md``, ``local/CLAUDE.local.md``), the merge base for the return.
Credentials, ``~/.claude.json``, other projects, transcripts, history, plugins and
the OpenCode data dir are never read into the session.

Return: the env archives the ``export_paths`` that exist in the same layout,
``extract_returned`` unpacks the allow-listed members into
``<session>/config/vm/<logical name>`` and ``config_items`` turns each difference
into a ``ReviewItem``, merged 3-way against the host file at review time. Nothing
is written to the host before the user approves an item; memory goes back under the
host's project key and the host config dir is created on the first write.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import unquote

from agenteval.dev.review import GROUP_ORDER, Group, ItemKind, ReviewItem
from agenteval.dev.session import AgentName, ConfigState, DevSession

MAX_FILE_BYTES: Final = 2 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 50 * 1024 * 1024
MAX_JSON_DEPTH: Final = 64
"""Deeper JSON from the env is treated as invalid (the merge code recurses)."""
LOCAL_FILE: Final = "CLAUDE.local.md"
SKIPPED_FILE: Final = "skipped.json"
"""``<session>/config/skipped.json``: returned files left out for size."""

CLAUDE_FILES: Final = ("settings.json", "CLAUDE.md")
CLAUDE_DIRS: Final = ("commands", "agents", "skills", "output-styles")
OPENCODE_FILES: Final = ("opencode.json", "opencode.jsonc", "AGENTS.md")
OPENCODE_DIRS: Final = ("agent", "agents", "command", "commands")
_SKIP_NAMES = frozenset({".credentials.json", ".claude.json", ".DS_Store"})
_AREA_HOME = {"claude": ".claude", "opencode": ".config/opencode"}
"""Logical area -> config dir in the env, relative to the agent's HOME."""

HELPER_KEYS: Final = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "gcpAuthRefresh",
    "otelHeadersHelper",
    "proxyAuthHelper",
)
PROTECTED_KEYS: Final = (
    *HELPER_KEYS,
    "model",
    "skipDangerousModePermissionPrompt",
    "autoMemoryDirectory",
)
"""settings.json keys the env can never change on the host."""
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW(?:OR)?D|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
_ROUTING_ENV = re.compile(
    r"^(?:ANTHROPIC_|CLAUDE_CODE_USE_|AWS_|GOOGLE_|VERTEX_|CLOUD_ML_)"
    r"|^(?:HTTPS?_PROXY|ALL_PROXY|NO_PROXY)$",
    re.I,
)

CLAUDE_WATCHED: Final = (
    "permissions",
    "hooks",
    "env",
    "statusLine",
    "enabledMcpjsonServers",
    "enableAllProjectMcpServers",
    "mcpServers",
    "sandbox",
    "enabledPlugins",
    "extraKnownMarketplaces",
    "outputStyle",
)
OPENCODE_WATCHED: Final = ("permission", "mcp", "plugin", "provider", "tools", "instructions")

_HOST_PATH = re.compile(r"/Users/|/opt/homebrew|/Applications\b|~/Library|/Volumes/")
_REFERENCE = re.compile(r"\{(?:env|file):[^}]*\}")
_FILE_REFERENCE = re.compile(r"\{file:\s*([~/][^}]*)\}")
_ANY_FILE_REFERENCE = re.compile(r"\{file:([^}]*)\}")
# Control characters, bidi controls and the code points HFS+ ignores in names.
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f؜​-‏‪-‮⁠-⁯﻿]")
_IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_TRAILING_CLOSE = re.compile(r"\s*[}\]]")
_TOOLS_KEY = re.compile(r"^\s*(?:allowed[-_]tools|tools)\s*:(.*)$", re.I)

_TEXT_FLAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "install command",
        re.compile(
            r"\b(?:npm|pnpm|yarn|pip3?|pipx|uv(?:\s+(?:pip|tool))?|brew|apt(?:-get)?|cargo|go)"
            r"\s+(?:install|add|i)\b",
            re.I,
        ),
    ),
    (
        "download piped to a shell",
        # Anchored on the first curl/wget (atomic) so long lines stay linear.
        re.compile(r"^(?>.*?\b(?:curl|wget)\b).*\|\s*(?:sudo\s+)?\w*sh\b", re.I),
    ),
    ("sudo", re.compile(r"\bsudo\b", re.I)),
    (
        "chmod +x",
        re.compile(
            r"\bchmod\s+(?:-\w+\s+)*(?:[ugoa]*\+[rws]*x|(?=[0-7]{3,4}\b)[0-7]*[1357])", re.I
        ),
    ),
    ("shell code block", re.compile(r"^\s*(?:```|~~~)\s*(?:bash|sh|zsh|shell|console)\b", re.I)),
    ("URL", re.compile(r"\bhttps?://", re.I)),
    ("@-import of an absolute or home path", re.compile(r"(?:^|\s)@(?:~|\.\.)?/")),
    (
        "tells the agent not to ask",
        re.compile(r"\b(?:don['’]?t|do\s+not|never)\s+(?:ask|confirm|prompt)", re.I),
    ),
    (
        "skips confirmation",
        re.compile(r"\bskip\w*\b.{0,40}?\b(?:confirmation|permission|approval)", re.I),
    ),
    (
        "ignore previous instructions",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules)", re.I
        ),
    ),
    ("without asking", re.compile(r"\bwithout\s+(?:asking|confirmation|permission)", re.I)),
    ("auto-approve", re.compile(r"\bauto[-\s]?approve", re.I)),
    ("always allow/approve/run", re.compile(r"\balways\s+(?:allow|approve|run)", re.I)),
)

_MISSING: Final = object()

JsonObject = dict[str, Any]


# --- discovery --------------------------------------------------------------------


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


# --- sanitizing ----------------------------------------------------------------------


def _sensitive_env(name: str) -> bool:
    return bool(_SECRET_ENV.search(name) or _ROUTING_ENV.search(name))


def sanitize_claude_settings(data: Mapping[str, Any]) -> tuple[JsonObject, list[str]]:
    """Drop credential helpers, ``autoMemoryDirectory`` and secret or model-routing
    ``env`` entries; returns the clean copy and what was removed (names only)."""
    clean: JsonObject = copy.deepcopy(dict(data))
    removed: list[str] = []
    for key in HELPER_KEYS:
        if key in clean:
            del clean[key]
            removed.append(f"{key} not copied (host credential helper)")
    if "autoMemoryDirectory" in clean:
        del clean["autoMemoryDirectory"]
        removed.append("autoMemoryDirectory is set: memory not synced")
    env = clean.get("env")
    if isinstance(env, dict):
        for name in [n for n in env if _sensitive_env(str(n))]:
            del env[name]
            removed.append(f"env.{name} not copied (secret or model routing)")
    return clean, removed


def _strip_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_string = char != '"'
        elif char == '"':
            in_string = True
            out.append(char)
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    in_string = escaped = False
    for i, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "," and _TRAILING_CLOSE.match(text, i + 1):
            continue
        out.append(char)
    return "".join(out)


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def _loads(text: str) -> object:
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise ValueError("JSON nested too deeply") from exc


def _too_deep(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth >= MAX_JSON_DEPTH:
                return True
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, depth + 1) for child in children)
    return False


def _parse_jsonc(text: str) -> tuple[JsonObject, bool]:
    """Parse JSON, falling back to JSONC (comments, trailing commas). Returns
    ``(data, had_comments)``; raises ValueError when it is neither, holds NaN or
    Infinity, or is nested deeper than ``MAX_JSON_DEPTH``."""
    stripped = False
    try:
        data = _loads(text)
    except ValueError:
        data = _loads(_strip_trailing_commas(_strip_comments(text)))
        stripped = True
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    if _too_deep(data):
        raise ValueError(f"JSON nested deeper than {MAX_JSON_DEPTH} levels")
    return data, stripped


def _literal(value: object) -> bool:
    """A configured secret value that is not an ``{env:...}``/``{file:...}`` reference."""
    if isinstance(value, str):
        return bool(value.strip()) and not _REFERENCE.search(value)
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, dict):
        return any(_literal(v) for v in value.values())
    if isinstance(value, list):
        return any(_literal(v) for v in value)
    return True


def _secret_slots(data: Mapping[str, Any]) -> Iterator[tuple[JsonObject, tuple[str, ...]]]:
    """``(parent, path)`` of each OpenCode setting that may hold a credential:
    ``provider.*.options`` headers and secret-named keys (``apiKey``,
    ``secretAccessKey``...), ``mcp.*.headers`` and secret-named ``mcp.*.environment``."""
    providers = data.get("provider")
    for pid, provider in providers.items() if isinstance(providers, dict) else ():
        options = provider.get("options") if isinstance(provider, dict) else None
        if isinstance(options, dict):
            for key in list(options):
                if key == "headers" or _SECRET_ENV.search(str(key)):
                    yield options, ("provider", str(pid), "options", str(key))
    servers = data.get("mcp")
    for name, server in servers.items() if isinstance(servers, dict) else ():
        if not isinstance(server, dict):
            continue
        if "headers" in server:
            yield server, ("mcp", str(name), "headers")
        env = server.get("environment")
        if isinstance(env, dict):
            for key in list(env):
                if _sensitive_env(str(key)):
                    yield env, ("mcp", str(name), "environment", str(key))


def _strip_secrets(data: JsonObject) -> list[str]:
    """Remove literal credential values (see ``_secret_slots``) in place; header
    maps keep their ``{env:...}``/``{file:...}`` entries."""
    removed: list[str] = []
    for parent, path in list(_secret_slots(data)):
        key, dotted = path[-1], ".".join(path)
        value = parent[key]
        if key == "headers" and isinstance(value, dict):
            for name in [h for h, v in value.items() if _literal(v)]:
                del value[name]
                removed.append(f"{dotted}.{name} stripped (literal value)")
            if not value:
                del parent[key]
        elif _literal(value):
            del parent[key]
            removed.append(f"{dotted} stripped (literal value)")
    return removed


def sanitize_opencode(text: str) -> tuple[str, list[str]]:
    """Strip literal credentials (``provider.*.options.apiKey``/``headers`` and other
    secret-named options, MCP ``headers``/secret ``environment``) from an OpenCode
    config. The text is returned verbatim when there are none; otherwise it is
    re-serialized as JSON (comments are lost). Raises ValueError on invalid JSON(C)."""
    data, had_comments = _parse_jsonc(text)
    removed = _strip_secrets(data)
    if not removed:
        return text, []
    if had_comments:
        removed.append("comments dropped")
    return _dump_json(data).decode(), removed


# --- reports -----------------------------------------------------------------------


def _strings_under(value: object, key: str) -> Iterator[str]:
    """Every string stored under ``key`` anywhere in a JSON value."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and isinstance(v, str):
                yield v
            elif k == key and isinstance(v, list):
                yield " ".join(str(x) for x in v)
            else:
                yield from _strings_under(v, key)
    elif isinstance(value, list):
        for v in value:
            yield from _strings_under(v, key)


def _all_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _all_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _all_strings(v)


def _names(value: object) -> list[str]:
    return sorted(str(k) for k in value) if isinstance(value, dict) else []


def _display(path: Path, home: Path) -> str:
    text, prefix = str(path), str(home)
    if text == prefix or text.startswith(prefix + os.sep):
        return "~" + text[len(prefix) :]
    return text


def _import_targets(text: str) -> Iterator[str]:
    """Path-like ``@`` imports outside code blocks and code spans."""
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in _IMPORT.finditer(re.sub(r"`[^`]*`", "", line)):
            target = match.group(1).replace("\\ ", " ").rstrip(".,;:)")
            if "/" in target or "." in target:
                yield target


def _broken_imports(source: Path, plan: ConfigPlan, *, in_repo: bool) -> list[str]:
    """``@`` imports of files that exist on the host but not in the env."""
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    broken: list[str] = []
    for target in _import_targets(text):
        if target.startswith("~/"):
            host_path = plan.home / target[2:]
            parts = PurePosixPath(target[2:]).parts
            usable = len(parts) > 2 and parts[0] == ".claude" and parts[1] in CLAUDE_DIRS
        elif target.startswith("/"):
            host_path, usable = Path(target), False
        else:
            host_path = source.parent / target
            first = PurePosixPath(target).parts[0]
            usable = (in_repo and first != "..") or first in CLAUDE_DIRS
        if not usable and host_path.exists() and target not in broken:
            broken.append(target)
    return broken


def unusable_reports(
    plan: ConfigPlan,
    settings: Mapping[str, Any] | None,
    host_global: Mapping[str, Any] | None,
) -> list[str]:
    """Host config that cannot work in the env (reported, never a failure).

    ``settings`` is the host ``settings.json`` (Claude) or OpenCode config;
    ``host_global`` the parsed Claude global state file.
    """
    reports: list[str] = []
    if plan.agent != "claude_code":
        if settings:
            servers = settings.get("mcp")
            for name, server in servers.items() if isinstance(servers, dict) else ():
                if isinstance(server, dict) and server.get("type") == "local":
                    if any(_HOST_PATH.search(s) for s in _strings_under(server, "command")):
                        reports.append(f"opencode config: local MCP server {name} runs a host path")
            plugins = settings.get("plugin")
            if isinstance(plugins, list) and plugins:
                listed = ", ".join(str(p) for p in plugins)
                reports.append(f"opencode config: plugins may not load in the env: {listed}")
            files = sorted({m for s in _all_strings(settings) for m in _FILE_REFERENCE.findall(s)})
            if files:
                reports.append(
                    "opencode config: {file:...} references to host files: " + ", ".join(files)
                )
        return reports
    if settings:
        hooks = settings.get("hooks")
        if isinstance(hooks, dict):
            for event, groups in hooks.items():
                if any(_HOST_PATH.search(c) for c in _strings_under(groups, "command")):
                    reports.append(f"settings.json: a {event} hook runs a host path")
        if any(_HOST_PATH.search(c) for c in _strings_under(settings.get("statusLine"), "command")):
            reports.append("settings.json: statusLine runs a host path")
        plugins = settings.get("enabledPlugins")
        if isinstance(plugins, dict) and plugins:
            names = ", ".join(_names(plugins))
            reports.append(f"settings.json: plugins are not copied ({names})")
    if host_global:
        source = _display(plan.global_json, plan.home) if plan.global_json else ".claude.json"
        user = _names(host_global.get("mcpServers"))
        local: list[str] = []
        projects = host_global.get("projects")
        if isinstance(projects, dict):
            for root in dict.fromkeys((str(plan.repo_root), str(plan.host_root))):
                project = projects.get(root)
                if isinstance(project, dict):
                    local += [n for n in _names(project.get("mcpServers")) if n not in local]
        if user:
            reports.append(
                f"MCP servers in {source} are not available (user scope): " + ", ".join(user)
            )
        if local:
            reports.append(
                f"MCP servers in {source} are not available (local scope): " + ", ".join(local)
            )
    claude_md = plan.host_dir / "CLAUDE.md"
    broken = _broken_imports(claude_md, plan, in_repo=False) if claude_md.is_file() else []
    if broken:
        reports.append("CLAUDE.md: @-imports of host files: " + ", ".join(broken))
    if plan.local_file is not None:
        broken = _broken_imports(plan.local_file, plan, in_repo=True)
        if broken:
            reports.append(f"{LOCAL_FILE}: @-imports of host files: " + ", ".join(broken))
    return reports


def _read_json_object(path: Path | None) -> JsonObject | None:
    if path is None:
        return None
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claude_global_seed(*, version: str, trusted: Sequence[str], theme: str | None) -> JsonObject:
    """``~/.claude/.claude.json`` for the env: onboarding done, ``trusted`` folders trusted."""
    seed: JsonObject = {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": version,
        "hasAcknowledgedCostThreshold": True,
        "projects": {
            path: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
            for path in trusted
        },
    }
    if theme:
        seed["theme"] = theme
    return seed


def host_theme(environ: Mapping[str, str], override: Path | None = None) -> str | None:
    """The ``theme`` from Claude's host global state file (nothing else is used)."""
    home = _home(environ)
    host_dir = host_config_dir("claude_code", override, environ)
    data = _read_json_object(_claude_global_json(host_dir, home, override, environ))
    theme = data.get("theme") if data else None
    if isinstance(theme, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", theme):
        return theme
    return None


# --- staging -----------------------------------------------------------------------


def _keep(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name not in _SKIP_NAMES and not name.endswith(".jsonl")


def _walk(root: Path) -> Iterator[tuple[str, Path]]:
    """Regular files under ``root`` following symlinks (loop-safe), as (posix rel, path)."""
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if path.is_file():
                yield path.relative_to(root).as_posix(), path


@dataclass
class _Staging:
    files: dict[str, tuple[bytes, bool]] = field(default_factory=dict)
    reports: list[str] = field(default_factory=list)
    total: int = 0

    def add(self, logical: str, data: bytes, executable: bool = False) -> None:
        if len(data) > MAX_FILE_BYTES:
            self.reports.append(f"{logical}: larger than {MAX_FILE_BYTES >> 20} MB, not copied")
        elif self.total + len(data) > MAX_TOTAL_BYTES:
            self.reports.append(f"{logical}: config copy limit reached, not copied")
        else:
            self.files[logical] = (data, executable)
            self.total += len(data)

    def add_file(self, logical: str, path: Path) -> None:
        try:
            info = path.stat()
            # Checked before reading: a symlinked tree may point at huge files.
            data = b"" if info.st_size > MAX_FILE_BYTES else path.read_bytes()
        except OSError as exc:
            self.reports.append(f"{logical}: not readable ({exc.strerror}), not copied")
            return
        if info.st_size > MAX_FILE_BYTES:
            self.reports.append(f"{logical}: larger than {MAX_FILE_BYTES >> 20} MB, not copied")
            return
        self.add(logical, data, bool(info.st_mode & 0o111))

    def add_tree(self, root: Path, prefix: str) -> None:
        for rel, path in _walk(root):
            if _keep(rel):
                self.add_file(f"{prefix}/{rel}", path)


def _stage_claude(plan: ConfigPlan, out: _Staging) -> bool:
    """Stage Claude's config; returns whether memory is synced."""
    memory = plan.host_key is not None
    settings: JsonObject | None = None
    path = plan.host_dir / "settings.json"
    if path.is_file():
        settings = _read_json_object(path)
        if settings is None:
            out.reports.append("settings.json: not a valid JSON object, not copied")
        else:
            clean, removed = sanitize_claude_settings(settings)
            out.reports.extend(f"settings.json: {r}" for r in removed)
            memory = memory and "autoMemoryDirectory" not in settings
            out.add("claude/settings.json", _dump_json(clean))
    if (plan.host_dir / "CLAUDE.md").is_file():
        out.add_file("claude/CLAUDE.md", plan.host_dir / "CLAUDE.md")
    if memory and plan.host_key:
        out.add_tree(plan.host_dir / "projects" / plan.host_key / "memory", "memory")
    for name in CLAUDE_DIRS:
        out.add_tree(plan.host_dir / name, f"claude/{name}")
    out.reports.extend(unusable_reports(plan, settings, _read_json_object(plan.global_json)))
    return memory


def _stage_opencode(plan: ConfigPlan, out: _Staging) -> None:
    for name in ("opencode.json", "opencode.jsonc"):
        path = plan.host_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            clean, removed = sanitize_opencode(text)
            data, _ = _parse_jsonc(text)
        except (OSError, ValueError):
            out.reports.append(f"{name}: not valid JSON, not copied")
            continue
        out.reports.extend(f"{name}: {r}" for r in removed)
        out.reports.extend(r for r in unusable_reports(plan, data, None) if r not in out.reports)
        out.add(f"opencode/{name}", clean.encode())
    if (plan.host_dir / "AGENTS.md").is_file():
        out.add_file("opencode/AGENTS.md", plan.host_dir / "AGENTS.md")
    for name in OPENCODE_DIRS:
        out.add_tree(plan.host_dir / name, f"opencode/{name}")


def _member_name(logical: str, vm_key: str | None) -> str:
    area, _, rest = logical.partition("/")
    if area == "memory":
        return f"home/.claude/projects/{vm_key}/memory/{rest}"
    if area == "local":
        return f"workspace/{rest}"
    return f"home/{_AREA_HOME[area]}/{rest}"


@dataclass(frozen=True)
class StageResult:
    tar: Path | None
    """``<session>/config.tar`` (None: nothing to copy in)."""
    state: ConfigState


def stage(plan: ConfigPlan, session_dir: Path) -> StageResult:
    """Copy the sanitized host config into ``<session>/config/base`` and ``config.tar``.

    A missing host config dir stages nothing; it is created when the first returned
    item is applied.
    """
    out = _Staging()
    memory = False
    if plan.agent == "claude_code":
        memory = _stage_claude(plan, out)
    else:
        _stage_opencode(plan, out)
    if plan.local_file is not None:
        out.add_file(f"local/{LOCAL_FILE}", plan.local_file)
    host_key, vm_key = (plan.host_key, plan.vm_key) if memory else (None, None)

    base = session_dir / "config" / "base"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    for logical, (data, executable) in out.files.items():
        target = base / logical
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if executable else 0o644)

    tar_path = session_dir / "config.tar"
    tar_path.unlink(missing_ok=True)
    if out.files:
        now = int(time.time())
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
            for logical in sorted(out.files):
                data, executable = out.files[logical]
                info = tarfile.TarInfo(_member_name(logical, vm_key))
                info.size = len(data)
                info.mode = 0o755 if executable else 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = now
                with (base / logical).open("rb") as source:
                    archive.addfile(info, source)

    local = f"local/{LOCAL_FILE}"
    state = ConfigState(
        agent=plan.agent,
        host_dir=str(plan.host_dir),
        host_key=host_key,
        vm_key=vm_key,
        local_file=str(plan.local_file) if local in out.files else None,
        staged=sorted(out.files),
        reports=out.reports,
    )
    return StageResult(tar=tar_path if out.files else None, state=state)


# --- return ------------------------------------------------------------------------


def export_paths(state: ConfigState) -> tuple[list[str], list[str]]:
    """Paths the env archives back: (relative to the agent's HOME, relative to the
    repo root in the env). Directories are listed whole so new files come back; the
    env archives the ones that exist as ``home/<path>`` and ``workspace/<path>``
    members (an empty archive means everything was deleted)."""
    if state.agent == "claude_code":
        home = [f".claude/{name}" for name in (*CLAUDE_FILES, *CLAUDE_DIRS)]
        if state.vm_key:
            home.append(f".claude/projects/{state.vm_key}/memory")
    else:
        home = [f".config/opencode/{name}" for name in (*OPENCODE_FILES, *OPENCODE_DIRS)]
    return home, [LOCAL_FILE] if state.local_file else []


def _valid_rel(rel: str) -> bool:
    parts = rel.split("/")
    return bool(rel) and not _BAD_NAME.search(rel) and all(p not in ("", ".", "..") for p in parts)


def _allowed(rest: str, files: Collection[str], dirs: Collection[str]) -> bool:
    parts = rest.split("/")
    allowed = parts[0] in files if len(parts) == 1 else parts[0] in dirs
    return allowed and _valid_rel(rest) and _keep(rest)


def _logical_name(member: str, state: ConfigState) -> str | None:
    """Map an archive member back to its logical name (None: not on the allow-list)."""
    name = member
    while name.startswith("./"):
        name = name[2:]
    if not _valid_rel(name):
        return None
    if state.local_file and name == f"workspace/{LOCAL_FILE}":
        return f"local/{LOCAL_FILE}"
    if state.agent == "claude_code":
        if state.vm_key:
            prefix = f"home/.claude/projects/{state.vm_key}/memory/"
            if name.startswith(prefix):
                rest = name[len(prefix) :]
                return f"memory/{rest}" if _valid_rel(rest) and _keep(rest) else None
        if name.startswith("home/.claude/"):
            rest = name[len("home/.claude/") :]
            return f"claude/{rest}" if _allowed(rest, CLAUDE_FILES, CLAUDE_DIRS) else None
        return None
    if name.startswith("home/.config/opencode/"):
        rest = name[len("home/.config/opencode/") :]
        return f"opencode/{rest}" if _allowed(rest, OPENCODE_FILES, OPENCODE_DIRS) else None
    return None


def extract_returned(tar: Path, state: ConfigState, session_dir: Path) -> list[str]:
    """Unpack the env's config archive into ``<session>/config/vm/<logical name>``.

    Only regular files on the allow-list are kept (tar ``data`` filter on top). Files
    over 2 MB are skipped, returned and recorded in ``config/skipped.json`` (review
    leaves them alone); more than 50 MB in total raises ValueError and keeps nothing.
    """
    config_dir = session_dir / "config"
    dest = config_dir / "vm"
    if dest.exists():
        shutil.rmtree(dest)
    (config_dir / SKIPPED_FILE).unlink(missing_ok=True)
    dest.mkdir(parents=True)
    skipped: list[str] = []
    total = 0

    def member_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        nonlocal total
        if not member.isfile():
            return None
        logical = _logical_name(member.name, state)
        if logical is None:
            return None
        if member.size > MAX_FILE_BYTES:
            skipped.append(logical)
            return None
        total += member.size
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"returned config is larger than {MAX_TOTAL_BYTES >> 20} MB")
        return tarfile.data_filter(member.replace(name=logical, deep=False), path)

    try:
        # "r:": uncompressed only, so a compressed archive cannot expand without bound.
        with tarfile.open(tar, "r:") as archive:
            archive.extractall(dest, filter=member_filter)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    names = sorted(set(skipped))
    (config_dir / SKIPPED_FILE).write_text(json.dumps(names) + "\n")
    return names


# --- 3-way merge -------------------------------------------------------------------


def _same(a: object, b: object) -> bool:
    """JSON equality (``1``, ``1.0`` and ``true`` differ; key order does not matter)."""
    if a is _MISSING or b is _MISSING:
        return a is b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _lookup(data: Mapping[str, Any], path: tuple[str, ...]) -> object:
    value: object = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _protect(
    base: Mapping[str, Any], theirs: Mapping[str, Any], protected: Iterable[tuple[str, ...]]
) -> JsonObject:
    """``theirs`` with each protected path reset to its base value (absent: removed)."""
    out: JsonObject = copy.deepcopy(dict(theirs))
    for path in protected:
        value = _lookup(base, path)
        parent: JsonObject | None = out
        for key in path[:-1]:
            assert parent is not None
            child = parent.get(key, _MISSING)
            if child is _MISSING and value is not _MISSING:
                child = parent[key] = {}
            parent = child if isinstance(child, dict) else None
            if parent is None:
                break
        if parent is None:
            continue
        if value is not _MISSING:
            parent[path[-1]] = copy.deepcopy(value)
            continue
        parent.pop(path[-1], None)
        # Drop containers left empty that the base did not have ("env": {} after
        # removing an injected ANTHROPIC_BASE_URL).
        for depth in range(len(path) - 1, 0, -1):
            prefix = path[:depth]
            if _lookup(out, prefix) != {} or _lookup(base, prefix) is not _MISSING:
                break
            holder = _lookup(out, prefix[:-1])
            if isinstance(holder, dict):
                holder.pop(prefix[-1], None)
    return out


def _merge_value(
    base: object, ours: object, theirs: object, path: tuple[str, ...], conflicts: list[str]
) -> object:
    if _same(theirs, base):
        return ours
    if _same(ours, base):
        return theirs
    if _same(ours, theirs):
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        if isinstance(base, dict) or base is _MISSING:
            return _merge_objects(
                base if isinstance(base, dict) else {}, ours, theirs, path, conflicts
            )
    conflicts.append(".".join(path))
    return ours


def _merge_objects(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    path: tuple[str, ...],
    conflicts: list[str],
) -> JsonObject:
    merged: JsonObject = {}
    for key in [*ours, *(k for k in theirs if k not in ours)]:
        value = _merge_value(
            base.get(key, _MISSING),
            ours.get(key, _MISSING),
            theirs.get(key, _MISSING),
            (*path, key),
            conflicts,
        )
        if value is not _MISSING:
            merged[key] = value
    return merged


def merge_json(
    base: Mapping[str, Any],
    ours: Mapping[str, Any],
    theirs: Mapping[str, Any],
    *,
    protected: Iterable[tuple[str, ...]] = (),
) -> tuple[JsonObject, list[str]]:
    """3-way merge of JSON objects, per key: unchanged in ``theirs`` -> ours; unchanged
    in ``ours`` -> theirs (absent: deleted); both equal -> that; objects on all sides
    -> recurse; anything else is a conflict that keeps ``ours`` and lists the dotted
    path. ``protected`` paths are first reset in ``theirs`` to their base value, so
    the env can neither add nor change them."""
    conflicts: list[str] = []
    merged = _merge_objects(base, ours, _protect(base, theirs, protected), (), conflicts)
    return copy.deepcopy(merged), conflicts


def settings_protected(base: Mapping[str, Any], theirs: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Protected settings.json paths: ``PROTECTED_KEYS`` and secret/routing env keys."""
    names: set[str] = set()
    for side in (base, theirs):
        env = side.get("env")
        if isinstance(env, dict):
            names.update(str(k) for k in env if _sensitive_env(str(k)))
    return [(key,) for key in PROTECTED_KEYS] + [("env", name) for name in sorted(names)]


def _opencode_protected(
    base: Mapping[str, Any], theirs: Mapping[str, Any]
) -> list[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for side in (base, theirs):
        providers = side.get("provider")
        if isinstance(providers, dict):
            paths.update(
                ("provider", str(pid), "options", key)
                for pid in providers
                for key in ("apiKey", "headers")
            )
        paths.update(path for _, path in _secret_slots(side))
    return sorted(paths)


# --- flags -------------------------------------------------------------------------


def _text(data: bytes | None) -> str:
    return data.decode("utf-8", "replace") if data else ""


def _added_lines(before: bytes | None, after: bytes | None) -> list[str]:
    """Lines of ``after`` that ``before`` does not contain (linear, unlike a diff)."""
    old = set(_text(before).splitlines())
    return [line for line in _text(after).splitlines() if line not in old]


def text_flags(before: bytes | None, after: bytes | None) -> list[str]:
    """Attention pointers in lines ``after`` adds: installs, shell, URLs, approval bypass."""
    flags: list[str] = []
    for line in _added_lines(before, after):
        for label, pattern in _TEXT_FLAGS:
            if label not in flags and pattern.search(line):
                flags.append(label)
    return [label for label, _ in _TEXT_FLAGS if label in flags]


def _changed_paths(a: object, b: object, path: tuple[str, ...]) -> list[tuple[str, ...]]:
    a_obj = isinstance(a, dict) or (a is _MISSING and isinstance(b, dict))
    b_obj = isinstance(b, dict) or (b is _MISSING and isinstance(a, dict))
    if a_obj and b_obj:
        left = a if isinstance(a, dict) else {}
        right = b if isinstance(b, dict) else {}
        out: list[tuple[str, ...]] = []
        for key in dict.fromkeys([*left, *right]):
            out += _changed_paths(
                left.get(key, _MISSING), right.get(key, _MISSING), (*path, str(key))
            )
        return out
    return [] if _same(a, b) else [path]


def settings_flags(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    agent: AgentName = "claude_code",
) -> list[str]:
    """``changes <key>`` for each security-relevant settings key that differs."""
    flags: list[str] = []
    for path in _changed_paths(dict(before or {}), dict(after or {}), ()):
        if not path:
            continue
        hit: str | None = None
        if agent == "claude_code":
            hit = path[0] if path[0] in CLAUDE_WATCHED else None
        elif path[0] in OPENCODE_WATCHED:
            hit = path[0]
        elif path[0] == "agent" and len(path) > 2 and path[2] == "permission":
            hit = f"agent.{path[1]}.permission"
        if hit and f"changes {hit}" not in flags:
            flags.append(f"changes {hit}")
    return flags


def memory_flags(
    before: bytes | None, after: bytes | None, *, rel: str, present: Collection[str]
) -> list[str]:
    """Lines added to a ``MEMORY.md`` that link to memory files not in ``present``."""
    if PurePosixPath(rel).name != "MEMORY.md":
        return []
    missing: list[str] = []
    for line in _added_lines(before, after):
        for match in _LINK.finditer(line):
            link = unquote(match.group(1).split("#", 1)[0])
            if not link or "://" in link or link.startswith(("/", "mailto:")):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
            if target not in present and link not in missing:
                missing.append(link)
    if not missing:
        return []
    return ["MEMORY.md links to memory files that do not exist: " + ", ".join(missing)]


def _grants_bash(text: str) -> bool:
    """Frontmatter ``tools``/``allowed-tools`` mentioning Bash (not ``bash: false``)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    in_tools = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _TOOLS_KEY.match(line)
        if match:
            in_tools, value = True, match.group(1)
        elif in_tools and (line[:1] in (" ", "\t") or line.lstrip().startswith("-")):
            value = line
        else:
            in_tools = False
            continue
        if re.search(r"\bbash\b", value, re.I) and not re.search(
            r"\bbash\b\s*:\s*(?:false|deny)", value, re.I
        ):
            return True
    return False


def definition_flags(
    logical: str, before: bytes | None, after: bytes | None, *, executable: bool
) -> list[str]:
    """New or changed commands/agents/skills/output styles, Bash grants and executable
    skill files."""
    area, _, rest = logical.partition("/")
    kind = rest.split("/", 1)[0]
    watched = CLAUDE_DIRS if area == "claude" else OPENCODE_DIRS
    if area not in ("claude", "opencode") or kind not in watched or after is None:
        return []
    flags = [f"{'new' if before is None else 'changed'} {kind} definition"]
    if _grants_bash(_text(after)):
        flags.append("allows the Bash tool")
    if executable and kind == "skills":
        flags.append("executable file in a skill")
    return flags


# --- review items ------------------------------------------------------------------


def _dump_json(data: object) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    if root.is_dir():
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath, name)
                if path.is_file() and not path.is_symlink():
                    found[path.relative_to(root).as_posix()] = path
    return found


def _group(logical: str) -> Group:
    area = logical.split("/", 1)[0]
    return "memory" if area == "memory" else "local" if area == "local" else "config"


def _item_id(logical: str) -> str:
    area, _, rest = logical.partition("/")
    if area == "memory":
        return f"memory:{rest}"
    if area == "local":
        return f"local:{rest}"
    return f"config:{logical}"


def _host_target(logical: str, state: ConfigState) -> Path | None:
    area, _, rest = logical.partition("/")
    if not _valid_rel(rest):
        return None
    if area == "claude" and state.agent == "claude_code":
        return Path(state.host_dir, rest) if _allowed(rest, CLAUDE_FILES, CLAUDE_DIRS) else None
    if area == "opencode" and state.agent == "opencode":
        allowed = _allowed(rest, OPENCODE_FILES, OPENCODE_DIRS)
        return Path(state.host_dir, rest) if allowed else None
    if area == "memory":
        if not (state.host_key and state.vm_key):
            return None
        return Path(state.host_dir, "projects", state.host_key, "memory", rest)
    if area == "local" and rest == LOCAL_FILE and state.local_file:
        return Path(state.local_file)
    return None


def _read(path: Path | None) -> bytes | None:
    try:
        return path.read_bytes() if path is not None and path.is_file() else None
    except OSError:
        return None


def _is_binary(*sides: bytes | None) -> bool:
    return any(side is not None and b"\0" in side[:8000] for side in sides)


def _write_atomic(target: Path, data: bytes, executable: bool) -> None:
    """Write through an existing symlink to its real path, creating parent dirs."""
    real = Path(os.path.realpath(target))
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = real.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o755 if executable else 0o644
    # Short prefix: a 255-byte name plus the temp suffix would be too long.
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name[:32]}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _writer(target: Path, executable: bool) -> Callable[[bytes | None], None]:
    def apply(content: bytes | None) -> None:
        if content is None:
            if target.is_symlink() or target.exists():
                target.unlink()
        else:
            _write_atomic(target, content, executable)

    return apply


def merge_text(ours: bytes, base: bytes, theirs: bytes) -> tuple[bytes, bool] | None:
    """``git merge-file -p`` (labels host / session start / env): (merged, conflicted).
    None when git cannot merge (binary content)."""
    with tempfile.TemporaryDirectory(prefix="agenteval-merge-") as tmp:
        paths = [Path(tmp, name) for name in ("host", "base", "env")]
        for path, data in zip(paths, (ours, base, theirs), strict=True):
            path.write_bytes(data)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "-L", "host", "-L", "session start", "-L", "env"]
            + [str(p) for p in paths],
            capture_output=True,
            cwd=tmp,
            check=False,
        )
    if proc.returncode < 0 or proc.returncode > 127:
        return None
    return proc.stdout, proc.returncode > 0


@dataclass
class _Change:
    """One returned file and the host file it targets."""

    logical: str
    target: Path
    base: bytes | None
    theirs: bytes | None
    ours: bytes | None
    executable: bool
    home: Path

    def item(
        self,
        proposed: bytes | None,
        *,
        conflict: bool = False,
        host_changed: bool = False,
        binary: bool = False,
        flags: Sequence[str] = (),
        note: str | None = None,
    ) -> ReviewItem | None:
        if proposed == self.ours:
            return None
        kind: ItemKind = (
            "added" if self.ours is None else "deleted" if proposed is None else "modified"
        )
        label = _display(self.target, self.home)
        if self.logical.startswith("memory/"):
            # the project key makes the full path long; show where it goes as a note
            where = f"writes {label}"
            note = f"{note}\n{where}" if note else where
            label = self.logical
        return ReviewItem(
            id=_item_id(self.logical),
            group=_group(self.logical),
            label=label,
            kind=kind,
            current=self.ours,
            proposed=proposed,
            apply=_writer(self.target, self.executable),
            binary=binary,
            conflict=conflict,
            host_changed=host_changed,
            flags=list(flags),
            note=note,
        )


def _text_item(change: _Change, present: Collection[str]) -> ReviewItem | None:
    base, theirs, ours = change.base, change.theirs, change.ours
    binary = _is_binary(base, theirs, ours)
    host_changed = ours != base
    note: str | None = None
    if theirs is None:
        if ours is None:
            return None
        if host_changed:
            note = "deleted in the env, but the host file changed since the session started"
        return change.item(None, conflict=host_changed, host_changed=host_changed, note=note)

    proposed: bytes | None = theirs
    conflict = False
    if ours is not None and ours != base:
        merged = None if binary else merge_text(ours, base or b"", theirs)
        if merged is None:
            conflict = True
            note = "changed on the host too and cannot be merged: applying replaces the host file"
        else:
            proposed, conflict = merged
            note = (
                "conflict markers: edit before applying" if conflict else "merged with host changes"
            )
    elif ours is None and base is not None:
        conflict = True
        note = "deleted on the host since the session started"

    flags: list[str] = []
    if not binary:
        if change.logical.startswith("opencode/opencode.json"):
            flags += _opencode_settings_flags(ours, proposed)
        else:
            flags += text_flags(ours, proposed)
        if change.logical.startswith("memory/"):
            flags += memory_flags(ours, proposed, rel=change.logical[7:], present=present)
    flags += definition_flags(change.logical, ours, proposed, executable=change.executable)
    return change.item(
        proposed,
        conflict=conflict,
        host_changed=host_changed,
        binary=binary,
        flags=flags,
        note=note,
    )


def _json_or_none(data: bytes | None) -> JsonObject | None:
    if data is None:
        return None
    try:
        value, _ = _parse_jsonc(data.decode("utf-8"))
    except ValueError:
        return None
    return value


def _opencode_flags(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> list[str]:
    """``settings_flags`` plus newly added ``{file:...}`` references (read on the host)."""
    flags = settings_flags(before, after, agent="opencode")
    old = {m for s in _all_strings(before) for m in _ANY_FILE_REFERENCE.findall(s)}
    new = sorted({m for s in _all_strings(after) for m in _ANY_FILE_REFERENCE.findall(s)} - old)
    if new:
        flags.append("adds {file:} references: " + ", ".join(new))
    return flags


def _opencode_settings_flags(before: bytes | None, after: bytes | None) -> list[str]:
    old, new = _json_or_none(before), _json_or_none(after)
    if after is not None and new is None:
        return ["invalid JSON", *text_flags(before, after)]
    return _opencode_flags(old, new)


def _json_item(change: _Change, *, opencode: bool) -> ReviewItem | None:
    """settings.json (always) and OpenCode configs whose host file holds literal keys.

    A settings.json deleted in the env merges as ``{}``: keys the env never saw stay.
    """
    theirs = {} if change.theirs is None else _json_or_none(change.theirs)
    if theirs is None:
        return change.item(
            change.theirs,
            conflict=True,
            flags=["invalid JSON"],
            note="the env left this file as invalid JSON: applying replaces the host file "
            "as is, dropping its host-only keys (credential helpers, secrets); [e] to fix it",
        )
    base = _json_or_none(change.base) or {}
    ours = _json_or_none(change.ours)
    protected = (_opencode_protected if opencode else settings_protected)(base, theirs)

    def flags(before: JsonObject | None, after: JsonObject) -> list[str]:
        return _opencode_flags(before, after) if opencode else settings_flags(before, after)

    if change.ours is not None and ours is None:
        merged = _protect(base, theirs, protected)
        return change.item(
            _dump_json(merged),
            conflict=True,
            host_changed=True,
            flags=flags(None, merged),
            note="the host file is not valid JSON: applying replaces it",
        )
    merged, conflicts = merge_json(base, ours or {}, theirs, protected=protected)
    if (ours is None and not merged) or (ours is not None and _same(merged, ours)):
        return None
    if ours is None:
        clean: JsonObject = {}
    elif opencode:
        clean = copy.deepcopy(ours)
        _strip_secrets(clean)
    else:
        clean = sanitize_claude_settings(ours)[0]
    notes: list[str] = []
    if change.theirs is None:
        notes.append("deleted in the env: only keys the env never saw are kept")
    if opencode:
        notes.append("merged as JSON (comments dropped): the host file holds literal credentials")
    if conflicts:
        notes.append("kept the host value for: " + ", ".join(conflicts))
    return change.item(
        _dump_json(merged),
        conflict=bool(conflicts),
        host_changed=not _same(clean, base),
        flags=flags(ours, merged),
        note="; ".join(notes) or None,
    )


def _read_skipped(config_dir: Path) -> set[str]:
    try:
        data = json.loads((config_dir / SKIPPED_FILE).read_text())
    except (OSError, ValueError):
        return set()
    return {str(name) for name in data} if isinstance(data, list) else set()


def config_items(session: DevSession, session_dir: Path) -> list[ReviewItem]:
    """One review item per returned config/memory file that differs from what was
    copied in, merged 3-way against the host file as it is now."""
    state = session.config
    config_dir = session_dir / "config"
    vm_dir = config_dir / "vm"
    if state is None or not vm_dir.is_dir():
        return []
    base_files, vm_files = _files(config_dir / "base"), _files(vm_dir)
    skipped = _read_skipped(config_dir)
    present = {name[len("memory/") :] for name in vm_files if name.startswith("memory/")}
    home = Path.home()
    items: list[ReviewItem] = []
    for logical in sorted(set(base_files) | set(vm_files)):
        target = _host_target(logical, state)
        if target is None or logical in skipped:
            continue
        base, theirs = _read(base_files.get(logical)), _read(vm_files.get(logical))
        if base == theirs:
            continue
        theirs_path = vm_files.get(logical)
        # Only skill scripts may come back executable (and they are flagged).
        executable = logical.startswith("claude/skills/") and bool(
            theirs_path and theirs_path.stat().st_mode & 0o111
        )
        change = _Change(
            logical=logical,
            target=target,
            base=base,
            theirs=theirs,
            ours=_read(target),
            executable=executable,
            home=home,
        )
        item: ReviewItem | None
        if logical == "claude/settings.json":
            item = _json_item(change, opencode=False)
        elif (
            theirs is not None
            and logical in ("opencode/opencode.json", "opencode/opencode.jsonc")
            and change.ours is not None
            and _has_literal_keys(change.ours)
        ):
            item = _json_item(change, opencode=True)
        else:
            item = _text_item(change, present)
        if item is not None:
            items.append(item)
    items.sort(key=lambda i: (GROUP_ORDER.index(i.group), i.id))
    return items


def _has_literal_keys(text: bytes) -> bool:
    try:
        return bool(sanitize_opencode(text.decode("utf-8"))[1])
    except ValueError:
        return False


def config_patch(session_dir: Path) -> str:
    """Readable diff of the returned config against what was copied in."""
    config_dir = session_dir / "config"
    base_files, vm_files = _files(config_dir / "base"), _files(config_dir / "vm")
    chunks: list[str] = []
    for logical in sorted(set(base_files) | set(vm_files)):
        old, new = _read(base_files.get(logical)), _read(vm_files.get(logical))
        if old == new:
            continue
        if _is_binary(old, new):
            chunks.append(f"Binary files a/{logical} and b/{logical} differ\n")
            continue
        diff = difflib.unified_diff(
            _text(old).splitlines(keepends=True),
            _text(new).splitlines(keepends=True),
            f"a/{logical}" if old is not None else "/dev/null",
            f"b/{logical}" if new is not None else "/dev/null",
        )
        chunks.extend(line if line.endswith("\n") else line + "\n" for line in diff)
    return "".join(chunks)
