import os

# Must be set before src modules read Settings; switches the DB to POSTGRES_TEST_DB.
os.environ["APP_ENV"] = "test"

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.db.session import engine, get_session  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    # Route requests through the rolled-back test session so API tests leave no rows behind.
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    # Outer transaction is rolled back so tests never leave rows behind, even if they commit.
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as s:
            yield s
        await trans.rollback()
