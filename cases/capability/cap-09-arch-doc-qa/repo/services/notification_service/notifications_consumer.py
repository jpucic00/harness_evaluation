"""notification-service: tells customers about their orders."""

from services.common.db import connect
from services.common.kafka import consume
from services.notification_service import notifications_config as config
from services.notification_service import notifications_email as email


def run() -> None:
    for event in consume(config.ORDERS_TOPIC, group="notifications"):
        if event.name != "OrderPlaced":
            continue
        with connect(config.ORDERS_REPLICA_DSN) as db:
            customer = db.query_one(
                "SELECT email, first_name FROM customers WHERE id = %s",
                [event.payload["customer_id"]],
            )
        email.send(customer["email"], "order_placed", {"name": customer["first_name"]})
