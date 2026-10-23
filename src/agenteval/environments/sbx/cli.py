"""The ``sbx`` CLI (Docker Sandboxes): readiness, templates and sandboxes.

Every call runs with ``SBX_NO_TELEMETRY=1``, so the CLI sends Docker no usage data.
The synchronous helpers serve ``agenteval env build``, ``doctor`` and ``cleanup``; the
provider runs sandbox commands through Inspect's async ``subprocess``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SBX_ENV = {"SBX_NO_TELEMETRY": "1"}
NAME_PREFIX = "agenteval-sbx-"
SAMPLE_NAME = re.compile(r"^agenteval-sbx-[0-9a-f]{8}$")
INSTALL_URL = "https://docs.docker.com/ai/sandboxes/install/"
SBX_MISSING_MESSAGE = f"Docker Sandboxes (sbx) is not on PATH; install it: {INSTALL_URL}"

REQUIRED_SETTINGS: dict[str, Any] = {"ssh.agentForwardingEnabled": False}
"""Settings a sandbox must not run without: forwarding hands the host's SSH agent
(and so every key loaded in it) to whatever runs in the sandbox."""

ADVISED_SETTINGS: dict[str, tuple[Any, str]] = {
    "browser.enabled": (False, "lets processes in a sandbox open URLs in the host's browser"),
}

TEMPLATE_LABEL = 'com.docker.sandboxes.start-docker="true"'
"""Gives root in the sandbox its full capability set, as Docker's own templates have it.
Without it root lacks CAP_NET_ADMIN and ``egress.sh`` cannot install its nftables rule.
The agent never runs as root, and the boundary is the microVM."""

KEEP_ENV = frozenset(
    {"PATH", "HOME", "HOSTNAME", "TERM", "LANG", "LANGUAGE", "LC_ALL", "USER", "LOGNAME"}
    | {"SHELL", "PWD", "OLDPWD", "SHLVL", "_"}
)
"""Variables a command keeps besides the image's own ``ENV``. sbx adds about forty more
(proxy settings, placeholder API keys, a git config override, its CA bundle, a display
socket); commands run without them, as they would in the Docker and Tart environments."""


class SbxError(RuntimeError):
    pass


def sbx_binary() -> str | None:
    return shutil.which("sbx")


def sbx_environ() -> dict[str, str]:
    return {**os.environ, **SBX_ENV}


def new_sandbox_name() -> str:
    return f"{NAME_PREFIX}{secrets.token_hex(4)}"


def is_sample_sandbox(name: str) -> bool:
    """Per-sample sandboxes only, so cleanup never removes anything else."""
    return SAMPLE_NAME.match(name) is not None


def template_ref(image: str) -> tuple[str, str]:
    """``(repository, tag)`` as ``sbx template ls`` shows a Docker image reference."""
    name, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "")
    first = name.split("/", 1)[0]
    if "/" not in name:
        name = f"docker.io/library/{name}"
    elif "." not in first and ":" not in first and first != "localhost":
        name = f"docker.io/{name}"
    return name, tag or "latest"


def run(args: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run ``sbx ARGS`` (synchronously); raises SbxError when sbx is missing or hangs."""
    sbx = sbx_binary()
    if sbx is None:
        raise SbxError(SBX_MISSING_MESSAGE)
    try:
        return subprocess.run(
            [sbx, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=sbx_environ(),
            check=False,
        )
    except subprocess.TimeoutExpired as ex:
        raise SbxError(f"`sbx {' '.join(args)}` did not finish within {timeout:.0f}s") from ex


def _json(result: subprocess.CompletedProcess[str], what: str) -> Any:
    if result.returncode != 0:
        raise SbxError(f"{what} failed: {_first_line(result.stderr or result.stdout)}")
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as ex:
        raise SbxError(f"{what}: unexpected output: {ex}") from ex


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0] if lines else "(no output)"


def setting(key: str) -> Any:
    record = _json(run(["settings", "get", "--json", key], timeout=30), f"sbx settings get {key}")
    return record.get("value") if isinstance(record, dict) else None


