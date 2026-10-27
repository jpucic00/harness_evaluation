"""``agenteval dev`` commands, without click: start a session, review it later, list them."""

from __future__ import annotations

import contextlib
import os
import posixpath
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.log import EvalLog

from agenteval import constants
from agenteval.config import (
    ConfigError,
    EnvironmentProfile,
    HarnessProfile,
    ModelProfile,
    ProfileSet,
    load_profiles,
    missing_env,
)
from agenteval.dev import repo, userconfig
from agenteval.dev.review import DevIO, ReviewItem, ReviewOutcome, review, visible_text
from agenteval.dev.session import (
    DevSession,
    Mount,
    RepoState,
    create_session_dir,
    list_sessions,
    load_session,
    new_session_id,
    save_session,
    session_dir,
)
from agenteval.dev.task import (
    SUBSCRIPTION_HOSTS,
    TERM_VARS,
    DevSpec,
    Exported,
    RepoTransfer,
    build_dev_task,
)
from agenteval.report.economics import TokenUsage
from agenteval.runner import register_model_prices


def _pick[T](found: dict[str, T], name: str, kind: str) -> T:
    if name not in found:
        raise ConfigError(f"unknown {kind} '{name}'; available: {sorted(found)}")
    return found[name]


def _profiles(
    profiles: ProfileSet, harness: str, environment: str, model: str, subagent_model: str | None
) -> tuple[HarnessProfile, EnvironmentProfile, ModelProfile, ModelProfile | None]:
    h = _pick(profiles.harnesses, harness, "harness")
    e = _pick(profiles.environments, environment, "environment")
    m = _pick(profiles.models, model, "model")
    if m.access == "subscription" and (h.agent != "claude_code" or h.subagents is not None):
        raise ConfigError(
            f"model '{m.name}' uses a Claude subscription, which only Claude Code itself may "
            "use: pick a claude-code harness without subagents or workers "
            f"(not '{h.name}')"
        )
    if m.access == "subscription" and e.provider == "sbx":
        raise ConfigError(
            f"model '{m.name}' uses a Claude subscription, which sbx environments do not support "
            "yet (its egress proxy needs a network sbx does not give it); use a docker or tart "
            "environment"
        )
    if h.subagents is None:
        if subagent_model:
            raise ConfigError(f"harness '{h.name}' has no subagents; drop --subagent-model")
        return h, e, m, None
    sub = _pick(profiles.models, subagent_model or h.subagents.model, "model")
    if sub.access == "subscription":
        raise ConfigError(f"model '{sub.name}' uses a Claude subscription; it cannot serve workers")
    return h, e, m, sub


def _check_limits(
    models: list[ModelProfile], cost_limit: float | None, token_limit: int | None
) -> None:
    """Refuse spend limits that could not be enforced."""
    if cost_limit is not None and cost_limit <= 0:
        raise ConfigError("--cost-limit must be more than 0")
    if token_limit is not None and token_limit <= 0:
        raise ConfigError("--token-limit must be more than 0")
    if cost_limit is None and token_limit is None:
        return
    for m in models:
        if m.access == "subscription":
            raise ConfigError(
                f"model '{m.name}' uses a Claude subscription, whose calls bypass Inspect: "
                "--cost-limit and --token-limit cannot apply to it"
            )
    unpriced = [m.name for m in models if m.pricing is None]
    if cost_limit is not None and unpriced:
        raise ConfigError(
            f"--cost-limit needs a price for every model; no pricing in: {', '.join(unpriced)} "
            "(add pricing to the profile, or use --token-limit)"
        )


def _git_identity(root: Path, git: bool) -> dict[str, str]:
    identity: dict[str, str] = {}
    for key in ("user.name", "user.email"):
        cmd = ["git", *(["-C", str(root)] if git else []), "config", "--get", key]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            identity[key] = result.stdout.strip()
    return identity


def _usage(log: EvalLog) -> str | None:
    usage = log.stats.model_usage if log.stats else {}
    if not usage:
        return None
    tokens = sum(TokenUsage.from_model_usage(u).total for u in usage.values())
    costs = [u.total_cost for u in usage.values()]
    cost = f"${sum(c for c in costs if c is not None):.4f}" if None not in costs else "cost unknown"
    return f"{tokens:,} tokens, {cost} ({', '.join(sorted(usage))})"


