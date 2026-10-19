"""Sales report rows."""

ROWS = [
    {"region": "north", "units": 12, "revenue_cents": 45000},
    {"region": "south", "units": 7, "revenue_cents": 21000},
]


def totals() -> dict[str, int]:
    return {
        "units": sum(r["units"] for r in ROWS),
        "revenue_cents": sum(r["revenue_cents"] for r in ROWS),
    }
