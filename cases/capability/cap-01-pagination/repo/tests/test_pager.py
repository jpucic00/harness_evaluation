from pager import get_page, page_count


def test_first_page() -> None:
    assert get_page(list(range(10)), 1, 3) == [0, 1, 2]


def test_page_count_when_all_pages_are_full() -> None:
    assert page_count(9, 3) == 3
