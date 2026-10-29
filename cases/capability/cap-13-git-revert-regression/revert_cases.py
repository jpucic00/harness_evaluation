"""Reference cases for cap-13-git-revert-regression: the revert commit and slugify behaviour."""

from agenteval.scorers.reference_cases import Case

SETUP = """
import subprocess


def git(*args):
    cmd = ["git", "-C", WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def by_subject(subject):
    lines = git("log", "--all", "--format=%H %s").splitlines()
    [sha] = [line.split(" ", 1)[0] for line in lines if line.split(" ", 1)[1] == subject]
    return sha


def is_ancestor(old, new="HEAD"):
    cmd = ["git", "-C", WORKSPACE, "merge-base", "--is-ancestor", old, new]
    return subprocess.run(cmd, capture_output=True).returncode == 0


BAD = by_subject("perf: precompile the slug pattern")
OLD_TIP = by_subject("docs: start a changelog")

from slug import slugify
from titles import titlecase
"""

CASES = [
    Case("branch", "git('branch', '--show-current')", "main"),
    Case("history_kept", "is_ancestor(OLD_TIP)", True),
    Case("one_commit_on_top", "int(git('rev-list', '--count', f'{OLD_TIP}..HEAD'))", 1),
    Case(
        "reverts_the_bad_commit",
        "f'This reverts commit {BAD}' in git('log', '-1', '--format=%B')",
        True,
    ),
    Case(
        "revert_touches_only_slug",
        "git('show', '--name-only', '--format=', 'HEAD').split()",
        ["slug.py"],
    ),
    Case("tracked_files_clean", "git('status', '--porcelain', '--untracked-files=no')", ""),
    Case("accents", "slugify('Crème Brûlée')", "creme-brulee"),
    Case("more_accents", "slugify('Ångström über Café')", "angstrom-uber-cafe"),
    Case("basic", "slugify('Hello, World!')", "hello-world"),
    Case("separator_kept", "slugify('Crème Brûlée', separator='_')", "creme_brulee"),
    Case("spaces", "slugify('  many   spaces  ')", "many-spaces"),
    Case("titlecase_kept", "titlecase('the lord of the rings')", "The Lord of the Rings"),
    Case("changelog_kept", "git('show', 'HEAD:CHANGELOG.md').splitlines()[0]", "# Changelog"),
]
