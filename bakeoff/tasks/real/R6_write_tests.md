`jobsched/notifications.py` has no test coverage at all. Write tests for
it in `tests/test_notifications.py` (create this file).

Cover the branching and error paths, not just the happy path:
`validate_customer_for_notification`'s error cases, `build_reminder_batch`'s
email gating, overdue-invoice reminders, quota reminders (including the
boundary and the "0 means unlimited" case), de-duplication, and
`format_reminder_digest`'s empty vs. non-empty output.

Constraints:
- Only create/modify `tests/test_notifications.py`. Don't touch
  `jobsched/notifications.py` or any other non-test file, even if you
  spot something you'd want to fix -- note it in your reply instead.
- Your tests must pass against the current, unmodified
  `jobsched/notifications.py`.
- Don't write a test that can't fail (e.g. asserting `True`, or mocking
  away the function under test) -- each test should exercise real
  behavior of the module.

Reply with a short summary of what you covered and any gaps you left.
