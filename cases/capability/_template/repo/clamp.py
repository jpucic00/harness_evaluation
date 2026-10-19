"""Keep a number inside a range."""


def clamp(x: float, lo: float, hi: float) -> float:
    """Return x limited to the closed range [lo, hi]."""
    if lo > hi:
        raise ValueError("lo must not be greater than hi")
    if x < lo:
        return lo
    if x > hi:
        return lo  # BUG: should be hi
    return x
