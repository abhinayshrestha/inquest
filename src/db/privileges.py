"""PostgreSQL least privilege (Phase 7D-2C-2, D040; audit H4).

Three identities, never mixed:

- Administrative: the bootstrap superuser (POSTGRES_USER). Creates the two roles below,
  moves ownership to the migration role and sets default privileges (`provision`, run by
  `scripts.provision_database`). Creating databases and roles stays here. The running
  application never uses it once the roles are configured.
- Migration (INQUEST_DB_MIGRATION_USER): owns the application schemas (`public`,
  `langgraph`) and every object in them; runs Alembic, which also prepares LangGraph's
  checkpoint tables. Not a superuser; no CREATEDB, CREATEROLE, BYPASSRLS or replication.
- Runtime (INQUEST_DB_USER): the FastAPI process with its investigators, MCP server and
  Tool Executor, and the operator scripts. USAGE on the schemas and the table privileges
  in `runtime_privileges` only: owns nothing, may create nothing (no CREATE on schemas or
  the database, no TEMPORARY), belongs to no role, NOINHERIT.

Every function takes a synchronous SQLAlchemy connection; async callers use run_sync.
"""

import logging

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.config import Settings

logger = logging.getLogger(__name__)

SCHEMAS = ("public", "langgraph")
DML = ("SELECT", "INSERT", "UPDATE", "DELETE")
# Every other table gets DML. Audit rows are append-only for the application (no update,
# no delete); the version tables of Alembic and LangGraph are written by migrations only.
RUNTIME_EXCEPTIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("public", "audit_events"): ("SELECT", "INSERT"),
    ("public", "alembic_version"): ("SELECT",),
    ("langgraph", "checkpoint_migrations"): ("SELECT",),
}
ROLE_ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION"
REDACTED = "PASSWORD '<scram-sha-256 verifier>'"

TABLES = text(
    "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p') ORDER BY 1, 2"
)
EXISTING_SCHEMAS = text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:schemas) ORDER BY 1")


class ProvisioningRefused(Exception):
    pass


def runtime_privileges(schema: str, table: str) -> tuple[str, ...]:
    return RUNTIME_EXCEPTIONS.get((schema, table), DML)


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _schemas(conn: Connection) -> list[str]:
    return list(conn.scalars(EXISTING_SCHEMAS, {"schemas": list(SCHEMAS)}))


def runtime_grant_statements(conn: Connection, runtime: str) -> list[str]:
    """The runtime role's exact privileges on the schemas, tables and sequences that exist
    now: everything revoked first, so re-running converges on the matrix."""
    role = ident(runtime)
    statements = []
    for schema in map(ident, _schemas(conn)):
        statements += [
            f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC, {role}",
            f"GRANT USAGE ON SCHEMA {schema} TO {role}",
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC, {role}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM PUBLIC, {role}",
            # Identity columns draw from sequences; setval (UPDATE) is not needed.
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}",
            f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM PUBLIC, {role}",
        ]
    for schema, table in conn.execute(TABLES, {"schemas": list(SCHEMAS)}):
        privileges = ", ".join(runtime_privileges(schema, table))
        statements.append(f"GRANT {privileges} ON {ident(schema)}.{ident(table)} TO {role}")
    return statements


def apply_runtime_grants(conn: Connection, runtime: str) -> None:
    """After migrations (migrations/env.py), as the owner: new tables get exactly their
    matrix entry, whatever the default privileges gave them."""
    if conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": runtime}) is None:
        return
    for statement in runtime_grant_statements(conn, runtime):
        conn.exec_driver_sql(statement)


def _roles(settings: Settings) -> tuple[str, str]:
    runtime, migration = settings.inquest_db_user, settings.inquest_db_migration_user
    if not runtime or not migration:
        raise ProvisioningRefused(
            "set INQUEST_DB_USER and INQUEST_DB_MIGRATION_USER (with their passwords) first"
        )
    if len({runtime, migration, settings.postgres_user}) != 3:
        raise ProvisioningRefused(
            "the runtime, migration and administrative roles must be three different roles"
        )
    return runtime, migration


def _role_statement(conn: Connection, role: str, password: str, inherit: str) -> tuple[str, str]:
    existing = conn.execute(
        text("SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = :r"),
        {"r": role},
    ).first()
    if existing is not None and any(existing):
        # Never demote (or hand our objects to) a role someone else relies on as privileged.
        raise ProvisioningRefused(f"role {role} already exists with administrative attributes")
    # Only a SCRAM verifier crosses the wire and reaches any statement log, never the
    # password itself.
    pgconn = conn.connection.driver_connection.pgconn  # type: ignore[union-attr]
    verifier = pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256")
    verb = "ALTER" if existing is not None else "CREATE"
    head = f"{verb} ROLE {ident(role)} {ROLE_ATTRIBUTES} {inherit}"
    return f"{head} PASSWORD '{verifier.decode()}'", f"{head} {REDACTED}"


