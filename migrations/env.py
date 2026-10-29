import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from src.config import get_settings
from src.db import privileges
from src.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_settings().migration_database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# Commands that change the schema; afterwards the runtime role's privileges are reapplied.
SCHEMA_COMMANDS = frozenset({"upgrade", "downgrade", "stamp"})


def do_run_migrations(connection: Connection) -> None:
    runtime = get_settings().inquest_db_user
    current = connection.exec_driver_sql("SELECT current_user").scalar()
    connection.commit()
    if runtime and current == runtime:
        # It would fail on the first DDL anyway; say why (D040).
        raise RuntimeError("Alembic must run as the migration role, not the runtime role")
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()
        command = getattr(config.cmd_opts, "cmd", None)
        if runtime and command and command[0].__name__ in SCHEMA_COMMANDS:
            # Default privileges cover new tables; this applies the matrix's exceptions
            # (e.g. append-only audit rows) to them as well.
            privileges.apply_runtime_grants(connection, runtime)


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = create_async_engine(
        get_settings().migration_database_url, poolclass=pool.NullPool
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
