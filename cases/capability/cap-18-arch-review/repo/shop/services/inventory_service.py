"""Stock lookups, cached in the shared infra cache."""

from shop.infra.database import cache
from shop.repositories import stock_repository


def stock(sku: str) -> int:
    cached = cache.get(f"stock:{sku}")
    if isinstance(cached, int):
        return cached
    qty = stock_repository.quantity(sku)
    cache.set(f"stock:{sku}", qty)
    return qty
