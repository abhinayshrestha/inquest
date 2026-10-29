import os

# Must be set before src modules read Settings; switches the DB to POSTGRES_TEST_DB.
os.environ["APP_ENV"] = "test"

from tests.database import provision_test_database, use_test_roles  # noqa: E402

# The suite runs as a least-privilege runtime role (D040).
use_test_roles()
provision_test_database()

from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.auth.settings import load_auth_settings  # noqa: E402
from src.db.session import SessionLocal, engine, get_session  # noqa: E402
from src.main import app  # noqa: E402
from src.models import RateLimitCounter  # noqa: E402
from src.services import limits  # noqa: E402
from tests.tenancy import Member, api_client, configure_auth, make_member  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_limits() -> AsyncIterator[None]:
    """Every test starts with empty rate windows: the in-memory ones of this process, and
    counters committed by tests that use the app's own sessions (all test requests come
    from one address). Only the test database is touched."""
    limits.reset_process_state()
    yield
    limits.reset_process_state()
    limits.limit_settings.cache_clear()
    async with SessionLocal() as s, s.begin():
        await s.execute(delete(RateLimitCounter))


@pytest.fixture
async def tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Member]:
    """A signed-in owner of their own organization; `client` browses as them."""
    configure_auth(monkeypatch)
    yield await make_member(session, name="tenant")
    load_auth_settings.cache_clear()


@pytest.fixture
async def client(session: AsyncSession, tenant: Member) -> AsyncIterator[AsyncClient]:
    # Route requests through the rolled-back test session so API tests leave no rows behind.
    app.dependency_overrides[get_session] = lambda: session
    async with api_client(tenant) as c:
        # Collection endpoints read one organization; detail routes ignore the parameter.
        c.params = {"organization_id": str(tenant.organization_id)}
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
