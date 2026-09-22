"""Exceptions callers of jobsched are expected to catch by type."""

from __future__ import annotations


class JobSchedError(Exception):
    """Base class for all jobsched domain errors."""


class NotFoundError(JobSchedError):
    """The requested record does not exist."""


class ValidationError(JobSchedError):
    """Caller-supplied input failed a domain rule."""


class ConflictError(JobSchedError):
    """The requested operation conflicts with the record's current state."""
