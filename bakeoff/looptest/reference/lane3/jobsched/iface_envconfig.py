"""AppConfig overrides from JOBSCHED_* environment variables.

Read-only wrapper over `jobsched.config`: environment variables are
validated and layered over the defaults, then an explicit `overrides`
dict wins over the environment. `os.environ` is never mutated.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace

from jobsched.config import AppConfig, load_config
from jobsched.errors import ValidationError

_STR_VAR_TO_FIELD = {
    "JOBSCHED_CURRENCY": "currency",
    "JOBSCHED_DB_PATH": "db_path",
}
_INT_VAR_TO_FIELD = {
    "JOBSCHED_TAX_RATE_BP": "tax_rate_bp",
    "JOBSCHED_MAX_JOB_RETRIES": "max_job_retries",
    "JOBSCHED_RESERVATION_LEASE_SECONDS": "reservation_lease_seconds",
}
_MIN_VALUES = {
    "JOBSCHED_TAX_RATE_BP": 0,
    "JOBSCHED_MAX_JOB_RETRIES": 0,
    "JOBSCHED_RESERVATION_LEASE_SECONDS": 1,
}


def load_config_from_env(
    environ: Mapping[str, str] | None = None,
    overrides: dict | None = None,
) -> AppConfig:
    """Build an AppConfig from defaults + JOBSCHED_* vars + overrides."""
    source = os.environ if environ is None else environ
    env_overrides: dict = {}
    for var, field in _STR_VAR_TO_FIELD.items():
        if var in source:
            value = source[var]
            if not value:
                raise ValidationError(f"{var} must not be empty")
            env_overrides[field] = value
    for var, field in _INT_VAR_TO_FIELD.items():
        if var in source:
            raw = source[var]
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValidationError(f"{var} must be an integer, got {raw!r}") from None
            minimum = _MIN_VALUES[var]
            if value < minimum:
                raise ValidationError(f"{var} must be >= {minimum}, got {value}")
            env_overrides[field] = value

    cfg = load_config(env_overrides)
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg
