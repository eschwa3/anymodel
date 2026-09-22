---
description: Print a cost/usage report for anymodel-subagents workers.
argument-hint: [group-by: day|swarm|role|model]
---

Call the `usage_report` tool (group by `$ARGUMENTS`, defaulting to `day` if no
argument is given) and print the result as a small Markdown table: one row per
group, columns for job count, total tokens, and total cost in USD. Add a final
totals row. Note that this reads the local ledger only — it reports what this
machine's workers have spent, not a live OpenRouter balance.
