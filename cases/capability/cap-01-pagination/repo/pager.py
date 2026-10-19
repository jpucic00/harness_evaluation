"""Split a list of items into numbered pages."""


def page_count(total: int, per_page: int) -> int:
    """Return how many pages are needed to show ``total`` items."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if total < 0:
        raise ValueError("total must not be negative")
    return total // per_page


def get_page[T](items: list[T], page: int, per_page: int) -> list[T]:
    """Return the items shown on ``page``. Pages are numbered from 1.

    Asking for a page past the end returns an empty list.
    """
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    if page < 1:
        raise ValueError("pages are numbered from 1")
    start = page * per_page
    return items[start : start + per_page]
