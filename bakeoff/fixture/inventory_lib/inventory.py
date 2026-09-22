"""Stock levels and reservation lifecycle.

Model: each SKU has an `on_hand` count (physical stock) and a `reserved`
count (units promised to open reservations but not yet shipped).
`available(sku) == on_hand - reserved`. Reserving stock never changes
`on_hand`; it only raises `reserved`. Releasing a reservation (in full
or in part) must lower `reserved` back down by the released amount so
the stock becomes available again.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .exceptions import (
    InsufficientStockError,
    InvalidQuantityError,
    ReservationNotFoundError,
)

_ids = itertools.count(1)


@dataclass
class Reservation:
    reservation_id: str
    sku: str
    quantity: int


class Inventory:
    """In-memory stock tracker with a reservation workflow."""

    def __init__(self) -> None:
        self._on_hand: dict[str, int] = {}
        self._reserved: dict[str, int] = {}
        self._reservations: dict[str, Reservation] = {}

    def add_stock(self, sku: str, quantity: int) -> None:
        if quantity <= 0:
            raise InvalidQuantityError(f"quantity must be > 0, got {quantity!r}")
        self._on_hand[sku] = self._on_hand.get(sku, 0) + quantity
        self._reserved.setdefault(sku, 0)

    def register(self, sku: str) -> None:
        """Ensure `sku` is tracked even with zero stock (e.g. a catalog
        entry that hasn't been stocked yet)."""
        self._on_hand.setdefault(sku, 0)
        self._reserved.setdefault(sku, 0)

    def on_hand(self, sku: str) -> int:
        return self._on_hand.get(sku, 0)

    def available(self, sku: str) -> int:
        """Units of `sku` that are physically present and not reserved."""
        return self._on_hand.get(sku, 0) - self._reserved.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> Reservation:
        """Reserve `quantity` units of `sku`, returning the new Reservation."""
        if quantity <= 0:
            raise InvalidQuantityError(f"quantity must be > 0, got {quantity!r}")
        if self.available(sku) < quantity:
            raise InsufficientStockError(
                f"only {self.available(sku)} of {sku!r} available, requested {quantity}"
            )
        self._reserved[sku] = self._reserved.get(sku, 0) + quantity
        reservation_id = f"res-{next(_ids)}"
        reservation = Reservation(reservation_id=reservation_id, sku=sku, quantity=quantity)
        self._reservations[reservation_id] = reservation
        return reservation

    def get_reservation(self, reservation_id: str) -> Reservation:
        try:
            return self._reservations[reservation_id]
        except KeyError:
            raise ReservationNotFoundError(f"no reservation {reservation_id!r}") from None

    def release(self, reservation_id: str, quantity: int | None = None) -> None:
        """Release a reservation, in full or in part, freeing reserved stock.

        If `quantity` is None (the default) the whole reservation is
        released. Otherwise only `quantity` units are released and the
        reservation is shrunk to cover the remainder.
        """
        reservation = self.get_reservation(reservation_id)
        release_qty = reservation.quantity if quantity is None else quantity

        if release_qty <= 0 or release_qty > reservation.quantity:
            raise InvalidQuantityError(
                f"cannot release {release_qty!r} units of a "
                f"{reservation.quantity}-unit reservation"
            )

        if release_qty == reservation.quantity:
            # Full release: drop the reservation and free all of its stock.
            self._reserved[reservation.sku] -= release_qty
            del self._reservations[reservation_id]
        else:
            # Partial release: shrink the reservation to the remaining
            # quantity. The released portion goes back to being available.
            reservation.quantity -= release_qty

    def fulfill(self, reservation_id: str) -> None:
        """Ship a reservation: remove the stock and the reservation entirely."""
        reservation = self.get_reservation(reservation_id)
        self._on_hand[reservation.sku] -= reservation.quantity
        self._reserved[reservation.sku] -= reservation.quantity
        del self._reservations[reservation_id]
