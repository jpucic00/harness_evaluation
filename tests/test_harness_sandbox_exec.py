"""sandbox-exec rendering and behaviour (fake bwrap/socat; no real sandbox needed)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agenteval import constants
from agenteval.config import InnerSandbox, load_profiles
from agenteval.harnesses.sandbox_exec import (
    CONFIG_DIRS,
    PROXY_DIR,
    STATE_DIRS,
    SYSTEM_HIDE_PATHS,
    env_allowlist,
    render_sandbox_exec,
)

REPO = Path(__file__).resolve().parents[1]
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]
MAIN_CALL = 'sx_main "$@"'


def bwrap_profile(**overrides: object) -> InnerSandbox:
    base: dict[str, object] = {
        "type": "bwrap",
        "hide_paths": ["~/.ssh", "~/.netrc", "~/.aws"],
        "network": "proxy-only",
        "clear_env": True,
    }
    return InnerSandbox.model_validate(base | overrides)


def functions_only(script: str) -> str:
    body, sep, _ = script.rpartition(MAIN_CALL)
    assert sep, "template must end with the main call"
    return body


def run_sh(code: str, shell: str = "sh", env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        [shell, "-c", code], capture_output=True, text=True, env=env, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


# --------------------------------------------------------------------------- render


@pytest.mark.parametrize("name", ["opencode-none", "opencode-bwrap"])
def test_example_profiles_render_and_parse(name: str, tmp_path: Path) -> None:
    profiles = load_profiles(REPO / "config")
    inner = profiles.harnesses[name].inner_sandbox
    script = render_sandbox_exec(inner, agent_home="/home/agent", workspace="/workspace")
    assert script.startswith("#!/bin/sh\n")
    assert "@@" not in script
    path = tmp_path / "sandbox-exec"
    path.write_text(script)
    for shell in SHELLS:
        check = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
        assert check.returncode == 0, f"{shell}: {check.stderr}"


def test_none_profile_just_execs(tmp_path: Path) -> None:
    script = render_sandbox_exec(InnerSandbox(), agent_home="/home/agent", workspace="/w")
    assert 'exec "$@"' in script
    assert "bwrap" not in script
    path = tmp_path / "sandbox-exec"
    path.write_text(script)
    path.chmod(0o755)
    ok = subprocess.run(
        [str(path), "sh", "-c", 'printf "%s" "$1"; exit 4', "x", "a b"],
        capture_output=True,
        text=True,
    )
    assert (ok.returncode, ok.stdout) == (4, "a b")
    usage = subprocess.run([str(path)], capture_output=True, text=True)
    assert usage.returncode == 125


def test_rendered_values_are_shell_safe() -> None:
    weird = "/work space/it's $(touch /tmp/pwned)"
    inner = bwrap_profile(
        hide_paths=["~/.ssh", "/etc/a b"],
        extra_writable_paths=["~/.npm", "/var/cache/x y"],
        env_passthrough=["MY_TOKEN_NAME"],
    )
    script = render_sandbox_exec(inner, agent_home="/home/a'gent", workspace=weird)
    out = run_sh(
        functions_only(script)
        + 'printf "%s|" "$sx_workspace" "$sx_agent_home"; printf "%s;" $sx_hide_paths;'
        + ' printf "%s;" $sx_writable_paths; printf "%s;" $sx_env_names'
    )
    ws, home, rest = out.split("|", 2)
    assert ws == weird
    assert home == "/home/a'gent"
    assert "~/.ssh;/etc/a b;" in rest
    assert "~/.npm;/var/cache/x y;" in rest
    assert rest.endswith("TZ;IS_SANDBOX;MY_TOKEN_NAME;")


def test_profile_lists_rendered() -> None:
    script = render_sandbox_exec(bwrap_profile(), agent_home="/home/agent", workspace="/ws")
    out = run_sh(
        functions_only(script)
        + 'printf "%s;" $sx_state_dirs; echo; printf "%s;" $sx_config_dirs; echo;'
        + ' printf "%s;" $sx_env_prefixes; echo; echo "$sx_proxy_dir $sx_network $sx_clear_env";'
        + ' echo "$sx_bwrap_group"'
    )
    lines = out.splitlines()
    assert lines[0] == "".join(f"{d};" for d in STATE_DIRS)
    # printf runs its format once even with no arguments, so an empty list prints ";"
    assert lines[1] == ("".join(f"{d};" for d in CONFIG_DIRS) or ";")
    assert lines[2] == "OPENCODE_;ANTHROPIC_;OPENAI_;GOOGLE_;OPENROUTER_;CLAUDE_;MCP_;"
    assert lines[3] == f"{PROXY_DIR} proxy-only 1"
    assert lines[4] == constants.BWRAP_GROUP


def test_system_paths_always_hidden() -> None:
    assert SYSTEM_HIDE_PATHS == (constants.SANDBOX_TOOLS_DIR, constants.STATE_DIR)
    code = 'printf "%s;" $sx_hide_paths'
    plain = render_sandbox_exec(bwrap_profile(), agent_home="/home/agent", workspace="/ws")
    assert run_sh(functions_only(plain) + code) == (
        "~/.ssh;~/.netrc;~/.aws;" + "".join(f"{p};" for p in SYSTEM_HIDE_PATHS)
    )
    # already listed by the profile -> not repeated
    listed = bwrap_profile(hide_paths=[constants.STATE_DIR, "~/.ssh"])
    script = render_sandbox_exec(listed, agent_home="/home/agent", workspace="/ws")
    assert run_sh(functions_only(script) + code) == (
        f"{constants.STATE_DIR};~/.ssh;{constants.SANDBOX_TOOLS_DIR};"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hide_paths": ["relative/path"]},
        {"hide_paths": ["/etc/x\ny"]},
        {"hide_paths": ["~user/x"]},
        {"extra_writable_paths": ["tmp"]},
        {"env_passthrough": ["BAD-NAME"]},
        {"env_passthrough": ["1ABC"]},
        {"env_passthrough": ["sx_args"]},
    ],
)
def test_invalid_profiles_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        render_sandbox_exec(bwrap_profile(**kwargs), agent_home="/home/agent", workspace="/w")


def test_invalid_workspace_rejected() -> None:
    with pytest.raises(ValueError):
        render_sandbox_exec(bwrap_profile(), agent_home="/home/agent", workspace="workspace")


def test_env_allowlist_names() -> None:
    names = env_allowlist(bwrap_profile(env_passthrough=["EXTRA", "PATH"]))
    assert names[:8] == ["PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TZ"]
    assert names.count("PATH") == 1
    assert names[-1] == "EXTRA"


# --------------------------------------------------------------------------- shell functions


@pytest.fixture(scope="module")
def funcs() -> str:
    inner = bwrap_profile(env_passthrough=["EXTRA_OK"])
    return functions_only(render_sandbox_exec(inner, agent_home="/home/agent", workspace="/w"))


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("http://localhost:3001/v1", "3001"),
        ("http://localhost:3001", "3001"),
        ("http://127.0.0.1:13131/v1", "13131"),
        ("http://localhost:65535/x", "65535"),
        ("https://localhost:3001/v1", ""),
        ("http://example.com:3001/v1", ""),
        ("http://localhost.evil:3001/v1", ""),
        ("http://localhost:/v1", ""),
        ("http://localhost:0300/v1", ""),
        ("http://localhost:65536/v1", ""),
        ("http://localhost:123456/v1", ""),
        ("http://localhost:30a1/v1", ""),
        ("http://localhost:3001?x=1", ""),
        ("", ""),
    ],
)
def test_parse_port(funcs: str, shell: str, url: str, port: str) -> None:
    result = subprocess.run(
        [shell, "-c", funcs + 'sx_parse_port "$1" || echo none', "sh", url],
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == (port or "none")


@pytest.mark.parametrize("shell", SHELLS)
def test_bridge_ports_deduplicated(funcs: str, shell: str) -> None:
    code = funcs + 'sx_bridge_ports; printf "%s;" $sx_ports'
    base = {"PATH": os.environ["PATH"]}
    same = base | {
        "OPENAI_BASE_URL": "http://localhost:3001/v1",
        "ANTHROPIC_BASE_URL": "http://localhost:3001/v1",
    }
    assert run_sh(code, shell, same) == "3001;"
    both = base | {
        "OPENAI_BASE_URL": "http://localhost:3001/v1",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4002",
    }
    assert run_sh(code, shell, both) == "3001;4002;"
    assert run_sh(code, shell, base | {"OPENAI_BASE_URL": "https://api.openai.com/v1"}) == ";"


@pytest.mark.parametrize("shell", SHELLS)
def test_env_allowed(funcs: str, shell: str) -> None:
    names = [
        "PATH",
        "HOME",
        "TZ",
        "EXTRA_OK",
        "OPENCODE_CONFIG",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "GOOGLE_X",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "EXTRA_OK2",
        "XOPENAI_X",
        "PATHX",
        "openai_x",
    ]
    code = funcs + 'for n in "$@"; do sx_env_allowed "$n" && echo "$n"; done'
    result = subprocess.run([shell, "-c", code, "sh", *names], capture_output=True, text=True)
    assert result.stdout.split() == [
        "PATH",
        "HOME",
        "TZ",
        "EXTRA_OK",
        "OPENCODE_CONFIG",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "GOOGLE_X",
    ]


@pytest.mark.parametrize("shell", SHELLS)
def test_expand_and_is_under(funcs: str, shell: str) -> None:
    code = funcs + textwrap.dedent(
        """
        sx_expand "~" /home/a
        sx_expand "~/.ssh" /home/a
        sx_expand "~/.ssh" /
        sx_expand "/etc/x" /home/a
        sx_expand "~x/y" /home/a
        sx_is_under /w/a /w && echo yes1
        sx_is_under /w /w || echo no2
        sx_is_under /wx /w || echo no3
        sx_is_under /w/a/b /w/ && echo yes4
        sx_is_under /anything / && echo yes5
        """
    )
    assert run_sh(code, shell).split("\n")[:-1] == [
        "/home/a",
        "/home/a/.ssh",
        "/.ssh",
        "/etc/x",
        "~x/y",
        "yes1",
        "no2",
        "no3",
        "yes4",
        "yes5",
    ]


# --------------------------------------------------------------------------- fake bwrap runs

FAKE_BWRAP = """\
import json, os, sys
argv = sys.argv[1:]
assert argv[:2] == ["--args", "4"], argv
data = b""
while True:
    chunk = os.read(4, 65536)
    if not chunk:
        break
    data += chunk
