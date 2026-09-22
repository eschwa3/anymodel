# Task 1: Find the bugs (read-only)

Mode: `read-only`

You are reviewing a small Python library, `inventory_lib`, that prices
customer orders (catalog items, tiered volume discounts, sales tax) and
tracks inventory (stock levels and reservations).

Review these two areas of the code:

- `inventory_lib/discounts.py` and `inventory_lib/pricing.py` (the
  pricing path: subtotal -> discount -> tax -> total)
- `inventory_lib/inventory.py` (stock levels and the reservation
  lifecycle: reserve / release / fulfill)

There are real, behavior-affecting bugs hiding in this code -- the kind
that produce a wrong number or wrong inventory count in some cases but
not others. Some of them are edge cases (e.g. an exact boundary value)
that the existing test suite (`tests/test_pricing.py`,
`tests/test_inventory.py`) does not exercise, so "the tests pass" is
not evidence the code is correct.

**Your job:** find and report the bugs. For each one, give:

1. The file and line number (or a small line range).
2. A one-line justification: what the code does vs. what it should do,
   and a concrete input where the two disagree.

Do not report style issues, naming preferences, missing type hints, or
anything that doesn't change program behavior for some input. Focus on
correctness. Read the docstrings -- they describe the intended
behavior, which is the spec you're checking the code against.

You do not need to fix anything or write any code. Do not modify any
files. Report your findings as your final message, as a numbered list.
