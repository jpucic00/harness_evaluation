"""Order pricing. All amounts are integer cents."""


def total(unit_cents: int, qty: int) -> int:
    """Total for qty units."""
    subtotal = unit_cents * qty
    return subtotal
