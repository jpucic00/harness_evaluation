"""Reference cases for cap-08-api-spec-qa: the agent's answers.json against openapi.yaml."""

from agenteval.scorers.reference_cases import Case

SETUP = """
import json
import re
from pathlib import Path

ANSWERS = json.loads(Path(WORKSPACE, "answers.json").read_text())


def text(key):
    return str(ANSWERS[key]).strip().strip("`").strip()


def lower(key):
    return text(key).lower()


def num(key):
    value = ANSWERS[key]
    if isinstance(value, bool):
        raise TypeError(f"{key}: expected a number")
    if isinstance(value, str):
        value = float(re.sub(r"[^0-9.\\-]", "", value))
    if float(value) != int(value):
        raise ValueError(f"{key}: not an integer")
    return int(value)


def names(key):
    return sorted(str(v).strip().strip("`").lower() for v in ANSWERS[key])


def endpoint(key):
    method, _, path = text(key).partition(" ")
    return f"{method.upper()} {path.strip()}"
"""

CASES = [
    Case("create_status", "num('q1')", 201),
    Case("insufficient_stock_status", "num('q2')", 409),
    Case("required_fields", "names('q3')", ["quantity", "sku", "warehouse_id"]),
    Case("max_page_size", "num('q4')", 200),
    Case("list_query_params", "names('q5')", ["cursor", "limit", "updated_since", "warehouse_id"]),
    Case("cancel_scope", "lower('q6')", "reservations:cancel"),
    Case("idempotency_header", "lower('q7')", "idempotency-key"),
    Case("idempotency_conflict_status", "num('q8')", 422),
    Case("default_expiry_minutes", "num('q9')", 15),
    Case("reservation_statuses", "names('q10')", ["active", "cancelled", "expired", "fulfilled"]),
    Case("deprecated_endpoint", "endpoint('q11')", "GET /items/{sku}/stock"),
    Case("rate_limit", "num('q12')", 600),
    Case("production_url", "lower('q13').rstrip('/')", "https://api.example.internal/inventory/v2"),
]
