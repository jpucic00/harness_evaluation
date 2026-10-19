"""Read the CSV export of the bank's expense tracker."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Expense:
    date: str
    """ISO date, ``YYYY-MM-DD``."""
    category: str
    description: str
    amount: Decimal


def parse_csv(text: str) -> list[Expense]:
    """Parse an export whose header row is ``date,category,description,amount``.

    Fields use standard CSV quoting, so a quoted description may contain
    commas. Blank lines are ignored. Amounts are money and must stay exact.
    """
    lines = text.strip().split("\n")
    expenses: list[Expense] = []
    for line in lines[1:]:
        date, category, description, amount = line.split(",")
        expenses.append(Expense(date, category, description, Decimal(float(amount))))
    return expenses
