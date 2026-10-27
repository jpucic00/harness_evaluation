"""Reference cases for cap-06-expense-report (the visible tests plus hidden ones)."""

from decimal import Decimal

from agenteval.suites.reference_cases import Case

SETUP = '''
from expenses import format_report, monthly_totals, parse_csv, totals_by_category

VISIBLE = """date,category,description,amount
2026-01-03,food,Groceries,42.10
2026-01-09,travel,Train ticket,19.90
2026-01-15,food,Bakery,7.40
"""

EXPORT = """date,category,description,amount
2026-01-03,food,Groceries,42.10
2026-01-09,travel,"Train ticket, return",19.90

2026-02-01,food,"Pizza, salad and drinks",23.75
2026-02-11,travel,Taxi,0.10
2026-02-12,travel,Bus,0.20

"""

HEADER = "date,category,description,amount\\n"


def rows(text):
    return [(e.date, e.category, e.description, e.amount) for e in parse_csv(text)]
'''

CASES = [
    # visible tests
    Case(
        "parse_rows",
        "rows(VISIBLE)",
        [
            ("2026-01-03", "food", "Groceries", Decimal("42.10")),
            ("2026-01-09", "travel", "Train ticket", Decimal("19.90")),
            ("2026-01-15", "food", "Bakery", Decimal("7.40")),
        ],
    ),
    Case(
        "totals_by_category_visible",
        "totals_by_category(parse_csv(VISIBLE))",
        {"food": Decimal("49.50"), "travel": Decimal("19.90")},
    ),
    # hidden tests
    Case(
        "quoted_fields_and_exact_amounts",
        "rows(EXPORT)",
        [
            ("2026-01-03", "food", "Groceries", Decimal("42.10")),
            ("2026-01-09", "travel", "Train ticket, return", Decimal("19.90")),
            ("2026-02-01", "food", "Pizza, salad and drinks", Decimal("23.75")),
            ("2026-02-11", "travel", "Taxi", Decimal("0.10")),
            ("2026-02-12", "travel", "Bus", Decimal("0.20")),
        ],
    ),
    Case(
        "totals_by_category_exact",
        "totals_by_category(parse_csv(EXPORT))",
        {"food": Decimal("65.85"), "travel": Decimal("20.20")},
    ),
    Case(
        "monthly_totals",
        "list(monthly_totals(parse_csv(EXPORT)).items())",
        [("2026-01", Decimal("62.00")), ("2026-02", Decimal("24.05"))],
    ),
    Case(
        "monthly_totals_sorted",
        "list(monthly_totals(parse_csv(HEADER + '2026-03-01,a,x,1\\n2025-12-31,a,y,2\\n')))",
        ["2025-12", "2026-03"],
    ),
    Case(
        "format_report",
        "format_report(parse_csv(EXPORT))",
        "food: 65.85\ntravel: 20.20\nTOTAL: 86.05",
    ),
    Case(
        "format_report_ties",
        "format_report(parse_csv(HEADER + '2026-01-01,zoo,a,5.5\\n2026-01-02,art,b,5.50\\n'))",
        "art: 5.50\nzoo: 5.50\nTOTAL: 11.00",
    ),
    Case("header_only_export", "parse_csv(HEADER)", []),
    Case("empty_report", "format_report([])", "TOTAL: 0.00"),
]
