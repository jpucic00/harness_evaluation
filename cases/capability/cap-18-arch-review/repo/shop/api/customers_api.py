"""HTTP handlers for customers."""

from shop.services import customer_service


def get_last_order(customer_id: int) -> tuple[int, dict[str, object]]:
    order_id = customer_service.last_order_id(customer_id)
    if order_id is None:
        return 404, {"error": "no orders"}
    return 200, {"order_id": order_id}
