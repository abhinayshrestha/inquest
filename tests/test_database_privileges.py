"""PostgreSQL least privilege (Phase 7D-2C-2, D040; audit H4), against real PostgreSQL
connections: the application runs as a runtime role that owns nothing and can create,
alter, bypass or impersonate nothing; the migration role owns the schema and runs Alembic;
provisioning is idempotent and reversible. Every other test in the suite also runs as the
runtime role (tests/database.py), so any code path that starts needing more fails there."""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import NullPool, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from src.config import Settings, get_settings
from src.db.privileges import (
    SCHEMAS,
    check_runtime_role,
    grant_mismatches,
    provision,
    rollback_statements,
    runtime_problems,
)
from src.db.session import engine
from src.investigators.checkpoint import MIGRATIONS, CheckpointSchemaNotReady, open_checkpointer
from tests.database import MIGRATION, RUNTIME

ROOT = Path(__file__).resolve().parent.parent


async def test_the_application_runs_as_the_least_privilege_runtime_role():
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT current_user")) == RUNTIME
        assert await conn.run_sync(runtime_problems) == []
        assert await conn.run_sync(grant_mismatches, RUNTIME) == []


async def test_role_attributes_and_memberships():
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, "
                    "rolreplication, rolinherit, rolcanlogin FROM pg_roles "
                    "WHERE rolname IN (:r, :m) ORDER BY rolname"
                ),
                {"r": RUNTIME, "m": MIGRATION},
            )
        ).all()
        memberships = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_auth_members a JOIN pg_roles u ON u.oid = a.member "
                "WHERE u.rolname IN (:r, :m)"
            ),
            {"r": RUNTIME, "m": MIGRATION},
        )
    assert [tuple(r) for r in rows] == [
        (RUNTIME, False, False, False, False, False, False, True),
        (MIGRATION, False, False, False, False, False, True, True),
    ]
    assert memberships == 0


async def test_the_migration_role_owns_the_schema_and_the_runtime_role_nothing():
    async with engine.connect() as conn:
        owners = set(
            await conn.scalars(
                text(
                    "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = ANY(:s) "
                    "UNION SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                    "WHERE nspname = ANY(:s) "
                    "UNION SELECT DISTINCT pg_get_userbyid(p.proowner) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = ANY(:s)"
                ),
                {"s": list(SCHEMAS)},
            )
        )
        runtime_owned = await conn.scalar(
            text(
                "SELECT (SELECT count(*) FROM pg_class WHERE relowner = r.oid) "
                "+ (SELECT count(*) FROM pg_namespace WHERE nspowner = r.oid) "
                "+ (SELECT count(*) FROM pg_proc WHERE proowner = r.oid) "
                "FROM pg_roles r WHERE rolname = :r"
            ),
            {"r": RUNTIME},
        )
    assert owners == {MIGRATION}
    assert runtime_owned == 0


FORBIDDEN = {
    "create a table": "CREATE TABLE public.intruder (i int)",
    "create a checkpoint table": "CREATE TABLE langgraph.intruder (i int)",
    "create a schema": "CREATE SCHEMA intruder",
    "create a temporary table": "CREATE TEMP TABLE intruder (i int)",
    "shadow a function": "CREATE FUNCTION public.now() RETURNS int LANGUAGE sql AS 'SELECT 1'",
    "alter a table": "ALTER TABLE environments ADD COLUMN intruder int",
    "drop a table": "DROP TABLE rate_limit_counters",
    "create an index": "CREATE INDEX intruder ON environments (name)",
    "disable the tenant trigger": "ALTER TABLE environments DISABLE TRIGGER tenant_scope",
    "skip triggers": "SET session_replication_role = replica",
    "truncate": "TRUNCATE rate_limit_counters",
    "edit audit rows": "UPDATE audit_events SET action = 'x'",
    "delete audit rows": "DELETE FROM audit_events",
    "rewrite the migration version": "UPDATE alembic_version SET version_num = 'x'",
    "rewrite checkpoint versions": "DELETE FROM langgraph.checkpoint_migrations",
    "call the trigger function": "SELECT public.inquest_check_tenant_scope()",
    "create a role": "CREATE ROLE intruder",
    "create a database": "CREATE DATABASE intruder",
    "become the migration role": f"SET ROLE {MIGRATION}",
    "become a superuser": f"ALTER ROLE {RUNTIME} SUPERUSER",
    "bypass row security": f"ALTER ROLE {RUNTIME} BYPASSRLS",
    "join the migration role": f"GRANT {MIGRATION} TO {RUNTIME}",
    "run a program": "COPY (SELECT 1) TO PROGRAM 'true'",
    "read server files": "SELECT pg_read_file('postgresql.conf')",
    "load an extension": "CREATE EXTENSION IF NOT EXISTS pgcrypto",
}


