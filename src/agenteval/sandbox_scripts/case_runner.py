#!/usr/bin/env python3
"""Capability case runner (stdlib only, Python 3.12).

Runs inside the environment as the agent user, started with ``python -I`` so the
workspace is importable only through the path this script adds. It reads one JSON
request on stdin::

    {"workspace": "/workspace", "setup": "<python>", "cases": [{"id": "...", "code": "..."}]}

For each case it runs ``setup`` in a fresh namespace (with ``WORKSPACE``, the
workspace path, predefined), then ``code``: an expression, or statements that assign
``result``. It prints exactly one JSON object on stdout,
``{"results": {case_id: <encoded value>}}``. Anything the workspace code prints goes
to stderr.

This process imports agent-written code, so it is untrusted by design: it never
sees the expected values. The host decodes the results and compares them.

Encoding: ``None``/bool/int/float/str and lists are plain JSON; tuples are
``{"$tuple": [...]}``, dicts ``{"$dict": [[key, value], ...]}``, Decimals
``{"$decimal": "1.10"}``. A raised exception is ``{"$raises": [qualified names of
its classes], "message": "..."}``; any other value is ``{"$unsupported": "<type>:
<repr>"}``.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal

_MAX_DEPTH = 32
_MAX_TEXT = 300


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "..."


def _qualname(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def encode(value: object, depth: int = 0) -> object:
    if depth > _MAX_DEPTH:
        return {"$unsupported": "nested too deeply"}
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is Decimal:
        return {"$decimal": str(value)}
    if type(value) is list:
        return [encode(item, depth + 1) for item in value]
    if type(value) is tuple:
        return {"$tuple": [encode(item, depth + 1) for item in value]}
    if type(value) is dict:
        return {"$dict": [[encode(k, depth + 1), encode(v, depth + 1)] for k, v in value.items()]}
    try:
        text = repr(value)
    except Exception:
        text = "?"
    return {"$unsupported": _clip(f"{_qualname(type(value))}: {text}")}


def raised(error: BaseException) -> dict[str, object]:
    try:
        message = str(error)
    except Exception:
        message = "?"
    names = [_qualname(cls) for cls in type(error).__mro__ if cls is not object]
    return {"$raises": names, "message": _clip(message)}


def run_case(setup: str, source: str, workspace: str) -> object:
    try:
        code = compile(source, "<case>", "eval")
        is_expression = True
    except SyntaxError:
        code = compile(source, "<case>", "exec")
        is_expression = False
    namespace: dict[str, object] = {"__name__": "__case__", "WORKSPACE": workspace}
    # BaseException: SystemExit or KeyboardInterrupt raised by workspace code is a result.
    try:
        exec(compile(setup, "<setup>", "exec"), namespace)
        if is_expression:
            value = eval(code, namespace)
        else:
            exec(code, namespace)
            value = namespace["result"]
        return encode(value)
    except BaseException as error:
        return raised(error)


def main() -> None:
    request = json.loads(sys.stdin.read())
    out = sys.stdout
    sys.stdout = sys.stderr
    sys.stdin = open(os.devnull)  # noqa: SIM115
    workspace = request["workspace"]
    sys.path.insert(0, workspace)
    results = {
        case["id"]: run_case(request["setup"], case["code"], workspace) for case in request["cases"]
    }
    out.write(json.dumps({"results": results}) + "\n")
    out.flush()


if __name__ == "__main__":
    main()
