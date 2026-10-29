"""Reference cases for cap-01-pagination (the visible tests plus hidden ones)."""

from agenteval.scorers.reference_cases import Case, Raises

SETUP = """
from pager import get_page, page_count
ITEMS = list(range(1, 11))
"""

ITEMS = list(range(1, 11))

CASES = [
    # visible tests
    Case("first_page", "get_page(list(range(10)), 1, 3)", [0, 1, 2]),
    Case("page_count_all_full", "page_count(9, 3)", 3),
    # hidden tests
    *(
        Case(f"page_{page}", f"get_page(ITEMS, {page}, 3)", expected)
        for page, expected in [(1, [1, 2, 3]), (2, [4, 5, 6]), (3, [7, 8, 9]), (4, [10]), (5, [])]
    ),
    *(
        Case(f"page_count_{total}_{per_page}", f"page_count({total}, {per_page})", expected)
        for total, per_page, expected in [
            (0, 3, 0),
            (1, 3, 1),
            (9, 3, 3),
            (10, 3, 4),
            (10, 1, 10),
            (10, 25, 1),
        ]
    ),
    Case(
        "every_item_on_one_page",
        "[get_page(ITEMS, p, 4) for p in range(1, page_count(len(ITEMS), 4) + 1)]",
        [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]],
    ),
    Case("page_zero", "get_page(ITEMS, 0, 3)", Raises("ValueError")),
    Case("per_page_zero", "get_page(ITEMS, 1, 0)", Raises("ValueError")),
    Case("page_count_per_page_zero", "page_count(5, 0)", Raises("ValueError")),
    Case("page_count_negative_total", "page_count(-1, 3)", Raises("ValueError")),
]
