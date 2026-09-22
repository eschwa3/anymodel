"""Admin CLI: thin argparse wrapper over the service layer."""

from __future__ import annotations

import argparse
import sys

from jobsched import billing, db, health, scheduler, service
from jobsched.config import load_config
from jobsched.errors import JobSchedError
from jobsched.utils.time import now


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobsched")
    parser.add_argument("--db", default="jobsched.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-customer")
    p.add_argument("--name", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--plan-id", type=int, required=True)

    p = sub.add_parser("create-job")
    p.add_argument("--customer-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--tag", action="append", dest="tags", default=None)

    p = sub.add_parser("tick")
    p.add_argument("--worker-id", default="cli-worker")

    p = sub.add_parser("jobs-by-tag")
    p.add_argument("--tag", required=True)

    p = sub.add_parser("generate-invoice")
    p.add_argument("--customer-id", type=int, required=True)
    p.add_argument("--period-start", required=True)
    p.add_argument("--period-end", required=True)

    p = sub.add_parser("import-customers")
    p.add_argument("--file", required=True)

    sub.add_parser("status")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    db.apply_migrations(conn)
    cfg = load_config()

    try:
        if args.command == "create-customer":
            customer = service.create_customer(conn, args.name, args.email, args.plan_id)
            print(f"created customer {customer.id}")
        elif args.command == "create-job":
            job = service.create_job(conn, args.customer_id, args.name, args.priority, tags=args.tags)
            print(f"created job {job.id} ({job.status.value}) tags={job.tags}")
        elif args.command == "tick":
            job = scheduler.reserve_next_job(conn, args.worker_id, cfg)
            print(f"reserved job {job.id}" if job else "no pending jobs")
        elif args.command == "jobs-by-tag":
            jobs = service.list_jobs_by_tag(conn, args.tag)
            print(f"{len(jobs)} job(s) tagged {args.tag!r}")
            for job in jobs:
                print(f"  job {job.id}: {job.name} ({job.status.value})")
        elif args.command == "generate-invoice":
            invoice = billing.generate_invoice(conn, args.customer_id, args.period_start, args.period_end, cfg)
            print(f"invoice {invoice.id}: total {invoice.total_cents} cents ({invoice.currency})")
        elif args.command == "import-customers":
            from jobsched.importer import import_customers

            result = import_customers(conn, args.file)
            print(f"imported {result.imported} customers, {len(result.errors)} errors")
        elif args.command == "status":
            health_info = health.healthcheck()
            print(f"jobsched status as of {now().isoformat()}: {health_info['status']}")
        else:
            parser.error(f"unknown command {args.command!r}")
    except JobSchedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