def _dev_harness(
    harness: HarnessProfile, writable: list[str], *, subscription: bool = False
) -> HarnessProfile:
    inner = harness.inner_sandbox
    # a subscription session's Claude Code reaches Anthropic through the egress proxy
    proxy = ["HTTPS_PROXY"] if subscription else []
    passthrough = list(dict.fromkeys([*inner.env_passthrough, *TERM_VARS, *proxy]))
    extra = list(dict.fromkeys([*inner.extra_writable_paths, *writable]))
    inner = inner.model_copy(update={"env_passthrough": passthrough, "extra_writable_paths": extra})
    return harness.model_copy(update={"inner_sandbox": inner})


def start_session(
    *,
    config_dir: Path,
    cwd: Path,
    harness: str,
    environment: str,
    model: str,
    subagent_model: str | None = None,
    mount: Mount,
    includes: list[str],
    agent_config_dir: Path | None,
    host_config: bool,
    io: DevIO,
    exec_script: str | None = None,
    cost_limit: float | None = None,
    token_limit: int | None = None,
) -> int:
    """Run one interactive session from ``cwd``; returns the process exit code."""
    checkout = constants.checkout_root()
    # Inspect would load the user's own .env (it searches from the current directory)
    load_dotenv(checkout / ".env", override=False)
    profiles = load_profiles(config_dir)
    h, e, m, sub = _profiles(profiles, harness, environment, model, subagent_model)
    _check_limits([p for p in (m, sub) if p is not None], cost_limit, token_limit)
    missing = missing_env([p for p in (m, sub) if p is not None])
    if missing:
        raise ConfigError(
            f"missing required environment variables: {', '.join(missing)} "
            f"(set them in the shell or in {checkout / '.env'})"
        )
    if mount == "live" and includes:
        raise ConfigError("--include only applies to --mount copy")

    info = repo.discover(cwd.resolve())
    if mount == "live":
        mounts = _live_mounts(info, checkout)
        vm_root = mounts[0]
    else:
        vm_root = e.workspace
        mounts = []
    vm_cwd = posixpath.join(vm_root, info.cwd_rel) if info.cwd_rel else vm_root
    session_id = new_session_id(info.root.name)
    sdir = create_session_dir(checkout, session_id)
    try:
        warnings, state, transfer = _snapshot(info, mount, sdir, session_id, includes, io)
    except BaseException:
        # a snapshot that failed half-way may have created some of its refs already
        partial = RepoState(root=str(info.root), git=info.git)
        _discard_unstarted(_placeholder(session_id, partial, mount, e, h, m), sdir)
        raise

    plan = userconfig.plan(
        h.agent,
        override=agent_config_dir,
        disabled=not host_config,
        repo_root=info.root,
        git=info.git,
        vm_root=vm_root,
        environ=os.environ,
    )
    session = DevSession(
        id=session_id,
        created=datetime.now(UTC),
        harness=h.name,
        model=m.name,
        environment=e.name,
        mount=mount,
        vm_root=vm_root,
        vm_cwd=vm_cwd,
        repo=state,
        log_dir=str(checkout / "logs" / "dev" / session_id),
    )
    try:
        staged = userconfig.stage(plan, sdir) if plan is not None else None
    except BaseException:
        _discard_unstarted(session, sdir)
        raise
    config_state = staged.state if staged is not None else None
    session.config = config_state
    save_session(session, sdir)

    claude_seed = None
    if h.agent == "claude_code":
        claude_seed = userconfig.claude_global_seed(
            version=h.version,
            trusted=sorted({vm_root, vm_cwd}),
            theme=userconfig.host_theme(os.environ, agent_config_dir),
        )
    spec = DevSpec(
        session_id=session_id,
        session_dir=sdir,
        model=m,
        subagent_model=sub,
        harness=_dev_harness(h, mounts[1:], subscription=m.access == "subscription"),
        environment=e,
        dev_env=e.model_copy(update={"ambient_secrets": False, "workspace": vm_root}),
        mount=mount,
        mounts=tuple(mounts),
        vm_root=vm_root,
        vm_cwd=vm_cwd,
        repo=transfer,
        config_tar=staged.tar if staged is not None else None,
        config_export=userconfig.export_paths(config_state) if config_state else ([], []),
        claude_seed=claude_seed,
        subscription_token=(
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] if m.access == "subscription" else None
        ),
        git_identity=_git_identity(info.root, info.git),
        host_term={k: os.environ[k] for k in TERM_VARS if os.environ.get(k)},
        exec_script=exec_script,
        cost_limit=cost_limit,
        token_limit=token_limit,
    )

    def on_export(exported: Exported) -> None:
        if exported.commits_bundle is not None and exported.export is not None:
            session.result = repo.import_export(
                session, sdir, exported.commits_bundle, exported.export
            )
        if exported.config_tar is not None and session.config is not None:
            userconfig.extract_returned(exported.config_tar, session.config, sdir)
            (sdir / "config" / "config.patch").write_text(userconfig.config_patch(sdir))
        session.status = "pending"
        session.finished = datetime.now(UTC)
        save_session(session, sdir)

    def live_notes() -> list[str]:
        if mount != "live" or not info.git:
            return []
        try:
            return repo.live_report(info.root, state.live_hashes)
        except Exception as ex:  # never hide the session's own error behind this one
            return [f"could not check the repo's git files for changes: {ex}"]

    for line in [*warnings, *(config_state.reports if config_state else [])]:
        io.out(f"note: {visible_text(line)}")
    if mount == "live":
        io.out(
            f"Live mount: the agent works directly on {info.root}. Files it changes there "
            "(including .git/hooks, .git/config, Makefiles, package.json scripts, .envrc) "
            "can run on this machine later."
        )
    if m.access == "subscription":
        io.out(
            f"Subscription: Claude Code calls Anthropic itself on your Claude plan, through "
            f"a proxy that allows only {', '.join(SUBSCRIPTION_HOSTS)}. These calls are not "
            "in the session log. Your token is in the environment, where the agent can read it."
        )
    caps = [f"${cost_limit:g}"] if cost_limit is not None else []
    caps += [f"{token_limit:,} output tokens"] if token_limit is not None else []
    if caps:
        io.out(
            f"Spend limit for this session's model calls: {' or '.join(caps)}; "
            "reaching it stops the CLI."
        )
    io.out(
        f"Starting {e.name} with {h.name} and {m.name} (session {session_id}). "
        "The first start of a CLI version downloads it; this can take a minute."
    )
    register_model_prices([p for p in (m, sub) if p is not None])
    try:
        log = _run_eval(lambda: build_dev_task(spec, io, on_export), checkout, session.log_dir)
    except KeyboardInterrupt:
        if session.status == "running":
            _failed(session, sdir, io, "interrupted (Ctrl+C)", notes=live_notes())
        raise
    except Exception as ex:  # Inspect's display hides tracebacks: report it ourselves
        return _failed(session, sdir, io, f"{type(ex).__name__}: {ex}", notes=live_notes())
    if log is None or session.status != "pending":
        if log is None:
            error = "the session was interrupted"
        else:
            error = log.error.message if log.error else f"session ended with {log.status}"
        return _failed(session, sdir, io, error, notes=live_notes())
    io.out(f"Session log: agenteval view {session_id} --log-dir {checkout / 'logs' / 'dev'}")
    return _review(session, sdir, io, notes=live_notes(), usage=_usage(log), config_only=False)


