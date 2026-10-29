"""Reference cases for cap-18-arch-review: findings.json against the planted violations.

A finding matches a planted violation when its rule is equal and its file and line
are one of the violation's accepted locations (the import or the offending
statements). The decoy modules follow the rules and must not be reported.
"""

from agenteval.scorers.reference_cases import Case

SETUP = """
import json
from pathlib import Path

PLANTED = {
    "api_uses_repository": (
        "layer-violation",
        [("shop/api/orders_api.py", range(3, 10))],
    ),
    "domain_imports_db": (
        "domain-impurity",
        [("shop/domain/order_model.py", [*range(5, 8), *range(20, 24)])],
    ),
    "repository_imports_service": (
        "layer-violation",
        [("shop/repositories/customer_repository.py", [*range(3, 6), *range(9, 12)])],
    ),
    "service_cycle": (
        "dependency-cycle",
        [
            ("shop/services/order_service.py", range(4, 7)),
            ("shop/services/customer_service.py", range(3, 6)),
        ],
    ),
    "module_level_cache": (
        "global-mutable-state",
        [("shop/services/pricing_service.py", range(4, 14))],
    ),
    "direct_http_call": (
        "direct-external-call",
        [("shop/services/shipping_service.py", [*range(3, 6), *range(16, 21)])],
    ),
    "query_per_order": (
        "n-plus-one-query",
        [("shop/services/sales_report_service.py", range(7, 11))],
    ),
}
DECOYS = [
    "shop/adapters/carrier_client.py",
    "shop/api/customers_api.py",
    "shop/domain/money_value.py",
    "shop/services/inventory_service.py",
    "shop/services/order_listing_service.py",
]


def norm_file(value):
    return str(value).strip().replace("\\\\", "/").removeprefix("./")


FINDINGS = [
    (norm_file(f["file"]), str(f["rule"]).strip().lower(), int(f["line"]))
    for f in json.loads(Path(WORKSPACE, "findings.json").read_text())
]


def hits(planted):
    rule, locations = planted
    return [
        f for f in FINDINGS
        if f[1] == rule and any(f[0] == file and f[2] in lines for file, lines in locations)
    ]


def found(key):
    return bool(hits(PLANTED[key]))


def decoy_findings():
    return sorted({f[0] for f in FINDINGS if f[0] in DECOYS})


def unmatched():
    matched = {f for planted in PLANTED.values() for f in hits(planted)}
    return len([f for f in FINDINGS if f not in matched])
"""

CASES = [
    Case("found_api_uses_repository", "found('api_uses_repository')", True),
    Case("found_domain_imports_db", "found('domain_imports_db')", True),
    Case("found_repository_imports_service", "found('repository_imports_service')", True),
    Case("found_service_cycle", "found('service_cycle')", True),
    Case("found_module_level_cache", "found('module_level_cache')", True),
    Case("found_direct_http_call", "found('direct_http_call')", True),
    Case("found_query_per_order", "found('query_per_order')", True),
    Case("decoys_not_reported", "decoy_findings()", []),
    Case("cycle_reported_once", "len(hits(PLANTED['service_cycle'])) <= 2", True),
    Case("at_most_two_other_findings", "unmatched() <= 2", True),
]
