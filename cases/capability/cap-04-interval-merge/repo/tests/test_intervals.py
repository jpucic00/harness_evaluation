from intervals import merge


def test_merge_overlapping() -> None:
    assert merge([(1, 3), (2, 6), (8, 10)]) == [(1, 6), (8, 10)]


def test_merge_unsorted_input() -> None:
    assert merge([(8, 10), (2, 6), (1, 3)]) == [(1, 6), (8, 10)]
