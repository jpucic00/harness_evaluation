"""Orders table."""

from shop.domain.order_model import Order
from shop.infra.database import connect


def get(order_id: int) -> Order:
    with connect() as db:
        row = db.execute("SELECT id, customer_id FROM orders WHERE id = ?", (order_id,)).fetchone()
    return Order(id=row[0], customer_id=row[1])


def list_recent(limit: int = 100) -> list[Order]:
    with connect() as db:
        rows = db.execute("SELECT id, customer_id FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        return [Order(id=r[0], customer_id=r[1]) for r in rows]
