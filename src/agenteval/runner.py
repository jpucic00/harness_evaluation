"""Logic behind the ``agenteval`` CLI.

Everything here works without click so it can be tested directly; ``cli.py`` only
parses arguments, prints and maps errors to exit codes. Heavy modules (Inspect
tasks, sandbox providers, the report builder) are imported lazily so that
``profiles`` and ``run --dry-run`` stay fast and work even when an optional
provider is unavailable.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, get_args

from agenteval.config import (
    ConfigError,
    EnvironmentProfile,
    MatrixCell,
    ModelProfile,
    ProfileSet,
    RunConfig,
    SuiteName,
    expand_matrix,
    load_profiles,
    load_run,
    missing_env,
)

if TYPE_CHECKING:
    from inspect_ai import Task
    from inspect_ai.log import EvalLog

Echo = Callable[[str], None]

DEFAULT_LOG_DIR = Path("logs")
DEFAULT_REPORT_DIR = Path("reports")
SUITE_NAMES: tuple[str, ...] = get_args(SuiteName)
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
LATEST = "latest"

# Path fragment of the compose files written by environments.docker; used to
# pick out agenteval's Docker projects among all Inspect compose projects.
COMPOSE_DIR_MARKER = "/.agenteval/compose/"

# Ubuntu 23.10+ hosts: 1 = unconfined processes (containers included) get no capabilities
# in new user namespaces, which breaks bubblewrap in Docker containers.
USERNS_RESTRICT_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def _echo(message: str) -> None:
    print(message)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row, strict=True)]
    lines = [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in [list(headers), *rows]
    ]
    return "\n".join(lines)


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def load_dotenv_file() -> Path | None:
    """Load ``.env`` (searched upward from the cwd, like Inspect) into ``os.environ``.

    Variables already set in the shell win. Returns the file loaded, or None. Values
    are never read out or printed.
    """
    from dotenv import find_dotenv, load_dotenv

    found = find_dotenv(usecwd=True)
    if not found:
        return None
    load_dotenv(found, override=False)
    return Path(found)


# --------------------------------------------------------------------------- run ids


def make_run_id(run_name: str, now: datetime | None = None) -> str:
    """``<UTC timestamp>-<run name>``; sortable and safe as a directory name."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", run_name).strip("-.") or "run"
    return f"{stamp}-{name}"[:128]


