"""Reference cases for cap-09-arch-doc-qa: the agent's answers.json against the doc and code."""

from agenteval.suites.cases import Case

SETUP = """
import json
import re
from pathlib import Path

ANSWERS = json.loads(Path(WORKSPACE, "answers.json").read_text())


def text(key):
    return re.sub(r"\\s+", " ", str(ANSWERS[key]).strip().strip("`").lower()).strip(" .")


def service(key):
    return text(key).replace("_", "-").replace(" ", "-")


def services(key):
    return sorted(str(v).strip().strip("`").lower().replace("_", "-") for v in ANSWERS[key])


def number(key):
    value = ANSWERS[key]
    if isinstance(value, bool):
        raise TypeError(f"{key}: expected a number")
    if isinstance(value, str):
        value = float(re.sub(r"[^0-9.\\-]", "", value))
    return round(float(value), 2)


def letter(key):
    match = re.match(r"^\\(?([a-d])\\b", text(key))
    return match.group(1) if match else text(key)
"""

CASES = [
    Case("system_of_record", "service('q1')", "order-service"),
    Case("payment_caller", "service('q2')", "order-service"),
    Case(
        "order_placed_consumers",
        "services('q3')",
        ["analytics-ingest", "fulfillment-service", "notification-service"],
    ),
    Case("payment_outage", "letter('q4')", "b"),
    # gateway 99.99 * checkout-api 99.95 * inventory 99.9 * order 99.95 * payment 99.9
    Case("request_path_availability", "number('q5')", 99.69),
    # 900 - (40 + 120 + 90 + 200)
    Case("payment_latency_budget", "number('q6')", 450.0),
    Case("inventory_datastore", "text('q7')", "redis"),
    Case("shipments_topic", "text('q8')", "shipments.v2"),
    Case("ownership_violation", "service('q9')", "notification-service"),
    Case("orders_db_writers", "services('q10')", ["order-service"]),
    Case("analytics_pii_days", "number('q11')", 30.0),
    Case("inventory_on_call", "text('q12')", "team-stock"),
    Case("payment_provider_change", "service('q13')", "payment-service"),
]
