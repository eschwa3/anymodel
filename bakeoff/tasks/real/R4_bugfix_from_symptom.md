A customer support ticket says: "Monthly invoices for customers who
changed their plan mid-cycle are a few cents off from what we'd expect --
looks like a rounding thing, not a huge miss, but it's not zero either.
Customers who didn't change plans this cycle look fine."

Find the root cause in this `jobsched` repo and fix it. I don't know
which file it's in -- figure that out from the symptom.

Requirements:
- Fix the actual bug, not just one example of it.
- Don't touch files outside what's needed for this fix.
- Keep the diff minimal and focused -- this is a bug fix, not a
  refactor.
- Make sure the existing test suite still passes.

Reply with: what the root cause was (file/function), what you changed,
and how you'd characterize the size of the fix.
