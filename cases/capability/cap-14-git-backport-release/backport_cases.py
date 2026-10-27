"""Reference cases for cap-14-git-backport-release: release/1.2, the tags and main."""

from agenteval.suites.reference_cases import Case

SETUP = """
import subprocess

REL = "release/1.2"


def git(*args):
    cmd = ["git", "-C", WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def ok(*args):
    return subprocess.run(["git", "-C", WORKSPACE, *args], capture_output=True).returncode == 0


def load(rev, path):
    namespace = {}
    exec(compile(git("show", f"{rev}:{path}") + "\\n", path, "exec"), namespace)
    return namespace


def by_subject(subject):
    lines = git("log", "--all", "--format=%H %s").splitlines()
    [sha] = [line.split(" ", 1)[0] for line in lines if line.split(" ", 1)[1] == subject]
    return sha


def release_to_cents(amount):
    return load(REL, "money.py")["to_cents"](amount)


def files(rev):
    return sorted(git("ls-tree", "-r", "--name-only", rev).split())
"""

CASES = [
    Case("release_rounds_half_up", "release_to_cents('0.125')", 13),
    Case("release_rounds_half_up_2", "release_to_cents('1.005')", 101),
    Case("release_whole_cents", "release_to_cents('12.34')", 1234),
    Case(
        "release_has_no_new_features",
        "files(REL)",
        [".gitignore", "VERSION", "money.py", "pytest.ini", "tests/test_money.py"],
    ),
    Case("release_version", "git('show', f'{REL}:VERSION')", "1.2.1"),
    Case("release_based_on_v120", "ok('merge-base', '--is-ancestor', 'v1.2.0', REL)", True),
    Case(
        "release_excludes_main_commits",
        "[ok('merge-base', '--is-ancestor', by_subject(s), REL) for s in "
        "('feat: add format_cents', 'chore: start 1.3.0 development')]",
        [False, False],
    ),
    Case("tag_is_annotated", "git('cat-file', '-t', 'v1.2.1')", "tag"),
    Case(
        "tag_on_release_tip", "git('rev-parse', 'v1.2.1^{commit}') == git('rev-parse', REL)", True
    ),
    Case(
        "old_tag_untouched",
        "git('rev-parse', 'v1.2.0^{commit}') == git('rev-list', '--max-parents=0', 'main')",
        True,
    ),
    Case(
        "main_untouched",
        "git('log', '--format=%s', 'main').splitlines()",
        [
            "chore: start 1.3.0 development",
            "fix: round half-cents up in to_cents",
            "feat: add format_cents",
            "Initial commit",
        ],
    ),
    Case("main_version", "git('show', 'main:VERSION')", "1.3.0-dev"),
    Case("tracked_files_clean", "git('status', '--porcelain', '--untracked-files=no')", ""),
]
