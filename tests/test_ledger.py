import json
import os
import stat
import time
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from anymodel_subagents import ledger
from anymodel_subagents.config import state_dir
from anymodel_subagents.types import Usage, WorkerResult


def _result(status="completed", error=None, **usage_kwargs) -> WorkerResult:
    return WorkerResult(
        status=status,
        final_message="done",
        model="test/model",
        turns=3,
        usage=Usage(**usage_kwargs),
        tool_calls=2,
        invalid_tool_calls=0,
        changed_files=["a.py"],
        sensitive_changed_files=[],
        duration_s=1.5,
        error=error,
    )


def test_record_creates_parent_with_restricted_mode(tmp_path):
    path = tmp_path / "state" / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id=None, role="reviewer", model="m", result=_result())

    assert path.exists()
    mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert mode == 0o700


def test_record_creates_file_with_restricted_mode(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id=None, role="reviewer", model="m", result=_result())

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_record_includes_sensitive_changed_files(tmp_path):
    path = tmp_path / "ledger.jsonl"
    result = WorkerResult(
        status="completed",
        final_message="done",
        model="m",
        turns=1,
        usage=Usage(),
        changed_files=["conftest.py"],
        sensitive_changed_files=["conftest.py"],
    )
    ledger.record(path, job_id="j1", swarm_id=None, role="r", model="m", result=result)

    entry = json.loads(path.read_text().strip())
    assert entry["sensitive_changed_files"] == ["conftest.py"]


def test_record_redacts_live_secret_from_error(tmp_path):
    path = tmp_path / "ledger.jsonl"
    secret = "sk-or-v1-ledgertestsecret0000000000"
    result = _result(status="error", error=f"request failed: bad key {secret}")

    ledger.record(
        path, job_id="j1", swarm_id=None, role="r", model="m", result=result, secrets=[secret]
    )

    text = path.read_text()
    assert secret not in text
    assert "[REDACTED]" in text


def test_record_redacts_key_shaped_patterns_without_secrets_list(tmp_path):
    path = tmp_path / "ledger.jsonl"
    result = _result(status="error", error="failed: ghp_abcdefghijklmnopqrstuvwxyz012345")

    ledger.record(path, job_id="j1", swarm_id=None, role="r", model="m", result=result)

    text = path.read_text()
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in text
    assert "[REDACTED]" in text


def test_record_appends_jsonl_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id="s1", role="reviewer", model="m1", result=_result())
    ledger.record(path, job_id="j2", swarm_id="s1", role="codegen", model="m2", result=_result())

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2


def test_record_tolerates_chmod_failure_on_parent(tmp_path, monkeypatch):
    # Some filesystems / sandboxes refuse chmod on an otherwise usable dir; the
    # entry must still be written rather than the whole job failing.
    path = tmp_path / "state" / "ledger.jsonl"

    def refuse_chmod(*args, **kwargs):
        raise OSError("chmod not permitted")

    monkeypatch.setattr(ledger.os, "chmod", refuse_chmod)
    ledger.record(path, job_id="j1", swarm_id=None, role="reviewer", model="m", result=_result())

    assert json.loads(path.read_text().strip())["job_id"] == "j1"


