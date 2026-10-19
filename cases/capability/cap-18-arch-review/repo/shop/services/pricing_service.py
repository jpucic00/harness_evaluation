"""Discounts."""

from decimal import Decimal

_DISCOUNTS: dict[str, Decimal] = {}

TIER_DISCOUNTS = (("gold", Decimal("0.10")), ("silver", Decimal("0.05")))


def loyalty_discount(tier: str) -> Decimal:
    if tier not in _DISCOUNTS:
        _DISCOUNTS[tier] = dict(TIER_DISCOUNTS).get(tier, Decimal("0"))
    return _DISCOUNTS[tier]