def validate_run_id(run_id: str) -> str:
    """Run ids become directory names under the log/report roots: one safe component only."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ConfigError(
            f"invalid run id {run_id!r}: use letters, digits, '.', '_' or '-' "
            "(max 128 chars, must start with a letter or digit)"
        )
    return run_id


def resolve_run_dir(root: Path, run_id: str = LATEST, *, marker: str | None = None) -> Path:
    """Directory of one run under ``root``; ``latest`` = most recently modified.

    ``marker`` restricts ``latest`` to directories containing that file.
    """
    if run_id != LATEST:
        path = root / validate_run_id(run_id)
        if not path.is_dir():
            raise ConfigError(f"run '{run_id}' not found in {root}")
        return path
    candidates = (
        [p for p in root.iterdir() if p.is_dir() and (marker is None or (p / marker).is_file())]
        if root.is_dir()
        else []
    )
    if not candidates:
        raise ConfigError(f"no runs found in {root}")

    def modified(p: Path) -> tuple[float, str]:
        return ((p / marker) if marker else p).stat().st_mtime, p.name

    return max(candidates, key=modified)


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class RunPlan:
    run: RunConfig
    run_file: Path
    cells: list[MatrixCell]
    run_id: str
    log_root: Path
    report_root: Path
    limit: int | None
    max_samples: int
    max_sandboxes: int

    @property
    def log_dir(self) -> Path:
        return self.log_root / self.run_id

    @property
    def report_dir(self) -> Path:
        return self.report_root / self.run_id

    @property
    def models(self) -> list[ModelProfile]:
        models = {cell.model.name: cell.model for cell in self.cells}
        for cell in self.cells:
            if cell.subagent_model is not None:
                models.setdefault(cell.subagent_model.name, cell.subagent_model)
        return list(models.values())

    def missing_env(self) -> list[str]:
        return missing_env(self.models)


def resolve_run_file(run_file: str | Path, config_dir: Path) -> Path:
    """Accept a path or a bare run name (``smoke-docker`` -> ``config/runs/smoke-docker.yaml``)."""
    path = Path(run_file)
    if path.is_file():
        return path
    for suffix in (".yaml", ".yml"):
        candidate = config_dir / "runs" / f"{run_file}{suffix}"
        if candidate.is_file():
            return candidate
    raise ConfigError(f"run file not found: {run_file} (also looked in {config_dir / 'runs'})")


def apply_overrides(
    run: RunConfig,
    *,
    models: Sequence[str] = (),
    harnesses: Sequence[str] = (),
    environments: Sequence[str] = (),
    suites: Sequence[str] = (),
) -> RunConfig:
    """Replace axis lists; overridden suites keep the options the run file gives them."""
    data = run.model_dump()
    if models:
        data["models"] = _dedupe(models)
    if harnesses:
        data["harnesses"] = _dedupe(harnesses)
    if environments:
        data["environments"] = _dedupe(environments)
    if suites:
        configured: dict[str, dict[str, object]] = {s.name: s.model_dump() for s in run.suites}
        data["suites"] = [configured.get(name, {"name": name}) for name in _dedupe(suites)]
    try:
        return RunConfig.model_validate(data)
    except Exception as ex:
        raise ConfigError(f"run '{run.name}': invalid override: {ex}") from ex


def plan_run(
    run_file: str | Path,
    *,
    config_dir: Path,
    models: Sequence[str] = (),
    harnesses: Sequence[str] = (),
    environments: Sequence[str] = (),
    suites: Sequence[str] = (),
    limit: int | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> RunPlan:
    path = resolve_run_file(run_file, config_dir)
    run = apply_overrides(
        load_run(path),
        models=models,
        harnesses=harnesses,
        environments=environments,
        suites=suites,
    )
    if limit is not None and limit < 1:
        raise ConfigError("--limit must be at least 1")
    cells = expand_matrix(run, load_profiles(config_dir))
    if not cells:
        raise ConfigError(f"run '{run.name}': the matrix is empty")
    # eval_set takes one value for all tasks, so use the most constrained environment:
    # a Tart host that can boot 2 VMs must not be asked for 4 because Docker could.
    concurrency = min(cell.environment.max_concurrent for cell in cells)
    return RunPlan(
        run=run,
        run_file=path,
        cells=cells,
        run_id=validate_run_id(run_id) if run_id else make_run_id(run.name, now),
        log_root=Path(run.log_dir),
        report_root=Path(run.report_dir),
        limit=limit,
        max_samples=run.max_samples or concurrency,
        max_sandboxes=concurrency,
    )


def format_plan(plan: RunPlan) -> str:
    run = plan.run
    limits = ", ".join(f"{k}={v}" for k, v in run.limits.model_dump().items() if v is not None)
    pairs = [
        ("run", f"{run.name}  ({plan.run_file})"),
        ("run id", plan.run_id),
        ("logs", str(plan.log_dir)),
        ("report", str(plan.report_dir)),
        (
            "eval_set",
            f"tasks={len(plan.cells)} epochs={run.epochs} limit={plan.limit or '-'} "
            f"max_tasks={run.max_tasks} max_samples={plan.max_samples} "
            f"max_sandboxes={plan.max_sandboxes} retry_attempts={run.retry_attempts} "
            f"fail_on_error={run.fail_on_error}",
        ),
        ("limits", limits or "-"),
    ]
    header = "\n".join(f"{key:<10}{value}" for key, value in pairs)
    rows = [
        [
            str(i),
            cell.suite.name,
            json.dumps(cell.suite.options, sort_keys=True) if cell.suite.options else "-",
            cell.model.name
            + (f" (workers: {cell.subagent_model.name})" if cell.subagent_model else ""),
            cell.harness.name,
            cell.environment.name,
            cell.task_name,
        ]
        for i, cell in enumerate(plan.cells, start=1)
    ]
    matrix = _table(["#", "suite", "options", "model", "harness", "environment", "task"], rows)
    return f"{header}\n\n{matrix}"


# --------------------------------------------------------------------------- execution


def register_model_prices(models: Sequence[ModelProfile]) -> list[str]:
    """Teach Inspect each model's price so samples carry a cost. Returns notes to print."""
    from inspect_ai.model import ModelCost, ModelInfo, get_model_info, set_model_info

    notes: list[str] = []
    for model in models:
        if model.pricing is None:
            notes.append(f"model '{model.name}': no pricing configured; cost is not tracked")
            continue
        # Unset cache prices fall back to the input price: cached tokens are never free
        # unless the profile says so.
        cost = ModelCost(
            input=model.pricing.input,
            output=model.pricing.output,
            input_cache_read=model.pricing.cache_read_rate(),
            input_cache_write=model.pricing.cache_write_rate(),
        )
        try:
            existing = get_model_info(model.inspect_model)
        except Exception:
            existing = None
        info = existing.model_copy(update={"cost": cost}) if existing else ModelInfo(cost=cost)
        try:
            set_model_info(model.inspect_model, info)
        except Exception:
            if model.inspect_model.startswith("mockllm/"):
                continue
            raise
    return notes