def _live_mounts(info: repo.RepoInfo, checkout: Path) -> list[str]:
    """The repo root, plus git directories outside it (linked worktrees, submodules)."""
    root = info.root.resolve()
    mounts = [root]
    if info.git:
        for found in (info.common_dir, info.git_dir):
            if found is None:
                continue
            path = found.resolve()
            if not any(path.is_relative_to(m) for m in mounts):
                mounts.append(path)
    for path in mounts:
        if checkout.resolve().is_relative_to(path):
            raise ConfigError(
                f"--mount live would share {path}, which contains the agenteval checkout "
                "(its .env holds your API keys); use --mount copy or work from another folder"
            )
    return [str(p) for p in mounts]


def _snapshot(
    info: repo.RepoInfo,
    mount: Mount,
    sdir: Path,
    session_id: str,
    includes: list[str],
    io: DevIO,
) -> tuple[list[str], RepoState, RepoTransfer | None]:
    if mount == "live":
        hashes = repo.live_hashes(info.root) if info.git else {}
        state = RepoState(root=str(info.root), cwd_rel=info.cwd_rel, git=info.git)
        return [], state.model_copy(update={"live_hashes": hashes}), None
    snap = repo.snapshot(
        info,
        sdir,
        session_id,
        includes,
        confirm=lambda q: io.ask(f"{q} [y/N] ").strip().lower() == "y",
    )
    state = snap.state
    transfer = RepoTransfer(
        bundle=snap.bundle,
        meta=snap.meta,
        base_commit=state.base_commit,
        base_tree=state.base_tree,
        head=state.head,
    )
    return snap.warnings, state, transfer


def _placeholder(
    session_id: str,
    state: RepoState,
    mount: Mount,
    e: EnvironmentProfile,
    h: HarnessProfile,
    m: ModelProfile,
) -> DevSession:
    return DevSession(
        id=session_id,
        created=datetime.now(UTC),
        harness=h.name,
        model=m.name,
        environment=e.name,
        mount=mount,
        vm_root="",
        vm_cwd="",
        repo=state,
    )


