"""Reference cases for cap-11-git-branch-push: local branches, the bare origin and tracking."""

from agenteval.suites.reference_cases import Case

SETUP = """
import subprocess

BRANCH = "feature/rate-limiter"


def git(*args, repo=None):
    cmd = ["git", "-C", repo or WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def origin(*args):
    return git(*args, repo=git("remote", "get-url", "origin"))


def root():
    return git("rev-list", "--max-parents=0", "HEAD")


def load(rev, path):
    namespace = {}
    exec(compile(git("show", f"{rev}:{path}") + "\\n", path, "exec"), namespace)
    return namespace
"""

CASES = [
    Case("on_feature_or_main", "git('branch', '--show-current') in (BRANCH, 'main')", True),
    Case("feature_commits", "int(git('rev-list', '--count', f'main..{BRANCH}'))", 1),
    Case(
        "feature_based_on_main", "git('rev-parse', f'{BRANCH}~1') == git('rev-parse', 'main')", True
    ),
    Case(
        "feature_files",
        "sorted(git('show', '--name-only', '--format=', BRANCH).split())",
        ["ratelimit.py", "tests/test_ratelimit.py"],
    ),
    Case("main_untouched", "git('rev-parse', 'main') == root()", True),
    Case("origin_main_untouched", "origin('rev-parse', 'main') == root()", True),
    Case(
        "pushed",
        "origin('rev-parse', BRANCH) == git('rev-parse', BRANCH)",
        True,
    ),
    Case(
        "upstream",
        "git('rev-parse', '--abbrev-ref', f'{BRANCH}@{{upstream}}')",
        "origin/feature/rate-limiter",
    ),
    Case(
        "origin_branches",
        "sorted(origin('for-each-ref', '--format=%(refname)', 'refs/heads').split())",
        ["refs/heads/feature/rate-limiter", "refs/heads/main"],
    ),
    Case(
        "committed_limiter",
        "result = load(BRANCH, 'ratelimit.py')['RateLimiter'](1, 10)\n"
        "result = [result.allow('x', t) for t in (0, 5, 10)]",
        [True, False, True],
    ),
    Case("tracked_files_clean", "git('status', '--porcelain', '--untracked-files=no')", ""),
    Case("no_stray_untracked", "git('status', '--porcelain')", ""),
]
