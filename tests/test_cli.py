"""CLI, run, doctor and environment tests. No real sandboxes, no paid models: suites are stubbed."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sys
import threading
import types
import urllib.request
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from inspect_ai import Task
from inspect_ai.dataset import Sample
from inspect_ai.model import get_model, get_model_info
from inspect_ai.model._model_info import clear_model_info_cache
from inspect_ai.scorer import match
from inspect_ai.solver import Generate, Solver, TaskState, generate, solver

from agenteval import doctor as host_doctor
from agenteval import environments
from agenteval.cli import main
from agenteval.config import (
    ConfigError,
    EnvironmentProfile,
    MatrixCell,
    ModelProfile,
    Pricing,
    TartSettings,
    load_run,
)
from agenteval.environments.tart import provider as tart_provider
from agenteval.report import serve
from agenteval.run import execute as run_execute
from agenteval.run import plan as run_plan

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config"
SECRET = "sk-agenteval-test-not-a-real-key"


def invoke(*args: str) -> Result:
    return CliRunner().invoke(main, ["--config-dir", str(CONFIG), *args])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Relative log/report dirs land in tmp; Inspect stays quiet; model prices are reset."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INSPECT_DISPLAY", "none")
    yield
    clear_model_info_cache()


# --------------------------------------------------------------------------- stubs


def _tiny_task(cell: MatrixCell, solvers: Sequence[Solver] | None = None) -> Task:
    return Task(
        name=cell.task_name,
        dataset=[Sample(input="say hi", target="hi")],
        solver=list(solvers) if solvers else [generate()],
        scorer=match(),
        model=get_model(cell.model.inspect_model),
        metadata=cell.metadata(),
    )


@solver
def _explode() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        raise RuntimeError("boom")

    return solve


class ReportStub:
    def __init__(self) -> None:
        self.reports: list[tuple[Path, Path, str | None]] = []
        self.indexes: list[Path] = []
        self.index_hrefs: list[str | None] = []

    def build_report(
        self,
        log_dir: Path,
        out_dir: Path,
        *,
        title: str | None = None,
        index_href: str | None = None,
    ) -> Path:
        self.reports.append((log_dir, out_dir, title))
        self.index_hrefs.append(index_href)
        out_dir.mkdir(parents=True, exist_ok=True)
        page = out_dir / "index.html"
        page.write_text("<html></html>")
        return page

    def build_index(self, report_root: Path) -> Path:
        self.indexes.append(report_root)
        return report_root / "index.html"


@pytest.fixture
def report_stub(monkeypatch: pytest.MonkeyPatch) -> ReportStub:
    stub = ReportStub()
    monkeypatch.setattr("agenteval.report.build_report", stub.build_report, raising=False)
    monkeypatch.setattr("agenteval.report.build_index", stub.build_index, raising=False)
    return stub


@pytest.fixture
def built_cells(monkeypatch: pytest.MonkeyPatch) -> list[MatrixCell]:
    """Replace suites.build_task with a one-sample mockllm task; records the cells."""
    cells: list[MatrixCell] = []

    def build_task(cell: MatrixCell) -> Task:
        cells.append(cell)
        return _tiny_task(cell)

    monkeypatch.setattr("agenteval.suites.build_task", build_task, raising=False)
    return cells


# --------------------------------------------------------------------------- profiles


def test_profiles_lists_every_profile() -> None:
    result = invoke("profiles")
    assert result.exit_code == 0, result.output
    for name in [
        "mock",
        "qwen38-27b",
        "opencode-none",
        "opencode-bwrap",
        "docker-ubuntu",
        "tart-ubuntu",
        "smoke-docker",
        "opencode-qwen-tart",
    ]:
        assert name in result.output
    assert "1m x 2h x 1e x 3s = 6 tasks" in result.output


def test_profiles_marks_invalid_run(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    shutil.copytree(CONFIG, config)
    (config / "runs" / "broken.yaml").write_text(
        "models: [nope]\nharnesses: [opencode-none]\nenvironments: [docker-ubuntu]\n"
        "suites: [containment]\n"
    )
    result = CliRunner().invoke(main, ["--config-dir", str(config), "profiles"])
    assert result.exit_code == 0, result.output
    assert "INVALID" in result.output and "unknown models" in result.output


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ("{session_titel: x}", "unknown options ['session_titel'] for inspect_swe opencode()"),
        ("{version: latest}", "options ['version'] are set by agenteval"),
        ("{session_title: x, attempts: 2}", None),
    ],
)
def test_plan_checks_harness_options(tmp_path: Path, options: str, error: str | None) -> None:
    config = tmp_path / "cfg"
    shutil.copytree(CONFIG, config)
    harness = (config / "harnesses" / "opencode-none.yaml").read_text()
    (config / "harnesses" / "opts.yaml").write_text(f"{harness}options: {options}\n")
    if error is None:
        plan = run_plan.plan_run("smoke-docker", config_dir=config, harnesses=["opts"])
        assert plan.cells[0].harness.options
        return
    with pytest.raises(ConfigError, match="harness 'opts'") as raised:
        run_plan.plan_run("smoke-docker", config_dir=config, harnesses=["opts"])
    assert error in str(raised.value)
    if "unknown" in error:
        assert "accepted: " in str(raised.value) and "session_title" in str(raised.value)
        assert "version" not in str(raised.value).split("accepted: ")[1].split(", ")


def test_profiles_bad_config_is_a_clean_error(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    (config / "models").mkdir(parents=True)
    (config / "models" / "bad.yaml").write_text("inspect_model: x\nunknown_field: 1\n")
    result = CliRunner().invoke(main, ["--config-dir", str(config), "profiles"])
    assert result.exit_code == 1
    assert "Error:" in result.output and "bad.yaml" in result.output


# --------------------------------------------------------------------------- run --dry-run


def test_run_dry_run_prints_matrix(tmp_path: Path) -> None:
    result = invoke("run", "smoke-docker", "--dry-run")
    assert result.exit_code == 0, result.output
    run = load_run(CONFIG / "runs" / "smoke-docker.yaml")
    assert run.name == "smoke-docker"
    for suite in ["containment", "capability", "injection"]:
        for harness in ["opencode-none", "opencode-bwrap"]:
            assert f"{suite}--{harness}--docker-ubuntu--mock" in result.output
    assert "tasks=6" in result.output
    assert "max_samples=4" in result.output  # docker-ubuntu max_concurrent
    assert "dry run: nothing executed" in result.output
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "reports").exists()


def test_run_dry_run_overrides_replace_axes() -> None:
    result = invoke(
        "run",
        str(CONFIG / "runs" / "smoke-docker.yaml"),
        "--harness",
        "opencode-bwrap",
        "--suite",
        "capability",
        "--suite",
        "containment",
        "--limit",
        "1",
        "--run-id",
        "my-run.1",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert "capability--opencode-bwrap--docker-ubuntu--mock" in result.output
    assert "containment--opencode-bwrap--docker-ubuntu--mock" in result.output
    assert "opencode-none" not in result.output
    assert "injection" not in result.output
    assert '{"limit": 2}' in result.output  # suite options from the run file are kept
    assert "limit=1" in result.output
    assert "logs/my-run.1" in result.output


def test_run_dry_run_uses_most_constrained_environment() -> None:
    result = invoke(
        "run", "smoke-docker", "--env", "docker-ubuntu", "--env", "tart-ubuntu", "--dry-run"
    )
    assert result.exit_code == 0, result.output
    assert "tasks=12" in result.output
    assert "max_samples=2 max_sandboxes=2" in result.output


def test_run_dry_run_warns_about_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = invoke("run", "opencode-qwen-tart", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "missing required environment variables: OPENROUTER_API_KEY" in result.output


def _unset_but_restored(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Unset ``name`` now and again after the test, even if a .env load sets it."""
    monkeypatch.setenv(name, "placeholder")
    monkeypatch.delenv(name)


