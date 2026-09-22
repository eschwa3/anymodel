"""Hidden integration test for the R8 (swarm-feature) bake-off task.

Injected as `tests/test_job_tags_integration.py` into the *merged* integration
checkout after all three workers' branches are combined -- never shown to any
individual worker. Exercises the full "add job tags" feature across all three
slices: persistence (migration + repository), service layer, and the
CLI/handler surface -- so it only passes if the three independently-written
pieces actually agree on the contract (tags param name, `tags` field on
`Job`, `service.create_job(..., tags=...)`, `service.list_jobs_by_tag`,
`JobRepository.add_tag`/`jobs_with_tag`).
"""

from __future__ import annotations

from jobsched import cli, db, service
from jobsched.repository import PlanRepository


def test_create_job_with_tags_round_trips(conn):
    plan = PlanRepository(conn).create("starter", 999)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan.id)

    job = service.create_job(conn, cust.id, "render", tags=["urgent", "gpu"])
    assert sorted(job.tags) == ["gpu", "urgent"]


def test_tag_job_and_list_by_tag(conn):
    plan = PlanRepository(conn).create("starter", 999)
    cust = service.create_customer(conn, "Acme", "ops@acme.test", plan.id)
    job_a = service.create_job(conn, cust.id, "render")
    service.create_job(conn, cust.id, "encode")

    service.tag_job(conn, job_a.id, "urgent")
    tagged = service.list_jobs_by_tag(conn, "urgent")

    assert [j.id for j in tagged] == [job_a.id]


def test_cli_create_job_with_tag_flag(tmp_path, capsys):
    db_path = str(tmp_path / "cli.db")
    seed_conn = db.connect(db_path)
    db.apply_migrations(seed_conn)
    PlanRepository(seed_conn).create("starter", 999)
    seed_conn.close()

    cli.main(["--db", db_path, "create-customer", "--name", "Acme", "--email", "a@b.test", "--plan-id", "1"])
    capsys.readouterr()

    rc = cli.main(
        [
            "--db",
            db_path,
            "create-job",
            "--customer-id",
            "1",
            "--name",
            "render",
            "--tag",
            "urgent",
            "--tag",
            "gpu",
        ]
    )
    assert rc == 0
    capsys.readouterr()  # the prompt doesn't specify create-job's output; just drain it

    rc2 = cli.main(["--db", db_path, "jobs-by-tag", "--tag", "urgent"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "1 job(s) tagged 'urgent'" in out2

    rc3 = cli.main(["--db", db_path, "jobs-by-tag", "--tag", "gpu"])
    assert rc3 == 0
    out3 = capsys.readouterr().out
    assert "1 job(s) tagged 'gpu'" in out3
