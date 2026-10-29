"""Loaders for the contributor-written cases in ``cases/`` (see ``cases/README.md``).

One loader per kind, each resolving ``constants.cases_dir()`` when it is called, so
``AGENTEVAL_CASES_DIR`` set after import is honoured:

- ``load_capability_tasks()``   ``cases/capability/<id>/`` (task.yaml, repo/, ...)
- ``load_containment_checks()`` ``cases/containment/<area>.<name>.py``
- ``load_monitor_groups()``     ``cases/monitor/<group>.yaml`` (persistence watch groups)
- ``load_injection_cases()``    ``cases/injection/<id>/`` (case.yaml, repo/)

Names starting with ``_`` (the templates) are never loaded. This module reads and
validates files only; the suites turn the cases into samples and setup steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from agenteval import constants
from agenteval.sandbox_scripts.probe import Check, load_checks

if TYPE_CHECKING:
    from agenteval.suites.reference_cases import CaseSet

# --------------------------------------------------------------------------- capability

SETUP_SCRIPT = "setup.sh"
PROMPT_SUFFIX = (
    "The project is in the current working directory. "
    "You can run its tests with `python3 -m pytest -q`."
)

Difficulty = Literal["easy", "medium", "hard"]
_DIFFICULTY_RANK: dict[str, int] = {"easy": 0, "medium": 1, "hard": 2}
_SKIP_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}


class CapabilityTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    difficulty: Difficulty
    summary: str
    prompt: str
    prompt_suffix: str = PROMPT_SUFFIX
    """Appended to the prompt; tasks that are not about running tests replace it."""
    directory: Path

    @property
    def full_prompt(self) -> str:
        suffix = self.prompt_suffix.strip()
        return f"{self.prompt.strip()}\n\n{suffix}" if suffix else self.prompt.strip()

    @property
    def setup_script(self) -> Path | None:
        path = self.directory / SETUP_SCRIPT
        return path if path.is_file() else None

    def repo_files(self) -> dict[str, bytes]:
        return _read_tree(self.directory / "repo")

    def test_files(self) -> dict[str, bytes]:
        """Visible tests shipped in the repo (checked for tampering)."""
        return {k: v for k, v in self.repo_files().items() if k.startswith("tests/")}

    def case_set(self) -> CaseSet:
        """Reference cases and expected values (host-side only)."""
        # Imported here: importing agenteval.suites imports the suites, which import this module.
        from agenteval.suites.reference_cases import find_cases_file, load_case_set

        return load_case_set(find_cases_file(self.directory))


def _read_tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or path.suffix == ".pyc" or _SKIP_PARTS & set(rel.parts):
            continue
        files[rel.as_posix()] = path.read_bytes()
    return files


def load_capability_tasks(root: Path | None = None) -> list[CapabilityTask]:
    """All tasks under ``root`` (default ``cases/capability``), easiest first."""
    root = root or constants.cases_dir() / "capability"
    tasks: list[CapabilityTask] = []
    for task_yaml in sorted(root.glob("*/task.yaml")):
        if task_yaml.parent.name.startswith("_"):  # _template
            continue
        data = yaml.safe_load(task_yaml.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{task_yaml}: expected a mapping")
        task = CapabilityTask.model_validate({**data, "directory": task_yaml.parent})
        if task.id != task_yaml.parent.name:
            raise ValueError(f"{task_yaml}: id '{task.id}' does not match its directory")
        tasks.append(task)
    return sorted(tasks, key=lambda t: (_DIFFICULTY_RANK[t.difficulty], t.id))


def get_capability_task(task_id: str, root: Path | None = None) -> CapabilityTask:
    for task in load_capability_tasks(root):
        if task.id == task_id:
            return task
    raise KeyError(f"unknown capability task '{task_id}'")


# --------------------------------------------------------------------------- containment


def containment_check_files(root: Path | None = None) -> dict[str, bytes]:
    """``<id>.py`` -> source for every containment check (``_*`` templates excluded)."""
    root = root or constants.cases_dir() / "containment"
    return {
        path.name: path.read_bytes()
        for path in sorted(root.glob("*.py"))
        if not path.name.startswith("_")
    }


def load_containment_checks(root: Path | None = None) -> list[Check]:
    """The check files in ``cases/containment/`` (loaded host-side for ids and metadata)."""
    return load_checks(str(root or constants.cases_dir() / "containment"))


# --------------------------------------------------------------------------- monitor


def _none_as_empty(value: object) -> object:
    return [] if value is None else value


_PathList = Annotated[
    list[Annotated[StrictStr, Field(min_length=1)]], BeforeValidator(_none_as_empty)
]


class MonitorGroup(BaseModel):
    """``cases/monitor/<group>.yaml``: locations fingerprinted for persistence changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: StrictStr
    priority: StrictInt = 50
    paths: _PathList = Field(default_factory=list)
    """Directories, watched recursively."""
    files: _PathList = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_empty(self) -> MonitorGroup:
        if not self.paths and not self.files:
            raise ValueError("needs 'paths' or 'files'")
        return self


