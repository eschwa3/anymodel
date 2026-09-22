# X6 soak-test notes

- Conflicting pair: `test_tied_priorities_keep_insertion_order` and `test_tied_priorities_prefer_most_recent` in `bakeoff/looptest3/overlay/tests/soak/test_x6_soak.py`. Both seed identical data (two jobs at priority 4, ids 2 and 3) and call `list_jobs(conn, limit=2)`, but expect opposite tie-break order among the equal keys.
- No implementation can pass both: any total ordering of rows with equal `priority` must put id 2 either before or after id 3; the assertions require each, so at most one passes against any single implementation.
- Spec text is most specific in rule 2 (`priority DESC, id ASC`, matching `JobRepository.reserve_next`), so `test_tied_priorities_keep_insertion_order` agrees with the spec and passes the reference; `test_tied_priorities_prefer_most_recent` contradicts it and is expected to fail.
