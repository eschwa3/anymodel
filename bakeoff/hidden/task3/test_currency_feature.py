"""Hidden acceptance test for Task 3 (bakeoff/tasks/03_multi_file_edit.md).

The worker never sees this file. run.py copies it into the worker's
temp copy of the repo (as tests/test_currency_feature_hidden.py) after
the run completes and re-runs pytest to check the `currency` feature
was implemented as specified:

- `Order` has a `currency` field, defaulting to "USD".
- `PriceBreakdown` carries a `currency` field that comes from the
  order, not from a separate `price_order()` parameter.
- The CLI's `price` subcommand accepts `--currency`, defaults to
  "USD", and the value shows up in the printed JSON breakdown.

This must FAIL against the unmodified fixture (no `currency` field
exists yet) and PASS once the feature is implemented per the task
description.
"""

import json

from inventory_lib.cli import main
from inventory_lib.models import Item, Order
from inventory_lib.pricing import price_order

CATALOG_CSV = """sku,name,unit_price,initial_stock,tax_exempt
WIDGET,Widget,10.00,100,false
"""


def write_catalog(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text(CATALOG_CSV)
    return path


def test_order_defaults_to_usd():
    order = Order(order_id="o1")
    assert order.currency == "USD"


def test_order_accepts_explicit_currency():
    order = Order(order_id="o1", currency="EUR")
    assert order.currency == "EUR"


def test_price_breakdown_carries_order_currency():
    order = Order(order_id="o1", currency="GBP")
    order.add_line(Item(sku="A", name="A", unit_price=5.0), 2)
    breakdown = price_order(order)
    assert breakdown.currency == "GBP"


def test_price_breakdown_defaults_to_usd_when_order_does():
    order = Order(order_id="o1")
    order.add_line(Item(sku="A", name="A", unit_price=5.0), 2)
    breakdown = price_order(order)
    assert breakdown.currency == "USD"


def test_cli_price_default_currency_is_usd(tmp_path, capsys):
    csv_path = write_catalog(tmp_path)
    rc = main(["price", "--csv", str(csv_path), "--item", "WIDGET:1"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["currency"] == "USD"


def test_cli_price_currency_flag_is_threaded_through(tmp_path, capsys):
    csv_path = write_catalog(tmp_path)
    rc = main(
        [
            "price",
            "--csv",
            str(csv_path),
            "--item",
            "WIDGET:1",
            "--currency",
            "EUR",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["currency"] == "EUR"
