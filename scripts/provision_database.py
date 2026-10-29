"""Least-privilege PostgreSQL roles for the configured database (Phase 7D-2C-2, D040).

Runs as the administrative role (POSTGRES_USER) and needs INQUEST_DB_USER,
INQUEST_DB_MIGRATION_USER and their passwords in .env. The database is POSTGRES_DB, or
POSTGRES_TEST_DB with APP_ENV=test. Without --apply everything runs in one transaction
that is rolled back, so the printed plan is exactly what --apply would do. Passwords never
appear in the output (only a SCRAM verifier is sent to the server).

    uv run python -m scripts.provision_database             # plan (dry run)
    uv run python -m scripts.provision_database --apply     # create/update roles, move
                                                            # ownership, grant privileges
    uv run python -m scripts.provision_database --check     # as the runtime role: report
                                                            # anything beyond least privilege
    uv run python -m scripts.provision_database --rollback [--apply]
                                                            # restore the bootstrap owner
                                                            # and PostgreSQL's defaults

Idempotent; changes no row of application data. Afterwards run `uv run alembic upgrade
head`, which now connects as the migration role. Procedure: docs/database.md.
"""

import argparse
import sys

from sqlalchemy import create_engine

from src.config import get_settings
from src.db.privileges import (
    ProvisioningRefused,
    grant_mismatches,
    provision,
    rollback_statements,
    runtime_problems,
)


def _check() -> int:
    settings = get_settings()
    if not settings.inquest_db_user:
        print("INQUEST_DB_USER is not configured: the application uses the bootstrap role")
        return 1
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        problems = runtime_problems(conn) + grant_mismatches(conn, settings.inquest_db_user)
    engine.dispose()
    for problem in problems:
        print(f"problem: {problem}")
    print("runtime role is least privilege" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    parser.add_argument("--rollback", action="store_true", help="undo the provisioning")
    parser.add_argument("--check", action="store_true", help="verify the runtime role")
    args = parser.parse_args()
    if args.check:
        return _check()
    settings = get_settings()
    engine = create_engine(settings.admin_database_url)
    try:
        with engine.connect() as conn:
            if args.rollback:
                statements = rollback_statements(conn, settings)
                for statement in statements:
                    conn.exec_driver_sql(statement)
            else:
                statements = provision(conn, settings)
            for statement in statements:
                print(f"{statement};")
            if args.apply:
                conn.commit()
            else:
                conn.rollback()
    except ProvisioningRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    action = "rollback" if args.rollback else "provisioning"
    database = settings.database_name
    print(f"-- {action} of {database}: {'applied' if args.apply else 'dry run, rolled back'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
