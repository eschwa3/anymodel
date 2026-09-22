from inventory_lib.discounts import tier_discount_rate
from inventory_lib.models import Item, Order
from inventory_lib.pricing import price_order


def make_order(order_id, sku, price, quantity):
    order = Order(order_id=order_id)
    order.add_line(Item(sku=sku, name=sku, unit_price=price), quantity)
    return order


def test_tier_discount_rate_below_first_threshold():
    assert tier_discount_rate(5) == 0.0


def test_tier_discount_rate_mid_first_tier():
    # Comfortably inside the 10-49 band, well clear of any boundary.
    assert tier_discount_rate(25) == 0.05


def test_tier_discount_rate_mid_second_tier():
    # Comfortably inside the 50-99 band.
    assert tier_discount_rate(75) == 0.10


def test_tier_discount_rate_mid_third_tier():
    # Comfortably inside the 100+ band.
    assert tier_discount_rate(250) == 0.15


def test_price_order_no_discount_taxes_full_subtotal():
    # Quantity below any discount tier: subtotal == taxable amount, so
    # tax should just be subtotal * rate.
    order = make_order("o1", "A", 100.0, 1)
    breakdown = price_order(order, tax_rate=0.10)
    assert breakdown.subtotal == 100.0
    assert breakdown.discount_amount == 0.0
    assert breakdown.tax_amount == 10.0
    assert breakdown.total == 110.0


def test_price_order_total_matches_component_formula():
    # Whatever the discount and tax turn out to be, total must equal
    # subtotal - discount + tax.
    order = make_order("o2", "B", 20.0, 25)  # mid-tier discount, non-boundary
    breakdown = price_order(order, tax_rate=0.08)
    expected_total = round(
        breakdown.subtotal - breakdown.discount_amount + breakdown.tax_amount, 2
    )
    assert breakdown.total == expected_total


def test_price_order_discount_lowers_total_vs_no_discount():
    small = make_order("o3", "C", 10.0, 5)  # no discount
    large = make_order("o4", "C", 10.0, 25)  # 5% discount, same unit price
    small_breakdown = price_order(small, tax_rate=0.0)
    large_breakdown = price_order(large, tax_rate=0.0)
    # Per-unit cost should drop once the discount kicks in.
    assert (large_breakdown.total / 25) < (small_breakdown.total / 5)
