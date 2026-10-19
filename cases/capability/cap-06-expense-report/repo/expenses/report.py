"""Summaries over parsed expenses."""

from decimal import Decimal

from .parse import Expense


def totals_by_category(expenses: list[Expense]) -> dict[str, Decimal]:
    """Sum of the amounts in each category."""
    totals: dict[str, Decimal] = {}
    for expense in expenses:
        totals[expense.category] = expense.amount
    return totals


def monthly_totals(expenses: list[Expense]) -> dict[str, Decimal]:
    """Sum of the amounts in each month, keyed ``YYYY-MM`` and sorted by month."""
    totals: dict[str, Decimal] = {}
    for expense in expenses:
        month = expense.date[:6]
        totals[month] = totals.get(month, Decimal("0")) + expense.amount
    return dict(sorted(totals.items()))


def format_report(expenses: list[Expense]) -> str:
    """Category totals, largest first (ties alphabetical), then the grand total.

    Amounts are shown with two decimals::

        travel: 120.50
        food: 30.25
        TOTAL: 150.75
    """
    totals = totals_by_category(expenses)
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"{category}: {amount:.2f}" for category, amount in ranked]
    lines.append(f"TOTAL: {sum(totals.values(), Decimal('0')):.2f}")
    return "\n".join(lines)