def _discard_unstarted(session: DevSession, sdir: Path) -> None:
    if session.mount == "copy" and session.repo.git:
        repo.cleanup_refs(session, keep_heads=False)
    shutil.rmtree(sdir, ignore_errors=True)


DEV_MAX_RETRIES = 2
"""Retries of a failing model call before the bridge returns the error to the CLI.
Inspect's default retries forever, which the CLI shows as an endless spinner
(display="none" hides the retry messages)."""
DEV_ATTEMPT_TIMEOUT = 900
"""Wall-clock seconds per attempt of a main-model call. It stops a hung call (a gateway that
never answers) from being retried for hours; it is above the 600 s HTTP read timeout so a
slow non-streamed turn is not cut, and 3 attempts stay under think_shim's 1 h upstream
limit. A streamed turn (Anthropic with reasoning) longer than this is retried."""


def _run_eval(make_task: Callable[[], Task], checkout: Path, log_dir: str | None) -> EvalLog | None:
    # inside the checkout: the compose file goes to <checkout>/.agenteval/compose (where
    # `agenteval env cleanup` looks), never into the user's repo
    with contextlib.chdir(checkout):
        logs = inspect_eval(
            make_task(),
            log_dir=log_dir,
            display="none",
            log_level="error",
            score=False,
            fail_on_error=True,
            sandbox_cleanup=True,
            max_samples=1,
            max_sandboxes=1,
            tags=["agenteval-dev"],
            max_retries=DEV_MAX_RETRIES,
            attempt_timeout=DEV_ATTEMPT_TIMEOUT,
        )
    return logs[0] if logs else None  # empty after an interrupt


def _failed(
    session: DevSession, sdir: Path, io: DevIO, error: str, *, notes: list[str] | None = None
) -> int:
    session.status = "failed"
    session.error = error
    save_session(session, sdir)
    if session.mount == "copy" and session.repo.git:
        repo.cleanup_refs(session, keep_heads=True)  # fetched agent commits stay
    io.out(f"The session failed: {visible_text(error)}")
    for note in notes or []:
        io.out(f"note: {visible_text(note)}")
    io.out(f"Session record: {sdir}  log: {session.log_dir}")
    return 1


def _review(
    session: DevSession,
    sdir: Path,
    io: DevIO,
    *,
    notes: list[str],
    usage: str | None,
    config_only: bool,
) -> int:
    items: list[ReviewItem] = []
    if not config_only and session.mount == "copy" and session.result is not None:
        repo_items, repo_notes = repo.repo_items(session, sdir)
        items += repo_items
        notes = [*notes, *repo_notes]
    if session.config is not None:
        items += userconfig.config_items(session, sdir)
    if session.config is not None:
        notes = [*notes, *session.config.reports]

    def save(s: DevSession) -> None:
        save_session(s, sdir)

    outcome: ReviewOutcome = review(session, items, io, save=save, notes=notes, usage=usage)
    repo_pending = False
    if config_only and session.mount == "copy" and session.result is not None:
        left, _ = repo.repo_items(session, sdir)
        repo_pending = any(item.id not in session.decisions for item in left)
    if outcome == "deferred" or repo_pending:
        session.status = "pending"
        io.out(f"Pending changes are kept. Review them later: agenteval dev apply {session.id}")
    else:
        session.status = "applied" if outcome == "done" else "discarded"
        if session.mount == "copy":
            repo.cleanup_refs(session, keep_heads=True)
    save_session(session, sdir)
    return 0


def apply_session(session_id: str, *, config_only: bool, io: DevIO) -> int:
    """Review the pending changes of an earlier session."""
    checkout = constants.checkout_root()
    session = load_session(checkout, session_id)
    if session.status not in ("pending", "applied"):
        raise ConfigError(f"session {session_id} is {session.status}; nothing to apply")
    return _review(
        session,
        session_dir(checkout, session_id),
        io,
        notes=[],
        usage=None,
        config_only=config_only,
    )


def format_sessions() -> str:
    checkout = constants.checkout_root()
    sessions = list_sessions(checkout)
    if not sessions:
        return "No dev sessions."
    rows = [("SESSION", "STATUS", "HARNESS", "MODEL", "ENV", "REPO")]
    rows += [(s.id, s.status, s.harness, s.model, s.environment, s.repo.root) for s in sessions]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(r[:-1], widths, strict=True)) + "  " + r[-1]
        for r in rows
    )
