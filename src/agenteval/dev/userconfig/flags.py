"""Flags: risky settings, definitions and text in returned config."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote

from agenteval.dev.session import AgentName
from agenteval.dev.userconfig.common import (
    _LINK,
    _MISSING,
    _TEXT_FLAGS,
    _TOOLS_KEY,
    CLAUDE_DIRS,
    CLAUDE_WATCHED,
    OPENCODE_DIRS,
    OPENCODE_WATCHED,
)
from agenteval.dev.userconfig.merge import _same


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
