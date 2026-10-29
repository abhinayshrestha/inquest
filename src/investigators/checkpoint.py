"""LangGraph checkpoints in PostgreSQL, schema `langgraph` (D015, D040).

Same database as investigation memory, separate schema. Its tables are LangGraph's own
(Alembic's autogenerate never compares them), but they are prepared by an Alembic
migration (`prepare_checkpoint_schema`, as the migration role), never at application
startup: the runtime role may not create or alter anything. Checkpoints are execution
state only; nothing reads conclusions from them.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.errors import InsufficientPrivilege, UndefinedTable
from sqlalchemy import Connection

from src.config import Settings, get_settings

SCHEMA = "langgraph"
# LangGraph's own migrations (what AsyncPostgresSaver.setup() runs), versioned in
# checkpoint_migrations.
MIGRATIONS = AsyncPostgresSaver.MIGRATIONS


class CheckpointSchemaNotReady(Exception):
    pass


def _conninfo(settings: Settings) -> str:
    url = settings.database_url
    return make_conninfo(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        options=f"-c search_path={SCHEMA}",
    )


def prepare_checkpoint_schema(conn: Connection) -> None:
    """Creates or migrates the checkpoint tables in SCHEMA exactly as setup() would. For
    migrations: needs an autocommit connection (some LangGraph migrations create indexes
    concurrently) and the right to create objects in SCHEMA. Idempotent."""
    previous = conn.exec_driver_sql("SHOW search_path").scalar()
    conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    conn.exec_driver_sql(f"SET search_path TO {SCHEMA}")
    try:
        conn.exec_driver_sql(MIGRATIONS[0])
        version = conn.exec_driver_sql("SELECT max(v) FROM checkpoint_migrations").scalar()
        start = -1 if version is None else version
        for v in range(start + 1, len(MIGRATIONS)):
            conn.exec_driver_sql(MIGRATIONS[v])
            conn.exec_driver_sql(f"INSERT INTO checkpoint_migrations (v) VALUES ({v})")
    finally:
        conn.exec_driver_sql(f"SET search_path TO {previous}")


async def _require_prepared(conninfo: str) -> None:
    latest = len(MIGRATIONS) - 1
    try:
        async with await AsyncConnection.connect(conninfo) as conn:
            row = await (await conn.execute("SELECT max(v) FROM checkpoint_migrations")).fetchone()
    except (UndefinedTable, InsufficientPrivilege):
        row = None
    if row is None or row[0] != latest:
        raise CheckpointSchemaNotReady(
            f"the {SCHEMA} checkpoint schema is not at LangGraph's version {latest}: run "
            "`uv run alembic upgrade head` (migration role)"
        )


@asynccontextmanager
async def open_checkpointer(settings: Settings | None = None) -> AsyncIterator[AsyncPostgresSaver]:
    conninfo = _conninfo(settings or get_settings())
    # Read-only check instead of setup(): the schema belongs to migrations (D040).
    await _require_prepared(conninfo)
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        yield saver


def thread_id(assignment_id: object) -> str:
    return f"asg:{assignment_id}"
