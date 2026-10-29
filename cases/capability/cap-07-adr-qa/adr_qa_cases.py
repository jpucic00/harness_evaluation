"""Reference cases for cap-07-adr-qa: the agent's answers.json against the ADRs."""

from agenteval.scorers.reference_cases import Case

SETUP = '''
import json
import re
from pathlib import Path

ANSWERS = json.loads(Path(WORKSPACE, "answers.json").read_text())


def text(key):
    return re.sub(r"\\s+", " ", str(ANSWERS[key]).strip().lower()).strip(" .")


def pick(key, aliases):
    """The canonical option the answer names, if it names exactly one; else the answer."""
    answer = text(key)
    found = {
        canon
        for alias, canon in aliases.items()
        if re.search(rf"\\b{re.escape(alias)}\\b", answer)
    }
    return found.pop() if len(found) == 1 else answer


def num(key):
    value = ANSWERS[key]
    if isinstance(value, bool):
        raise TypeError(f"{key}: expected a number")
    if isinstance(value, str):
        value = float(re.sub(r"[^0-9.\\-]", "", value))
    if float(value) != int(value):
        raise ValueError(f"{key}: not an integer")
    return int(value)


def nums(key):
    return sorted(int(v) for v in ANSWERS[key])


def letter(key):
    match = re.match(r"^\\(?([a-d])\\b", text(key))
    return match.group(1) if match else text(key)


BROKERS = {"kafka": "kafka", "msk": "kafka", "rabbitmq": "rabbitmq", "rabbit": "rabbitmq"}
DATABASES = {
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "mongodb": "mongodb",
    "mongo": "mongodb",
}
'''

CASES = [
    Case("current_broker", "pick('q1', BROKERS)", "kafka"),
    Case("superseded_adr", "num('q2')", 3),
    Case("accepted_adrs", "nums('q3')", [1, 2, 4, 5, 8]),
    Case("proposed_adr", "num('q4')", 6),
    Case("retention_days", "num('q5')", 7),
    Case("partition_key", "text('q6').strip('`')", "order_id"),
    Case("rejection_reason", "letter('q7')", "b"),
    Case("access_token_minutes", "num('q8')", 15),
    Case("catalog_database", "pick('q9', DATABASES)", "postgresql"),
    Case("messaging_accepted_on", "text('q10')", "2025-03-11"),
    Case("adr_for_broker_choice", "num('q11')", 5),
    Case("rejected_count", "num('q12')", 1),
]
