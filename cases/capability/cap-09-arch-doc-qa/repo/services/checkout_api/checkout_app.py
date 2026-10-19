"""checkout-api: orchestrates a checkout."""

from services.checkout_api import checkout_clients as clients


def checkout(cart: dict) -> dict:
    clients.inventory.reserve(cart["items"])
    return clients.orders.place_order(cart)
