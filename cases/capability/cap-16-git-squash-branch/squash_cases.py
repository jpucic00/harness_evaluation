"""Reference cases for cap-16-git-squash-branch: one commit on top of main, identical tree."""

from agenteval.suites.cases import Case

SETUP = """
import subprocess

BRANCH = "feature/csv-export"
# Tree of the branch tip that setup.sh creates (file contents and modes only, so it
# does not depend on commit dates or authors).
ORIGINAL_TREE = "5f4bc0c1e4c1a904c8c423f7a323f80a7d650704"


def git(*args):
    cmd = ["git", "-C", WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


from export import to_csv
"""

CASES = [
    Case("branch", "git('branch', '--show-current')", "feature/csv-export"),
    Case("one_commit", "int(git('rev-list', '--count', f'main..{BRANCH}'))", 1),
    Case("parent_is_main", "git('rev-parse', f'{BRANCH}~1') == git('rev-parse', 'main')", True),
    Case("message", "git('log', '-1', '--format=%s', BRANCH)", "feat: add CSV export"),
    Case("same_tree", "git('rev-parse', f'{BRANCH}^{{tree}}') == ORIGINAL_TREE", True),
    Case(
        "files",
        "sorted(git('show', '--name-only', '--format=', BRANCH).split())",
        ["export.py", "tests/test_export.py"],
    ),
    Case("main_untouched", "git('log', '--format=%s', 'main').splitlines()", ["Initial commit"]),
    Case("author_kept", "git('log', '-1', '--format=%ae', BRANCH)", "dana@example.com"),
    Case("tracked_files_clean", "git('status', '--porcelain', '--untracked-files=no')", ""),
    Case("export_header", "to_csv().splitlines()[0]", "region,units,revenue_cents"),
    Case("export_rows", "to_csv().splitlines()[1:]", ["north,12,45000", "south,7,21000"]),
    Case("export_line_endings", "'\\r' in to_csv()", False),
]
