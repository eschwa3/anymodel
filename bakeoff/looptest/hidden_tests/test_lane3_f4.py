"""Hidden acceptance tests for LANE 3, feature F4 (JOBSCHED_* env config)."""

from __future__ import annotations

import os

import pytest
from jobsched.config import DEFAULT_CONFIG
from jobsched.errors import ValidationError
from jobsched.iface_envconfig import load_config_from_env

KNOWN_VARS = (
    "JOBSCHED_CURRENCY",
    "JOBSCHED_TAX_RATE_BP",
    "JOBSCHED_MAX_JOB_RETRIES",
    "JOBSCHED_RESERVATION_LEASE_SECONDS",
    "JOBSCHED_DB_PATH",
)


@pytest.fixture(autouse=True)
def _clean_jobsched_env(monkeypatch):
    for var in KNOWN_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_no_vars_set(monkeypatch):
    monkeypatch.setenv("JOBSCHED_FOO", "ignored")  # unknown vars are ignored
    assert load_config_from_env() == DEFAULT_CONFIG


def test_single_int_override(monkeypatch):
    monkeypatch.setenv("JOBSCHED_TAX_RATE_BP", "900")
    cfg = load_config_from_env()
    assert cfg.tax_rate_bp == 900
    assert cfg.currency == "USD"
    assert cfg.max_job_retries == 3
    assert cfg.reservation_lease_seconds == 300
    assert cfg.db_path == "jobsched.db"


def test_all_five_vars_applied(monkeypatch):
    monkeypatch.setenv("JOBSCHED_CURRENCY", "EUR")
    monkeypatch.setenv("JOBSCHED_TAX_RATE_BP", "100")
    monkeypatch.setenv("JOBSCHED_MAX_JOB_RETRIES", "7")
    monkeypatch.setenv("JOBSCHED_RESERVATION_LEASE_SECONDS", "60")
    monkeypatch.setenv("JOBSCHED_DB_PATH", "other.db")
    cfg = load_config_from_env()
    assert cfg.currency == "EUR"
    assert cfg.tax_rate_bp == 100
    assert cfg.max_job_retries == 7
    assert cfg.reservation_lease_seconds == 60
    assert cfg.db_path == "other.db"


def test_overrides_dict_beats_environment(monkeypatch):
    monkeypatch.setenv("JOBSCHED_CURRENCY", "EUR")
    monkeypatch.setenv("JOBSCHED_MAX_JOB_RETRIES", "7")
    cfg = load_config_from_env(overrides={"currency": "GBP"})
    assert cfg.currency == "GBP"
    assert cfg.max_job_retries == 7


def test_non_integer_value_raises_naming_the_var(monkeypatch):
    monkeypatch.setenv("JOBSCHED_MAX_JOB_RETRIES", "abc")
    with pytest.raises(ValidationError, match="JOBSCHED_MAX_JOB_RETRIES"):
        load_config_from_env()


def test_negative_and_empty_values_raise_naming_the_var(monkeypatch):
    monkeypatch.setenv("JOBSCHED_TAX_RATE_BP", "-1")
    with pytest.raises(ValidationError, match="JOBSCHED_TAX_RATE_BP"):
        load_config_from_env()
    monkeypatch.setenv("JOBSCHED_TAX_RATE_BP", "10")
    monkeypatch.setenv("JOBSCHED_CURRENCY", "")
    with pytest.raises(ValidationError, match="JOBSCHED_CURRENCY"):
        load_config_from_env()


def test_lease_seconds_must_be_positive(monkeypatch):
    monkeypatch.setenv("JOBSCHED_RESERVATION_LEASE_SECONDS", "0")
    with pytest.raises(ValidationError, match="JOBSCHED_RESERVATION_LEASE_SECONDS"):
        load_config_from_env()


def test_explicit_environ_mapping_used_and_os_environ_untouched(monkeypatch):
    monkeypatch.setenv("JOBSCHED_CURRENCY", "AAA")  # must be ignored: mapping wins
    cfg = load_config_from_env(environ={"JOBSCHED_TAX_RATE_BP": "1200"})
    assert cfg.tax_rate_bp == 1200
    assert cfg.currency == "USD"
    assert "JOBSCHED_TAX_RATE_BP" not in os.environ
