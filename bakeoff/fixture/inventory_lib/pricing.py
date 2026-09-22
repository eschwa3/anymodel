"""Turns an Order into a priced breakdown: subtotal, discount, tax, total."""

from __future__ import annotations

from dataclasses import dataclass

from .discounts import tier_discount_rate
from .models import Order

DEFAULT_TAX_RATE = 0.0825  # 8.25%, a typical US combined sales-tax rate


@dataclass
class PriceBreakdown:
    """The result of pricing an order."""

    order_id: str
    subtotal: float
    discount_rate: float
    discount_amount: float
    taxable_amount: float
    tax_amount: float
    total: float


def price_order(order: Order, tax_rate: float = DEFAULT_TAX_RATE) -> PriceBreakdown:
    """Price an order: apply the volume discount, then tax the remainder.

    Tax is charged on the discounted (taxable) amount, not on the
    pre-discount subtotal -- a customer should never pay tax on money
    they didn't actually spend.
    """
    subtotal = order.subtotal()
    quantity = order.total_quantity()

    discount_rate = tier_discount_rate(quantity)
    discount_amount = round(subtotal * discount_rate, 2)
    taxable_amount = subtotal - discount_amount

    tax_amount = round(subtotal * tax_rate, 2)
    total = round(subtotal - discount_amount + tax_amount, 2)

    return PriceBreakdown(
        order_id=order.order_id,
        subtotal=subtotal,
        discount_rate=discount_rate,
        discount_amount=discount_amount,
        taxable_amount=taxable_amount,
        tax_amount=tax_amount,
        total=total,
    )
