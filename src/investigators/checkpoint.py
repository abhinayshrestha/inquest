"""LangGraph checkpoints in PostgreSQL, schema `langgraph` (D015).

Same database as investigation memory, separate schema, not managed by Alembic (its
env.py does not include other schemas, so autogenerate never sees these tables).
Checkpoints are execution state only; nothing reads conclusions from them.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

from src.config import Settings, get_settings

SCHEMA = "langgraph"


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


@asynccontextmanager
async def open_checkpointer(settings: Settings | None = None) -> AsyncIterator[AsyncPostgresSaver]:
    conninfo = _conninfo(settings or get_settings())
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
        # Idempotent; creates or migrates the checkpoint tables in SCHEMA.
        await saver.setup()
        yield saver


def thread_id(assignment_id: object) -> str:
    return f"asg:{assignment_id}"
