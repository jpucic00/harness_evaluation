"""Money helpers."""

from decimal import Decimal


def to_cents(amount: str) -> int:
    """Parse a decimal amount such as "12.34" into integer cents."""
    return int((Decimal(amount) * 100).quantize(Decimal("1")))
