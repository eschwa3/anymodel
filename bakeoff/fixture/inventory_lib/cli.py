"""A tiny CLI tying the catalog importer and pricing together.

Usage:
    python -m inventory_lib.cli import-catalog --csv catalog.csv
    python -m inventory_lib.cli price --csv catalog.csv --item SKU:QTY [--item SKU:QTY ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .importer import import_catalog
from .models import Order
from .pricing import DEFAULT_TAX_RATE, price_order


def _parse_item_arg(raw: str) -> tuple[str, int]:
    try:
        sku, qty_raw = raw.split(":", 1)
        qty = int(qty_raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--item must look like SKU:QTY, got {raw!r}"
        ) from None
    if qty <= 0:
        raise argparse.ArgumentTypeError(f"quantity in {raw!r} must be > 0")
    return sku, qty


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inventory-lib")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import-catalog", help="Import a catalog CSV")
    import_parser.add_argument("--csv", required=True, help="Path to the catalog CSV")

    price_parser = subparsers.add_parser("price", help="Price an order")
    price_parser.add_argument("--csv", required=True, help="Path to the catalog CSV")
    price_parser.add_argument(
        "--item",
        dest="items",
        action="append",
        required=True,
        type=_parse_item_arg,
        help="SKU:QTY, may be repeated",
    )
    price_parser.add_argument("--order-id", default="order-1")
    price_parser.add_argument("--tax-rate", type=float, default=DEFAULT_TAX_RATE)

    return parser


def _cmd_import_catalog(args: argparse.Namespace) -> int:
    result = import_catalog(args.csv)
    print(f"imported {len(result.items)} item(s), {len(result.errors)} error(s)")
    for err in result.errors:
        print(f"  line {err.line_number}: {err.message}", file=sys.stderr)
    return 0 if result.ok else 1


def _cmd_price(args: argparse.Namespace) -> int:
    result = import_catalog(args.csv)
    catalog = {item.sku: item for item in result.items}

    order = Order(order_id=args.order_id)
    for sku, qty in args.items:
        if sku not in catalog:
            print(f"unknown sku: {sku!r}", file=sys.stderr)
            return 1
        order.add_line(catalog[sku], qty)

    breakdown = price_order(order, tax_rate=args.tax_rate)
    print(json.dumps(asdict(breakdown), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-catalog":
        return _cmd_import_catalog(args)
    if args.command == "price":
        return _cmd_price(args)

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
