"""The test database runs with least-privilege roles (D040): the application's engine
connects as a runtime role that owns nothing and may create nothing, so any code that
starts needing more fails the suite. Roles are test-only (`inquest_test_*`, passwords
random per run and never stored), provisioned by the bootstrap role at session start.
Cleanup that the application itself may not do (deleting append-only audit rows) uses
the migration role through OwnerSession."""

import os
import secrets
from functools import cache

from sqlalchemy import NullPool, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

RUNTIME, MIGRATION = "inquest_test_app", "inquest_test_migrator"


def use_test_roles() -> None:
    """Before anything reads Settings. Always these names, whatever .env says, so a test
    run can never change the password of a role another database relies on."""
    os.environ["INQUEST_DB_USER"] = RUNTIME
    os.environ["INQUEST_DB_PASSWORD"] = secrets.token_urlsafe(32)
    os.environ["INQUEST_DB_MIGRATION_USER"] = MIGRATION
    os.environ["INQUEST_DB_MIGRATION_PASSWORD"] = secrets.token_urlsafe(32)


def provision_test_database() -> None:
    from src.config import get_settings
    from src.db.privileges import provision

    settings = get_settings()
    # Only ever the test database.
    assert settings.app_env == "test"
    assert settings.database_name == settings.postgres_test_db != settings.postgres_db
    engine = create_engine(settings.admin_database_url, poolclass=NullPool)
    with engine.begin() as conn:
        provision(conn, settings)
    engine.dispose()


@cache
def _owner_sessions() -> async_sessionmaker[AsyncSession]:
    # Built on first use: Settings must not be read before use_test_roles().
    from src.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url, poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False)


def OwnerSession() -> AsyncSession:  # noqa: N802 (used like a sessionmaker)
    return _owner_sessions()()
