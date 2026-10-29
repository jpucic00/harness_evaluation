"""Host-executed paths: files git or other host tools run or read as config."""

from __future__ import annotations

import json
import os
import posixpath
import tomllib
from collections.abc import Callable

from agenteval.dev.repo.common import (
    _ABSENT,
    _AGENT_DIRS,
    _AGENT_FILES,
    _EXEC,
    _SYMLINK,
    _TOOL_VERSION_FILES,
)


def repo_path_flags(
    path: str, old_mode: str, new_mode: str, before: bytes | None, after: bytes | None
) -> list[str]:
    """Why a changed repo path deserves a careful look: something on the host runs or obeys it.

    Attention pointers, not a verdict. ``before``/``after`` are the contents (None: absent;
    a symlink's target), the modes git's octal strings (``000000`` when absent). Names are
    compared case-insensitively: on a macOS host ``.GitHub/x.yml`` lands in ``.github/``.
    """
    if after is None:
        return []
    parts = path.casefold().split("/")
    name = parts[-1]
    rules = (
        (name == ".envrc", "direnv runs .envrc in your shell when you enter the directory"),
        (
            name in ("makefile", "gnumakefile") or name.endswith(".mk"),
            "make runs its recipes on your host",
        ),
        (name in ("justfile", ".justfile"), "just runs its recipes on your host"),
        (
            name in ("taskfile.yml", "taskfile.yaml", "taskfile.dist.yml", "taskfile.dist.yaml"),
            "Task runs its commands on your host",
        ),
        (
            name == "package.json" and _changed(before, after, _package_scripts),
            "package.json scripts changed: npm, yarn and pnpm run them on your host "
            "(install hooks included)",
        ),
        (
            name in (".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs"),
            "package manager config: can change registries and run code on install",
        ),
        (
            name == "pyproject.toml" and _changed(before, after, _pyproject_hooks),
            "pyproject.toml build system or scripts changed: they run when the project is "
            "built, installed or run",
        ),
        (name == "setup.py", "setup.py runs when the package is built or installed"),
        (name == "conftest.py", "pytest runs conftest.py on every test run"),
        (name in ("tox.ini", "noxfile.py"), "tox and nox run its commands on your host"),
        (
            parts[0] == ".github",
            "GitHub config: workflows run in CI with repository secrets once pushed",
        ),
        (name == ".gitlab-ci.yml", "GitLab CI config: runs with CI secrets once pushed"),
        (
            name in (".pre-commit-config.yaml", ".pre-commit-hooks.yaml"),
            "pre-commit runs these hooks on your host on git commit",
        ),
        (
            parts[0] in (".husky", ".githooks")
            or name.removeprefix(".").removesuffix(".yml").removesuffix(".yaml")
            in ("lefthook", "lefthook-local"),
            "git hook config: runs on your host on git operations",
        ),
        (
            name in _AGENT_FILES or any(p in _AGENT_DIRS for p in parts[:-1]),
            "coding-agent config or instructions: agents on your host read and act on it",
        ),
        (
            parts[0] == ".vscode" and name in ("tasks.json", "settings.json", "launch.json"),
            "VS Code config: can run commands when the folder is opened",
        ),
        (
            parts[0] == ".devcontainer" or name == ".devcontainer.json",
            "dev container config: runs commands when the container is built or opened",
        ),
        (
            name == ".gitattributes",
            ".gitattributes selects filters and diff/merge drivers that git runs on your host",
        ),
        (name == ".gitmodules", ".gitmodules sets where submodules are fetched from"),
        (
            name in _TOOL_VERSION_FILES,
            "tool-version file: picks the toolchain version your host installs and runs",
        ),
        (
            new_mode == _EXEC and old_mode != _EXEC,
            "new executable file" if old_mode == _ABSENT else "made executable",
        ),
        (
            new_mode == _SYMLINK and _escapes(path, after),
            "symlink pointing outside the repo",
        ),
        (
            new_mode == _SYMLINK
            and any(p.rstrip(". ").casefold() == ".git" for p in os.fsdecode(after).split("/")),
            "symlink pointing into a .git directory (hooks and config run on your host)",
        ),
    )
    return list(dict.fromkeys(text for hit, text in rules if hit))


def _changed(before: bytes | None, after: bytes, extract: Callable[[bytes | None], object]) -> bool:
    try:
        return extract(before) != extract(after)
    except (ValueError, RecursionError):  # unparsable, or nested too deeply to parse
        return True


def _package_scripts(data: bytes | None) -> object:
    if data is None:
        return None
    doc = json.loads(data)
    return doc.get("scripts") if isinstance(doc, dict) else None


def _pyproject_hooks(data: bytes | None) -> object:
    if data is None:
        return None
    doc = tomllib.loads(data.decode())
    picked: dict[str, object] = {"build-system": doc.get("build-system")}
    project = doc.get("project")
    if isinstance(project, dict):
        for key in ("scripts", "gui-scripts", "entry-points"):
            picked[f"project.{key}"] = project.get(key)
    tool = doc.get("tool")
    if isinstance(tool, dict):
        for name, section in tool.items():
            if isinstance(section, dict):
                picked[f"tool.{name}"] = (section.get("scripts"), section.get("tasks"))
        hatch = tool.get("hatch")
        envs = hatch.get("envs") if isinstance(hatch, dict) else None
        if isinstance(envs, dict):
            picked["tool.hatch.envs"] = {
                env: spec.get("scripts") for env, spec in envs.items() if isinstance(spec, dict)
            }
    return picked


def _escapes(path: str, target: bytes) -> bool:
    link = os.fsdecode(target)
    if link.startswith("/"):
        return True
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), link))
    return resolved == ".." or resolved.startswith("../")