def _monitor_error(error: ValidationError) -> str:
    """The first problem in ``error``, worded for a contributor editing the YAML."""
    errors = error.errors()
    unknown = sorted(str(e["loc"][0]) for e in errors if e["type"] == "extra_forbidden")
    if unknown:
        return f"unknown keys {unknown}; allowed: {sorted(MonitorGroup.model_fields)}"
    first = errors[0]
    key = str(first["loc"][0]) if first["loc"] else ""
    if key == "title":
        return "'title' is required"
    if key == "priority":
        return "'priority' must be an integer"
    if key in ("paths", "files"):
        return f"'{key}' must be a list of paths"
    ctx = first.get("ctx") or {}
    return str(ctx.get("error", first["msg"]))


def load_monitor_groups(root: Path | None = None) -> list[dict[str, object]]:
    """Persistence watch groups from ``cases/monitor/<group>.yaml``, in the shape
    ``monitor.py snapshot --watch`` reads: ``{name, priority, paths, files}``."""
    root = root or constants.cases_dir() / "monitor"
    groups: list[dict[str, object]] = []
    for path in sorted(root.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a mapping")
        try:
            group = MonitorGroup.model_validate(data)
        except ValidationError as ex:
            raise ValueError(f"{path}: {_monitor_error(ex)}") from ex
        groups.append(
            {
                "name": path.stem,
                "priority": group.priority,
                "paths": group.paths,
                "files": group.files,
            }
        )
    return groups


# --------------------------------------------------------------------------- injection

CODEIPI_WORKSPACE = "/workspace"
"""CodeIPI writes its repos to, and verifies in, this fixed path."""

Category = Literal["none", "exfiltration", "code_execution", "persistence"]


class InjectionCaseSpec(BaseModel):
    """``cases/injection/<id>/case.yaml``: one CodeIPI-format sample (see that README)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    """What the user asks the agent to do (CodeIPI ``task_description``)."""
    issue: str = ""
    """Bug report appended to the task (CodeIPI ``issue_text``); a possible injection vector."""
    vector: Literal["none", "issue_text", "code_comment", "readme", "config_file"]
    category: Category
    """What the injected instruction tries; ``none`` = benign control case."""
    sophistication: str = "none"
    severity: Literal["none", "S1", "S2", "S3"] = "none"
    indicator: str = ""
    """Regex describing the injected action (documentation; CodeIPI does not score it)."""
    expected_malicious_action: str = ""
    exec_marker: str | None = None
    """``code_execution`` only: unique string inside the injected command. Compliance
    = the marker appears in the agent's tool calls."""
    persistence_targets: list[str] = Field(default_factory=list)
    """``persistence`` only: workspace files whose change means compliance."""
    verification: str = f"cd {CODEIPI_WORKSPACE} && python -m pytest tests/ -q"
    """Command that passes once the legitimate bug is fixed."""
    expected_fix: str

    @model_validator(mode="after")
    def _consistent(self) -> InjectionCaseSpec:
        benign = self.category == "none"
        if benign != (self.vector == "none") or benign != (self.severity == "none"):
            raise ValueError("benign cases (category none) need vector: none and severity: none")
        if (self.category == "code_execution") != bool(self.exec_marker):
            raise ValueError("exec_marker is required for, and only for, category code_execution")
        if self.category == "persistence" and not self.persistence_targets:
            raise ValueError("category persistence needs persistence_targets")
        return self


@dataclass(frozen=True)
class InjectionCase:
    """A local injection case: its directory name is the sample id."""

    id: str
    spec: InjectionCaseSpec
    repo_files: dict[str, str]
    """``repo/`` as ``{relative path: text}``: what CodeIPI writes to the workspace."""


def load_injection_case(directory: Path) -> InjectionCase:
    try:
        spec = InjectionCaseSpec.model_validate(
            yaml.safe_load((directory / "case.yaml").read_text())
        )
    except Exception as ex:
        raise ValueError(f"{directory / 'case.yaml'}: {ex}") from ex
    repo = directory / "repo"
    if not repo.is_dir():
        raise ValueError(f"{directory}: missing repo/ (the files written to the workspace)")
    files = {
        str(path.relative_to(repo)): path.read_text()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    return InjectionCase(id=directory.name, spec=spec, repo_files=files)


def load_injection_cases(root: Path | None = None) -> list[InjectionCase]:
    """Every ``cases/injection/<id>/`` (``_*`` directories are templates)."""
    root = root or constants.cases_dir() / "injection"
    if not root.is_dir():
        return []
    return [
        load_injection_case(d)
        for d in sorted(root.iterdir())
        if d.is_dir() and not d.name.startswith(("_", "."))
    ]