def build_tasks(cells: Sequence[MatrixCell]) -> list[Task]:
    from agenteval.suites import build_task

    return [build_task(cell) for cell in cells]


def _log_summary(log: EvalLog) -> str:
    samples = log.results.completed_samples if log.results else 0
    total = log.results.total_samples if log.results else 0
    line = f"  {log.status:<9} {log.eval.task}  samples {samples}/{total}"
    if log.error:
        line += f"  error: {log.error.message.splitlines()[0] if log.error.message else ''}"
    return line


INDEX_HREF = "../index.html"
"""Link from a run report (``<report-root>/<run-id>/``) back to the all-runs index."""


def build_run_report(plan: RunPlan, *, echo: Echo = _echo) -> Path | None:
    from agenteval.report import build_index, build_report

    if not plan.log_dir.is_dir():
        echo(f"no logs in {plan.log_dir}; skipping the report")
        return None
    path: Path = build_report(
        plan.log_dir,
        plan.report_dir,
        title=f"{plan.run.name} ({plan.run_id})",
        index_href=INDEX_HREF,
    )
    build_index(plan.report_root)
    return path


def provision_sha256() -> str | None:
    """sha256 of the provision.sh shipped with this agenteval (images record theirs too)."""
    try:
        return hashlib.sha256(provision_script().read_bytes()).hexdigest()
    except OSError:
        return None


def environment_identity(env: EnvironmentProfile) -> dict[str, str | None]:
    """What results depend on beyond the profile: the built image and its provisioning.

    Base images are pinned by (mutable) tag, so the Docker image ID is what ties results
    to the exact image. A built image's own provision.sh hash is in
    /etc/agenteval/image.json inside it.
    """
    identity: dict[str, str | None] = {
        "provider": env.provider,
        "provision_sha256": provision_sha256(),
    }
    if env.provider == "docker" and env.docker is not None:
        from agenteval.environments.docker import image_id

        identity["image"] = env.docker.image
        identity["image_id"] = image_id(env.docker.image)
    elif env.provider == "tart" and env.tart is not None:
        identity["image"] = env.tart.image
        identity["base_image"] = env.tart.base_image
    return identity


