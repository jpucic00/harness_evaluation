"""Reference cases for cap-15-git-untrack-files: tracked files, ignore rules and local copies."""

from agenteval.scorers.reference_cases import Case

SETUP = """
import subprocess
from pathlib import Path

REMOVED = ["config.local.ini", "dist/app.bundle.js", "dist/app.bundle.js.map", "server.log"]


def git(*args):
    cmd = ["git", "-C", WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def ignored(path):
    cmd = ["git", "-C", WORKSPACE, "check-ignore", "-q", "--no-index", path]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def tracked():
    return sorted(git("ls-tree", "-r", "--name-only", "HEAD").split())
"""

CASES = [
    Case("removed_from_head", "[p for p in REMOVED if p in tracked()]", []),
    Case("local_copies_kept", "[Path(WORKSPACE, p).is_file() for p in REMOVED]", [True] * 4),
    Case(
        "local_config_content_kept",
        "Path(WORKSPACE, 'config.local.ini').read_text().split()[-1]",
        "true",
    ),
    Case("removed_files_ignored", "[ignored(p) for p in REMOVED]", [True] * 4),
    Case("future_build_output_ignored", "ignored('dist/vendor.bundle.js')", True),
    Case("example_config_tracked", "'config.example.ini' in tracked()", True),
    Case("example_config_not_ignored", "ignored('config.example.ini')", False),
    Case(
        "source_not_ignored",
        "[ignored(p) for p in ('settings.py', 'src/new_module.py', 'tests/test_x.py')]",
        [False] * 3,
    ),
    Case("pycache_still_ignored", "ignored('__pycache__/app.cpython-312.pyc')", True),
    Case(
        "still_tracked",
        "[p for p in ('.gitignore', 'settings.py', 'pytest.ini', 'tests/test_settings.py')"
        " if p not in tracked()]",
        [],
    ),
    Case("one_new_commit", "int(git('rev-list', '--count', 'HEAD'))", 2),
    Case(
        "history_not_rewritten",
        "'server.log' in git('ls-tree', '-r', '--name-only', 'HEAD~1').split()",
        True,
    ),
    Case("status_clean", "git('status', '--porcelain')", ""),
]
