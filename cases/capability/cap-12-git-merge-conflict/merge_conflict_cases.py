"""Reference cases for cap-12-git-merge-conflict: the merge commit and the merged pricing."""

from agenteval.suites.reference_cases import Case

SETUP = """
import re
import subprocess
from pathlib import Path


def git(*args):
    cmd = ["git", "-C", WORKSPACE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def subject(rev):
    return git("log", "-1", "--format=%s", rev)


def parents(rev="HEAD"):
    return git("log", "-1", "--format=%P", rev).split()


def tracked_with_markers():
    found = []
    for path in git("ls-files").split():
        text = Path(WORKSPACE, path).read_text(errors="replace")
        if re.search(r"^(<{7}|>{7}|={7})( |$)", text, re.MULTILINE):
            found.append(path)
    return found


def test_names():
    text = git("show", "HEAD:tests/test_pricing.py")
    return sorted(re.findall(r"^def (test_\\w+)", text, re.MULTILINE))


from pricing import total
"""

CASES = [
    Case("branch", "git('branch', '--show-current')", "feature/bulk-discount"),
    Case("head_is_merge", "len(parents())", 2),
    Case("first_parent_is_feature", "subject(parents()[0])", "feat: bulk discount for 10+ units"),
    Case("second_parent_is_main", "parents()[1] == git('rev-parse', 'main')", True),
    Case("main_untouched", "subject('main')", "feat: charge 25% VAT"),
    Case("main_commit_count", "int(git('rev-list', '--count', 'main'))", 3),
    Case("no_conflict_markers", "tracked_with_markers()", []),
    Case("merge_finished", "Path(WORKSPACE, '.git', 'MERGE_HEAD').exists()", False),
    Case("tracked_files_clean", "git('status', '--porcelain', '--untracked-files=no')", ""),
    Case(
        "tests_from_both_sides",
        "test_names()",
        ["test_bulk_discount", "test_single_unit", "test_vat"],
    ),
    Case("vat_only", "total(1000, 1)", 1250),
    Case("vat_rounds_half_up", "total(333, 3)", 1249),
    Case("discount_then_vat", "total(1000, 10)", 11875),
    Case("discount_rounds_down_then_vat", "total(199, 12)", 2836),
    Case("below_threshold", "total(100, 9)", 1125),
    Case("zero", "total(0, 50)", 0),
]
