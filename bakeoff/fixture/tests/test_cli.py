import json

from inventory_lib.cli import main

CATALOG_CSV = """sku,name,unit_price,initial_stock,tax_exempt
WIDGET,Widget,9.99,100,false
GADGET,Gadget,19.99,50,false
"""


def write_catalog(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text(CATALOG_CSV)
    return path


def test_import_catalog_command_reports_counts(tmp_path, capsys):
    csv_path = write_catalog(tmp_path)
    rc = main(["import-catalog", "--csv", str(csv_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "imported 2 item(s), 0 error(s)" in out


def test_price_command_prints_json_breakdown(tmp_path, capsys):
    csv_path = write_catalog(tmp_path)
    rc = main(
        [
            "price",
            "--csv",
            str(csv_path),
            "--item",
            "WIDGET:2",
            "--tax-rate",
            "0.10",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["subtotal"] == 19.98
    assert payload["total"] > payload["subtotal"]


def test_price_command_unknown_sku_fails(tmp_path, capsys):
    csv_path = write_catalog(tmp_path)
    rc = main(["price", "--csv", str(csv_path), "--item", "NOPE:1"])
    assert rc == 1
