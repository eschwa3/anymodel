"""Mutant 1: drops the blank-`name` validation.

Used to mutation-score a worker's tests/test_importer.py (written for
Task 2): a good test suite checks that a row with a missing/blank name
shows up in `result.errors` (mirroring the sku check), so it should
FAIL against this mutant, which lets a blank name through as a valid
item instead.

Note: a blank *sku* or a negative *unit_price* both still get caught
here too, but via `Item.__post_init__`'s own validation rather than
this module's -- so mutating those checks alone would not change
observable behavior. `name` has no such backstop, which is exactly the
kind of asymmetry a thorough test suite should notice.

This file is a drop-in replacement for inventory_lib/importer.py --
run.py copies it over the real module in a scratch copy of the repo
and re-runs the worker's tests against it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from .exceptions import ImportValidationError
from .inventory import Inventory
from .models import Item

REQUIRED_COLUMNS = ("sku", "name", "unit_price", "initial_stock")
_TRUTHY = {"1", "true", "yes"}


@dataclass
class ImportRowError:
    line_number: int
    message: str
    raw_row: dict[str, str]


@dataclass
class ImportResult:
    items: list[Item] = field(default_factory=list)
    inventory: Inventory = field(default_factory=Inventory)
    errors: list[ImportRowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in _TRUTHY


def _parse_row(row: dict[str, str], line_number: int) -> Item:
    sku = (row.get("sku") or "").strip()
    name = (row.get("name") or "").strip()
    if not sku:
        raise ValueError("sku is required")
    # MUTATION: "name is required" check removed.

    price_raw = (row.get("unit_price") or "").strip()
    try:
        unit_price = float(price_raw)
    except ValueError:
        raise ValueError(f"unit_price {price_raw!r} is not a number") from None
    if unit_price < 0:
        raise ValueError(f"unit_price must be >= 0, got {unit_price!r}")

    stock_raw = (row.get("initial_stock") or "").strip()
    try:
        initial_stock = int(stock_raw)
    except ValueError:
        raise ValueError(f"initial_stock {stock_raw!r} is not an integer") from None
    if initial_stock < 0:
        raise ValueError(f"initial_stock must be >= 0, got {initial_stock!r}")

    tax_exempt = _parse_bool(row.get("tax_exempt") or "")

    return Item(sku=sku, name=name, unit_price=unit_price, tax_exempt=tax_exempt), initial_stock


def import_catalog(source: str | Path | StringIO) -> ImportResult:
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source.read()

    reader = csv.DictReader(StringIO(text))
    fieldnames = reader.fieldnames

    if not text.strip():
        return ImportResult()

    if not fieldnames:
        raise ImportValidationError("CSV has no header row")

    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise ImportValidationError(f"CSV header missing required column(s): {missing}")

    result = ImportResult()
    for line_number, row in enumerate(reader, start=2):
        try:
            item, initial_stock = _parse_row(row, line_number)
        except ValueError as exc:
            result.errors.append(
                ImportRowError(line_number=line_number, message=str(exc), raw_row=row)
            )
            continue

        result.items.append(item)
        if initial_stock > 0:
            result.inventory.add_stock(item.sku, initial_stock)
        else:
            result.inventory.register(item.sku)

    return result
