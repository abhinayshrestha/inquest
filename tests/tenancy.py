"""Test helper: signed-in members of organizations, without going through a provider.

Users, organizations, memberships and sessions are written straight into the test session
(rolled back afterwards); the API then resolves the session cookie like any browser's.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import oidc
from src.auth.sessions import create_session
from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal
from src.main import app
from src.models import AuthSession, Organization, OrganizationMembership, OrganizationRole, User
from tests.auth_fixtures import CLIENT_ID, CLIENT_SECRET, ISSUER, PUBLIC_URL

COOKIE = "__Host-inquest_session"


@dataclass
class Member:
    user_id: uuid.UUID
    organization_id: uuid.UUID
    token: str
    csrf: str
    session_id: uuid.UUID

    @property
    def owns(self) -> dict[str, uuid.UUID]:
        """Fields for an Environment this member created in their organization."""
        return {"organization_id": self.organization_id, "owner_user_id": self.user_id}


def configure_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "ISSUER": ISSUER, "CLIENT_ID": CLIENT_ID, "CLIENT_SECRET": CLIENT_SECRET,
        "PUBLIC_URL": PUBLIC_URL,
    }.items():  # fmt: skip
        monkeypatch.setenv(f"INQUEST_AUTH_{key}", value)
    load_auth_settings.cache_clear()
    oidc.clear_caches()


async def make_member(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None = None,
    role: OrganizationRole = OrganizationRole.OWNER,
    name: str = "member",
) -> Member:
    """A signed-in user in `organization_id`, or in a new organization they own."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        issuer=ISSUER,
        subject=f"{name}-{suffix}",
        email=f"{name}-{suffix}@example.com",
        email_verified=True,
        display_name=name,
    )
    session.add(user)
    await session.flush()
    if organization_id is None:
        organization = Organization(
            name=f"Org {name}", slug=f"org-{name}-{suffix}", created_by_user_id=user.id
        )
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    session.add(OrganizationMembership(organization_id=organization_id, user_id=user.id, role=role))
    token, row = await create_session(session, user, load_auth_settings())
    return Member(user.id, organization_id, token, row.csrf_token, row.id)


def api_client(member: Member | None) -> AsyncClient:
    """A browser signed in as `member` (or nobody). The session cookie is sent as a
    header: the test transport is plain http, which a cookie jar would not send it over."""
    headers = {"cookie": f"{COOKIE}={member.token}", "X-CSRF-Token": member.csrf} if member else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)


@asynccontextmanager
async def committed_members(count: int = 1) -> AsyncIterator[list[Member]]:
    """Members of one organization, committed for tests that use the app's own sessions
    (runner, SSE). Records owned by the organization must be deleted before exit."""
    async with SessionLocal() as s:
        first = await make_member(s, name="owner")
        members = [first] + [
            await make_member(s, organization_id=first.organization_id, name=f"m{i}")
            for i in range(1, count)
        ]
    try:
        yield members
    finally:
        users = [m.user_id for m in members]
        async with SessionLocal() as s, s.begin():
            await s.execute(delete(AuthSession).where(AuthSession.user_id.in_(users)))
            await s.execute(delete(Organization).where(Organization.id == first.organization_id))
            await s.execute(delete(User).where(User.id.in_(users)))
