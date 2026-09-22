# inventory_lib -- agent conventions

This is a small, self-contained Python (stdlib-only) library: catalog
items, tiered-discount + tax pricing, stock reservations, a CSV
importer, and a tiny CLI.

- Run tests from this directory: `python -m pytest tests -q` (or from
  the repo root: `python -m pytest bakeoff/fixture/tests -q`).
- No third-party dependencies. Don't add any.
- Style: standard library `dataclasses`, type hints, `from __future__
  import annotations`. Keep functions small and pure where practical.
- Money values are plain `float` dollars, rounded to cents with
  `round(x, 2)` at the point they're finalized (not on every
  intermediate step).
- Raise the specific exceptions in `inventory_lib/exceptions.py`
  instead of bare `ValueError`/`KeyError` for anything a caller might
  want to catch.
- Tests live in `tests/`, one file per module (`test_<module>.py`).
- Keep edits scoped to what you were asked to do; don't reformat
  unrelated code or rename things in passing.
