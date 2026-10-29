"""Reference cases for capability tasks, checked outside the code under test.

A task's ``<name>_cases.py`` defines ``SETUP`` (Python run before every case,
typically the imports from the workspace) and ``CASES``, a list of :class:`Case`.
Each case's ``code`` is sent to ``guest/scripts/case_runner.py``, which runs it
against the workspace and returns the value it produced (or the exception it
raised) as tagged JSON. The expected values stay in the scorer: they are never
written into the environment, so workspace code cannot read or forge them.

Expected values may be ``None``, bool, int, float, str, Decimal, and lists, tuples
and dicts of those, or :class:`Raises`. Values compare with ``==``, like a pytest
``assert``, except that bools never equal numbers; ``Raises`` matches the named
exception class or a subclass.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

CASES_GLOB = "*_cases.py"
_MAX_DEPTH = 64
_MAX_DESCRIBE = 200


@dataclass(frozen=True)
class Raises:
    """Expect the case to raise ``exception`` (``ValueError`` means ``builtins.ValueError``)."""

    exception: str

    @property
    def qualified(self) -> str:
        return self.exception if "." in self.exception else f"builtins.{self.exception}"


@dataclass(frozen=True)
class Case:
    name: str
    code: str
    """An expression, or statements that assign ``result``."""
    expected: object


@dataclass(frozen=True)
class CaseSet:
    setup: str
    cases: tuple[Case, ...]

    def request(self, workspace: str) -> str:
        """The runner's stdin. Carries the case code, never the expected values."""
        cases = [{"id": case.name, "code": case.code} for case in self.cases]
        return json.dumps({"workspace": workspace, "setup": self.setup, "cases": cases})


@dataclass(frozen=True)
class Raised:
    types: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Unsupported:
    description: str


class ProtocolError(ValueError):
    """The runner's output is not a valid result document."""


def find_cases_file(task_dir: Path) -> Path:
    found = sorted(task_dir.glob(CASES_GLOB))
    if len(found) != 1:
        raise ValueError(f"{task_dir}: expected exactly one {CASES_GLOB}, found {len(found)}")
    return found[0]


def load_case_set(path: Path) -> CaseSet:
    spec = importlib.util.spec_from_file_location(f"agenteval_cases_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setup = getattr(module, "SETUP", "")
    cases = getattr(module, "CASES", None)
    if not isinstance(setup, str):
        raise ValueError(f"{path}: SETUP must be a string")
    if not isinstance(cases, list) or not cases or not all(isinstance(c, Case) for c in cases):
        raise ValueError(f"{path}: CASES must be a non-empty list of Case")
    names = [case.name for case in cases]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate case names {duplicates}")
    for case in cases:
        if not _is_expected_value(case.expected):
            raise ValueError(f"{path}: case {case.name!r} has an unsupported expected value")
    return CaseSet(setup=setup, cases=tuple(cases))


def _is_expected_value(value: object, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, Raises):
        return depth == 0
    if value is None or isinstance(value, bool | int | float | str | Decimal):
        return True
    if isinstance(value, list | tuple):
        return all(_is_expected_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            _is_expected_value(k, depth + 1) and _is_expected_value(v, depth + 1)
            for k, v in value.items()
        )
    return False


def decode(value: object, depth: int = 0) -> object:
    """Python value for one encoded runner result. Raises ProtocolError on bad input."""
    if depth > _MAX_DEPTH:
        raise ProtocolError("result nested too deeply")
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is list:
        return [decode(item, depth + 1) for item in value]
    if type(value) is not dict or len(value) not in (1, 2):
        raise ProtocolError(f"unexpected result value: {describe(value)}")
    if set(value) == {"$tuple"} and type(value["$tuple"]) is list:
        return tuple(decode(item, depth + 1) for item in value["$tuple"])
    if set(value) == {"$decimal"} and type(value["$decimal"]) is str:
        try:
            return Decimal(value["$decimal"])
        except InvalidOperation as error:
            raise ProtocolError(f"bad decimal {describe(value['$decimal'])}") from error
    if set(value) == {"$dict"} and type(value["$dict"]) is list:
        pairs = value["$dict"]
        if not all(type(pair) is list and len(pair) == 2 for pair in pairs):
            raise ProtocolError("bad dict encoding")
        try:
            return {decode(k, depth + 1): decode(v, depth + 1) for k, v in pairs}
        except TypeError as error:  # an unhashable key
            raise ProtocolError(f"bad dict key: {error}") from error
    if set(value) == {"$raises", "message"}:
        types, message = value["$raises"], value["message"]
        if type(types) is list and all(type(t) is str for t in types) and type(message) is str:
            return Raised(types=tuple(types), message=message)
    if set(value) == {"$unsupported"} and type(value["$unsupported"]) is str:
        return Unsupported(value["$unsupported"])
    raise ProtocolError(f"unexpected result value: {describe(value)}")


def parse_results(stdout: str, case_set: CaseSet) -> dict[str, object]:
    """Decoded results by case name, from the last line of the runner's stdout."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ProtocolError("no result document on stdout")
    try:
        document = json.loads(lines[-1])
    except (ValueError, RecursionError) as error:
        raise ProtocolError(f"result document is not JSON: {error}") from error
    if type(document) is not dict or set(document) != {"results"}:
        raise ProtocolError('result document must be {"results": {...}}')
    raw = document["results"]
    if type(raw) is not dict:
        raise ProtocolError("results must be an object")
    expected_names = {case.name for case in case_set.cases}
    unknown = sorted(set(raw) - expected_names)
    if unknown:
        raise ProtocolError(f"results for unknown cases {unknown[:5]}")
    try:
        return {name: decode(value) for name, value in raw.items()}
    except RecursionError as error:
        raise ProtocolError("result nested too deeply") from error


def matches(expected: object, actual: object) -> bool:
    if isinstance(expected, Raises):
        return isinstance(actual, Raised) and expected.qualified in actual.types
    try:
        return _equal(expected, actual)
    except Exception:
        return False


def _equal(expected: object, actual: object) -> bool:
    """``==`` as in a pytest assert, except that bools never equal numbers."""
    if isinstance(actual, Raised | Unsupported):
        return False
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, list | tuple):
        return (
            type(actual) is type(expected)
            and len(actual) == len(expected)
            and all(_equal(e, a) for e, a in zip(expected, actual, strict=True))
        )
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and expected.keys() == actual.keys()
            and all(_equal(value, actual[key]) for key, value in expected.items())
        )
    return bool(expected == actual)


def describe(value: object) -> str:
    if isinstance(value, Raised):
        name = value.types[0].removeprefix("builtins.") if value.types else "exception"
        text = f"raised {name}: {value.message}"
    elif isinstance(value, Unsupported):
        text = f"unsupported value {value.description}"
    elif isinstance(value, Raises):
        text = f"raises {value.exception}"
    else:
        text = repr(value)
    return text if len(text) <= _MAX_DESCRIBE else text[:_MAX_DESCRIBE] + "..."
