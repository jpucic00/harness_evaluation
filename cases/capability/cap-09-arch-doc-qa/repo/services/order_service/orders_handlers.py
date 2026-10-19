"""order-service: the order state machine."""

from services.common.db import connect
from services.common.grpc import Channel
from services.order_service import orders_config as config
from services.order_service import orders_outbox as outbox

payments = Channel("payment-service:50051").stub("PaymentService")


def place_order(cart: dict) -> dict:
    with connect(config.ORDERS_DSN) as db:
        order = db.insert("orders", {"cart": cart, "state": "NEW"})
        outbox.add(db, config.ORDERS_TOPIC, "OrderPlaced", order)
    try:
        payments.authorise(order["id"], cart["total"])
    except ConnectionError:
        return _set_state(order, "PENDING_PAYMENT")
    return _set_state(order, "PAID")


def _set_state(order: dict, state: str) -> dict:
    with connect(config.ORDERS_DSN) as db:
        return db.update("orders", order["id"], {"state": state})
