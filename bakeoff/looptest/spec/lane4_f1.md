## L4-F1 Final invoice total

Purpose: price an invoice the way billing settles it, in one call — apply the
caller's discounts (L1-F1) to the invoice subtotal, subtract the invoice's
credit notes (L1-F4), then add the late fee (L1-F2) on what is still owed —
returning every intermediate amount so each step is checkable.

New module: `jobsched/integ_invoice_total.py` (new file; existing files
untouched). It must CALL these, not re-implement them:
`jobsched.repository.InvoiceRepository(conn).get(invoice_id)`,
`jobsched.billing_discounts.apply_discount`,
`jobsched.billing_discounts.discount_amount`,
`jobsched.billing_credits.list_credit_notes`,
`jobsched.billing_latefees.late_fee_cents`. It must NOT use
`billing_credits.outstanding_balance` (wrong base: the invoice's stored total,
not the discounted amount) and must not write any row.

Public contract (exact signature):

```python
def final_invoice_total(
    conn: sqlite3.Connection,
    invoice_id: int,
    *,
    today: datetime,
    percent_off_bp: int = 0, off_cents: int = 0,
    grace_days: int = 5, daily_rate_bp: int = 50,
) -> dict[str, int]
```

- `today` is the injected current time, a naive `datetime.datetime`; only its
  calendar date participates. No wall-clock reads.
- `percent_off_bp` / `off_cents` are exactly L1-F1's discount parameters
  (basis points of the subtotal; fixed cents after the percentage);
  `grace_days` / `daily_rate_bp` are exactly L1-F2's, same names/defaults.

Return shape: a dict with EXACTLY these five keys, all integer cents:
`subtotal`, `discount`, `credits`, `late_fee`, `total`.

Rules (in this exact order):
1. Resolve the invoice first: `InvoiceRepository(conn).get(invoice_id)` — a
   missing invoice raises `jobsched.errors.NotFoundError` (propagated, never
   re-wrapped). First DB access.
2. `subtotal = invoice.subtotal_cents`; `discount =
   discount_amount(subtotal, percent_off_bp, off_cents)`; the discounted
   amount is `apply_discount(subtotal, percent_off_bp, off_cents)` — so the
   discount inherits L1-F1's half-up rounding (once) and clamping at 0.
3. `credits` = sum of `amount_cents` over `list_credit_notes(conn,
   invoice_id)` (that call lazily creates the `credit_notes` table).
4. `owed = max(0, discounted_amount - credits)` — credits subtract from the
   discounted amount, not from `invoice.total_cents`.
5. Days overdue: the due date is `invoice.period_end` (no due-date column
   exists; the `YYYY-MM-DD` text parses with `date.fromisoformat`).
   `days_overdue = max(0, (today.date() - due).days)`; not-yet-due is 0.
6. `late_fee = late_fee_cents(owed, days_overdue, grace_days=grace_days,
   daily_rate_bp=daily_rate_bp)` — inherits L1-F2's rounding (half-up once)
   and its cap at the passed-in total, i.e. the fee never exceeds `owed`.
7. `total = max(0, owed + late_fee)`; both summands are >= 0, so the total is
   never negative. The stored `tax_cents` / `total_cents` are NOT part of the
   computation (stored tax would double-charge after discounting): `total` is
   owed plus late fee, nothing else.
8. Validation propagates unchanged as `jobsched.errors.ValidationError`:
   L1-F1 rejects `percent_off_bp` outside 0..10000 or `off_cents < 0`; L1-F2
   rejects `grace_days < 0` or `daily_rate_bp < 0`. Nothing is caught here.
9. Read-only for rows: no writes to `invoices` or `credit_notes` (the lazy
   `CREATE TABLE IF NOT EXISTS` done by `list_credit_notes` is the only
   permitted schema effect).

Worked example: invoice with `subtotal_cents=10000`, `tax_cents=2000`,
`total_cents=12000`, `period_end="2024-02-01"`; one credit note of 1500;
`today = datetime(2024, 2, 11)`; `percent_off_bp=2500`, other defaults.
Discount = round_half_up(10000 * 2500 / 10000) = 2500; discounted 7500;
credits 1500; owed 6000; days overdue = 10, so 5 chargeable days; fee =
round_half_up(6000 * 50 * 5 / 10000) = 150; total 6150. Returns
`{"subtotal": 10000, "discount": 2500, "credits": 1500, "late_fee": 150,
"total": 6150}`.

Clamp example: same invoice with `percent_off_bp=10000` -> discount 10000,
discounted 0, owed `max(0, 0 - 1500) = 0`, late fee 0 (L1-F2: zero total),
total 0.

Existing behaviour and existing tests keep passing; this feature only adds a
new module.
