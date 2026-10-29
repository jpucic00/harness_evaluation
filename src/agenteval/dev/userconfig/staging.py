"""Staging: the sanitized config archive and merge bases for a session."""

from __future__ import annotations

import os
import shutil
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agenteval.dev.session import ConfigState
from agenteval.dev.userconfig.common import (
    _AREA_HOME,
    _SKIP_NAMES,
    CLAUDE_DIRS,
    LOCAL_FILE,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    OPENCODE_DIRS,
    JsonObject,
    _dump_json,
)
from agenteval.dev.userconfig.discovery import ConfigPlan
from agenteval.dev.userconfig.reports import _read_json_object, unusable_reports
from agenteval.dev.userconfig.sanitizing import (
    _parse_jsonc,
    sanitize_claude_settings,
    sanitize_opencode,
)


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