def run_metadata(plan: RunPlan) -> dict[str, Any]:
    environments = {cell.environment.name: cell.environment for cell in plan.cells}
    return {
        "agenteval_run_id": plan.run_id,
        "agenteval_run": plan.run.name,
        "agenteval_environments": {
            name: environment_identity(env) for name, env in environments.items()
        },
    }


def view_hint(plan: RunPlan) -> str:
    hint = f"agenteval view {plan.run_id}"
    if plan.log_root != DEFAULT_LOG_DIR:
        hint += f" --log-dir {shlex.quote(str(plan.log_root))}"
    return hint


def risk_warnings(cells: Sequence[MatrixCell]) -> list[str]:
    """Warn where an upstream scorer runs agent-written code as root in a relaxed container.

    CodeIPI's ipi_scorer runs the workspace tests as root. In a Docker cell with the bwrap
    relaxations (no gVisor runtime) container root can write global /proc/sys entries, so a
    real model there is only safe on a disposable Docker host.
    """
    warnings: list[str] = []
    for cell in cells:
        env = cell.environment
        if (
            cell.suite.name == "injection"
            and env.provider == "docker"
            and env.docker is not None
            and env.docker.runtime is None
            and cell.harness.inner_sandbox.type == "bwrap"
            and not cell.model.inspect_model.startswith("mockllm/")
        ):
            warnings.append(
                f"WARNING {cell.task_name}: CodeIPI's scorer runs agent-written tests as root in a "
                "Docker container relaxed for bubblewrap. Use a Tart environment, a gVisor runtime "
                "(docker.runtime: runsc) or a disposable Docker host for real models."
            )
    return warnings


def execute_run(plan: RunPlan, *, report: bool = True, echo: Echo = _echo) -> int:
    """Run the matrix with ``inspect_ai.eval_set`` and build the report. Returns an exit code."""
    load_dotenv_file()
    missing = plan.missing_env()
    if missing:
        raise ConfigError(
            "missing required environment variables: "
            + ", ".join(missing)
            + " (set them in the shell that runs agenteval or in a .env file)"
        )

    from inspect_ai import eval_set

    for warning in risk_warnings(plan.cells):
        echo(warning)
    for note in register_model_prices(plan.models):
        echo(note)
    tasks = build_tasks(plan.cells)
    plan.log_dir.mkdir(parents=True, exist_ok=True)

    success = False
    try:
        success, logs = eval_set(
            tasks,
            log_dir=str(plan.log_dir),
            max_tasks=plan.run.max_tasks,
            max_samples=plan.max_samples,
            max_sandboxes=plan.max_sandboxes,
            retry_attempts=plan.run.retry_attempts,
            fail_on_error=plan.run.fail_on_error,
            limit=plan.limit,
            # Keep logs of failed attempts: the report adds up their usage, which the
            # retried log does not carry.
            retry_cleanup=False,
            tags=["agenteval", plan.run.name],
            metadata=run_metadata(plan),
        )
        echo(f"eval set {'completed' if success else 'did not complete'}: {plan.log_dir}")
        for log in logs:
            echo(_log_summary(log))
    except Exception as ex:
        echo(f"eval_set failed: {type(ex).__name__}: {ex}")
        echo(traceback.format_exc())

    code = 0 if success else 1
    if report:
        try:
            path = build_run_report(plan, echo=echo)
            if path is not None:
                echo(f"report: {path}")
        except Exception as ex:
            echo(f"report build failed: {type(ex).__name__}: {ex}")
            echo(traceback.format_exc())
            code = 1
    echo(f"view transcripts: {view_hint(plan)}")
    return code


# --------------------------------------------------------------------------- reports