@pytest.mark.parametrize("statement", FORBIDDEN.values(), ids=FORBIDDEN.keys())
async def test_the_runtime_role_cannot(statement: str):
    async with engine.connect() as conn:
        # Autocommit: some of these (CREATE DATABASE) may not run in a transaction at all.
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        with pytest.raises(DBAPIError) as refused:
            await conn.execute(text(statement))
    # Refused for privilege, not for a typo or a missing object.
    assert refused.value.orig.sqlstate == "42501", refused.value.orig


async def test_grants_and_defaults_it_attempts_change_nothing():
    """PostgreSQL only warns when a role grants what it does not own, and lets any role
    set default privileges for objects it would create itself (it can create none)."""
    async with engine.connect() as conn:
        await conn.execute(text(f"GRANT CREATE ON SCHEMA public TO {RUNTIME}"))
        await conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {RUNTIME}"))
        await conn.execute(text("ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC"))
        assert await conn.run_sync(runtime_problems) == []
        assert await conn.run_sync(grant_mismatches, RUNTIME) == []
        await conn.rollback()


async def test_no_application_function_escalates_privileges():
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT p.proname, p.prosecdef, has_function_privilege(:r, p.oid, 'EXECUTE') "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = ANY(:s)"
                ),
                {"r": RUNTIME, "s": list(SCHEMAS)},
            )
        ).all()
    # One trigger function; it runs as whoever fires the trigger (no SECURITY DEFINER),
    # and nobody may call it directly. Triggers still fire (tenant isolation tests).
    assert [tuple(r) for r in rows] == [("inquest_check_tenant_scope", False, False)]


def test_the_role_check_reports_inherited_privileges():
    """A role that is a member of the migration role (or anything else) acts with its
    privileges: the startup check must say so, not only check its own attributes."""
    admin = create_engine(get_settings().admin_database_url, poolclass=NullPool)
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql("CREATE ROLE inquest_test_member NOLOGIN")
            conn.exec_driver_sql(f"GRANT {MIGRATION} TO inquest_test_member")
            conn.exec_driver_sql("SET ROLE inquest_test_member")
            problems = runtime_problems(conn)
            conn.rollback()
    finally:
        admin.dispose()
    assert f"is a member of role {MIGRATION}" in problems
    assert any(p.startswith("owns (or may act as the owner of)") for p in problems)
    assert "may create objects in schema public" in problems


async def test_startup_refuses_a_privileged_role_in_production():
    settings = get_settings()
    production = settings.model_copy(update={"app_env": "production"})
    admin = create_async_engine(settings.admin_database_url, poolclass=NullPool)
    migrator = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    try:
        for privileged in (admin, migrator):
            with pytest.raises(RuntimeError, match="not a least-privilege runtime role"):
                await check_runtime_role(privileged, production)
        # Local development still starts with the bootstrap role, with warnings.
        assert await check_runtime_role(admin, settings)
        assert await check_runtime_role(engine, production) == []
    finally:
        await admin.dispose()
        await migrator.dispose()


# --- a fresh database, provisioned and migrated from scratch ----------------------------------


def _alembic(settings: Settings, *args: str, **env: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "APP_ENV": "test", "POSTGRES_TEST_DB": settings.postgres_test_db}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, env=environment | env, capture_output=True, text=True,
    )  # fmt: skip


