"""Reference cases for cap-10-git-commit-split: the repository's commits and working tree."""

from agenteval.suites.cases import Case

SETUP = """
import re
import subprocess
from pathlib import Path


def git(*args):
    cmd = ["git", "-C", WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def load(rev, path):
    namespace = {}
    exec(compile(git("show", f"{rev}:{path}") + "\\n", path, "exec"), namespace)
    return namespace


def new_commits():
    return git("rev-list", "HEAD", "--not", git("rev-list", "--max-parents=0", "HEAD")).split()


def commit_type(sha):
    subject = git("log", "-1", "--format=%s", sha)
    return re.match(r"^([a-z]+)(\\([^)]*\\))?!?: \\S", subject).group(1)


def files_of(kind):
    [sha] = [c for c in new_commits() if commit_type(c) == kind]
    return sorted(git("show", "--name-only", "--format=", sha).split())
"""

CASES = [
    Case("commit_count", "int(git('rev-list', '--count', 'HEAD'))", 3),
    Case("branch", "git('branch', '--show-current')", "main"),
    Case("commit_types", "sorted(commit_type(c) for c in new_commits())", ["docs", "fix"]),
    Case("fix_commit_files", "files_of('fix')", ["tests/test_word_count.py", "textstats.py"]),
    Case("docs_commit_files", "files_of('docs')", ["README.md"]),
    Case(
        "scratch_log_never_committed",
        "'scratch.log' in git('log', '--all', '--name-only', '--format=').split()",
        False,
    ),
    Case("scratch_log_kept", "Path(WORKSPACE, 'scratch.log').is_file()", True),
    Case("tracked_files_clean", "git('status', '--porcelain', '--untracked-files=no')", ""),
    Case("committed_fix", "load('HEAD', 'textstats.py')['word_count']('a  b\\n c')", 3),
    Case("committed_fix_empty", "load('HEAD', 'textstats.py')['word_count']('')", 0),
    Case(
        "committed_readme",
        "git('show', 'HEAD:README.md').splitlines()[2]",
        "Counts words and lines in plain text.",
    ),
    Case(
        "root_commit_untouched",
        "git('log', '-1', '--format=%s', git('rev-list', '--max-parents=0', 'HEAD'))",
        "Initial commit",
    ),
    Case(
        "author",
        "sorted({git('log', '-1', '--format=%ae', c) for c in new_commits()})",
        ["dana@example.com"],
    ),
]