def report_from_logs(
    log_dir: Path,
    *,
    out: Path | None = None,
    report_root: Path = DEFAULT_REPORT_DIR,
    title: str | None = None,
) -> Path:
    """Build a report for ``log_dir``; refresh the index when it lands under ``report_root``."""
    from agenteval.report import build_index, build_report

    if not log_dir.is_dir():
        raise ConfigError(f"log directory not found: {log_dir}")
    target = out if out is not None else report_root / log_dir.resolve().name
    in_root = target.resolve().parent == report_root.resolve()
    path: Path = build_report(
        log_dir, target, title=title, index_href=INDEX_HREF if in_root else None
    )
    if in_root:
        build_index(report_root)
    return path


def report_page(report_root: Path, run_id: str = LATEST) -> Path:
    if run_id == "index":
        page = report_root / "index.html"
    else:
        page = resolve_run_dir(report_root, run_id, marker="index.html") / "index.html"
    if not page.is_file():
        raise ConfigError(f"report not found: {page}")
    return page


def open_path(path: Path) -> bool:
    """Open with the macOS ``open`` command; False when not available (caller prints the path)."""
    if sys.platform != "darwin" or shutil.which("open") is None:
        return False
    return subprocess.run(["open", str(path)], check=False).returncode == 0


def make_report_server(report_root: Path, port: int) -> ThreadingHTTPServer:
    """Static file server for ``report_root`` bound to 127.0.0.1 only."""
    if not report_root.is_dir():
        raise ConfigError(f"report directory not found: {report_root}")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(report_root.resolve()))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def view_command(log_root: Path, run_id: str = LATEST) -> list[str]:
    run_dir = resolve_run_dir(log_root, run_id)
    return [sys.executable, "-m", "inspect_ai", "view", "--log-dir", str(run_dir)]


# --------------------------------------------------------------------------- profiles


def _run_files(config_dir: Path) -> list[Path]:
    runs = config_dir / "runs"
    return sorted([*runs.glob("*.yaml"), *runs.glob("*.yml")]) if runs.is_dir() else []


def describe_profiles(config_dir: Path) -> str:
    profiles = load_profiles(config_dir)
    sections: list[str] = []

    def price(m: ModelProfile) -> str:
        return f"{m.pricing.input:g}/{m.pricing.output:g}" if m.pricing else "-"

    sections.append(
        "Models (pricing: USD per 1M input/output tokens)\n"
        + _table(
            ["name", "inspect model", "pricing", "required env", "description"],
            [
                [
                    m.name,
                    m.inspect_model or f"- ({m.access})",
                    price(m),
                    ",".join(m.required_env) or "-",
                    m.description,
                ]
                for m in profiles.models.values()
            ],
        )
    )
    sections.append(
        "Harnesses\n"
        + _table(
            ["name", "agent", "inner sandbox", "description"],
            [
                [
                    h.name,
                    f"{h.agent}@{h.version}",
                    h.inner_sandbox.type
                    + (f" ({h.inner_sandbox.network})" if h.inner_sandbox.type != "none" else ""),
                    h.description,
                ]
                for h in profiles.harnesses.values()
            ],
        )
    )
    sections.append(
        "Environments\n"
        + _table(
            ["name", "provider", "image", "egress", "secrets", "concurrency", "description"],
            [
                [
                    e.name,
                    e.provider,
                    _env_image(e),
                    e.egress,
                    "planted" if e.ambient_secrets else "none",
                    str(e.max_concurrent),
                    e.description,
                ]
                for e in profiles.environments.values()
            ],
        )
    )
    run_rows: list[list[str]] = []
    for path in _run_files(config_dir):
        try:
            run = load_run(path)
            cells = len(expand_matrix(run, profiles))
            matrix = (
                f"{len(run.models)}m x {len(run.harnesses)}h x {len(run.environments)}e x "
                f"{len(run.suites)}s = {cells} tasks"
            )
            run_rows.append([run.name, str(path), matrix, run.description])
        except ConfigError as ex:
            run_rows.append([path.stem, str(path), "INVALID", str(ex)])
    sections.append("Runs\n" + _table(["name", "file", "matrix", "description"], run_rows))
    return "\n\n".join(sections)


