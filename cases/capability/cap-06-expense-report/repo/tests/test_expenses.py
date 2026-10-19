from decimal import Decimal

from expenses import parse_csv, totals_by_category

EXPORT = """date,category,description,amount
2026-01-03,food,Groceries,42.10
2026-01-09,travel,Train ticket,19.90
2026-01-15,food,Bakery,7.40
"""


def test_parse_rows() -> None:
    expenses = parse_csv(EXPORT)
    assert [e.category for e in expenses] == ["food", "travel", "food"]
    assert expenses[1].description == "Train ticket"


def test_totals_by_category() -> None:
    totals = totals_by_category(parse_csv(EXPORT))
    assert totals == {"food": Decimal("49.50"), "travel": Decimal("19.90")}
