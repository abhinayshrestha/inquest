"""Organizations, roles and invitations (Phase 7A): server-side membership checks,
cross-organization denial, and single-use, expiring, email-bound invitations."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_session
from src.main import app
from src.models import OrganizationInvitation, OrganizationMembership
from tests.auth_fixtures import PUBLIC_URL, FakeProvider
from tests.test_auth import provider, sign_in  # noqa: F401  (fixture)


class Browser:
    """One signed-in person: their own cookie jar and CSRF token."""

    def __init__(self, client: AsyncClient, csrf: str, user_id: str):
        self.client, self.csrf, self.user_id = client, csrf, user_id

    def _h(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf, "Origin": PUBLIC_URL}

    async def get(self, path: str):
        return await self.client.get(path)

    async def post(self, path: str, body: dict | None = None):
        return await self.client.post(path, json=body or {}, headers=self._h())

    async def patch(self, path: str, body: dict):
        return await self.client.patch(path, json=body, headers=self._h())

    async def delete(self, path: str):
        return await self.client.delete(path, headers=self._h())


Login = Callable[..., Awaitable[Browser]]


@pytest.fixture
async def login(provider: FakeProvider, session: AsyncSession) -> AsyncIterator[Login]:  # noqa: F811
    app.dependency_overrides[get_session] = lambda: session
    clients: list[AsyncClient] = []

    async def make(sub: str, email: str, verified: bool = True) -> Browser:
        client = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
        clients.append(client)
        await sign_in(client, provider, sub=sub, email=email, email_verified=verified)
        me = (await client.get("/auth/session")).json()
        return Browser(client, me["csrf_token"], me["user"]["id"])

    yield make
    for client in clients:
        await client.aclose()
    app.dependency_overrides.pop(get_session, None)


async def _org(owner: Browser, name: str = "Acme Ops") -> str:
    response = await owner.post("/organizations", {"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _invite(inviter: Browser, org: str, email: str, role: str = "member") -> str:
    response = await inviter.post(
        f"/organizations/{org}/invitations", {"email": email, "role": role}
    )
    assert response.status_code == 201, response.text
    url = response.json()["accept_url"]
    assert urlsplit(url).fragment.startswith("token=")
    return urlsplit(url).fragment.removeprefix("token=")


async def test_creator_becomes_owner_and_sees_it_in_their_memberships(login: Login):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)

    me = (await ada.get("/me")).json()
    assert [(m["organization_id"], m["role"]) for m in me["memberships"]] == [(org, "owner")]
    detail = (await ada.get(f"/organizations/{org}")).json()
    assert (detail["name"], detail["slug"], detail["role"], detail["member_count"]) == (
        "Acme Ops", "acme-ops", "owner", 1,
    )  # fmt: skip
    # Slugs stay unique across organizations.
    second = (await ada.post("/organizations", {"name": "Acme  Ops"})).json()
    assert second["slug"] == "acme-ops-2"


async def test_outsiders_cannot_see_or_touch_an_organization(login: Login):
    ada = await login("ada", "ada@example.com")
    eve = await login("eve", "eve@example.com")
    org = await _org(ada)

    for path in (
        f"/organizations/{org}",
        f"/organizations/{org}/members",
        f"/organizations/{org}/invitations",
    ):
        assert (await eve.get(path)).status_code == 404, path
    assert (
        await eve.post(f"/organizations/{org}/invitations", {"email": "eve2@example.com"})
    ).status_code == 404
    assert (await eve.delete(f"/organizations/{org}/members/{ada.user_id}")).status_code == 404
    assert (
        await eve.patch(f"/organizations/{org}/members/{eve.user_id}", {"role": "owner"})
    ).status_code == 404
    assert (await eve.get("/me")).json()["memberships"] == []


async def test_invitation_flow_for_a_new_user(login: Login, session: AsyncSession):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)
    token = await _invite(ada, org, "Bob@Example.com")
    # Only the hash is stored.
    stored = await session.scalar(select(OrganizationInvitation.token_hash))
    assert token not in stored

    bob = await login("bob", "bob@example.com")  # signs up after being invited
    accepted = await bob.post("/invitations/accept", {"token": token})

    assert accepted.status_code == 200
    assert (accepted.json()["organization_id"], accepted.json()["role"]) == (org, "member")
    members = (await bob.get(f"/organizations/{org}/members")).json()
    assert sorted((m["email"], m["role"]) for m in members) == [
        ("ada@example.com", "owner"), ("bob@example.com", "member"),
    ]  # fmt: skip
    invitations = (await ada.get(f"/organizations/{org}/invitations")).json()
    assert [i["status"] for i in invitations] == ["accepted"]


async def test_an_invitation_works_once(login: Login):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)
    token = await _invite(ada, org, "bob@example.com")
    bob = await login("bob", "bob@example.com")
    assert (await bob.post("/invitations/accept", {"token": token})).status_code == 200

    replay = await bob.post("/invitations/accept", {"token": token})
    assert replay.status_code == 410


async def test_expired_and_revoked_invitations_are_refused(login: Login, session: AsyncSession):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)
    bob = await login("bob", "bob@example.com")
    carol = await login("carol", "carol@example.com")

    expired = await _invite(ada, org, "bob@example.com")
    await session.execute(
        update(OrganizationInvitation)
        .where(OrganizationInvitation.email == "bob@example.com")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert (await bob.post("/invitations/accept", {"token": expired})).status_code == 410

    revoked = await _invite(ada, org, "carol@example.com")
    pending = [
        i
        for i in (await ada.get(f"/organizations/{org}/invitations")).json()
        if i["status"] == "pending"
    ]
    assert (
        await ada.delete(f"/organizations/{org}/invitations/{pending[0]['id']}")
    ).status_code == 204
    assert (await carol.post("/invitations/accept", {"token": revoked})).status_code == 410
    assert (
        await ada.delete(f"/organizations/{org}/invitations/{pending[0]['id']}")
    ).status_code == 409


async def test_a_new_invitation_replaces_the_previous_link(login: Login):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)
    first = await _invite(ada, org, "bob@example.com")
    second = await _invite(ada, org, "bob@example.com")
    bob = await login("bob", "bob@example.com")

    assert (await bob.post("/invitations/accept", {"token": first})).status_code == 410
    assert (await bob.post("/invitations/accept", {"token": second})).status_code == 200


async def test_invitations_are_bound_to_a_verified_matching_email(login: Login):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)
    token = await _invite(ada, org, "bob@example.com")
    mallory = await login("mallory", "mallory@example.com")
    unverified = await login("bob-unverified", "bob@example.com", verified=False)

    assert (await mallory.post("/invitations/accept", {"token": token})).status_code == 403
    assert (await unverified.post("/invitations/accept", {"token": token})).status_code == 403
    # Neither attempt consumed it.
    bob = await login("bob", "bob@example.com")
    assert (await bob.post("/invitations/accept", {"token": token})).status_code == 200


async def test_role_rules_are_enforced_on_the_server(login: Login, session: AsyncSession):
    ada = await login("ada", "ada@example.com")  # owner
    org = await _org(ada)
    bob = await login("bob", "bob@example.com")
    await bob.post(
        "/invitations/accept", {"token": await _invite(ada, org, "bob@example.com", "admin")}
    )
    carol = await login("carol", "carol@example.com")
    await carol.post("/invitations/accept", {"token": await _invite(bob, org, "carol@example.com")})

    # Members cannot invite or list invitations; admins invite members but not admins.
    assert (
        await carol.post(f"/organizations/{org}/invitations", {"email": "x@example.com"})
    ).status_code == 403
    assert (await carol.get(f"/organizations/{org}/invitations")).status_code == 403
    assert (
        await bob.post(
            f"/organizations/{org}/invitations", {"email": "x@example.com", "role": "admin"}
        )
    ).status_code == 403
    assert (
        await ada.post(
            f"/organizations/{org}/invitations", {"email": "x@example.com", "role": "owner"}
        )
    ).status_code == 422
    # Only owners change roles; nobody can leave the organization without an owner.
    assert (
        await bob.patch(f"/organizations/{org}/members/{carol.user_id}", {"role": "admin"})
    ).status_code == 403
    assert (
        await ada.patch(f"/organizations/{org}/members/{ada.user_id}", {"role": "member"})
    ).status_code == 409
    assert (await ada.delete(f"/organizations/{org}/members/{ada.user_id}")).status_code == 409
    # Admins remove members, not owners or other admins.
    assert (await bob.delete(f"/organizations/{org}/members/{ada.user_id}")).status_code == 403
    assert (await bob.delete(f"/organizations/{org}/members/{carol.user_id}")).status_code == 204
    # With a second owner, the first may step down.
    assert (
        await ada.patch(f"/organizations/{org}/members/{bob.user_id}", {"role": "owner"})
    ).status_code == 200
    assert (
        await ada.patch(f"/organizations/{org}/members/{ada.user_id}", {"role": "member"})
    ).status_code == 200
    roles = dict(
        (
            await session.execute(
                select(OrganizationMembership.user_id, OrganizationMembership.role)
            )
        ).all()
    )
    assert sorted(r.value for r in roles.values()) == ["member", "owner"]


async def test_existing_members_cannot_be_invited_again(login: Login):
    ada = await login("ada", "ada@example.com")
    org = await _org(ada)
    response = await ada.post(f"/organizations/{org}/invitations", {"email": "ada@example.com"})
    assert response.status_code == 409


async def test_bodies_cannot_smuggle_identity_or_extra_fields(login: Login):
    ada = await login("ada", "ada@example.com")
    response = await ada.post("/organizations", {"name": "X", "created_by_user_id": "someone"})
    assert response.status_code == 422
