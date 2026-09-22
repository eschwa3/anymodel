"""Core data structures: catalog items and customer orders."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    """A catalog item.

    `unit_price` is a plain float in the store's currency (e.g. dollars).
    `tax_exempt` items (e.g. gift cards) are excluded from tax by callers
    that check the flag; the pricing module in this fixture taxes whole
    orders rather than per-line, so the flag is informational here.
    """

    sku: str
    name: str
    unit_price: float
    tax_exempt: bool = False

    def __post_init__(self) -> None:
        if not self.sku:
            raise ValueError("sku must be non-empty")
        if self.unit_price < 0:
            raise ValueError(f"unit_price must be >= 0, got {self.unit_price!r}")


@dataclass
class OrderLine:
    """A single line item: one catalog item at a given quantity."""

    item: Item
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity!r}")

    def line_total(self) -> float:
        """Extended price for this line, rounded to cents."""
        return round(self.item.unit_price * self.quantity, 2)


@dataclass
class Order:
    """A customer order: an id plus zero or more order lines."""

    order_id: str
    lines: list[OrderLine] = field(default_factory=list)

    def add_line(self, item: Item, quantity: int) -> OrderLine:
        line = OrderLine(item=item, quantity=quantity)
        self.lines.append(line)
        return line

    def subtotal(self) -> float:
        """Sum of all line totals, rounded to cents."""
        return round(sum(line.line_total() for line in self.lines), 2)

    def total_quantity(self) -> int:
        """Total number of units across all lines (used for volume discounts)."""
        return sum(line.quantity for line in self.lines)

    def is_empty(self) -> bool:
        return len(self.lines) == 0
