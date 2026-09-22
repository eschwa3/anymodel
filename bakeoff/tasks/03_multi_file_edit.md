# Task 3: Add a `currency` field (edit, multi-file)

Mode: `edit`

Add currency support to `inventory_lib`. Right now every price is an
unlabeled float; we want orders and priced breakdowns to carry an
explicit currency code so the CLI output is unambiguous.

Requirements:

1. **`inventory_lib/models.py`**: add a `currency: str = "USD"` field
   to `Order`.
2. **`inventory_lib/pricing.py`**: add a `currency: str` field to
   `PriceBreakdown`, and set it in `price_order()` from
   `order.currency` (do not add a separate `currency` parameter to
   `price_order()` -- it should come from the order itself).
3. **`inventory_lib/cli.py`**: add a `--currency` flag to the `price`
   subcommand, default `"USD"`, and pass it through when constructing
   the `Order` so it ends up on the printed breakdown.
4. **Tests**: update `tests/test_pricing.py` and `tests/test_cli.py`
   so they still pass, and add at least one new assertion/test that
   specifically checks currency propagation end-to-end (e.g. that a
   `price_order()` result carries the order's currency, and that the
   CLI's `--currency` flag changes the printed `currency` field, and
   that omitting `--currency` defaults to `"USD"`).

This should touch at least three files
(`inventory_lib/models.py`, `inventory_lib/pricing.py`,
`inventory_lib/cli.py`, plus test files) and keep them consistent with
each other -- e.g. don't introduce a second, conflicting way to specify
currency. Existing behavior for callers that don't care about currency
must keep working unchanged (default `"USD"` everywhere).

Do not touch `inventory_lib/importer.py`, `inventory_lib/discounts.py`,
`inventory_lib/inventory.py`, or `inventory_lib/exceptions.py` -- this
task is scoped to the order/pricing/CLI path only.