def readiness_problems() -> list[str]:
    """Why sandboxes cannot be created here (empty when they can)."""
    if sbx_binary() is None:
        return [SBX_MISSING_MESSAGE]
    listing = run(["ls", "--json"], timeout=60)
    if listing.returncode != 0:
        detail = _first_line(listing.stderr or listing.stdout)
        hint = " (run `sbx login`)" if "sign" in detail.casefold() else ""
        return [f"sbx is not ready: {detail}{hint}"]
    problems: list[str] = []
    for key, wanted in REQUIRED_SETTINGS.items():
        value = setting(key)
        if value != wanted:
            text = json.dumps(wanted)
            problems.append(
                f"sbx setting {key} is {json.dumps(value)}; it must be {text}: "
                f"`sbx settings set {key} {text}` then `sbx daemon restart`"
            )
    return problems


def setting_warnings() -> list[str]:
    warnings: list[str] = []
    for key, (wanted, why) in ADVISED_SETTINGS.items():
        try:
            value = setting(key)
        except SbxError:
            continue
        if value != wanted:
            text = json.dumps(wanted)
            warnings.append(
                f"sbx setting {key} is {json.dumps(value)}: it {why}. "
                f"Consider `sbx settings set {key} {text}`."
            )
    return warnings


def templates() -> list[dict[str, Any]]:
    listing = _json(run(["template", "ls", "--json"], timeout=60), "sbx template ls")
    return [t for t in listing or [] if isinstance(t, dict)]


def template_id(image: str) -> str | None:
    """Short image ID of the loaded template for ``image`` (None: not loaded)."""
    repository, tag = template_ref(image)
    for template in templates():
        if template.get("repository") == repository and template.get("tag") == tag:
            return str(template.get("id") or "") or None
    return None


def build_template(image: str, template: str, *, docker: str = "docker") -> None:
    """Tag ``template`` as ``image`` plus ``TEMPLATE_LABEL`` (a local build, no pull)."""
    dockerfile = f"FROM {image}\nLABEL {TEMPLATE_LABEL}\n"
    built = subprocess.run(
        [docker, "build", "--quiet", "--tag", template, "-"],
        input=dockerfile,
        capture_output=True,
        text=True,
        check=False,
    )
    if built.returncode != 0:
        raise SbxError(f"building {template} from {image} failed: {_first_line(built.stderr)}")


def load_template(image: str, *, docker: str = "docker") -> None:
    """Copy a local Docker image into sbx's image store (``docker save`` + ``sbx template
    load``); sbx cannot pull images that exist only in Docker's store."""
    with tempfile.TemporaryDirectory(prefix="agenteval-sbx-") as tmp:
        tar = Path(tmp) / "image.tar"
        saved = subprocess.run(
            [docker, "save", "--output", str(tar), image], capture_output=True, text=True
        )
        if saved.returncode != 0:
            raise SbxError(f"docker save {image} failed: {_first_line(saved.stderr)}")
        loaded = run(["template", "load", str(tar)], timeout=1800)
        if loaded.returncode != 0:
            raise SbxError(f"sbx template load failed: {_first_line(loaded.stderr)}")


def sandboxes() -> list[dict[str, Any]]:
    listing = _json(run(["ls", "--json"], timeout=60), "sbx ls")
    return [s for s in listing or [] if isinstance(s, dict)]


def remove(names: list[str]) -> list[str]:
    """Remove sandboxes by name; returns those removed."""
    removed: list[str] = []
    for name in names:
        result = run(["rm", "--force", name], timeout=300)
        if result.returncode == 0:
            removed.append(name)
    return removed


def cleanup(name: str | None = None) -> list[str]:
    """Remove ``name``, or every per-sample sandbox (``agenteval-sbx-<8 hex>``)."""
    if name is not None:
        return remove([name])
    return remove([str(s["name"]) for s in sandboxes() if is_sample_sandbox(str(s.get("name")))])


def image_env_names(image: str, *, docker: str = "docker") -> frozenset[str]:
    """Names in the image's own ``ENV`` (empty when Docker cannot tell)."""
    try:
        result = subprocess.run(
            [docker, "image", "inspect", "--format", "{{json .Config.Env}}", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    try:
        entries = json.loads(result.stdout or "null") or []
    except json.JSONDecodeError:
        return frozenset()
    return frozenset(str(e).split("=", 1)[0] for e in entries if isinstance(e, str))
