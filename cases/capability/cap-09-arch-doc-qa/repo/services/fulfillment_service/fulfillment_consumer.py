"""fulfillment-service: picks placed orders and ships paid ones."""

from services.common.kafka import consume, publish
from services.fulfillment_service import fulfillment_config as config
from services.fulfillment_service import picking


def run() -> None:
    for event in consume(config.ORDERS_TOPIC, group="fulfillment"):
        if event.name == "OrderPlaced":
            picking.create_pick_list(event.payload)
        elif event.name == "OrderPaid":
            shipment = {"order_id": event.payload["id"], "carrier": "dhl"}
            publish(config.SHIPMENTS_TOPIC, "ShipmentCreated", shipment)
