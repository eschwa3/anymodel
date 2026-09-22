"""CSV catalog importer.

Expected columns (header required, any order):
    sku, name, unit_price, initial_stock[, tax_exempt]

`tax_exempt` is optional and defaults to false; accepted truthy spellings
are "1", "true", "yes" (case-insensitive), everything else is false.

Rows that fail to parse are collected as `ImportRowError` entries rather
than raising, so a single bad row in a large file doesn't abort the
whole import. A structurally broken file (no header, or a header
missing a required column) raises `ImportValidationError`.

NOTE: this module intentionally has no tests yet -- see
bakeoff/tasks/02_write_tests.md.
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
    if not name:
        raise ValueError("name is required")

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
    """Import a catalog CSV from a path or an open text stream.

    Returns an ImportResult even when some (or all) rows are invalid;
    check `result.errors` / `result.ok`.
    """
    if isinstance(source, (str, Path)):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source.read()

    reader = csv.DictReader(StringIO(text))
    fieldnames = reader.fieldnames

    if not text.strip():
        # Genuinely empty file: nothing to import, nothing to complain about.
        return ImportResult()

    if not fieldnames:
        raise ImportValidationError("CSV has no header row")

    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        raise ImportValidationError(f"CSV header missing required column(s): {missing}")

    result = ImportResult()
    for line_number, row in enumerate(reader, start=2):  # header is line 1
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
            # add_stock() rejects zero, but a zero-stock item is still a
            # valid catalog entry -- just make sure it's tracked at 0.
            result.inventory.register(item.sku)

    return result