def test_record_closes_fd_and_reraises_when_fdopen_fails(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    opened: list[int] = []

    def refuse_fdopen(fd, *args, **kwargs):
        opened.append(fd)
        raise OSError("fdopen refused")

    monkeypatch.setattr(ledger.os, "fdopen", refuse_fdopen)
    with pytest.raises(OSError, match="fdopen refused"):
        ledger.record(path, job_id="j1", swarm_id=None, role="r", model="m", result=_result())

    assert opened, "record() should have opened the ledger file"
    # The raw fd must not leak: it is closed before the error propagates.
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert path.read_text() == ""


def test_summarize_group_by_model(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(
        path,
        job_id="j1",
        swarm_id=None,
        role="reviewer",
        model="model-a",
        result=_result(prompt_tokens=100, completion_tokens=10, cost=0.01, requests=1),
    )
    ledger.record(
        path,
        job_id="j2",
        swarm_id=None,
        role="reviewer",
        model="model-a",
        result=_result(prompt_tokens=50, completion_tokens=5, cost=0.005, requests=1),
    )
    ledger.record(
        path,
        job_id="j3",
        swarm_id=None,
        role="researcher",
        model="model-b",
        result=_result(prompt_tokens=20, completion_tokens=2, cost=0.001, requests=1),
    )

    summary = ledger.summarize(path, group_by="model")
    assert summary["model-a"]["jobs"] == 2
    assert summary["model-a"]["prompt_tokens"] == 150
    assert abs(summary["model-a"]["cost"] - 0.015) < 1e-9
    assert summary["model-b"]["jobs"] == 1


def test_summarize_group_by_role(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id=None, role="reviewer", model="m", result=_result())
    ledger.record(path, job_id="j2", swarm_id=None, role="reviewer", model="m", result=_result())
    ledger.record(path, job_id="j3", swarm_id=None, role="codegen", model="m", result=_result())

    summary = ledger.summarize(path, group_by="role")
    assert summary["reviewer"]["jobs"] == 2
    assert summary["codegen"]["jobs"] == 1


def test_summarize_group_by_swarm(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id="swarm-1", role="r", model="m", result=_result())
    ledger.record(path, job_id="j2", swarm_id="swarm-1", role="r", model="m", result=_result())
    ledger.record(path, job_id="j3", swarm_id=None, role="r", model="m", result=_result())

    summary = ledger.summarize(path, group_by="swarm")
    assert summary["swarm-1"]["jobs"] == 2
    assert summary["unknown"]["jobs"] == 1


def test_summarize_group_by_day_tracks_statuses(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(
        path, job_id="j1", swarm_id=None, role="r", model="m", result=_result(status="completed")
    )
    ledger.record(
        path, job_id="j2", swarm_id=None, role="r", model="m", result=_result(status="error")
    )

    summary = ledger.summarize(path, group_by="day")
    today_key = next(iter(summary))
    assert summary[today_key]["jobs"] == 2
    assert summary[today_key]["statuses"]["completed"] == 1
    assert summary[today_key]["statuses"]["error"] == 1


def test_summarize_since_filters_old_entries(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="old", swarm_id=None, role="r", model="m", result=_result())

    # Manually inject an old-dated line to simulate a past entry.
    old_line = path.read_text().strip()
    import json

    entry = json.loads(old_line)
    entry["ts"] = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    path.write_text(json.dumps(entry) + "\n")

    ledger.record(path, job_id="new", swarm_id=None, role="r", model="m", result=_result())

    since = datetime.now(UTC) - timedelta(days=1)
    summary = ledger.summarize(path, since=since, group_by="model")
    total_jobs = sum(g["jobs"] for g in summary.values())
    assert total_jobs == 1


def test_summarize_empty_ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    summary = ledger.summarize(path, group_by="day")
    assert summary == {}


def test_summarize_skips_malformed_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id=None, role="r", model="m", result=_result())
    with path.open("a") as f:
        f.write("not valid json\n")

    summary = ledger.summarize(path, group_by="model")
    total_jobs = sum(g["jobs"] for g in summary.values())
    assert total_jobs == 1


def test_summarize_ignores_blank_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id=None, role="r", model="m", result=_result())
    with path.open("a") as f:
        f.write("\n")
        f.write("   \n")
    ledger.record(path, job_id="j2", swarm_id=None, role="r", model="m", result=_result())

    summary = ledger.summarize(path, group_by="model")
    assert summary["m"]["jobs"] == 2


def test_summarize_rejects_unknown_group_by(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="j1", swarm_id=None, role="r", model="m", result=_result())

    with pytest.raises(ValueError, match="unknown group_by"):
        ledger.summarize(path, group_by="nonsense")


def test_summarize_since_skips_entries_with_unparseable_timestamp(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record(path, job_id="ok", swarm_id=None, role="r", model="m", result=_result())
    with path.open("a") as f:
        f.write(json.dumps({"ts": "not-a-timestamp", "model": "m", "usage": {}}) + "\n")

    since = datetime.now(UTC) - timedelta(days=1)
    summary = ledger.summarize(path, since=since, group_by="model")
    total_jobs = sum(g["jobs"] for g in summary.values())
    assert total_jobs == 1


def test_summarize_since_treats_naive_timestamp_as_utc(tmp_path):
    path = tmp_path / "ledger.jsonl"
    naive_ts = datetime.now(UTC).replace(tzinfo=None).isoformat()
    path.write_text(json.dumps({"ts": naive_ts, "model": "m", "usage": {}}) + "\n")

    since = datetime.now(UTC) - timedelta(days=1)
    summary = ledger.summarize(path, since=since, group_by="model")
    assert summary["m"]["jobs"] == 1


# ---------------------------------------------------------------------------
# spent_on
# ---------------------------------------------------------------------------


def _today() -> date:
    return datetime.now(UTC).astimezone().date()


def _ts_at(days_back: int, *, offset_s: int = 60) -> str:
    """ISO timestamp `offset_s` after the start of the local day `days_back` ago."""
    return (ledger.since_day_start(days_back) + timedelta(seconds=offset_s)).isoformat()


@pytest.fixture()
def los_angeles_tz():
    """Pin the process to America/Los_Angeles for one test (restored after).

    Lets a test construct instants whose UTC date and local date differ,
    whatever timezone the machine running the suite happens to be in.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("POSIX-only: no time.tzset")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_spent_on_uses_the_local_day_boundary_not_utc(tmp_path, los_angeles_tz):
    # 2024-01-16 01:00 UTC is 2024-01-15 17:00 in Los Angeles: the UTC date and
    # the local date differ, and the entry belongs to the LOCAL day.
    path = tmp_path / "ledger.jsonl"
    ts = datetime(2024, 1, 16, 1, 0, tzinfo=UTC)
    path.write_text(json.dumps({"ts": ts.isoformat(), "usage": {"cost": 1.0}}) + "\n")

    assert ledger.spent_on(date(2024, 1, 15), path) == pytest.approx(1.0)
    assert ledger.spent_on(date(2024, 1, 16), path) == 0.0


def test_spent_on_sums_costs_on_the_local_day(tmp_path):
    path = tmp_path / "ledger.jsonl"
    lines = [
        {"ts": _ts_at(0, offset_s=60), "usage": {"cost": 0.25}},
        {"ts": _ts_at(0, offset_s=3600), "usage": {"cost": 0.5}},
        {"ts": _ts_at(1, offset_s=60), "usage": {"cost": 9.0}},
        {"ts": _ts_at(7, offset_s=60), "usage": {"cost": 100.0}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    today = _today()
    assert ledger.spent_on(today, path) == pytest.approx(0.75)
    assert ledger.spent_on(today - timedelta(days=1), path) == pytest.approx(9.0)
    assert ledger.spent_on(today - timedelta(days=8), path) == 0.0


def test_spent_on_treats_naive_timestamp_as_utc(tmp_path):
    # summarize() reads naive `ts` values as UTC; spent_on must agree with it.
    path = tmp_path / "ledger.jsonl"
    naive_today = (ledger.since_day_start(0) + timedelta(minutes=1)).replace(tzinfo=None)
    naive_yesterday = (ledger.since_day_start(1) + timedelta(minutes=1)).replace(tzinfo=None)
    lines = [
        {"ts": naive_today.isoformat(), "usage": {"cost": 1.5}},
        {"ts": naive_yesterday.isoformat(), "usage": {"cost": 2.5}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    assert ledger.spent_on(_today(), path) == pytest.approx(1.5)


def test_spent_on_missing_file_returns_zero(tmp_path):
    assert ledger.spent_on(_today(), tmp_path / "nope.jsonl") == 0.0


def test_spent_on_never_raises_on_unreadable_path(tmp_path):
    # A directory where the ledger should be makes open() raise; a broken
    # ledger must degrade to 0.0, never take the server down.
    assert ledger.spent_on(_today(), tmp_path) == 0.0


def test_spent_on_skips_malformed_and_garbage_entries(tmp_path):
    path = tmp_path / "ledger.jsonl"
    lines = [
        "not valid json\n",
        json.dumps(42) + "\n",  # valid JSON, not an object
        json.dumps({"usage": {"cost": 3.0}}) + "\n",  # missing ts
        json.dumps({"ts": "not-a-timestamp", "usage": {"cost": 3.0}}) + "\n",
        json.dumps({"ts": _ts_at(0)}) + "\n",  # missing usage
        json.dumps({"ts": _ts_at(0), "usage": [1, 2]}) + "\n",  # usage not a mapping
        json.dumps({"ts": _ts_at(0), "usage": {}}) + "\n",  # missing cost
        json.dumps({"ts": _ts_at(0), "usage": {"cost": "2.0"}}) + "\n",
        json.dumps({"ts": _ts_at(0), "usage": {"cost": True}}) + "\n",
        json.dumps({"ts": _ts_at(0), "usage": {"cost": -1.0}}) + "\n",
        json.dumps({"ts": _ts_at(0), "usage": {"cost": 0.0}}) + "\n",
        json.dumps({"ts": _ts_at(0), "usage": {"cost": float("nan")}}) + "\n",
        json.dumps({"ts": _ts_at(0), "usage": {"cost": float("inf")}}) + "\n",
        json.dumps({"ts": _ts_at(0), "usage": {"cost": 2.0}}) + "\n",
    ]
    path.write_text("".join(lines))

    assert ledger.spent_on(_today(), path) == pytest.approx(2.0)


def test_spent_on_defaults_to_state_dir_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "state"))
    path = state_dir() / "ledger.jsonl"
    ledger.record(
        path,
        job_id="j1",
        swarm_id=None,
        role="r",
        model="m",
        result=_result(cost=0.4),
    )

    assert ledger.spent_on(_today()) == pytest.approx(0.4)


def test_spent_on_missing_default_ledger_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYMODEL_STATE_DIR", str(tmp_path / "state"))
    state_dir()  # creates the dir, but no ledger file
    assert ledger.spent_on(_today()) == 0.0


# ---------------------------------------------------------------------------
# since_day_start (the `usage` roll-up's since_days cutoff)
# ---------------------------------------------------------------------------


def test_since_day_start_anchors_on_the_local_day(los_angeles_tz):
    # Local 2024-01-15 23:30 PST == 2024-01-16 07:30 UTC: the UTC date has
    # already rolled over, but "today" must still mean the local date, so the
    # cutoff is local midnight, not `now`.
    now = datetime(2024, 1, 15, 23, 30, tzinfo=timezone(timedelta(hours=-8)))
    cutoff = ledger.since_day_start(0, now=now)

    assert cutoff == datetime(2024, 1, 15, 8, 0, tzinfo=UTC)
    assert cutoff.astimezone(now.tzinfo).date() == date(2024, 1, 15)
    assert cutoff <= now.astimezone(UTC)

    # days=N reaches back over local calendar days, not rolling 24h windows.
    assert ledger.since_day_start(2, now=now) == datetime(2024, 1, 13, 8, 0, tzinfo=UTC)


def test_since_day_start_default_now_is_utc_and_today():
    cutoff = ledger.since_day_start(0)
    assert cutoff.tzinfo is UTC
    assert cutoff.astimezone().date() == _today()
    assert cutoff <= datetime.now(UTC)


def test_summarize_since_day_start_includes_jobs_run_earlier_today(tmp_path):
    # Regression: `usage(since_days=0)` is documented as "since the start of
    # today", but the cutoff used to be `datetime.now(UTC)`, which excluded
    # every job run earlier the same day. The cutoff must be the start of the
    # local day (server.py passes `ledger.since_day_start(since_days)`).
    path = tmp_path / "ledger.jsonl"
    now = datetime.now(UTC)
    earlier_today = max(
        ledger.since_day_start(0) + timedelta(minutes=1),
        now - timedelta(hours=2),
    )
    assert earlier_today < now, "test needs an instant earlier today but before now"

    with path.open("w") as f:
        f.write(
            json.dumps({"ts": earlier_today.isoformat(), "model": "m", "usage": {"cost": 0.3}})
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "ts": (ledger.since_day_start(1) + timedelta(minutes=1)).isoformat(),
                    "model": "m",
                    "usage": {"cost": 7.0},
                }
            )
            + "\n"
        )

    fixed = ledger.summarize(path, since=ledger.since_day_start(0), group_by="model")
    assert fixed["m"]["jobs"] == 1
    assert fixed["m"]["cost"] == pytest.approx(0.3)

    # The old cutoff (`datetime.now(UTC) - timedelta(days=0)`) dropped that entry.
    buggy = ledger.summarize(path, since=now, group_by="model")
    assert buggy.get("m", {}).get("jobs", 0) == 0
