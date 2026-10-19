def mean(values: list[float]) -> float:
    """Arithmetic mean of ``values``."""
    return sum(values) / len(values)  # BUG: crashes on an empty list


def spread(values: list[float]) -> float:
    """Difference between the largest and the smallest value (0.0 for no values)."""
    return max(values) - min(values) if values else 0.0
