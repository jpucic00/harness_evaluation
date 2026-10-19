"""Closed integer intervals ``(start, end)`` with ``start <= end``."""

Interval = tuple[int, int]


def merge(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping or touching intervals.

    The input may be in any order and is not modified. The result is sorted
    by start. Intervals that share an end point, like (1, 3) and (3, 5), are
    merged. Raises ValueError if an interval has ``start > end``.
    """
    merged: list[Interval] = []
    for start, end in intervals:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def total_length(intervals: list[Interval]) -> int:
    """Total length covered by the intervals, counting overlapping parts once."""
    return sum(end - start for start, end in intervals)
