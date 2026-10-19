"""Placing and looking up orders."""

from shop.domain.order_model import Order
from shop.repositories import order_repository
from shop.services import customer_service


def place(order: Order) -> Order:
    customer_service.ensure_active(order.customer_id)
    order.save()
    return order


def latest_for_customer(customer_id: int) -> Order | None:
    orders = [o for o in order_repository.list_recent() if o.customer_id == customer_id]
    return orders[0] if orders else None
