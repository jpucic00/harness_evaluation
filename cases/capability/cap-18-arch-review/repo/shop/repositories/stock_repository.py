"""Stock table."""

from shop.infra.database import connect


def quantity(sku: str) -> int:
    with connect() as db:
        row = db.execute("SELECT qty FROM stock WHERE sku = ?", (sku,)).fetchone()
    return int(row[0]) if row else 0