def test_run_dry_run_reads_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_but_restored(monkeypatch, "OPENROUTER_API_KEY")
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={SECRET}\n")
    result = invoke("run", "opencode-qwen-tart", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "missing required environment variables" not in result.output
    assert SECRET not in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--env", "nope"], "unknown environments ['nope']"),
        (["--model", "nope"], "unknown models ['nope']"),
        (["--run-id", "../escape"], "invalid run id"),
        (["--run-id", ".hidden"], "invalid run id"),
    ],
)
def test_run_rejects_bad_input(args: list[str], message: str) -> None:
    result = invoke("run", "smoke-docker", *args, "--dry-run")
    assert result.exit_code == 1
    assert message in result.output


def test_run_rejects_unknown_suite_and_file() -> None:
    assert invoke("run", "smoke-docker", "--suite", "nope", "--dry-run").exit_code == 2
    result = invoke("run", "does-not-exist", "--dry-run")
    assert result.exit_code == 1
    assert "run file not found" in result.output


# --------------------------------------------------------------------------- run (executes)


def test_run_missing_env_fails_before_building_tasks(
    monkeypatch: pytest.MonkeyPatch, report_stub: ReportStub
) -> None:
    def build_task(cell: MatrixCell) -> Task:
        raise AssertionError("tasks must not be built when env vars are missing")

    monkeypatch.setattr("agenteval.suites.build_task", build_task, raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    result = invoke("run", "opencode-qwen-tart", "--run-id", "x")
    assert result.exit_code == 1
    assert "missing required environment variables: OPENROUTER_API_KEY" in result.output
    assert not report_stub.reports


def test_run_reads_dotenv_before_the_env_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_stub: ReportStub
) -> None:
    _unset_but_restored(monkeypatch, "OPENROUTER_API_KEY")
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={SECRET}\n")
    calls = _capture_eval_set(monkeypatch)
    monkeypatch.setattr("agenteval.suites.build_task", lambda cell: cell.task_name, raising=False)
    result = invoke("run", "opencode-qwen-tart", "--run-id", "dotenv", "--no-report")
    assert result.exit_code == 0, result.output
    assert "missing required environment variables" not in result.output
    assert SECRET not in result.output
    assert len(calls) == 1 and os.environ["OPENROUTER_API_KEY"] == SECRET


