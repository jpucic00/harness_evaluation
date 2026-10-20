"""``agenteval`` command line. Thin wrapper over :mod:`agenteval.runner`."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from agenteval import runner
from agenteval.config import DEFAULT_CONFIG_DIR, ConfigError


@contextmanager
def _config_errors() -> Iterator[None]:
    try:
        yield
    except ConfigError as ex:
        raise click.ClickException(str(ex)) from ex


@contextmanager
def _dev_errors() -> Iterator[None]:
    """``agenteval dev`` errors the user can act on, as messages instead of tracebacks."""
    from agenteval.constants import CheckoutError
    from agenteval.dev.repo import RepoError

    try:
        yield
    except (ConfigError, CheckoutError, RepoError, FileNotFoundError) as ex:
        raise click.ClickException(str(ex)) from ex


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_CONFIG_DIR,
    show_default=True,
    envvar="AGENTEVAL_CONFIG_DIR",
    help="Directory with models/, harnesses/, environments/ and runs/ profiles.",
)
@click.pass_context
def main(ctx: click.Context, config_dir: Path) -> None:
    """Evaluate coding-agent harnesses across models x harnesses x environments x suites."""
    ctx.obj = config_dir


@main.command()
@click.option(
    "--env", "env_name", metavar="NAME", help="Also require this environment to be ready."
)
@click.pass_obj
def doctor(config_dir: Path, env_name: str | None) -> None:
    """Check tools, versions, images, SSH key and required env vars (values are never shown).

    Required env vars may come from the shell or a .env file, as with Inspect.
    """
    checks = runner.doctor(config_dir, env_name)
    click.echo(runner.format_checks(checks))
    if any(c.status == "fail" for c in checks):
        raise SystemExit(1)


@main.command()
@click.pass_obj
def profiles(config_dir: Path) -> None:
    """List model, harness, environment and run profiles."""
    with _config_errors():
        click.echo(runner.describe_profiles(config_dir))


# --------------------------------------------------------------------------- env


@main.group()
def env() -> None:
    """Build or clean up evaluation environments."""


@env.command("build")
@click.argument("name")
@click.option(
    "--force",
    is_flag=True,
    help="Rebuild from scratch (Tart: re-clone the base VM; Docker: no cache, re-pull base).",
)
@click.pass_obj
def env_build(config_dir: Path, name: str, force: bool) -> None:
    """Build the image for environment NAME (Docker image or Tart VM)."""
    with _config_errors():
        profile = runner.get_environment(config_dir, name)
        click.echo(f"building {profile.provider} image for environment '{name}'")
        try:
            runner.build_environment(profile, force=force)
        except (ConfigError, KeyboardInterrupt):
            raise
        except Exception as ex:
            raise click.ClickException(f"build failed: {type(ex).__name__}: {ex}") from ex
    click.echo("done")


@env.command("cleanup")
def env_cleanup() -> None:
    """Delete leftover agenteval Tart VMs and Docker compose projects."""
    raise SystemExit(runner.cleanup_environments(echo=click.echo))


# --------------------------------------------------------------------------- run


@main.command()
@click.argument("run_file")
@click.option("--model", "models", multiple=True, metavar="NAME", help="Replace the run's models.")
@click.option(
    "--harness", "harnesses", multiple=True, metavar="NAME", help="Replace the run's harnesses."
)
@click.option(
    "--env", "environments", multiple=True, metavar="NAME", help="Replace the run's environments."
)
@click.option(
    "--suite",
    "suites",
    multiple=True,
    type=click.Choice(runner.SUITE_NAMES),
    help="Replace the run's suites (options from the run file are kept).",
)
@click.option("--limit", type=click.IntRange(min=1), help="Max samples per task.")
@click.option("--run-id", help="Log/report directory name; reuse one to resume an interrupted run.")
@click.option("--no-report", is_flag=True, help="Skip building the HTML report.")
@click.option("--dry-run", is_flag=True, help="Print the matrix and exit.")
@click.pass_obj
def run(
    config_dir: Path,
    run_file: str,
    models: tuple[str, ...],
    harnesses: tuple[str, ...],
    environments: tuple[str, ...],
    suites: tuple[str, ...],
    limit: int | None,
    run_id: str | None,
    no_report: bool,
    dry_run: bool,
) -> None:
    """Run RUN_FILE (a path, or a run name under <config-dir>/runs)."""
    with _config_errors():
        plan = runner.plan_run(
            run_file,
            config_dir=config_dir,
            models=models,
            harnesses=harnesses,
            environments=environments,
            suites=suites,
            limit=limit,
            run_id=run_id,
        )
        click.echo(runner.format_plan(plan))
        runner.load_dotenv_file()  # Inspect reads .env too; check what it will see
        missing = plan.missing_env()
        if dry_run:
            if missing:
                click.echo(
                    f"\nwarning: missing required environment variables: {', '.join(missing)}"
                )
            click.echo("\ndry run: nothing executed")
            return
        click.echo()
        code = runner.execute_run(plan, report=not no_report, echo=click.echo)
    raise SystemExit(code)


# --------------------------------------------------------------------------- report


@main.group()
def report() -> None:
    """Build, open or serve HTML reports."""


_report_dir_option = click.option(
    "--report-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=runner.DEFAULT_REPORT_DIR,
    show_default=True,
    help="Root directory holding one report per run.",
)


@report.command("build")
@click.argument("log_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    help="Output directory (default: <report-dir>/<LOG_DIR name>).",
)
@click.option("--title", help="Report title.")
@_report_dir_option
def report_build(log_dir: Path, out: Path | None, title: str | None, report_dir: Path) -> None:
    """Build a report from the eval logs in LOG_DIR."""
    with _config_errors():
        path = runner.report_from_logs(log_dir, out=out, report_root=report_dir, title=title)
    click.echo(f"report: {path}")


@report.command("open")
@click.argument("run_id", default=runner.LATEST)
@_report_dir_option
def report_open(run_id: str, report_dir: Path) -> None:
    """Open the report of RUN_ID ('latest' by default, 'index' for the run list)."""
    with _config_errors():
        page = runner.report_page(report_dir, run_id)
    if not runner.open_path(page):
        click.echo(page)


@report.command("serve")
@click.option("--port", type=click.IntRange(1, 65535), default=8765, show_default=True)
@_report_dir_option
def report_serve(port: int, report_dir: Path) -> None:
    """Serve the report directory on http://127.0.0.1:PORT (local only)."""
    with _config_errors():
        server = runner.make_report_server(report_dir, port)
    click.echo(
        f"serving {report_dir} at http://127.0.0.1:{server.server_address[1]}/ (Ctrl-C stops)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --------------------------------------------------------------------------- dev

_DEV_START_OPTIONS = (
    "harness",
    "environment",
    "model",
    "subagent_model",
    "includes",
    "agent_config_dir",
    "no_host_config",
    "exec_script",
)


def _dev_config_dir(ctx: click.Context, config_dir: Path) -> Path:
    """Profiles of the agenteval checkout unless --config-dir / AGENTEVAL_CONFIG_DIR is set.

    ``agenteval dev`` runs from the user's repo, whose own ``config/`` is unrelated.
    """
    from agenteval import constants

    source = ctx.find_root().get_parameter_source("config_dir")
    if source in (click.core.ParameterSource.DEFAULT, click.core.ParameterSource.DEFAULT_MAP):
        return constants.checkout_root() / DEFAULT_CONFIG_DIR
    return config_dir.resolve()


@main.group(invoke_without_command=True)
@click.option("--harness", metavar="NAME", help="Harness profile (e.g. opencode-bwrap).")
@click.option("--env", "environment", metavar="NAME", help="Environment profile.")
@click.option("--model", metavar="NAME", help="Model profile.")
@click.option(
    "--subagent-model",
    metavar="NAME",
    help="Model profile for the harness's subagents/workers (default: the harness profile's).",
)
@click.option(
    "--mount",
    type=click.Choice(["copy", "live"]),
    default="copy",
    show_default=True,
    help="copy: the repo is copied in and changes come back for review. "
    "live: the repo is mounted and the agent writes it directly.",
)
@click.option(
    "--include",
    "includes",
    multiple=True,
    metavar="GLOB",
    help="Also copy in gitignored files matching GLOB (copy mode; repeatable).",
)
@click.option(
    "--agent-config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="CLI config to copy in and update (default: $CLAUDE_CONFIG_DIR or ~/.claude; "
    "$XDG_CONFIG_HOME/opencode or ~/.config/opencode).",
)
@click.option(
    "--no-host-config",
    is_flag=True,
    help="Start with an empty CLI config and bring no CLI config back (repo changes are "
    "still reviewed).",
)
@click.option("--exec", "exec_script", hidden=True, help="Test hook: run a script, no TTY.")
@click.pass_context
def dev(
    ctx: click.Context,
    harness: str | None,
    environment: str | None,
    model: str | None,
    subagent_model: str | None,
    mount: str,
    includes: tuple[str, ...],
    agent_config_dir: Path | None,
    no_host_config: bool,
    exec_script: str | None,
) -> None:
    """Work interactively in a harness inside an environment, on the current repo.

    Starts one environment with the harness CLI and model, attaches this terminal to the
    CLI, and when it exits brings the changes back for review. Subcommands: `apply`
    reviews a deferred session, `list` shows sessions.
    """
    import sys

    from agenteval.dev import commands
    from agenteval.dev.review import terminal_io

    given = {
        "harness": harness,
        "environment": environment,
        "model": model,
        "subagent_model": subagent_model,
        "includes": includes,
        "agent_config_dir": agent_config_dir,
        "no_host_config": no_host_config,
        "exec_script": exec_script,
    }
    if ctx.invoked_subcommand is not None:
        used = [name for name in _DEV_START_OPTIONS if given[name]]
        if used or mount != "copy":
            raise click.UsageError("session options go with `agenteval dev`, not its subcommands")
        return
    if not (harness and environment and model):
        raise click.UsageError("--harness, --env and --model are required")
    if exec_script is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise click.UsageError("agenteval dev needs an interactive terminal")
    with _dev_errors():
        code = commands.start_session(
            config_dir=_dev_config_dir(ctx, ctx.obj),
            cwd=Path.cwd(),
            harness=harness,
            environment=environment,
            model=model,
            subagent_model=subagent_model,
            mount="live" if mount == "live" else "copy",
            includes=list(includes),
            agent_config_dir=agent_config_dir.expanduser().resolve() if agent_config_dir else None,
            host_config=not no_host_config,
            io=terminal_io(),
            exec_script=exec_script,
        )
    raise SystemExit(code)


@dev.command("apply")
@click.argument("session_id")
@click.option("--config", "config_only", is_flag=True, help="Only the CLI config and memory.")
def dev_apply(session_id: str, config_only: bool) -> None:
    """Review the pending changes of an earlier dev session."""
    from agenteval.dev import commands
    from agenteval.dev.review import terminal_io

    with _dev_errors():
        try:
            code = commands.apply_session(session_id, config_only=config_only, io=terminal_io())
        except ValueError as ex:  # invalid session id
            raise click.ClickException(str(ex)) from ex
    raise SystemExit(code)


@dev.command("list")
def dev_list() -> None:
    """List dev sessions (newest first)."""
    from agenteval.dev import commands

    with _dev_errors():
        click.echo(commands.format_sessions())


# --------------------------------------------------------------------------- view


@main.command()
@click.argument("run_id", default=runner.LATEST)
@click.option(
    "--log-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=runner.DEFAULT_LOG_DIR,
    show_default=True,
    help="Root directory holding one log directory per run.",
)
def view(run_id: str, log_dir: Path) -> None:
    """Open Inspect's log viewer for RUN_ID ('latest' by default)."""
    with _config_errors():
        cmd = runner.view_command(log_dir, run_id)
    click.echo(f"inspect view --log-dir {cmd[-1]}")
    raise SystemExit(subprocess.run(cmd, check=False).returncode)


if __name__ == "__main__":
    main()
