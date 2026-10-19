"""Order list for the customer service desk."""

from shop.repositories import customer_repository, order_repository


def recent_orders_with_names() -> list[dict[str, object]]:
    orders = order_repository.list_recent()
    customers = customer_repository.get_many(sorted({o.customer_id for o in orders}))
    return [{"order": o.id, "customer": customers[o.customer_id]["name"]} for o in orders]
