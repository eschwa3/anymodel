# LANE 4 — Integration features for jobsched

Four features that COMBINE modules from lanes 1-3. Each lives in its own NEW module
`jobsched/integ_<name>.py` and must call the public functions of the lane 1-3 modules it
names (do not re-implement their logic, do not edit them). Build a lane 4 feature only after
the features it depends on are integrated. Same conventions as the other lanes:
`from __future__ import annotations`, absolute imports, no third-party dependencies,
exceptions from `jobsched.errors`, parameterized SQL only, time always injected.

Existing behaviour and all existing tests must keep passing.
