I need answers to three questions about this `jobsched` repo before I plan
some work on it. For each, trace the actual code (don't guess from names)
and cite the function(s)/file(s) you base the answer on.

1. When a job's retry count reaches the configured limit, what releases
   its reservation, and what exactly decides dead-lettering vs. another
   retry? Name the function(s) and the precise condition.
2. Which `AppConfig` keys affect how an invoice's tax is rounded, and in
   which function are they read?
3. Trace what happens when a customer's plan is changed mid-cycle: which
   function/table records the change, and how does invoice generation
   pick it up?

Reply with one short paragraph per question, each citing at least one
file/function. Don't answer questions I didn't ask, and don't guess if
you can't find something -- say what you checked and that you couldn't
confirm it, rather than stating something you're not sure of as fact.