def _capture_eval_set(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace inspect_ai.eval_set with a recorder that reports success."""
    calls: list[dict[str, Any]] = []

    def eval_set(tasks: list[Any], **kwargs: Any) -> tuple[bool, list[Any]]:
        calls.append({"tasks": tasks, **kwargs})
        return True, []

    monkeypatch.setattr("inspect_ai.eval_set", eval_set)
    return calls


def test_run_keeps_failed_attempts_and_records_image_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_eval_set(monkeypatch)
    monkeypatch.setattr("agenteval.suites.build_task", lambda cell: cell.task_name, raising=False)
    monkeypatch.setattr(
        "agenteval.environments.docker.image_id", lambda image: f"sha256:id-of-{image}"
    )
    plan = run_plan.plan_run(
        "smoke-docker",
        config_dir=CONFIG,
        suites=["containment"],
        harnesses=["opencode-none"],
        run_id="meta",
    )
    assert run_execute.execute_run(plan, report=False, echo=lambda _: None) == 0
    (call,) = calls
    assert call["retry_cleanup"] is False
    assert call["retry_attempts"] == plan.run.retry_attempts
    script_hash = hashlib.sha256(environments.PROVISION_SCRIPT.read_bytes()).hexdigest()
    assert call["metadata"] == {
        "agenteval_run_id": "meta",
        "agenteval_run": "smoke-docker",
        "agenteval_environments": {
            "docker-ubuntu": {
                "provider": "docker",
                "provision_sha256": script_hash,
                "image": "agenteval/ubuntu-24.04:latest",
                "image_id": "sha256:id-of-agenteval/ubuntu-24.04:latest",
            }
        },
    }


def test_environment_identity_tart() -> None:
    env = EnvironmentProfile(name="vm", provider="tart")
    identity = environments.environment_identity(env)
    assert identity["provider"] == "tart"
    assert identity["image"] == "agenteval-ubuntu-24.04"
    assert identity["base_image"] == "ghcr.io/cirruslabs/ubuntu:24.04"
    assert identity["provision_sha256"] == environments.provision_sha256()
    assert "password" not in json.dumps(identity)


def test_view_hint_names_custom_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_file = tmp_path / "custom.yaml"
    run_file.write_text(
        "name: custom\nmodels: [mock]\nharnesses: [opencode-none]\n"
        "environments: [docker-ubuntu]\nsuites: [containment]\n"
        "log_dir: /data/agent logs\n"
    )
    plan = run_plan.plan_run(run_file, config_dir=CONFIG, run_id="r1")
    assert run_execute.view_hint(plan) == "agenteval view r1 --log-dir '/data/agent logs'"
    default = run_plan.plan_run("smoke-docker", config_dir=CONFIG, run_id="r2")
    assert run_execute.view_hint(default) == "agenteval view r2"

    _capture_eval_set(monkeypatch)
    monkeypatch.setattr("agenteval.suites.build_task", lambda cell: cell.task_name, raising=False)
    monkeypatch.setattr(run_plan.RunPlan, "log_dir", property(lambda self: tmp_path / "logs"))
    messages: list[str] = []
    run_execute.execute_run(plan, report=False, echo=messages.append)
    assert messages[-1] == "view transcripts: agenteval view r1 --log-dir '/data/agent logs'"


def test_run_executes_eval_set_and_builds_report(
    tmp_path: Path, built_cells: list[MatrixCell], report_stub: ReportStub
) -> None:
    result = invoke(
        "run",
        "smoke-docker",
        "--suite",
        "containment",
        "--harness",
        "opencode-none",
        "--run-id",
        "t1",
    )
    assert result.exit_code == 0, result.output
    assert [c.task_name for c in built_cells] == ["containment--opencode-none--docker-ubuntu--mock"]
    logs = list((tmp_path / "logs" / "t1").glob("*.eval"))
    assert len(logs) == 1
    assert report_stub.reports == [(Path("logs/t1"), Path("reports/t1"), "smoke-docker (t1)")]
    assert report_stub.indexes == [Path("reports")]
    assert "success" in result.output
    assert "agenteval view t1" in result.output

    from inspect_ai.log import read_eval_log

    log = read_eval_log(str(logs[0]), header_only=True)
    metadata = log.eval.metadata or {}
    assert metadata["agenteval_run_id"] == "t1" and metadata["agenteval_run"] == "smoke-docker"
    identity = metadata["agenteval_environments"]["docker-ubuntu"]
    assert identity["image"] == "agenteval/ubuntu-24.04:latest"
    assert len(identity["provision_sha256"]) == 64
    assert metadata["agenteval"]["harness"]["name"] == "opencode-none"  # task metadata kept
    assert "agenteval" in (log.eval.tags or [])
    # prices from config/models/mock.yaml were registered with Inspect
    info = get_model_info("mockllm/model")
    assert info is not None and info.cost is not None and info.cost.output == 0


def test_run_no_report(built_cells: list[MatrixCell], report_stub: ReportStub) -> None:
    result = invoke(
        "run",
        "smoke-docker",
        "--suite",
        "containment",
        "--harness",
        "opencode-none",
        "--run-id",
        "t2",
        "--no-report",
    )
    assert result.exit_code == 0, result.output
    assert report_stub.reports == []


def test_run_failure_is_nonzero_and_still_reports(
    monkeypatch: pytest.MonkeyPatch, report_stub: ReportStub
) -> None:
    monkeypatch.setattr(
        "agenteval.suites.build_task",
        lambda cell: _tiny_task(cell, [_explode()]),
        raising=False,
    )
    result = invoke(
        "run",
        "smoke-docker",
        "--suite",
        "containment",
        "--harness",
        "opencode-none",
        "--run-id",
        "t3",
    )
    assert result.exit_code == 1, result.output
    assert "did not complete" in result.output
    assert report_stub.reports and report_stub.reports[0][0] == Path("logs/t3")


def test_run_report_failure_is_nonzero(
    monkeypatch: pytest.MonkeyPatch, built_cells: list[MatrixCell]
) -> None:
    def broken(
        log_dir: Path, out_dir: Path, *, title: str | None = None, index_href: str | None = None
    ) -> Path:
        raise ValueError("bad template")

    monkeypatch.setattr("agenteval.report.build_report", broken, raising=False)
    monkeypatch.setattr("agenteval.report.build_index", lambda root: root, raising=False)
    result = invoke(
        "run",
        "smoke-docker",
        "--suite",
        "containment",
        "--harness",
        "opencode-none",
        "--run-id",
        "t4",
    )
    assert result.exit_code == 1
    assert "report build failed: ValueError: bad template" in result.output


# --------------------------------------------------------------------------- report


def test_report_build_default_location(tmp_path: Path, report_stub: ReportStub) -> None:
    (tmp_path / "logs" / "run-a").mkdir(parents=True)
    result = invoke("report", "build", "logs/run-a", "--title", "T")
    assert result.exit_code == 0, result.output
    assert report_stub.reports == [(Path("logs/run-a"), Path("reports/run-a"), "T")]
    assert report_stub.indexes == [Path("reports")]
    assert "reports/run-a/index.html" in result.output


def test_report_build_custom_out_skips_index(tmp_path: Path, report_stub: ReportStub) -> None:
    (tmp_path / "logs").mkdir()
    result = invoke("report", "build", "logs", "--out", "elsewhere/x")
    assert result.exit_code == 0, result.output
    assert report_stub.reports == [(Path("logs"), Path("elsewhere/x"), None)]
    assert report_stub.indexes == []


def test_report_build_missing_dir(report_stub: ReportStub) -> None:
    result = invoke("report", "build", "nope")
    assert result.exit_code == 1
    assert "log directory not found" in result.output


def test_report_build_real_builder_on_empty_dir(tmp_path: Path) -> None:
    import agenteval.report as report_module

    if not hasattr(report_module, "build_report"):
        pytest.skip("agenteval.report.build_report not implemented yet")
    (tmp_path / "logs" / "empty").mkdir(parents=True)
    result = invoke("report", "build", "logs/empty")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "reports" / "empty" / "index.html").is_file()


def _make_report(root: Path, run_id: str, mtime: float) -> Path:
    page = root / run_id / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(run_id)
    os.utime(page, (mtime, mtime))
    return page


def test_report_open_prints_path_when_open_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(serve, "open_path", lambda path: False)
    reports = tmp_path / "reports"
    _make_report(reports, "zz-old", 1_000)
    newest = _make_report(reports, "aa-new", 2_000)
    (reports / "index.html").write_text("index")
    (reports / "not-a-report").mkdir()

    result = invoke("report", "open")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(Path("reports") / newest.relative_to(reports))
    assert invoke("report", "open", "zz-old").output.strip() == "reports/zz-old/index.html"
    assert invoke("report", "open", "index").output.strip() == "reports/index.html"
    assert invoke("report", "open", "missing").exit_code == 1
    assert "invalid run id" in invoke("report", "open", "../reports").output


def test_report_open_uses_open_on_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Path] = []

    def fake_open(path: Path) -> bool:
        opened.append(path)
        return True

    monkeypatch.setattr(serve, "open_path", fake_open)
    _make_report(tmp_path / "reports", "r1", 1_000)
    result = invoke("report", "open", "r1")
    assert result.exit_code == 0 and result.output == ""
    assert opened == [Path("reports/r1/index.html")]


def test_report_open_without_reports() -> None:
    result = invoke("report", "open")
    assert result.exit_code == 1
    assert "no runs found" in result.output


def test_report_server_is_local_only(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "index.html").write_text("hello report")
    server = serve.make_report_server(tmp_path / "reports", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        # No proxies: a corporate http_proxy would otherwise intercept 127.0.0.1.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/index.html", timeout=5) as resp:
            assert resp.read() == b"hello report"
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(ConfigError):
        serve.make_report_server(tmp_path / "missing", 0)


# --------------------------------------------------------------------------- view


def test_view_runs_inspect_view_for_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 0

    def fake_run(cmd: list[str], check: bool) -> Done:
        calls.append(cmd)
        return Done()

    monkeypatch.setattr("agenteval.cli.subprocess.run", fake_run)
    for name, mtime in [("run-b", 1_000), ("run-a", 2_000)]:
        (tmp_path / "logs" / name).mkdir(parents=True)
        os.utime(tmp_path / "logs" / name, (mtime, mtime))

    result = invoke("view")
    assert result.exit_code == 0, result.output
    assert calls == [[sys.executable, "-m", "inspect_ai", "view", "--log-dir", "logs/run-a"]]
    assert invoke("view", "run-b").exit_code == 0
    assert calls[-1][-1] == "logs/run-b"
    assert invoke("view", "nope").exit_code == 1


# --------------------------------------------------------------------------- env


def test_env_build_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[str, bool]] = []

    def build_image(env: EnvironmentProfile, *, force: bool = False) -> None:
        built.append((env.name, force))

    monkeypatch.setattr(environments.docker, "build_image", build_image)
    result = invoke("env", "build", "docker-ubuntu", "--force")
    assert result.exit_code == 0, result.output
    assert built == [("docker-ubuntu", True)]
    assert invoke("env", "build", "docker-ubuntu").exit_code == 0
    assert built[-1] == ("docker-ubuntu", False)


def test_env_build_failure_is_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def build_image(env: EnvironmentProfile, *, force: bool = False) -> None:
        raise RuntimeError("docker build failed with exit code 1")

    monkeypatch.setattr(environments.docker, "build_image", build_image)
    result = invoke("env", "build", "docker-ubuntu")
    assert result.exit_code == 1
    assert "build failed: RuntimeError: docker build failed with exit code 1" in result.output


def test_env_build_tart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path, bool, str, str]] = []

    async def build_image(
        settings: TartSettings,
        provision_script: Path,
        *,
        force: bool,
        agent_user: str = "agent",
        workspace: str = "/workspace",
    ) -> None:
        calls.append((settings.image, provision_script, force, agent_user, workspace))

    fake = types.ModuleType("agenteval.environments.tart.image")
    fake.build_image = build_image  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agenteval.environments.tart.image", fake)
    script = tmp_path / "provision.sh"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(environments, "PROVISION_SCRIPT", script)

    result = invoke("env", "build", "tart-ubuntu", "--force")
    assert result.exit_code == 0, result.output
    assert calls == [("agenteval-ubuntu-24.04", script, True, "agent", "/workspace")]

    custom = EnvironmentProfile(name="vm", provider="tart", agent_user="dev", workspace="/work")
    environments.build_environment(custom)
    assert calls[-1] == ("agenteval-ubuntu-24.04", script, False, "dev", "/work")


def test_env_build_unknown() -> None:
    result = invoke("env", "build", "nope")
    assert result.exit_code == 1
    assert "unknown environment 'nope'" in result.output


def test_provision_script_path_points_into_package() -> None:
    path = environments.PROVISION_SCRIPT
    assert path.parts[-3:] == ("guest", "provision", "provision.sh")


class FakeCommands:
    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(cmd))
        for prefix, response in self.responses.items():
            if " ".join(cmd).startswith(prefix):
                return response
        return 0, ""


def test_cleanup_targets_only_agenteval_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    projects = [
        {"Name": "inspect-ours-i1", "ConfigFiles": "/repo/.agenteval/compose/docker-ubuntu.yaml"},
        {"Name": "inspect-other-i2", "ConfigFiles": "/elsewhere/compose.yaml"},
        {"Name": "unrelated"},
    ]
    commands = FakeCommands({"docker compose ls": (0, json.dumps(projects))})
    messages: list[str] = []
    assert environments.cleanup_environments(echo=messages.append, runner=commands) == 0
    inspect_calls = [c[3:] for c in commands.calls if c[:3] == [sys.executable, "-m", "inspect_ai"]]
    assert inspect_calls == [
        ["sandbox", "cleanup", "tart"],
        ["sandbox", "cleanup", "sbx"],
        ["sandbox", "cleanup", "docker", "inspect-ours-i1"],
    ]


def test_cleanup_reports_docker_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "tart" else name)
    commands = FakeCommands({"docker compose ls": (1, "daemon down")})
    messages: list[str] = []
    assert environments.cleanup_environments(echo=messages.append, runner=commands) == 1
    assert any("daemon down" in m for m in messages)
    assert "tart: not installed, skipped" in messages


# --------------------------------------------------------------------------- doctor


def test_doctor_cli_never_prints_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "OPENROUTER_API_KEY" in result.output and "set (model qwen38-27b)" in result.output
    assert "inspect-ai" in result.output


def test_doctor_reads_dotenv_without_printing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    _unset_but_restored(monkeypatch, "OPENROUTER_API_KEY")
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={SECRET}\n")
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "set (model qwen38-27b)" in result.output
    assert "not set (model qwen38-27b)" not in result.output
    assert ".env" in result.output


def test_doctor_tart_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    checks = host_doctor.doctor(CONFIG, "tart-ubuntu", runner=FakeCommands({}))
    tart = {c.name: c for c in checks}["tart"]
    assert tart.status == "fail"
    assert "https://github.com/openai/tart/releases" in tart.detail
    assert "brew install" not in tart.detail


def _write_lease_plist(path: Path, lease: int | None) -> Path:
    data: dict[str, Any] = {"bootpd": {"DHCPLeaseTimeSecs": lease} if lease else {}}
    path.write_bytes(plistlib.dumps(data))
    return path


@pytest.mark.parametrize(
    ("lease", "status"), [(None, "warn"), (86_400, "warn"), (172_800, "warn"), (600, "ok")]
)
def test_dhcp_lease_check(tmp_path: Path, lease: int | None, status: str) -> None:
    check = tart_provider.dhcp_lease_check(_write_lease_plist(tmp_path / "is.plist", lease))
    assert check.status == status
    if status == "warn":
        assert (
            "sudo defaults write" in check.detail and "DHCPLeaseTimeSecs -int 600" in check.detail
        )
    else:
        assert check.detail == "DHCPLeaseTimeSecs=600"


def test_dhcp_lease_check_missing_plist_warns(tmp_path: Path) -> None:
    check = tart_provider.dhcp_lease_check(tmp_path / "missing.plist")
    assert check.status == "warn" and "86400" in check.detail


def test_doctor_warns_about_dhcp_leases_for_tart_nat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    plist = _write_lease_plist(tmp_path / "is.plist", None)
    monkeypatch.setattr("agenteval.environments.tart.image.DHCP_PLIST", plist)
    by_name = {
        c.name: c for c in host_doctor.doctor(CONFIG, "tart-ubuntu", runner=FakeCommands({}))
    }
    assert by_name["tart dhcp lease"].status == "warn"
    docker_only = host_doctor.doctor(CONFIG, "docker-ubuntu", runner=FakeCommands({}))
    assert "tart dhcp lease" not in {c.name for c in docker_only}


@pytest.mark.parametrize(("value", "status"), [("1", "warn"), ("0", "ok"), (None, "ok")])
def test_doctor_linux_userns_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None, status: str
) -> None:
    sysctl = tmp_path / "apparmor_restrict_unprivileged_userns"
    if value is not None:
        sysctl.write_text(value + "\n")
    monkeypatch.setattr(environments.docker, "USERNS_RESTRICT_SYSCTL", sysctl)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    by_name = {
        c.name: c for c in host_doctor.doctor(CONFIG, "docker-ubuntu", runner=FakeCommands({}))
    }
    check = by_name["docker bwrap userns"]
    assert check.status == status
    if status == "warn":
        assert "docker-ubuntu" in check.detail and "AppArmor profile" in check.detail


def test_doctor_userns_check_skips_other_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "cfg"
    shutil.copytree(CONFIG, config)
    env_file = config / "environments" / "docker-ubuntu.yaml"
    env_file.write_text(env_file.read_text().replace("runtime: null", "runtime: runsc"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    names = {c.name for c in host_doctor.doctor(config, "docker-ubuntu", runner=FakeCommands({}))}
    assert "docker bwrap userns" not in names


def test_doctor_selected_env_problems_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    commands = FakeCommands(
        {"docker info": (0, "29.0.0"), "docker image inspect": (1, "No such image")}
    )
    checks = host_doctor.doctor(CONFIG, "docker-ubuntu", runner=commands)
    by_name = {c.name: c for c in checks}
    assert by_name["docker"].status == "ok"
    assert by_name["env docker-ubuntu"].status == "fail"
    assert "agenteval env build docker-ubuntu" in by_name["env docker-ubuntu"].detail
    assert "env tart-ubuntu" not in by_name
    assert invoke("doctor", "--env", "nope").exit_code == 1


def test_doctor_tart_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("tart checks only run on macOS")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("HOME", str(tmp_path))
    key = tmp_path / ".agenteval" / "ssh" / "id_ed25519"
    key.parent.mkdir(parents=True)
    key.write_text("fake")
    key.chmod(0o644)
    monkeypatch.setattr(
        tart_provider, "image_exists", lambda name: name == "agenteval-ubuntu-24.04"
    )
    checks = host_doctor.doctor(
        CONFIG, "tart-ubuntu", runner=FakeCommands({"tart --version": (0, "2.30.0")})
    )
    by_name = {c.name: c for c in checks}
    assert by_name["tart"].status == "ok"
    assert by_name["env tart-ubuntu"].status == "ok"
    assert by_name["env tart-ubuntu ssh key"].status == "warn"


# --------------------------------------------------------------------------- run units


def test_make_run_id() -> None:
    now = datetime(2026, 10, 18, 7, 5, 9, tzinfo=UTC)
    assert run_plan.make_run_id("smoke-docker", now) == "20261018T070509Z-smoke-docker"
    assert run_plan.make_run_id("a b/../c", now) == "20261018T070509Z-a-b-..-c"
    assert run_plan.validate_run_id(run_plan.make_run_id("../..", now))


@pytest.mark.parametrize("bad", ["", "..", "a/b", "-x", "x" * 129, "a b"])
def test_validate_run_id_rejects(bad: str) -> None:
    with pytest.raises(ConfigError):
        run_plan.validate_run_id(bad)


def test_apply_overrides_validates_suites() -> None:
    run = load_run(CONFIG / "runs" / "smoke-docker.yaml")
    same = run_plan.apply_overrides(run)
    assert same == run
    narrowed = run_plan.apply_overrides(run, suites=["injection", "injection"], models=["mock"])
    assert [s.name for s in narrowed.suites] == ["injection"]
    assert narrowed.suites[0].options == {"limit": 2}
    with pytest.raises(ConfigError):
        run_plan.apply_overrides(run, suites=["bogus"])


def test_resolve_run_file_by_name_or_path() -> None:
    assert (
        run_plan.resolve_run_file("smoke-docker", CONFIG) == CONFIG / "runs" / "smoke-docker.yaml"
    )
    path = CONFIG / "runs" / "opencode-qwen-tart.yaml"
    assert run_plan.resolve_run_file(str(path), CONFIG) == path


def test_register_model_prices() -> None:
    priced = ModelProfile(
        name="priced",
        inspect_model="mockllm/priced",
        pricing=Pricing(input=1.5, output=3.0, input_cache_read=0.1),
    )
    unpriced = ModelProfile(name="free", inspect_model="mockllm/free")
    notes = run_execute.register_model_prices([priced, unpriced])
    assert notes == ["model 'free': no pricing configured; cost is not tracked"]
    info = get_model_info("mockllm/priced")
    assert info is not None and info.cost is not None
    assert (info.cost.input, info.cost.output, info.cost.input_cache_read) == (1.5, 3.0, 0.1)
    assert info.cost.input_cache_write == 1.5  # unset cache price = input price


def test_register_model_prices_never_prices_cache_tokens_at_zero_by_default() -> None:
    model = ModelProfile(
        name="m", inspect_model="mockllm/cache", pricing=Pricing(input=0.24, output=2.2)
    )
    run_execute.register_model_prices([model])
    info = get_model_info("mockllm/cache")
    assert info is not None and info.cost is not None
    assert (info.cost.input_cache_read, info.cost.input_cache_write) == (0.24, 0.24)
    explicit = ModelProfile(
        name="z",
        inspect_model="mockllm/zero",
        pricing=Pricing(input=1, output=2, input_cache_read=0, input_cache_write=0),
    )
    run_execute.register_model_prices([explicit])
    zero = get_model_info("mockllm/zero")
    assert zero is not None and zero.cost is not None
    assert (zero.cost.input_cache_read, zero.cost.input_cache_write) == (0, 0)


def test_risk_warnings_flag_real_model_injection_in_relaxed_docker() -> None:
    from agenteval.config import (
        DockerSettings,
        EnvironmentProfile,
        HarnessProfile,
        InnerSandbox,
        Limits,
        MatrixCell,
        ModelProfile,
        SuiteSelection,
    )

    def cell(model: str, sandbox: str, runtime: str | None = None) -> MatrixCell:
        return MatrixCell(
            run="r",
            suite=SuiteSelection(name="injection"),
            model=ModelProfile(name=model.split("/")[0], inspect_model=model),
            harness=HarnessProfile(
                name=f"oc-{sandbox}", agent="opencode", inner_sandbox=InnerSandbox(type=sandbox)
            ),
            environment=EnvironmentProfile(
                name="d", provider="docker", docker=DockerSettings(runtime=runtime)
            ),
            limits=Limits(),
        )

    assert len(run_execute.risk_warnings([cell("openrouter/qwen/x", "bwrap")])) == 1
    assert run_execute.risk_warnings([cell("mockllm/model", "bwrap")]) == []
    assert run_execute.risk_warnings([cell("openrouter/qwen/x", "none")]) == []
    assert run_execute.risk_warnings([cell("openrouter/qwen/x", "bwrap", runtime="runsc")]) == []


# --------------------------------------------------------------------------- dev


def _capture_dev_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from agenteval.dev import commands

    seen: dict[str, Any] = {}

    def fake_start(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(commands, "start_session", fake_start)
    return seen


def test_dev_help() -> None:
    result = CliRunner().invoke(main, ["dev", "--help"])
    assert result.exit_code == 0
    for option in ("--harness", "--env", "--model", "--mount", "--agent-config-dir"):
        assert option in result.output
    assert "--exec" not in result.output


def test_dev_requires_profiles() -> None:
    result = CliRunner().invoke(main, ["dev", "--harness", "opencode-none"])
    assert result.exit_code == 2 and "--harness, --env and --model" in result.output


def test_dev_requires_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_dev_start(monkeypatch)
    args = ["dev", "--harness", "opencode-none", "--env", "docker-ubuntu", "--model", "mock"]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2 and "interactive terminal" in result.output


def test_dev_forwards_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _capture_dev_start(monkeypatch)
    args = [
        "dev", "--harness", "opencode-none", "--env", "docker-ubuntu", "--model", "mock",
        "--include", "*.local", "--agent-config-dir", str(tmp_path / "cfg"),
        "--no-host-config", "--exec", "true",
    ]  # fmt: skip
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert seen["harness"] == "opencode-none" and seen["environment"] == "docker-ubuntu"
    assert seen["model"] == "mock" and seen["mount"] == "copy"
    assert seen["subagent_model"] is None
    assert seen["includes"] == ["*.local"] and seen["host_config"] is False
    assert seen["agent_config_dir"] == (tmp_path / "cfg").resolve()
    assert seen["exec_script"] == "true"
    assert seen["cwd"] == Path.cwd()
    # cwd is a tmp dir (autouse fixture): profiles still come from the checkout
    assert seen["config_dir"] == REPO / "config"


def test_dev_forwards_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_dev_start(monkeypatch)
    base = ["dev", "--harness", "h", "--env", "e", "--model", "m", "--exec", "true"]
    result = CliRunner().invoke(main, base)
    assert result.exit_code == 0, result.output
    assert seen["cost_limit"] is None and seen["token_limit"] is None  # no default limit
    result = CliRunner().invoke(main, [*base, "--cost-limit", "2.5", "--token-limit", "5000"])
    assert result.exit_code == 0, result.output
    assert seen["cost_limit"] == 2.5 and seen["token_limit"] == 5000


@pytest.mark.parametrize(
    "option", [["--cost-limit", "0"], ["--cost-limit", "-1"], ["--token-limit", "0"]]
)
def test_dev_rejects_non_positive_limits(
    monkeypatch: pytest.MonkeyPatch, option: list[str]
) -> None:
    _capture_dev_start(monkeypatch)
    args = ["dev", "--harness", "h", "--env", "e", "--model", "m", "--exec", "true", *option]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 2 and "Invalid value" in result.output


@pytest.mark.parametrize("option", [["--cost-limit", "1"], ["--token-limit", "10"]])
def test_dev_subcommands_reject_limits(option: list[str]) -> None:
    for sub in (["list"], ["apply", "x"]):
        result = CliRunner().invoke(main, ["dev", *option, *sub])
        assert result.exit_code == 2 and "subcommands" in result.output


def test_dev_explicit_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _capture_dev_start(monkeypatch)
    args = ["dev", "--harness", "h", "--env", "e", "--model", "m", "--exec", "true"]
    result = invoke(*args)  # passes --config-dir explicitly
    assert result.exit_code == 0, result.output
    assert seen["config_dir"] == CONFIG.resolve()


def test_dev_subcommand_rejects_session_options() -> None:
    result = CliRunner().invoke(main, ["dev", "--harness", "x", "list"])
    assert result.exit_code == 2 and "subcommands" in result.output


def test_dev_list_and_unknown_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agenteval import constants

    (tmp_path / "config").mkdir()
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(constants, "checkout_root", lambda: tmp_path)
    result = CliRunner().invoke(main, ["dev", "list"])
    assert result.exit_code == 0 and "No dev sessions" in result.output
    result = CliRunner().invoke(main, ["dev", "apply", "nope"])
    assert result.exit_code == 1 and "no dev session 'nope'" in result.output