@pytest.fixture
def fresh() -> Iterator[Settings]:
    """A new database created the way an operator would (owned by the bootstrap role),
    then provisioned; dropped afterwards."""
    name = f"inquest_privileges_{uuid.uuid4().hex[:8]}"
    settings = get_settings().model_copy(update={"postgres_test_db": name})
    server = create_engine(get_settings().admin_database_url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    admin = create_engine(settings.admin_database_url, poolclass=NullPool)
    with admin.begin() as conn:
        provision(conn, settings)
    admin.dispose()
    try:
        yield settings
    finally:
        with server.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        server.dispose()


def _as_runtime(settings: Settings, check):
    runtime = create_engine(settings.database_url, poolclass=NullPool)
    try:
        with runtime.connect() as conn:
            return check(conn)
    finally:
        runtime.dispose()


async def test_alembic_runs_as_the_migration_role_and_never_as_the_runtime_role(fresh):
    runtime_login = {
        "INQUEST_DB_MIGRATION_USER": RUNTIME,
        "INQUEST_DB_MIGRATION_PASSWORD": os.environ["INQUEST_DB_PASSWORD"],
    }
    refused = _alembic(fresh, "upgrade", "head", **runtime_login)
    assert refused.returncode != 0
    assert "must run as the migration role" in refused.stderr
    # Without that guard (runtime role not configured), PostgreSQL refuses the DDL itself.
    unguarded = _alembic(fresh, "upgrade", "head", **runtime_login, INQUEST_DB_USER="",
                         INQUEST_DB_PASSWORD="")  # fmt: skip
    assert unguarded.returncode != 0
    assert "permission denied" in unguarded.stderr

    # Before the checkpoint migration, the runtime cannot open checkpoints (and does not
    # try to create them).
    assert _alembic(fresh, "upgrade", "0b9ac8fb6d48").returncode == 0
    with pytest.raises(CheckpointSchemaNotReady):
        async with open_checkpointer(fresh):
            pass

    for step in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")):
        result = _alembic(fresh, *step)
        assert result.returncode == 0, result.stderr[-2000:]
    assert _alembic(fresh, "check").returncode == 0

    # Everything new is owned by the migration role and granted per the matrix.
    assert _as_runtime(fresh, runtime_problems) == []
    assert _as_runtime(fresh, lambda c: grant_mismatches(c, RUNTIME)) == []
    version = _as_runtime(
        fresh, lambda c: c.scalar(text("SELECT max(v) FROM langgraph.checkpoint_migrations"))
    )
    assert version == len(MIGRATIONS) - 1
    async with open_checkpointer(fresh) as saver:
        assert [c async for c in saver.alist({"configurable": {"thread_id": "none"}})] == []


async def test_provisioning_is_idempotent_reversible_and_keeps_data(fresh):
    assert _alembic(fresh, "upgrade", "head").returncode == 0
    admin = create_engine(fresh.admin_database_url, poolclass=NullPool)
    runtime = create_engine(fresh.database_url, poolclass=NullPool)
    try:
        with runtime.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, issuer, subject) VALUES (:i, 'iss', 'kept')"),
                {"i": uuid.uuid4()},
            )
        with admin.begin() as conn:
            again = provision(conn, fresh)
        assert not any(s.startswith(("CREATE ROLE", "ALTER TABLE")) for s in again)

        with admin.begin() as conn:
            for statement in rollback_statements(conn, fresh):
                conn.exec_driver_sql(statement)
        with admin.connect() as conn:
            owners = set(
                conn.scalars(
                    text(
                        "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = ANY(:s)"
                    ),
                    {"s": list(SCHEMAS)},
                )
            )
            public_owner = conn.scalar(
                text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'")
            )
            stripped = grant_mismatches(conn, RUNTIME)
        assert owners == {fresh.postgres_user} and public_owner == "pg_database_owner"
        assert stripped and all(m.endswith(": nothing") for m in stripped)
        with pytest.raises(DBAPIError):  # the runtime role keeps nothing after a rollback
            with runtime.connect() as conn:
                conn.execute(text("SELECT count(*) FROM users"))

        # Provisioning takes over what the bootstrap role owns (an existing database).
        with admin.begin() as conn:
            taken = provision(conn, fresh)
        assert sum(s.startswith("ALTER TABLE") for s in taken) >= 25
        with runtime.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM users WHERE subject = 'kept'")) == 1
            assert runtime_problems(conn) == [] and grant_mismatches(conn, RUNTIME) == []
    finally:
        admin.dispose()
        runtime.dispose()


async def test_provisioning_refuses_to_mix_up_roles():
    from src.db.privileges import ProvisioningRefused

    settings = get_settings()
    admin = create_engine(settings.admin_database_url, poolclass=NullPool)
    privileged = f"inquest_test_admin_{uuid.uuid4().hex[:6]}"
    try:
        for update in (
            {"inquest_db_user": settings.postgres_user},
            {"inquest_db_migration_user": RUNTIME},
            # An existing privileged role is never demoted or handed the schema.
            {"inquest_db_migration_user": privileged},
        ):
            with admin.connect() as conn:
                conn.exec_driver_sql(f'CREATE ROLE "{privileged}" CREATEROLE')
                with pytest.raises(ProvisioningRefused):
                    provision(conn, settings.model_copy(update=update))
                conn.rollback()
    finally:
        admin.dispose()
