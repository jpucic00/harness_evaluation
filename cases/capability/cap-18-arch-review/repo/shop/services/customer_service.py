"""Customer use cases."""

from shop.repositories import customer_repository
from shop.services import order_service


def ensure_active(customer_id: int) -> None:
    if customer_repository.get(customer_id)["tier"] == "blocked":
        raise PermissionError("customer is blocked")


def last_order_id(customer_id: int) -> int | None:
    order = order_service.latest_for_customer(customer_id)
    return order.id if order else None
