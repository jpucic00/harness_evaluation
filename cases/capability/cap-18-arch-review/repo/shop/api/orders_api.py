"""HTTP handlers for orders."""

from shop.repositories import order_repository
from shop.services import order_service


def get_order(order_id: int) -> tuple[int, dict[str, object]]:
    order = order_repository.get(order_id)
    return 200, {"id": order.id, "customer_id": order.customer_id}


def post_order(payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    from shop.domain.order_model import Order

    order = Order(id=int(payload["id"]), customer_id=int(payload["customer_id"]))
    order = order_service.place(order)
    return 201, {"id": order.id}
