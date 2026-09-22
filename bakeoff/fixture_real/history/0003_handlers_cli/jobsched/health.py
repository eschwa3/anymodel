"""Process-local health/status helpers used by the `status` CLI command."""

from __future__ import annotations

from jobsched.utils.time import now


def healthcheck() -> dict:
    return {"status": "ok", "checked_at": now().isoformat()}