def _env_image(env: EnvironmentProfile) -> str:
    if env.provider == "tart" and env.tart is not None:
        return env.tart.image
    if env.provider == "docker" and env.docker is not None:
        return env.docker.image + (f" (runtime {env.docker.runtime})" if env.docker.runtime else "")
    return "-"


# --------------------------------------------------------------------------- environments


def provision_script() -> Path:
    return Path(__file__).parent / "environments" / "provision" / "provision.sh"


def get_environment(config_dir: Path, name: str) -> EnvironmentProfile:
    environments = load_profiles(config_dir).environments
    if name not in environments:
        raise ConfigError(f"unknown environment '{name}'; available: {sorted(environments)}")
    return environments[name]


def build_environment(env: EnvironmentProfile, *, force: bool = False) -> None:
    if env.provider == "docker":
        from agenteval.environments.docker import build_image

        build_image(env, force=force)
    else:
        from agenteval.environments.tart.image import build_image as build_tart_image

        if env.tart is None:
            raise ConfigError(f"environment '{env.name}' has no tart settings")
        script = provision_script()
        if not script.is_file():
            raise ConfigError(f"provision script missing: {script}")
        asyncio.run(
            build_tart_image(
                env.tart,
                script,
                force=force,
                agent_user=env.agent_user,
                workspace=env.workspace,
            )
        )


CommandRunner = Callable[[Sequence[str], float], tuple[int, str]]


