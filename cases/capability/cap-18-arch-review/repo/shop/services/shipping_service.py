"""Shipping costs."""

import json
import urllib.request

from shop.adapters import carrier_client

FREE_SHIPPING_COUNTRIES = ("AT", "DE")


def shipping_cost(weight_grams: int, country: str) -> int:
    if country in FREE_SHIPPING_COUNTRIES:
        return 0
    return carrier_client.quote(weight_grams, country)


def tracking_status(tracking_id: str) -> str:
    url = f"https://carrier.example/api/track/{tracking_id}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return str(json.load(response)["status"])
