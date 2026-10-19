"""Sales report for the back office."""

from shop.repositories import customer_repository, order_repository


def recent_orders_with_customers() -> list[dict[str, object]]:
    report = []
    for order in order_repository.list_recent():
        customer = customer_repository.get(order.customer_id)
        report.append({"order": order.id, "customer": customer["name"]})
    return report
