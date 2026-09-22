from __future__ import annotations

from jobsched import cli


def test_create_customer_and_job(tmp_path, capsys):
    db_path = str(tmp_path / "cli.db")
    assert cli.main(["--db", db_path, "create-customer", "--name", "x", "--email", "a@b.test", "--plan-id", "1"]) != 0
    # plan 1 doesn't exist yet -- expect a clean error exit, not a traceback
    out = capsys.readouterr()
    assert "error" in out.err


def test_status_command_runs(tmp_path, capsys):
    db_path = str(tmp_path / "cli.db")
    rc = cli.main(["--db", db_path, "status"])
    assert rc == 0
    out = capsys.readouterr()
    assert "jobsched status" in out.out
