"""Customers table."""

from shop.infra.database import connect
from shop.services.pricing_service import loyalty_discount


def get(customer_id: int) -> dict[str, object]:
    with connect() as db:
        row = db.execute("SELECT id,name,tier FROM customers WHERE id=?", (customer_id,)).fetchone()
    return {"id": row[0], "name": row[1], "tier": row[2], "discount": loyalty_discount(row[2])}


def get_many(customer_ids: list[int]) -> dict[int, dict[str, object]]:
    if not customer_ids:
        return {}
    marks = ",".join("?" * len(customer_ids))
    with connect() as db:
        sql = f"SELECT id, name, tier FROM customers WHERE id IN ({marks})"
        rows = db.execute(sql, customer_ids)
        return {r[0]: {"id": r[0], "name": r[1], "tier": r[2]} for r in rows}
