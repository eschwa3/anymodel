import pytest
from inventory_lib.exceptions import (
    InsufficientStockError,
    InvalidQuantityError,
    ReservationNotFoundError,
)
from inventory_lib.inventory import Inventory


def test_add_stock_and_available():
    inv = Inventory()
    inv.add_stock("A", 10)
    assert inv.on_hand("A") == 10
    assert inv.available("A") == 10


def test_reserve_reduces_available():
    inv = Inventory()
    inv.add_stock("A", 10)
    inv.reserve("A", 4)
    assert inv.available("A") == 6
    assert inv.on_hand("A") == 10  # on-hand stock is untouched by reserving


def test_reserve_raises_when_insufficient_stock():
    inv = Inventory()
    inv.add_stock("A", 3)
    with pytest.raises(InsufficientStockError):
        inv.reserve("A", 4)


def test_reserve_rejects_nonpositive_quantity():
    inv = Inventory()
    inv.add_stock("A", 3)
    with pytest.raises(InvalidQuantityError):
        inv.reserve("A", 0)


def test_release_full_restores_available():
    inv = Inventory()
    inv.add_stock("A", 10)
    res = inv.reserve("A", 4)
    inv.release(res.reservation_id)
    assert inv.available("A") == 10
    with pytest.raises(ReservationNotFoundError):
        inv.get_reservation(res.reservation_id)


def test_release_partial_shrinks_reservation():
    inv = Inventory()
    inv.add_stock("A", 10)
    res = inv.reserve("A", 6)
    inv.release(res.reservation_id, quantity=2)
    remaining = inv.get_reservation(res.reservation_id)
    assert remaining.quantity == 4


def test_release_unknown_reservation_raises():
    inv = Inventory()
    with pytest.raises(ReservationNotFoundError):
        inv.release("res-does-not-exist")


def test_release_more_than_reserved_raises():
    inv = Inventory()
    inv.add_stock("A", 10)
    res = inv.reserve("A", 4)
    with pytest.raises(InvalidQuantityError):
        inv.release(res.reservation_id, quantity=5)


def test_fulfill_removes_stock_and_reservation():
    inv = Inventory()
    inv.add_stock("A", 10)
    res = inv.reserve("A", 4)
    inv.fulfill(res.reservation_id)
    assert inv.on_hand("A") == 6
    assert inv.available("A") == 6
    with pytest.raises(ReservationNotFoundError):
        inv.get_reservation(res.reservation_id)
