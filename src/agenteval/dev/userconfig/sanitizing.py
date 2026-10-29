"""Sanitizing: JSONC parsing, and the secrets, helpers and routing removed before copy-in."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator, Mapping
from typing import Any

from agenteval.dev.userconfig.common import (
    _REFERENCE,
    _ROUTING_ENV,
    _SECRET_ENV,
    _TRAILING_CLOSE,
    HELPER_KEYS,
    MAX_JSON_DEPTH,
    JsonObject,
    _dump_json,
)


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
