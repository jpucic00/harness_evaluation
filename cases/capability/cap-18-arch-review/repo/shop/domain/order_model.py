"""Order entity."""

from dataclasses import dataclass, field

from shop.domain.money_value import Money
from shop.infra.database import connect


@dataclass
class Order:
    id: int
    customer_id: int
    lines: list[tuple[str, Money]] = field(default_factory=list)

    def total(self) -> Money:
        total = Money(amount=0)
        for _, price in self.lines:
            total = total + price
        return total

    def save(self) -> None:
        with connect() as db:
            db.execute("UPDATE orders SET customer_id=? WHERE id=?", (self.customer_id, self.id))