def run_command(cmd: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run without a shell; (returncode, combined output). 127 = not found, 124 = timeout."""
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout:g}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _inspect_cmd(*args: str) -> list[str]:
    return [sys.executable, "-m", "inspect_ai", *args]


def agenteval_docker_projects(runner: CommandRunner = run_command) -> list[str]:
    """Inspect compose projects created from agenteval's generated compose files."""
    code, out = runner(["docker", "compose", "ls", "--all", "--format", "json"], 30)
    if code != 0:
        raise ConfigError(f"docker compose ls failed: {out}")
    try:
        projects = json.loads(out or "[]")
    except json.JSONDecodeError as ex:
        raise ConfigError(f"docker compose ls: unexpected output: {ex}") from ex
    names: list[str] = []
    for project in projects if isinstance(projects, list) else []:
        if not isinstance(project, dict):
            continue
        name, config_files = project.get("Name"), project.get("ConfigFiles")
        if isinstance(name, str) and isinstance(config_files, str):
            if COMPOSE_DIR_MARKER in config_files.replace("\\", "/"):
                names.append(name)
    return names


def cleanup_environments(*, echo: Echo = _echo, runner: CommandRunner = run_command) -> int:
    """Delete leftover agenteval Tart VMs and Docker compose projects. Returns an exit code."""
    code = 0
    if shutil.which("tart"):
        echo("tart: deleting leftover agenteval VMs")
        rc, out = runner(_inspect_cmd("sandbox", "cleanup", "tart"), 600)
        if out:
            echo(out)
        code = 1 if rc else 0
    else:
        echo("tart: not installed, skipped")
    if shutil.which("docker"):
        try:
            projects = agenteval_docker_projects(runner)
        except ConfigError as ex:
            echo(f"docker: {ex}")
            return 1
        if not projects:
            echo("docker: no agenteval compose projects")
        for name in projects:
            echo(f"docker: removing compose project {name}")
            rc, out = runner(_inspect_cmd("sandbox", "cleanup", "docker", name), 300)
            if rc:
                echo(out)
                code = 1
    else:
        echo("docker: not installed, skipped")
    return code


# --------------------------------------------------------------------------- doctor

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def dhcp_lease_check(plist: Path | None = None) -> Check:
    """Tart's nat network uses macOS bootpd, whose 1-day leases run out after ~253 VMs/day."""
    from agenteval.environments.tart.image import (
        DHCP_PLIST,
        dhcp_lease_problem,
        dhcp_lease_seconds,
    )

    path = plist or DHCP_PLIST
    problem = dhcp_lease_problem(path)
    if problem:
        return Check("tart dhcp lease", "warn", problem)
    return Check("tart dhcp lease", "ok", f"DHCPLeaseTimeSecs={dhcp_lease_seconds(path)}")


def userns_restriction_check(docker_envs: Sequence[str], sysctl: Path | None = None) -> Check:
    """Linux Docker hosts that restrict unprivileged user namespaces break bwrap cells."""
    name = "docker bwrap userns"
    sysctl = sysctl or USERNS_RESTRICT_SYSCTL
    try:
        value = sysctl.read_text().strip()
    except OSError:
        return Check(name, "ok", f"{sysctl} not present (no AppArmor userns restriction)")
    if value != "1":
        return Check(name, "ok", f"kernel.apparmor_restrict_unprivileged_userns={value}")
    return Check(
        name,
        "warn",
        "kernel.apparmor_restrict_unprivileged_userns=1 on this host: bwrap cells in "
        f"{', '.join(docker_envs)} fail closed unless the host has an AppArmor profile "
        "granting userns to /usr/bin/bwrap, the sysctl is 0, or the environment uses "
        "runtime: runsc",
    )


def _tart_image_exists(name: str) -> bool:
    from agenteval.environments.tart.image import image_exists

    exists: bool = asyncio.run(image_exists(name))
    return exists


def doctor(
    config_dir: Path,
    env_name: str | None = None,
    *,
    runner: CommandRunner = run_command,
    tart_image_exists: Callable[[str], bool] = _tart_image_exists,
) -> list[Check]:
    """Check the host. Problems with the selected ``--env`` are failures, others warnings.

    Required env vars are reported by name only; their values are never read out.
    """
    checks: list[Check] = []
    add = checks.append

    py = sys.version_info
    add(
        Check(
            "python",
            "ok" if py >= (3, 12) else "fail",
            f"{py.major}.{py.minor}.{py.micro} ({sys.executable})",
        )
    )
    if shutil.which("uv"):
        rc, out = runner(["uv", "--version"], 15)
        add(Check("uv", "ok" if rc == 0 else "warn", _first_line(out)))
    else:
        add(Check("uv", "warn", "not on PATH (needed to install/sync the project)"))

    for dist in ("agenteval", "inspect-ai", "inspect-swe", "inspect-evals"):
        try:
            add(Check(dist, "ok", metadata.version(dist)))
        except metadata.PackageNotFoundError:
            add(Check(dist, "fail", "not installed (run `uv sync`)"))
    registered = any(ep.name == "agenteval" for ep in metadata.entry_points(group="inspect_ai"))
    add(
        Check(
            "inspect entry point",
            "ok" if registered else "warn",
            "agenteval sandboxes registered"
            if registered
            else "agenteval not registered with Inspect (reinstall with `uv sync`)",
        )
    )

    try:
        profiles: ProfileSet | None = load_profiles(config_dir)
    except ConfigError as ex:
        profiles = None
        add(Check("config", "fail", str(ex)))
    if profiles is None:
        return checks
    add(
        Check(
            "config",
            "ok",
            f"{config_dir}: {len(profiles.models)} models, {len(profiles.harnesses)} harnesses, "
            f"{len(profiles.environments)} environments",
        )
    )
    for path in _run_files(config_dir):
        try:
            cells = expand_matrix(load_run(path), profiles)
            add(Check(f"run {path.stem}", "ok", f"{len(cells)} tasks"))
        except ConfigError as ex:
            add(Check(f"run {path.stem}", "fail", str(ex)))

    if env_name is not None:
        if env_name not in profiles.environments:
            add(
                Check(
                    f"env {env_name}",
                    "fail",
                    f"unknown; available: {sorted(profiles.environments)}",
                )
            )
            return checks
        environments = [profiles.environments[env_name]]
    else:
        environments = list(profiles.environments.values())
    problem: Status = "fail" if env_name is not None else "warn"

    providers = {e.provider for e in environments}
    available: dict[str, bool] = {}
    if "docker" in providers:
        if shutil.which("docker"):
            rc, out = runner(["docker", "info", "--format", "{{.ServerVersion}}"], 20)
            available["docker"] = rc == 0
            detail = (
                f"daemon {_first_line(out)}"
                if rc == 0
                else f"daemon not reachable: {_first_line(out)}"
            )
            add(Check("docker", "ok" if rc == 0 else problem, detail))
        else:
            available["docker"] = False
            add(Check("docker", problem, "not on PATH"))
    if "tart" in providers:
        if sys.platform != "darwin":
            available["tart"] = False
            add(Check("tart", problem, "Tart needs a macOS host on Apple silicon"))
        elif shutil.which("tart"):
            rc, out = runner(["tart", "--version"], 15)
            available["tart"] = rc == 0
            add(Check("tart", "ok" if rc == 0 else problem, _first_line(out)))
        else:
            available["tart"] = False
            from agenteval.environments.tart.image import TART_RELEASES_URL

            add(
                Check(
                    "tart",
                    problem,
                    f"not on PATH; install the release tarball from {TART_RELEASES_URL} "
                    "(the Homebrew formula is broken)",
                )
            )

    for env in environments:
        label = f"env {env.name}"
        if not available.get(env.provider, False):
            add(Check(label, problem, f"{env.provider} unavailable; image not checked"))
            continue
        if env.provider == "docker" and env.docker is not None:
            rc, _ = runner(
                ["docker", "image", "inspect", "--format", "{{.Id}}", env.docker.image], 20
            )
            add(
                Check(
                    label,
                    "ok" if rc == 0 else problem,
                    f"image {env.docker.image} "
                    + ("present" if rc == 0 else f"missing (agenteval env build {env.name})"),
                )
            )
        elif env.provider == "tart" and env.tart is not None:
            try:
                exists = tart_image_exists(env.tart.image)
                add(
                    Check(
                        label,
                        "ok" if exists else problem,
                        f"VM image {env.tart.image} "
                        + ("present" if exists else f"missing (agenteval env build {env.name})"),
                    )
                )
            except Exception as ex:
                add(Check(label, problem, f"could not check VM image: {type(ex).__name__}: {ex}"))
            key = Path(env.tart.ssh_key).expanduser()
            if not key.is_file():
                add(Check(f"{label} ssh key", problem, f"{key} missing (created by env build)"))
            elif key.stat().st_mode & 0o077:
                add(
                    Check(f"{label} ssh key", "warn", f"{key} is group/world accessible; chmod 600")
                )
            else:
                add(Check(f"{label} ssh key", "ok", str(key)))

    if sys.platform == "darwin" and any(
        e.provider == "tart" and e.tart is not None and e.tart.network == "nat"
        for e in environments
    ):
        add(dhcp_lease_check())
    if sys.platform.startswith("linux") and any(
        h.inner_sandbox.type == "bwrap" for h in profiles.harnesses.values()
    ):
        runc_envs = [
            e.name
            for e in environments
            if e.provider == "docker" and e.docker is not None and not e.docker.runtime
        ]
        if runc_envs:
            add(userns_restriction_check(runc_envs))

    dotenv = load_dotenv_file()
    if dotenv is not None:
        add(Check(".env", "ok", f"loaded {dotenv} (shell variables take precedence)"))
    for model in profiles.models.values():
        for var in model.required_env:
            unset = var in missing_env([model])
            add(
                Check(
                    f"env var {var}",
                    "warn" if unset else "ok",
                    f"{'not set' if unset else 'set'} (model {model.name})",
                )
            )
    return checks


def format_checks(checks: Sequence[Check]) -> str:
    return _table(
        ["status", "check", "detail"], [[c.status.upper(), c.name, c.detail] for c in checks]
    )
