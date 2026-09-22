# Planted bugs (answer key -- not shown to workers)

This is the hidden answer key for `bakeoff/fixture`. It is used to score
Task 1 (`bakeoff/tasks/01_find_bugs.md`) and to sanity-check that the
fixture's own test suite genuinely misses these three bugs. Do not put
this file, or its contents, in front of a worker.

---

## Bug 1 -- tiered-discount off-by-one at tier boundaries

- **File:** `bakeoff/fixture/inventory_lib/discounts.py`
- **Line:** 34 (`tier_discount_rate`), condition `if quantity > threshold:`
- **Description:** The docstring (and `TIERS` comment) say a quantity
  *qualifies* for a tier once it *reaches* the threshold (`quantity >=
  threshold`), e.g. 10 units should get the 5% tier. The code uses a
  strict `>` instead of `>=`, so a quantity exactly equal to a
  threshold (10, 50, or 100) is treated as still belonging to the
  *previous, lower* tier. Every boundary quantity is under-discounted
  by one tier step.
- **Why existing tests miss it:** `test_pricing.py` only exercises
  quantities that sit comfortably inside a band (5, 25, 75, 250) and
  never an exact threshold value (10, 50, 100).
- **Minimal failing assertion:**
  ```python
  from inventory_lib.discounts import tier_discount_rate
  assert tier_discount_rate(10) == 0.05   # actual: 0.0
  ```

---

## Bug 2 -- tax computed on pre-discount subtotal, not the taxable amount

- **File:** `bakeoff/fixture/inventory_lib/pricing.py`
- **Line:** 40, `tax_amount = round(subtotal * tax_rate, 2)`
- **Description:** `price_order`'s own docstring says tax should be
  charged "on the discounted (taxable) amount, not on the pre-discount
  subtotal". The function even computes `taxable_amount = subtotal -
  discount_amount` on the line above, but then ignores it and taxes
  `subtotal` directly. Any order that qualifies for a volume discount
  is overcharged tax (tax is computed as if no discount had been
  applied).
- **Why existing tests miss it:** the only test that checks an exact
  tax figure (`test_price_order_no_discount_taxes_full_subtotal`) uses
  a quantity of 1, which gets a 0% discount -- so `subtotal ==
  taxable_amount` and the bug is invisible. The other pricing test
  (`test_price_order_total_matches_component_formula`) only checks
  that `total == subtotal - discount_amount + tax_amount`, which holds
  regardless of how `tax_amount` itself was computed.
- **Minimal failing assertion:**
  ```python
  from inventory_lib.models import Item, Order
  from inventory_lib.pricing import price_order

  order = Order(order_id="o1")
  order.add_line(Item(sku="A", name="A", unit_price=10.0), 25)  # 5% tier
  b = price_order(order, tax_rate=0.10)
  assert b.tax_amount == round(b.taxable_amount * 0.10, 2)  # actual: taxed on subtotal instead
  ```

---

## Bug 3 -- partial reservation release never frees the reserved stock

- **File:** `bakeoff/fixture/inventory_lib/inventory.py`
- **Line:** 96-102, the `else` branch of `Inventory.release()`
  (partial release: `reservation.quantity -= release_qty` at line 102,
  missing the corresponding `self._reserved[reservation.sku] -=
  release_qty`)
- **Description:** `available(sku)` is defined as `on_hand - reserved`.
  The full-release branch correctly does
  `self._reserved[reservation.sku] -= release_qty` before dropping the
  reservation. The partial-release branch only shrinks
  `reservation.quantity` and forgets to lower `self._reserved[...]` by
  the same amount, so the released units stay counted as reserved
  forever (until the rest of the reservation is eventually released in
  full, at which point the accounting is still off by the
  never-freed partial amount).
- **Why existing tests miss it:**
  `test_release_partial_shrinks_reservation` in `test_inventory.py`
  only asserts that `reservation.quantity` shrank; it never calls
  `inv.available(...)` afterward, so the stuck `reserved` count is
  never checked.
- **Minimal failing assertion:**
  ```python
  from inventory_lib.inventory import Inventory

  inv = Inventory()
  inv.add_stock("A", 10)
  res = inv.reserve("A", 6)          # available == 4
  inv.release(res.reservation_id, quantity=2)  # partially release 2
  assert inv.available("A") == 6     # actual: 4 (released units still reserved)
  ```
