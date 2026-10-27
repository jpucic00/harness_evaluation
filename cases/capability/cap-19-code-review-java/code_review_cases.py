"""Reference cases for cap-19-code-review-java: findings.json against the planted defects.

A finding matches a planted defect when its category is equal and its file and line
are one of the defect's accepted locations. OrderSummary.java and findByStatus are
correct on purpose and must not be reported.
"""

from agenteval.suites.reference_cases import Case

SETUP = """
import json
from pathlib import Path

J = "src/main/java/com/example/orders/"
PLANTED = {
    "sql_injection": ("security", J + "OrderRepository.java", range(50, 55)),
    "unclosed_connection": ("resource-leak", J + "OrderRepository.java", range(48, 53)),
    "swallowed_exception": ("error-handling", J + "OrderRepository.java", range(57, 61)),
    "remote_call_per_order": ("performance", J + "OrderService.java", range(24, 28)),
    "page_off_by_one": ("correctness", J + "OrderService.java", range(34, 39)),
    "string_identity": ("correctness", J + "OrderService.java", range(46, 49)),
    "equals_without_hashcode": ("correctness", J + "OrderKey.java", range(3, 16)),
    "shared_date_format": ("concurrency", J + "ReportFormatter.java", [*range(8, 11), 14]),
}


def norm_file(value):
    return str(value).strip().replace("\\\\", "/").removeprefix("./")


FINDINGS = [
    (norm_file(f["file"]), str(f["category"]).strip().lower(), int(f["line"]))
    for f in json.loads(Path(WORKSPACE, "findings.json").read_text())
]


def hits(planted):
    category, file, lines = planted
    return [f for f in FINDINGS if f[1] == category and f[0] == file and f[2] in lines]


def found(key):
    return bool(hits(PLANTED[key]))


def unmatched():
    matched = {f for planted in PLANTED.values() for f in hits(planted)}
    return [f for f in FINDINGS if f not in matched]
"""

CASES = [
    *[
        Case(f"found_{key}", f"found({key!r})", True)
        for key in (
            "sql_injection",
            "unclosed_connection",
            "swallowed_exception",
            "remote_call_per_order",
            "page_off_by_one",
            "string_identity",
            "equals_without_hashcode",
            "shared_date_format",
        )
    ],
    Case(
        "decoy_summary_not_reported",
        "[f for f in FINDINGS if f[0].endswith('OrderSummary.java')]",
        [],
    ),
    Case(
        "decoy_find_by_status_not_reported",
        "[f for f in unmatched() if f[0].endswith('OrderRepository.java') and 31 <= f[2] <= 44]",
        [],
    ),
    Case("at_most_three_other_findings", "len(unmatched()) <= 3", True),
]