file_args = [a.decode() for a in data.split(b"\\0")[:-1]]
command = argv[2:]
with open(os.environ["FAKE_BWRAP_LOG"], "w") as f:
    json.dump({"file_args": file_args, "command": command}, f)
mode = os.environ.get("FAKE_BWRAP_MODE", "run")
if mode == "fail":
    print("bwrap: simulated setup failure", file=sys.stderr)
    sys.exit(1)
if mode == "record":
    os.write(3, b"started")
    sys.exit(0)
env = {} if "--clearenv" in file_args else dict(os.environ)
i = 0
while i < len(file_args):
    if file_args[i] == "--setenv":
        env[file_args[i + 1]] = file_args[i + 2]
        i += 3
    else:
        i += 1
os.execve(command[0], command, env)
"""

FAKE_SOCAT = """\
import json, os, socket, sys, time
with open(os.environ["FAKE_SOCAT_LOG"], "a") as f:
    f.write(json.dumps({"pid": os.getpid(), "argv": sys.argv[1:]}) + "\\n")
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if args and args[0].startswith("UNIX-LISTEN:"):
    path = args[0][len("UNIX-LISTEN:"):].split(",")[0]
    s = socket.socket(socket.AF_UNIX)
    s.bind(path)
    s.listen()
time.sleep(60)
"""


class Fakes:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.home = root / "home"
        self.home.mkdir()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.bwrap_log = root / "bwrap.json"
        self.socat_log = root / "socat.jsonl"
        for name, code in (("bwrap", FAKE_BWRAP), ("socat", FAKE_SOCAT)):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{code}")
            path.chmod(0o755)
        self.script = root / "sandbox-exec"

    def install(self, inner: InnerSandbox) -> None:
        self.script.write_text(
            render_sandbox_exec(inner, agent_home=str(self.home), workspace=str(self.workspace))
        )
        self.script.chmod(0o755)

    def run(
        self,
        *cmd: str,
        mode: str = "run",
        env: dict[str, str] | None = None,
        path: str | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        full_env = {
            "PATH": path if path is not None else f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_BWRAP_LOG": str(self.bwrap_log),
            "FAKE_BWRAP_MODE": mode,
            "FAKE_SOCAT_LOG": str(self.socat_log),
        } | (env or {})
        return subprocess.run(
            [str(self.script), *cmd],
            capture_output=True,
            text=True,
            env=full_env,
            input=stdin,
            timeout=60,
            cwd=self.workspace,
        )

    def bwrap_args(self) -> list[str]:
        data = json.loads(self.bwrap_log.read_text())
        args: list[str] = data["file_args"]
        return args

    def bwrap_command(self) -> list[str]:
        data = json.loads(self.bwrap_log.read_text())
        command: list[str] = data["command"]
        return command

    def socat_calls(self) -> list[dict[str, object]]:
        if not self.socat_log.exists():
            return []
        return [json.loads(line) for line in self.socat_log.read_text().splitlines()]


@pytest.fixture
def fakes(tmp_path: Path) -> Iterator[Fakes]:
    # unix socket paths must stay short (macOS: 104 bytes); sandbox-exec uses /tmp
    yield Fakes(tmp_path)


def pairs(args: list[str], flag: str, arity: int) -> list[tuple[str, ...]]:
    return [tuple(args[i + 1 : i + 1 + arity]) for i, a in enumerate(args) if a == flag]


@pytest.mark.skipif(os.geteuid() == 0, reason="root uses a different namespace/caps setup")
def test_bwrap_profile_arguments(fakes: Fakes) -> None:
    (fakes.home / ".ssh").mkdir()
    (fakes.home / ".netrc").write_text("machine x login y password AGENTEVAL_CANARY_1")
    secret_file = fakes.root / "service-token"
    secret_file.write_text("x")
    inner = bwrap_profile(
        hide_paths=[
            "~/.ssh",
            "~/.netrc",
            "~/.aws",
            str(secret_file),
            str(fakes.workspace / ".env"),
        ],
        extra_writable_paths=["~/.npm"],
    )
    (fakes.workspace / ".env").write_text("TOKEN=AGENTEVAL_CANARY_2")
    fakes.install(inner)

    result = fakes.run("true", mode="record")
    assert result.returncode == 0, result.stderr
    args = fakes.bwrap_args()
    home, ws = str(fakes.home), str(fakes.workspace)

    assert args[:5] == [
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
    ]
    assert "--share-net" not in args
    for flag in ("--cap-add", "--cap-drop"):
        assert flag not in args
    i = args.index("--ro-bind")
    assert args[i : i + 11] == [
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
    ]
    tmpfs = [p[0] for p in pairs(args, "--tmpfs", 1)]
    devnull = [p[1] for p in pairs(args, "--ro-bind", 2) if p[0] == "/dev/null"]
    assert f"{home}/.ssh" in tmpfs
    assert f"{home}/.aws" not in tmpfs + devnull  # does not exist -> not mounted
    assert f"{home}/.netrc" in devnull and str(secret_file) in devnull
    # hidden file below the workspace is hidden again after the workspace bind
    env_file = f"{ws}/.env"
    assert devnull.count(env_file) == 2
    bind_ws = args.index("--bind")
    assert args[bind_ws : bind_ws + 3] == ["--bind", ws, ws]
    assert max(i for i, a in enumerate(args) if a == env_file) > bind_ws
    # every hide (first pass) comes before the workspace bind
    assert args.index(f"{home}/.ssh") < bind_ws

    try_binds = pairs(args, "--bind-try", 2)
    for d in STATE_DIRS:
        assert (f"{home}/{d}", f"{home}/{d}") in try_binds
        assert (fakes.home / d).is_dir()  # created for the user
    assert (f"{home}/.npm", f"{home}/.npm") in try_binds
    assert (f"{home}/.config/opencode",) * 2 in try_binds

    assert "--clearenv" in args
    assert "--ro-bind-try" not in args  # resolver re-bind only with network: host
    command = fakes.bwrap_command()
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[3:] == ["sx-inner", PROXY_DIR, "", "true"]
    # no bridge URL -> no network at all, no relays
    assert fakes.socat_calls() == []
    assert not any(a == PROXY_DIR for a in args)


def test_env_is_allow_listed(fakes: Fakes) -> None:
    fakes.install(bwrap_profile(env_passthrough=["EXTRA_OK"]))
    env = {
        "AWS_SECRET_ACCESS_KEY": "AGENTEVAL_CANARY_aws",
        "GITHUB_TOKEN": "AGENTEVAL_CANARY_gh",
        "OPENCODE_CONFIG": "/home/agent/.config/opencode/opencode.json",
        "OPENAI_EXTRA": "line1\nAWS_SECRET_ACCESS_KEY=nope",
        "EXTRA_OK": "yes",
        "TZ": "UTC",
    }
    result = fakes.run("true", mode="record", env=env)
    assert result.returncode == 0, result.stderr
    setenv = {p[0]: p[1] for p in pairs(fakes.bwrap_args(), "--setenv", 2)}
    assert setenv["OPENCODE_CONFIG"] == env["OPENCODE_CONFIG"]
    assert setenv["OPENAI_EXTRA"] == env["OPENAI_EXTRA"]
    assert setenv["EXTRA_OK"] == "yes"
    assert setenv["TZ"] == "UTC"
    assert setenv["HOME"] == str(fakes.home)
    assert "PATH" in setenv
    for name in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "FAKE_BWRAP_LOG", "FAKE_SOCAT_LOG"):
        assert name not in setenv
    assert "AGENTEVAL_CANARY" not in json.dumps(fakes.bwrap_command())


def test_env_kept_without_clear_env(fakes: Fakes) -> None:
    fakes.install(bwrap_profile(clear_env=False))
    result = fakes.run(
        "sh", "-c", 'printf "%s" "$AWS_SECRET_ACCESS_KEY"', env={"AWS_SECRET_ACCESS_KEY": "c"}
    )
    assert (result.returncode, result.stdout) == (0, "c"), result.stderr
    assert "--clearenv" not in fakes.bwrap_args()
    assert "--setenv" not in fakes.bwrap_args()


def test_run_passes_stdin_env_and_exit_code(fakes: Fakes) -> None:
    fakes.install(bwrap_profile())
    result = fakes.run(
        "sh",
        "-c",
        'read line; printf "%s|%s|%s" "$line" "$OPENCODE_X" "${AWS_KEY-unset}"; exit 3',
        env={"OPENCODE_X": "1", "AWS_KEY": "secret"},
        stdin="hello\n",
    )
    assert result.returncode == 3, result.stderr
    assert result.stdout == "hello|1|unset"


def test_host_network_shares_net_and_skips_relay(fakes: Fakes) -> None:
    fakes.install(bwrap_profile(network="host"))
    result = fakes.run("true", mode="record", env={"OPENAI_BASE_URL": "http://localhost:3005/v1"})
    assert result.returncode == 0, result.stderr
    args = fakes.bwrap_args()
    assert args.index("--share-net") > args.index("--unshare-all")
    assert fakes.socat_calls() == []
    assert fakes.bwrap_command()[5] == ""
    # /run stays hidden; only the resolver file /etc/resolv.conf points to is re-bound
    assert ("/run",) in pairs(args, "--tmpfs", 1)
    resolv = os.path.realpath("/etc/resolv.conf")
    rebinds = pairs(args, "--ro-bind-try", 2)
    if resolv.startswith("/run/"):
        assert rebinds == [(resolv, resolv)]
        assert args.index("--ro-bind-try") > args.index("/run")
    else:
        assert rebinds == []


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(
    ("resolv", "rebind"),
    [
        ("/run/systemd/resolve/stub-resolv.conf", True),
        ("/etc/resolv.conf", False),
        ("/private/var/run/resolv.conf", False),
        ("", False),
    ],
)
def test_resolver_rebind_only_below_run(
    funcs: str, shell: str, tmp_path: Path, resolv: str, rebind: bool
) -> None:
    fake_readlink = tmp_path / "readlink"
    fake_readlink.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_RESOLV"\n')
    fake_readlink.chmod(0o755)
    code = funcs + 'sx_args=$1; : >"$sx_args"; sx_resolv_args; tr "\\0" "\\n" <"$sx_args"'
    result = subprocess.run(
        [shell, "-c", code, "sh", str(tmp_path / "args")],
        capture_output=True,
        text=True,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "FAKE_RESOLV": resolv},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == (["--ro-bind-try", resolv, resolv] if rebind else [])


def test_proxy_only_relays_bridge_port(fakes: Fakes) -> None:
    fakes.install(bwrap_profile())
    result = fakes.run(
        "true",
        mode="record",
        env={
            "OPENAI_BASE_URL": "http://localhost:3005/v1",
            "ANTHROPIC_BASE_URL": "http://localhost:3005/v1",
        },
    )
    assert result.returncode == 0, result.stderr
    calls = fakes.socat_calls()
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert isinstance(argv, list)
    listen = [a for a in argv if a.startswith("UNIX-LISTEN:")][0]
    sock = listen[len("UNIX-LISTEN:") :].split(",")[0]
    assert sock.startswith("/tmp/agenteval-sx.") and sock.endswith("/proxy/3005.sock")
    assert "TCP:127.0.0.1:3005" in argv
    args = fakes.bwrap_args()
    # read-only: the sandbox cannot add or replace entries next to the relay socket
    assert (str(Path(sock).parent), PROXY_DIR) in pairs(args, "--ro-bind", 2)
    assert PROXY_DIR not in [p[1] for p in pairs(args, "--bind", 2)]
    assert "--share-net" not in args
    command = fakes.bwrap_command()
    assert command[4:7] == [PROXY_DIR, " 3005", "true"]
    # cleanup: private temp dir removed, outer relay stopped
    assert not Path(sock).parent.parent.exists()
    pid = calls[0]["pid"]
    assert isinstance(pid, int)
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("outer socat relay still running")


def test_fail_closed_when_bwrap_cannot_start(fakes: Fakes) -> None:
    fakes.install(bwrap_profile())
    marker = fakes.workspace / "ran"
    result = fakes.run("touch", str(marker), mode="fail")
    assert result.returncode == 125
    assert "could not set up the sandbox" in result.stderr
    assert "NOT run" in result.stderr
    assert not marker.exists()


def test_fail_closed_without_bwrap(fakes: Fakes) -> None:
    fakes.install(bwrap_profile())
    marker = fakes.workspace / "ran"
    result = fakes.run("/usr/bin/touch", str(marker), path=str(fakes.root / "empty"))
    assert result.returncode == 125
    assert "bubblewrap (bwrap) is not installed" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(
    any(Path(d, "socat").exists() for d in ("/usr/bin", "/bin", "/usr/local/bin")),
    reason="needs a PATH without socat",
)
def test_fail_closed_without_socat(fakes: Fakes) -> None:
    (fakes.bin / "socat").unlink()
    fakes.install(bwrap_profile())
    marker = fakes.workspace / "ran"
    result = fakes.run(
        "/usr/bin/touch", str(marker), env={"OPENAI_BASE_URL": "http://localhost:3005/v1"}
    )
    assert result.returncode == 125
    assert "socat is not installed" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root may execute any file with an x bit")
def test_fail_closed_when_bwrap_not_executable(fakes: Fakes) -> None:
    (fakes.bin / "bwrap").chmod(0o644)
    fakes.install(bwrap_profile())
    marker = fakes.workspace / "ran"
    result = fakes.run("/usr/bin/touch", str(marker), path=str(fakes.bin))
    assert result.returncode == 125
    assert f"{fakes.bin}/bwrap is not executable" in result.stderr
    assert constants.BWRAP_GROUP in result.stderr
    assert not marker.exists()


def test_root_run_warns_and_drops_caps(fakes: Fakes) -> None:
    # a fake `id` makes the script take its root path without real root
    fake_id = fakes.bin / "id"
    fake_id.write_text('#!/bin/sh\nif [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
    fake_id.chmod(0o755)
    fakes.install(bwrap_profile())
    result = fakes.run("true", mode="record")
    assert result.returncode == 0, result.stderr
    warnings = [line for line in result.stderr.splitlines() if "warning" in line]
    assert len(warnings) == 1
    assert "running as root" in warnings[0] and "weaker boundary" in warnings[0]
    args = fakes.bwrap_args()
    if "--unshare-all" in args:  # no CAP_SYS_ADMIN+CAP_NET_ADMIN: userns-root mode
        i = args.index("--unshare-all")
        assert args[i : i + 5] == [
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--cap-drop",
            "ALL",
        ]
    assert ("/run",) in pairs(args, "--tmpfs", 1)


def test_unprivileged_run_does_not_warn(fakes: Fakes) -> None:
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    fakes.install(bwrap_profile())
    result = fakes.run("true", mode="record")
    assert result.returncode == 0, result.stderr
    assert "warning" not in result.stderr


def test_usage_error(fakes: Fakes) -> None:
    fakes.install(bwrap_profile())
    result = fakes.run()
    assert result.returncode == 125
    assert "usage" in result.stderr