def provision(conn: Connection, settings: Settings) -> list[str]:
    """Gives the configured database least-privilege roles, as the administrative role,
    inside the caller's transaction (commit to apply, roll back for a dry run). Returns
    the statements as shown (role passwords redacted). Idempotent: each step reads the
    catalog as the previous ones left it; no row of application data changes."""
    runtime, migration = _roles(settings)
    assert settings.inquest_db_password and settings.inquest_db_migration_password
    r, m, db = ident(runtime), ident(migration), ident(settings.database_name)
    shown = []

    def run(statements: list[str]) -> None:
        for statement in statements:
            conn.exec_driver_sql(statement)
            shown.append(statement)

    for role, password, inherit in (
        (migration, settings.inquest_db_migration_password, "INHERIT"),
        (runtime, settings.inquest_db_password, "NOINHERIT"),
    ):
        statement, redacted = _role_statement(conn, role, password.get_secret_value(), inherit)
        conn.exec_driver_sql(statement)
        shown.append(redacted)
    # Neither role may hold another role's privileges (in particular runtime → migration).
    memberships = conn.execute(
        text(
            "SELECT g.rolname, u.rolname FROM pg_auth_members a "
            "JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles u ON u.oid = a.member "
            "WHERE u.rolname IN (:r, :m)"
        ),
        {"r": runtime, "m": migration},
    )
    run([f"REVOKE {ident(granted)} FROM {ident(member)}" for granted, member in memberships])
    run(
        [
            f"REVOKE ALL ON DATABASE {db} FROM PUBLIC, {r}, {m}",
            f"GRANT CONNECT ON DATABASE {db} TO {r}",
            f"GRANT CONNECT, CREATE ON DATABASE {db} TO {m}",
            f"CREATE SCHEMA IF NOT EXISTS langgraph AUTHORIZATION {m}",
        ]
        + [f"ALTER SCHEMA {schema} OWNER TO {m}" for schema in map(ident, SCHEMAS)]
    )
    run(_ownership_statements(conn, migration, m))
    run(
        [
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA {schema} GRANT {grant} TO {r}"
            for schema in map(ident, SCHEMAS)
            for grant in ("SELECT, INSERT, UPDATE, DELETE ON TABLES", "USAGE, SELECT ON SEQUENCES")
        ]
        + [f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"]
    )
    run(runtime_grant_statements(conn, runtime))
    return shown


# Objects in the application schemas not yet owned by `owner`. Identity and serial
# sequences follow their table; indexes and triggers belong to their table.
OBJECTS = text(
    """
    SELECT CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'c' THEN 'TYPE'
                WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
           format('%I.%I', n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S', 'c')
      AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = :owner)
      AND NOT (c.relkind = 'S' AND EXISTS (
          SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
          AND d.objid = c.oid AND d.deptype IN ('a', 'i')))
    UNION ALL
    SELECT CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END,
           format('%I.%I(%s)', n.nspname, p.proname, pg_get_function_identity_arguments(p.oid))
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = ANY(:schemas) AND p.prokind IN ('f', 'p')
      AND p.proowner <> (SELECT oid FROM pg_roles WHERE rolname = :owner)
    UNION ALL
    SELECT CASE t.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END,
           format('%I.%I', n.nspname, t.typname)
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = ANY(:schemas) AND t.typtype IN ('e', 'd', 'r')
      AND t.typowner <> (SELECT oid FROM pg_roles WHERE rolname = :owner)
    ORDER BY 1, 2
    """
)  # fmt: skip


def _ownership_statements(conn: Connection, owner: str, quoted: str) -> list[str]:
    rows = conn.execute(OBJECTS, {"schemas": list(SCHEMAS), "owner": owner})
    return [f"ALTER {kind} {name} OWNER TO {quoted}" for kind, name in rows]


def rollback_statements(conn: Connection, settings: Settings) -> list[str]:
    """Undoes provision_statements on the configured database: the administrative role
    owns everything again, PostgreSQL's default schema and database privileges return,
    and the two roles keep nothing here (they are not dropped: roles span databases)."""
    runtime, migration = _roles(settings)
    r, m, db = ident(runtime), ident(migration), ident(settings.database_name)
    admin = ident(settings.postgres_user)
    statements = []
    for schema in map(ident, _schemas(conn)):
        statements += [
            f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {r}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {r}",
            f"REVOKE ALL ON SCHEMA {schema} FROM {r}",
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {schema} TO PUBLIC",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA {schema} "
            f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {r}",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} IN SCHEMA {schema} "
            f"REVOKE USAGE, SELECT ON SEQUENCES FROM {r}",
        ]
    statements.append(f"ALTER DEFAULT PRIVILEGES FOR ROLE {m} GRANT EXECUTE ON FUNCTIONS TO PUBLIC")
    rows = conn.execute(OBJECTS, {"schemas": list(SCHEMAS), "owner": settings.postgres_user})
    statements += [f"ALTER {kind} {name} OWNER TO {admin}" for kind, name in rows]
    statements += [
        "ALTER SCHEMA public OWNER TO pg_database_owner",
        "GRANT USAGE ON SCHEMA public TO PUBLIC",
        f"REVOKE ALL ON DATABASE {db} FROM {r}, {m}",
        f"GRANT CONNECT, TEMPORARY ON DATABASE {db} TO PUBLIC",
    ]
    if "langgraph" in _schemas(conn):
        statements.append(f"ALTER SCHEMA langgraph OWNER TO {admin}")
    return statements


TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def grant_mismatches(conn: Connection, runtime: str) -> list[str]:
    """Where the runtime role's table privileges differ from the matrix (directly or
    through PUBLIC). By table oid, so it reports even where the schema is not usable."""
    mismatches = []
    tables = conn.execute(
        text(
            "SELECT n.nspname, c.relname, c.oid FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY(:schemas) AND c.relkind IN ('r', 'p') ORDER BY 1, 2"
        ),
        {"schemas": list(SCHEMAS)},
    ).all()
    for schema, table, oid in tables:
        held = tuple(
            p
            for p in TABLE_PRIVILEGES
            if conn.scalar(
                text("SELECT has_table_privilege(:r, CAST(:t AS oid), :p)"),
                {"r": runtime, "t": oid, "p": p},
            )
        )
        if held != runtime_privileges(schema, table):
            mismatches.append(f"{schema}.{table}: {', '.join(held) or 'nothing'}")
    return mismatches


def runtime_problems(conn: Connection) -> list[str]:
    """Why the connected role is more than a runtime role, if it is: anything that would
    let the application change the schema, bypass constraints or act as another role."""
    problems = []
    role = conn.execute(
        text(
            "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    for flag, name in zip(role[1:], ("superuser", "CREATEROLE", "CREATEDB", "BYPASSRLS",
                                     "REPLICATION"), strict=True):  # fmt: skip
        if flag:
            problems.append(f"role {role[0]} has {name}")
    schemas = {"schemas": list(SCHEMAS)}
    owned = conn.scalar(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY(:schemas) AND pg_has_role(c.relowner, 'USAGE')"
        ),
        schemas,
    )
    if owned:
        problems.append(f"owns (or may act as the owner of) {owned} application objects")
    for (schema,) in conn.execute(
        text(
            "SELECT nspname FROM pg_namespace WHERE nspname = ANY(:schemas) "
            "AND (pg_has_role(nspowner, 'USAGE') OR has_schema_privilege(oid, 'CREATE'))"
        ),
        schemas,
    ):
        problems.append(f"may create objects in schema {schema}")
    for privilege in ("CREATE", "TEMPORARY"):
        if conn.scalar(text(f"SELECT has_database_privilege(current_database(), '{privilege}')")):
            problems.append(f"has {privilege} on the database")
    for (granted,) in conn.execute(
        text(
            "SELECT g.rolname FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid "
            "WHERE a.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)"
        )
    ):
        problems.append(f"is a member of role {granted}")
    for (function,) in conn.execute(
        text(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = ANY(:schemas) AND p.prosecdef"
        ),
        schemas,
    ):
        problems.append(f"SECURITY DEFINER function {function} in an application schema")
    return problems


async def check_runtime_role(engine: AsyncEngine, settings: Settings) -> list[str]:
    """At startup: production refuses to run with a privileged database role; local
    development (which may still use the bootstrap role) only warns."""
    async with engine.connect() as conn:
        problems = await conn.run_sync(runtime_problems)
    if problems and settings.app_env == "production":
        raise RuntimeError(
            "refusing to start: the database role is not a least-privilege runtime role "
            f"({'; '.join(problems)}); configure INQUEST_DB_USER (scripts.provision_database)"
        )
    for problem in problems:
        logger.warning("database role: %s", problem)
    return problems
