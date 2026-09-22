"""Application configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class AppConfig:
    currency: str = "USD"
    tax_rate_bp: int = 750  # basis points; 750 = 7.50%
    max_job_retries: int = 3
    reservation_lease_seconds: int = 300
    db_path: str = "jobsched.db"
    # Page size for the ops dashboard's job search endpoint
    # (`handlers.handle_search_jobs`); unrelated to billing/scheduling.
    search_page_size: int = 50


DEFAULT_CONFIG = AppConfig()


def load_config(overrides: dict | None = None) -> AppConfig:
    if not overrides:
        return DEFAULT_CONFIG
    return replace(DEFAULT_CONFIG, **overrides)
