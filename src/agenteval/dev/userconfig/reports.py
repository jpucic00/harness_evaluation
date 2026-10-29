"""Reports: host config the env cannot use, and the Claude global state seed and theme."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from agenteval.dev.userconfig.common import (
    _FILE_REFERENCE,
    _HOST_PATH,
    _IMPORT,
    CLAUDE_DIRS,
    LOCAL_FILE,
    JsonObject,
)
from agenteval.dev.userconfig.discovery import (
    ConfigPlan,
    _claude_global_json,
    _home,
    host_config_dir,
)


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
