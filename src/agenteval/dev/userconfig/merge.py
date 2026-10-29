"""3-way merge of returned JSON config with the host file."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from agenteval.dev.userconfig.common import _MISSING, PROTECTED_KEYS, JsonObject
from agenteval.dev.userconfig.sanitizing import _secret_slots, _sensitive_env


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
