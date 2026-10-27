"""agenteval dev end to end in Docker and Tart with the mock model (no TTY: ``--exec``).

Each test starts a real session from a throwaway repo, runs a script in the
environment as the agent user (instead of attaching a terminal to the CLI), then
reviews and applies everything with scripted answers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agenteval import constants
from agenteval.dev import commands, userconfig
from agenteval.dev.review import DevIO
from agenteval.dev.session import load_session, session_dir

IMAGE = "agenteval/ubuntu-24.04:latest"
TART_IMAGE = "agenteval-ubuntu-24.04"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return result.returncode == 0


def _tart_ready() -> bool:
    if shutil.which("tart") is None:
        return False
    result = subprocess.run(
        ["tart", "list", "--source", "local", "--format", "json"], capture_output=True, text=True
    )
    try:
        names = {vm.get("Name") for vm in json.loads(result.stdout or "[]")}
    except ValueError:
        return False
    return TART_IMAGE in names


def _sbx_ready() -> bool:
    from agenteval.config import SbxSettings
    from agenteval.environments.sbx import cli

    try:
        template = SbxSettings().template
        return not cli.readiness_problems() and cli.template_id(template) is not None
    except cli.SbxError:
        return False


@pytest.fixture(
    params=[
        pytest.param(
            "docker-ubuntu",
            marks=[
                pytest.mark.docker,
                pytest.mark.skipif(not _docker_ready(), reason=f"needs Docker and {IMAGE}"),
            ],
        ),
        pytest.param(
            "tart-ubuntu",
            marks=[
                pytest.mark.tart,
                pytest.mark.skipif(not _tart_ready(), reason=f"needs Tart and {TART_IMAGE}"),
            ],
        ),
        pytest.param(
            "sbx-ubuntu",
            marks=[
                pytest.mark.sbx,
                pytest.mark.skipif(
                    not _sbx_ready(), reason="needs sbx (signed in) and the sbx-ubuntu template"
                ),
            ],
        ),
    ]
)
def environment(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Session records, logs and compose files go to a temporary checkout."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    (root / "config").symlink_to(REPO / "config")
    monkeypatch.setattr(constants, "checkout_root", lambda: root)
    return root


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Dev Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "dev@test.invalid")
    root = (tmp_path / "proj").resolve()
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one two\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    (root / "notes.txt").write_text("untracked on the host\n")
    return root


@pytest.fixture
def agent_config(tmp_path: Path, project: Path) -> Path:
    cfg = tmp_path / "cfg"
    memory = cfg / "projects" / userconfig.claude_project_key(str(project)) / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [Old](old.md) - old fact\n")
    (memory / "old.md").write_text("old fact\n")
    settings = {"permissions": {"allow": ["Bash(ls)"]}, "apiKeyHelper": "echo host-secret"}
    (cfg / "settings.json").write_text(json.dumps(settings) + "\n")
    return cfg


def scripted_io(output: list[str]) -> DevIO:
    return DevIO(
        out=output.append,
        ask=lambda prompt: "y",
        edit=lambda content, suffix: content,
        color=False,
        interactive=True,
    )


def start(
    environment: str,
    project: Path,
    cfg: Path | None,
    harness: str,
    script: str,
    *,
    mount: str = "copy",
    model: str = "mock",
    io: DevIO | None = None,
    config_dir: Path = REPO / "config",
    cost_limit: float | None = None,
    token_limit: int | None = None,
) -> tuple[int, str]:
    output: list[str] = []
    code = commands.start_session(
        config_dir=config_dir,
        cwd=project,
        harness=harness,
        environment=environment,
        model=model,
        mount="live" if mount == "live" else "copy",
        includes=[],
        agent_config_dir=cfg,
        host_config=cfg is not None,
        io=io or scripted_io(output),
        exec_script=script,
        cost_limit=cost_limit,
        token_limit=token_limit,
    )
    return code, "\n".join(output)


CLAUDE_SCRIPT = r"""
set -e
echo "hello from env" > new.txt
sed -i 's/one/ONE/' a.txt
git add -A && git commit -qm "agent commit"
echo "uncommitted line" >> a.txt
m="$HOME/.claude/projects/-workspace/memory"
printf -- '- [Old](old.md) - old fact\n- [Fact](fact.md) - tests use pytest\n' > "$m/MEMORY.md"
echo "fact: tests use pytest" > "$m/fact.md"
"$AGENTEVAL_DEV_LAUNCH" -p "say hi" </dev/null
echo "ENV-SETTINGS: $(cat "$HOME/.claude/settings.json" | tr -d '\n ')"
"""


def test_claude_bwrap_round_trip(environment: str, project: Path, agent_config: Path) -> None:
    code, output = start(environment, project, agent_config, "claude-code-bwrap", CLAUDE_SCRIPT)
    assert code == 0, output
    # the CLI ran through the bridge, inside bwrap, and saw only the sanitized settings
    assert "Default output from mockllm/model" in output
    assert 'ENV-SETTINGS: {"permissions":{"allow":["Bash(ls)"]}}' in output

    # commits fast-forwarded, the uncommitted edit applied, the host's untracked file kept
    assert git(project, "log", "-1", "--format=%s") == "agent commit"
    assert (project / "a.txt").read_text() == "ONE two\nuncommitted line\n"
    assert (project / "new.txt").read_text() == "hello from env\n"
    assert (project / "notes.txt").read_text() == "untracked on the host\n"
    assert not (project / ".agenteval").exists()  # nothing of agenteval's in the user repo

    # memory came back under the host key; the host's credential helper is untouched
    memory = agent_config / "projects" / userconfig.claude_project_key(str(project)) / "memory"
    assert (memory / "fact.md").read_text() == "fact: tests use pytest\n"
    assert "fact.md" in (memory / "MEMORY.md").read_text()
    settings = json.loads((agent_config / "settings.json").read_text())
    assert settings == {"permissions": {"allow": ["Bash(ls)"]}, "apiKeyHelper": "echo host-secret"}

    session_id = output.split("(session ", 1)[1].split(")", 1)[0]
    session = load_session(constants.checkout_root(), session_id)
    assert session.status == "applied"
    assert not git(project, "for-each-ref", f"refs/agenteval/{session_id}/base")


def test_opencode_round_trip_without_host_config(environment: str, project: Path) -> None:
    script = 'set -e\necho "from opencode env" > oc.txt\n"$AGENTEVAL_DEV_LAUNCH" --help >/dev/null'
    code, output = start(environment, project, None, "opencode-none", script)
    assert code == 0, output
    assert (project / "oc.txt").read_text() == "from opencode env\n"
    assert git(project, "log", "-1", "--format=%s") == "init"  # no commits: no fast-forward


def test_live_mount_writes_host(environment: str, project: Path) -> None:
    script = 'set -e\n[ "$(id -un)" = agent ]\necho live > live.txt\ngit status --short'
    code, output = start(environment, project, None, "opencode-bwrap", script, mount="live")
    assert code == 0, output
    assert (project / "live.txt").read_text() == "live\n"
    assert "?? live.txt" in output
    assert not (project / ".agenteval").exists()


LIMIT_SCRIPT = r"""
set -e
echo "before the limit" > work.txt
git add -A && git commit -qm "agent commit"
echo "uncommitted line" >> a.txt
"$AGENTEVAL_DEV_LAUNCH" -p "say hi" </dev/null || true
sleep 600
"""


@pytest.mark.parametrize("kind", ["token", "cost"])
def test_spend_limit_keeps_the_work(
    environment: str, project: Path, tmp_path: Path, kind: str
) -> None:
    """mockllm counts each reply's characters as output tokens (33 for its default
    reply), so the first call trips a limit of 1 token, or of $0.00001 at $1 per 1M
    tokens. Inspect then cancels the sample; the session still exports."""
    config = tmp_path / "config"
    shutil.copytree(REPO / "config", config)
    mock = config / "models" / "mock.yaml"
    mock.write_text(mock.read_text().replace("input: 0\n  output: 0", "input: 1\n  output: 1"))
    assert "output: 1" in mock.read_text()
    output: list[str] = []
    answers = DevIO(
        out=output.append,
        ask=lambda prompt: "l",  # review later: the changes stay pending
        edit=lambda content, suffix: content,
        color=False,
        interactive=True,
    )
    code, _ = start(
        environment, project, None, "claude-code-none", LIMIT_SCRIPT, io=answers,
        config_dir=config,
        token_limit=1 if kind == "token" else None,
        cost_limit=0.00001 if kind == "cost" else None,
    )  # fmt: skip
    text = "\n".join(output)
    assert code == 0, text
    reached = "Output token limit reached (1 tokens)" if kind == "token" else "Spend limit reached"
    assert reached in text and "the CLI was stopped" in text, text
    session_id = text.split("(session ", 1)[1].split(")", 1)[0]
    session = load_session(constants.checkout_root(), session_id)
    assert session.status == "pending"
    sdir = session_dir(constants.checkout_root(), session_id)
    patch = (sdir / "worktree.patch").read_text()
    assert "before the limit" in patch and "uncommitted line" in patch
    assert (sdir / "commits.bundle").is_file()
    assert git(project, "log", "-1", "--format=%s") == "init"  # nothing applied yet


SUBSCRIPTION_SCRIPT = r"""
set -u
get="curl -sS -m 20 -o /dev/null"
via="-x http://127.0.0.1:13133"
$get -m 5 https://1.1.1.1 2>/dev/null && echo "DIRECT:open" || echo "DIRECT:blocked"
echo "ANTHROPIC:$($get -w '%{http_code}' $via https://api.anthropic.com/v1/models)"
echo "OTHER:$($get -w '%{http_connect}' $via https://example.com 2>/dev/null)"
echo "TOKEN:$(stat -c '%U:%a' /var/lib/agenteval-dev/claude-oauth-token)"
"$AGENTEVAL_DEV_LAUNCH" -p "say hi" </dev/null >/tmp/cli.out 2>&1; echo "CLI-EXIT:$?"
echo "CLI:$(head -c 600 /tmp/cli.out | tr '\n' ' ')"
"""


@pytest.mark.internet
@pytest.mark.skipif(
    os.environ.get("AGENTEVAL_INTERNET_TESTS") != "1", reason="set AGENTEVAL_INTERNET_TESTS=1"
)
@pytest.mark.parametrize("harness", ["claude-code-none", "claude-code-bwrap"])
def test_subscription_reaches_only_anthropic(
    environment: str, project: Path, harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake token: Anthropic answers 401, which proves the path without a real plan."""
    if environment == "sbx-ubuntu":
        pytest.skip("sbx environments do not support subscription sessions yet")
    fake = "sk-ant-oat01-agenteval-fake-token"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", fake)
    code, output = start(
        environment, project, None, harness, SUBSCRIPTION_SCRIPT, model="claude-sonnet-subscription"
    )
    assert code == 0, output
    assert "DIRECT:blocked" in output  # the agent itself has no egress
    assert "ANTHROPIC:401" in output  # the proxy tunnels to Anthropic
    assert "OTHER:403" in output  # and nowhere else
    assert "TOKEN:agent:400" in output
    # Claude Code itself got through with the token, which Anthropic refused
    assert "CLI-EXIT:0" not in output and "OAuth access token is invalid" in output
    logs = list((constants.checkout_root() / "logs" / "dev").rglob("*.eval"))
    assert logs and not any(fake.encode() in log.read_bytes() for log in logs)
