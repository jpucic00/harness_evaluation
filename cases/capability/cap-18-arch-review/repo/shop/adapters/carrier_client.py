"""Client for the carrier's rate API."""

import json
import urllib.request

CARRIER_URL = "https://carrier.example/api/rates"


def quote(weight_grams: int, country: str) -> int:
    body = json.dumps({"weight": weight_grams, "country": country}).encode()
    request = urllib.request.Request(CARRIER_URL, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return int(json.load(response)["price_cents"])
