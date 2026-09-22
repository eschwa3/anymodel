import pytest
from inventory_lib.models import Item, Order, OrderLine


def make_item(sku="WIDGET", price=9.99):
    return Item(sku=sku, name="Widget", unit_price=price)


def test_item_rejects_empty_sku():
    with pytest.raises(ValueError):
        Item(sku="", name="Widget", unit_price=1.0)


def test_item_rejects_negative_price():
    with pytest.raises(ValueError):
        Item(sku="W1", name="Widget", unit_price=-1.0)


def test_order_line_total_rounds_to_cents():
    line = OrderLine(item=make_item(price=1.005), quantity=3)
    assert line.line_total() == round(1.005 * 3, 2)


def test_order_line_rejects_nonpositive_quantity():
    with pytest.raises(ValueError):
        OrderLine(item=make_item(), quantity=0)


def test_order_subtotal_sums_lines():
    order = Order(order_id="o1")
    order.add_line(make_item("A", 10.00), 2)
    order.add_line(make_item("B", 5.50), 1)
    assert order.subtotal() == 25.50


def test_order_total_quantity_sums_lines():
    order = Order(order_id="o1")
    order.add_line(make_item("A", 10.00), 2)
    order.add_line(make_item("B", 5.50), 3)
    assert order.total_quantity() == 5


def test_empty_order_is_empty():
    order = Order(order_id="o1")
    assert order.is_empty()
    assert order.subtotal() == 0
    assert order.total_quantity() == 0
