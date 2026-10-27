"""Reference cases for cap-04-interval-merge (the visible tests plus hidden ones)."""

from agenteval.suites.reference_cases import Case, Raises

SETUP = "from intervals import merge, total_length"

CASES = [
    # visible tests
    Case("merge_overlapping", "merge([(1, 3), (2, 6), (8, 10)])", [(1, 6), (8, 10)]),
    Case("merge_unsorted", "merge([(8, 10), (2, 6), (1, 3)])", [(1, 6), (8, 10)]),
    # hidden tests
    Case("touching_merge", "merge([(3, 5), (1, 3)])", [(1, 5)]),
    Case("nested_keep_outer_end", "merge([(1, 10), (2, 3), (4, 5)])", [(1, 10)]),
    Case("separate_stay_separate", "merge([(3, 4), (1, 2)])", [(1, 2), (3, 4)]),
    Case("empty", "merge([])", []),
    Case("single", "merge([(4, 7)])", [(4, 7)]),
    Case("degenerate", "merge([(3, 3)])", [(3, 3)]),
    Case(
        "input_not_modified",
        "data = [(5, 8), (1, 6)]\nmerge(data)\nresult = data",
        [(5, 8), (1, 6)],
    ),
    Case("invalid_interval", "merge([(1, 2), (5, 1)])", Raises("ValueError")),
    Case("total_length_overlap", "total_length([(1, 5), (2, 3), (10, 12)])", 6),
    Case("total_length_touching", "total_length([(4, 6), (0, 2), (2, 4)])", 6),
    Case("total_length_empty", "total_length([])", 0),
    Case("total_length_degenerate", "total_length([(3, 3)])", 0),
]
