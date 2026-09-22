# Task 2: Write tests for the CSV importer (edit)

Mode: `edit`

`inventory_lib/importer.py` (function `import_catalog`) parses a
catalog CSV into a list of `Item`s plus an `Inventory`, and collects
per-row errors instead of raising on a single bad row. It currently has
**no tests at all**.

Write pytest tests in `tests/test_importer.py` (create this file) that
cover:

1. **Normal rows** -- a well-formed CSV with a header
   (`sku,name,unit_price,initial_stock,tax_exempt`) and a few valid
   rows imports the expected `Item`s with the expected fields, and
   stocks the resulting `Inventory` with the expected `on_hand`
   quantities. Include at least one row exercising `tax_exempt` and at
   least one row with `initial_stock` of `0`.
2. **Malformed rows** -- rows with problems such as: missing/blank
   `sku`, non-numeric `unit_price`, negative `unit_price`, non-integer
   `initial_stock`, negative `initial_stock`. These should not raise;
   they should show up in `result.errors` (check `line_number` and
   that `result.ok` is `False`), and a bad row should not prevent
   valid rows in the same file from importing successfully.
3. **Empty input** -- an empty file/string should import cleanly with
   no items and no errors (it should not raise). Also cover a
   structurally invalid file, e.g. a CSV missing one of the required
   header columns, which *should* raise `ImportValidationError`.

You can build input either as temp files (`tmp_path`) or in-memory
text streams (`io.StringIO`) -- `import_catalog` accepts a path or an
open text stream.

**Constraints:**
- Only create/modify `tests/test_importer.py`. Do not modify
  `inventory_lib/importer.py` or any other non-test file, even if you
  think you see a bug or a possible improvement.
- Your tests must pass against the current, unmodified
  `inventory_lib/importer.py`.
- Use plain pytest (no new dependencies).
