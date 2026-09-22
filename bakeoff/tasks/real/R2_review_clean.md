Review the last commit on this branch (`git log -p -1`, or equivalently
`git diff HEAD~1..HEAD`) in this `jobsched` repo. It adds a read-only
customer-summary rollup (`service.get_customer_summary`), a CLI
subcommand for it, and a test.

Same bar as any other review: correctness bugs, security issues, and
resource/transaction-handling problems only, not style. Reply with a
numbered list of anything real you find, each with a file:line and a
one-line justification. If it's clean, say so plainly instead of
inventing nits to pad the list.
