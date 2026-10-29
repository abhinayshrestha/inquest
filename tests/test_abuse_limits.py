"""Abuse prevention and resource quotas (Phase 7D-2C-1, D039; audit H3): per-address
first line, shared rate limits, quotas under concurrency, request and metadata bounds,
credential-validation abuse, event-stream bounds, retention, and refusals that disclose
nothing. Several organizations and users plus unauthenticated clients; providers are mock
transports. Concurrency tests commit rows (the app's own sessions) and delete them."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.api.routes import credentials as credential_routes
from src.api.routes.credentials import provider_client
from src.api.security import client_address, json_too_deep
from src.auth import oidc
from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal, engine, get_session
from src.main import app
from src.models import (
    AuthLoginState,
    AuthSession,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentType,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationTurn,
    Organization,
    OrganizationInvitation,
    OrganizationRole,
    ProgressEvent,
    RateLimitCounter,
    User,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations, limits, turns
from tests.auth_fixtures import FakeProvider
from tests.credential_fixtures import (
    NEBIUS_SECRET,
    NEBIUS_URL,
    SIGNOZ_ORIGIN,
    SIGNOZ_SECRET,
    credential_storage,  # noqa: F401  (fixture)
)
from tests.tenancy import COOKIE, Member, api_client, configure_auth, make_member

NEBIUS_BODY = {
    "api_key": NEBIUS_SECRET,
    "base_url": NEBIUS_URL,
    "main_model": "nvidia/main",
    "local_model": "nvidia/local",
}
SIGNOZ_BODY = {"base_url": SIGNOZ_ORIGIN, "api_key": SIGNOZ_SECRET}
MODELS = {"data": [{"id": "nvidia/main"}, {"id": "nvidia/local"}]}
QUERY_OK = {"status": "success", "data": {"type": "scalar", "data": {"results": []}}}
SCOPE = {"signoz": {"hosts": ["web-1"], "services": ["checkout"]}}


def configure(monkeypatch: pytest.MonkeyPatch, rates=None, quotas=None, **settings: Any) -> None:
    monkeypatch.setenv("INQUEST_RATE_LIMITS", json.dumps(rates or {}))
    monkeypatch.setenv("INQUEST_QUOTAS", json.dumps(quotas or {}))
    for name, value in settings.items():
        monkeypatch.setenv(f"INQUEST_{name.upper()}", str(value))
    limits.limit_settings.cache_clear()


@pytest.fixture
def statements() -> Iterator[list[str]]:
    """Every SQL statement the application engine runs meanwhile."""
    seen: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


def assert_rate_limited(response: httpx.Response, *, within: int = 3600) -> None:
    assert response.status_code == 429, response.text
    assert response.json() == {
        "detail": {"code": "rate_limited", "message": "too many requests; try again later"}
    }
    assert 1 <= int(response.headers["retry-after"]) <= within


def assert_quota(response: httpx.Response, name: str, *, status: int = 409) -> dict:
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "quota_exceeded" and detail["quota"] == name
    assert set(detail) == {"code", "quota", "message"}
    assert ("retry-after" in response.headers) == (status == 429)
    return detail


def assert_discloses_nothing(response: httpx.Response, *private: object) -> None:
    for value in private:
        assert str(value) not in response.text


# --- settings --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variable, value",
    [
        ("INQUEST_RATE_LIMITS", '{"login.everyone": "1/60"}'),
        ("INQUEST_RATE_LIMITS", '{"login.ip": "ten per minute"}'),
        ("INQUEST_RATE_LIMITS", '{"login.ip": "0/60"}'),
        ("INQUEST_RATE_LIMITS", '{"login.ip": "1/999999"}'),
        ("INQUEST_QUOTAS", '{"environments": 5}'),
        ("INQUEST_QUOTAS", '{"environments_per_organization": 0}'),
        ("INQUEST_MAX_REQUEST_BYTES", "10"),
    ],
)
def test_invalid_limits_refuse_to_start(monkeypatch: pytest.MonkeyPatch, variable, value):
    monkeypatch.setenv(variable, value)
    limits.limit_settings.cache_clear()
    with pytest.raises(ValueError):
        limits.limit_settings()


def test_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch):
    configure(monkeypatch, {"login.ip": "3/30"}, {"environments_per_organization": 7})
    assert limits.rate("login.ip") == (3, 30)
    assert limits.rate("callback.ip") == (20, 60)
    assert limits.quota("environments_per_organization") == 7
    assert limits.quota("organizations_per_user") == 5


# --- the first line: requests per address, in memory ------------------------------------------


async def test_an_address_flood_is_refused_before_any_database_work(
    monkeypatch: pytest.MonkeyPatch, statements: list[str]
):
    configure(monkeypatch, {"requests.ip": "5/60"})
    async with api_client(None) as anonymous:
        for _ in range(5):
            assert (await anonymous.get("/health")).status_code == 200
        served = len(statements)
        refused = await asyncio.gather(*(anonymous.get("/health") for _ in range(50)))
        writes = await anonymous.post("/organizations", json={"name": "x"})
    for response in [*refused, writes]:
        assert_rate_limited(response, within=60)
    # Refused traffic cost no session, connection or query.
    assert len(statements) == served
    assert engine.pool.checkedout() == 0


async def test_no_public_path_is_outside_the_address_limit(monkeypatch: pytest.MonkeyPatch):
    """MCP used to be exempt on this listener; it has its own now (D041), so every path
    here, /mcp included, counts against the caller's address."""
    configure(monkeypatch, {"requests.ip": "1/60"})
    async with api_client(None) as anonymous:
        await anonymous.get("/health")
        mcp = await anonymous.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert_rate_limited(mcp, within=60)


async def test_the_address_window_expires(monkeypatch: pytest.MonkeyPatch):
    configure(monkeypatch, {"requests.ip": "2/60"})
    clock = [1_800_000_010.0]
    monkeypatch.setattr(limits, "time", SimpleNamespace(time=lambda: clock[0]))
    async with api_client(None) as anonymous:
        assert [(await anonymous.get("/health")).status_code for _ in range(3)] == [200, 200, 429]
        refused = await anonymous.get("/health")
        assert refused.headers["retry-after"] == "50"
        clock[0] += 50
        assert (await anonymous.get("/health")).status_code == 200


async def test_forwarded_headers_never_choose_the_counted_address(monkeypatch: pytest.MonkeyPatch):
    configure(monkeypatch, {"requests.ip": "3/60"})
    spoofed = [
        {"X-Forwarded-For": "198.51.100.1"},
        {"X-Forwarded-For": "198.51.100.2, 10.0.0.1"},
        {"Forwarded": "for=198.51.100.3"},
        {"X-Real-IP": "198.51.100.4"},
    ]
    async with api_client(None) as anonymous:
        codes = [(await anonymous.get("/health", headers=h)).status_code for h in spoofed]
    assert codes == [200, 200, 200, 429]

    # Behind uvicorn's proxy-header handling (the documented deployment), only a trusted
    # proxy peer may name the client; anyone else's header is ignored.
    limits.reset_process_state()
    proxied = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")

    def peer(address: str) -> AsyncClient:
        transport = ASGITransport(app=proxied, client=(address, 4000))
        return AsyncClient(transport=transport, base_url="http://test")

    async with peer("203.0.113.7") as outsider, peer("127.0.0.1") as proxy:
        outside = [
            (await outsider.get("/health", headers={"X-Forwarded-For": f"192.0.2.{i}"})).status_code
            for i in range(4)
        ]
        # Through the trusted proxy, each client has its own budget.
        clients = [
            (await proxy.get("/health", headers={"X-Forwarded-For": f"192.0.2.{i}"})).status_code
            for i in range(4)
        ]
    assert outside == [200, 200, 200, 429]
    assert clients == [200, 200, 200, 200]


def test_addresses_are_counted_per_ipv4_host_and_ipv6_64():
    def address(host: str) -> str:
        return client_address({"client": (host, 1)})

    assert address("203.0.113.7") == "203.0.113.7"
    assert address("2001:db8:1:2:aaaa::1") == address("2001:db8:1:2:ffff::9")
    assert address("2001:db8:1:2::1") != address("2001:db8:1:3::1")
    assert address("::ffff:203.0.113.7") == "203.0.113.7"
    assert client_address({}) == "unknown"


# --- request bodies and bounded fields ---------------------------------------------------------


async def test_oversized_bodies_are_refused_before_parsing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, statements: list[str]
):
    configure(monkeypatch, max_request_bytes=8192)
    big = {"name": "x" * 9000, "type": "local", "organization_id": str(uuid.uuid4())}
    before = len(statements)
    declared = await client.post("/environments", json=big)

    async def chunks():
        for _ in range(10):
            yield b" " * 1000

    streamed = await client.post(
        "/environments", content=chunks(), headers={"Content-Type": "application/json"}
    )
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json() == {
            "detail": {
                "code": "request_too_large",
                "message": "the request body exceeds this server's limit",
            }
        }
        assert "xxxx" not in response.text
    assert "chunked" in streamed.request.headers.get("transfer-encoding", "")
    # Not even the session cookie was looked up.
    assert len(statements) == before


async def test_deep_or_malformed_json_is_a_validation_error_not_a_crash(client: AsyncClient):
    deep = b'{"name": "a", "metadata_json": ' + b"[" * 5000 + b"]" * 5000 + b"}"
    as_json = {"Content-Type": "application/json"}
    responses = [
        await client.post("/environments", content=deep, headers=as_json),
        await client.post("/environments", content=b'{"name": ', headers=as_json),
        await client.post("/environments", content=b"[" * 5000),
    ]
    assert [r.status_code for r in responses] == [422, 422, 422]
    assert responses[0].json()["detail"][0]["type"] == "json_too_deep"
    assert responses[1].json()["detail"][0]["type"] == "json_invalid"
    # An integer too long for Python's conversion limit: refused, not a 500.
    huge = b'{"name": "a", "type": "local", "metadata_json": {"n": ' + b"9" * 5000 + b"}}"
    response = await client.post(
        "/environments", content=huge, headers={"Content-Type": "application/json"}
    )
    assert response.status_code in (400, 422)


def test_brackets_inside_strings_do_not_count_as_nesting():
    assert not json_too_deep(json.dumps({"q": "[" * 100 + '\\"{' * 100}).encode(), 4)
    assert json_too_deep(b'{"a": [[[[1]]]]}', 4)
    assert not json_too_deep(b'{"a": [[[1]]]}', 4)


async def test_environment_metadata_and_questions_are_bounded(client: AsyncClient, tenant: Member):
    def environment(metadata: Any) -> dict:
        return _environment(tenant.organization_id, f"e-{uuid.uuid4().hex[:6]}") | {
            "metadata_json": metadata
        }

    nested: Any = "x"
    for _ in range(9):
        nested = {"n": nested}
    for metadata in ({"blob": "x" * 17_000}, nested):
        response = await client.post("/environments", json=environment(metadata))
        assert response.status_code == 422, response.text
        assert "xxxx" not in response.text

    # Valid configurations keep working, the SigNoz scope included.
    created = await client.post("/environments", json=environment(SCOPE))
    assert created.status_code == 201, created.text
    too_long = {"environment_id": created.json()["id"], "title": "t", "question": "q" * 10_001}
    assert (await client.post("/investigations", json=too_long)).status_code == 422
    fine = too_long | {"question": "q" * 10_000}
    assert (await client.post("/investigations", json=fine)).status_code == 201


# --- unauthenticated sign-in ------------------------------------------------------------------


@pytest.fixture
def identity_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeProvider]:
    fake = FakeProvider()
    configure_auth(monkeypatch)

    async def http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            yield c

    app.dependency_overrides[oidc.http_client] = http
    yield fake
    app.dependency_overrides.pop(oidc.http_client, None)
    load_auth_settings.cache_clear()
    oidc.clear_caches()


@asynccontextmanager
async def browser(session: AsyncSession | None = None) -> AsyncIterator[AsyncClient]:
    """A browser over https (Secure cookies kept). With `session`, through the rolled-back
    test session; without, the app's own (committed) sessions."""
    if session is not None:
        app.dependency_overrides[get_session] = lambda: session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _login_rows(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(AuthLoginState)) or 0


async def test_login_floods_are_limited_per_address(
    session: AsyncSession, identity_provider, monkeypatch: pytest.MonkeyPatch
):
    configure(monkeypatch, {"login.ip": "3/60", "callback.ip": "2/60"})
    async with browser(session) as b:
        started = [await b.get("/auth/login") for _ in range(3)]
        assert [r.status_code for r in started] == [303, 303, 303]
        rows = await _login_rows(session)
        assert_rate_limited(await b.get("/auth/login"), within=60)
        assert await _login_rows(session) == rows  # a refused start stores nothing

        # Callbacks with forged or random states, likewise per address.
        for _ in range(2):
            response = await b.get("/auth/callback", params={"code": "c", "state": "s" * 40})
            assert response.status_code == 303 and "invalid_state" in response.headers["location"]
        assert_rate_limited(
            await b.get("/auth/callback", params={"code": "c", "state": "s"}), within=60
        )


async def test_shared_rate_windows_expire(session: AsyncSession, identity_provider, monkeypatch):
    configure(monkeypatch, {"login.ip": "2/1"})
    async with browser(session) as b:
        # Start right after a window boundary so the three requests share one window.
        await asyncio.sleep(1 - time.time() % 1 + 0.02)
        codes = [(await b.get("/auth/login")).status_code for _ in range(3)]
        assert codes == [303, 303, 429]
        await asyncio.sleep(1.05)
        assert (await b.get("/auth/login")).status_code == 303


async def test_pending_logins_are_bounded_under_concurrent_floods(identity_provider, monkeypatch):
    configure(monkeypatch, {"login.ip": "1000/60"}, {"pending_logins": 4})
    async with SessionLocal() as s:
        started_at = await s.scalar(select(func.now()))
        assert await _login_rows(s) == 0
    try:
        async with browser() as b:
            responses = await asyncio.gather(*(b.get("/auth/login") for _ in range(15)))
        codes = sorted(r.status_code for r in responses)
        assert codes == [303] * 4 + [429] * 11
        for response in responses:
            if response.status_code == 429:
                detail = assert_quota(response, "pending_logins", status=429)
                assert detail["message"].startswith("too many sign-ins are in progress")
                assert int(response.headers["retry-after"]) <= 601
        async with SessionLocal() as s:
            assert await _login_rows(s) == 4
    finally:
        async with SessionLocal() as s, s.begin():
            await s.execute(delete(AuthLoginState).where(AuthLoginState.created_at >= started_at))


async def test_a_users_sessions_are_capped(session: AsyncSession, identity_provider, monkeypatch):
    configure(monkeypatch, quotas={"sessions_per_user": 2})
    tokens = []
    async with browser(session) as b:
        for _ in range(4):
            start = await b.get("/auth/login")
            code = identity_provider.authorize(start.headers["location"])
            state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
            done = await b.get("/auth/callback", params={"code": code, "state": state})
            assert done.status_code == 303
            tokens.append(done.cookies[COOKIE])
            b.cookies.clear()
        live = []
        for token in tokens:
            me = await b.get("/auth/session", headers={"cookie": f"{COOKIE}={token}"})
            live.append(me.json()["authenticated"])
    assert live == [False, False, True, True]


# --- organizations and invitations -----------------------------------------------------------


@asynccontextmanager
async def committed_organizations(count: int) -> AsyncIterator[list[Member]]:
    """`count` owners of their own organizations, committed, with everything created in
    those organizations or by those users deleted afterwards."""
    async with SessionLocal() as s:
        owners = [await make_member(s, name=f"owner{i}") for i in range(count)]
        await s.commit()
    users = [o.user_id for o in owners]
    try:
        yield owners
    finally:
        async with SessionLocal() as s, s.begin():
            organizations = select(Organization.id).where(
                Organization.created_by_user_id.in_(users)
            )
            investigation_ids = select(Investigation.id).where(
                Investigation.organization_id.in_(organizations)
            )
            for model, column in (
                (ProgressEvent, ProgressEvent.investigation_id),
                (InvestigationAssignment, InvestigationAssignment.investigation_id),
                (InvestigationTurn, InvestigationTurn.investigation_id),
                (InvestigationEnvironment, InvestigationEnvironment.investigation_id),
                (Investigation, Investigation.id),
            ):
                await s.execute(delete(model).where(column.in_(investigation_ids)))
            await s.execute(
                delete(Environment).where(Environment.organization_id.in_(organizations))
            )
            await s.execute(
                delete(OrganizationInvitation).where(
                    OrganizationInvitation.organization_id.in_(organizations)
                )
            )
            await s.execute(delete(Organization).where(Organization.id.in_(organizations)))
            await s.execute(delete(AuthSession).where(AuthSession.user_id.in_(users)))
            await s.execute(delete(User).where(User.id.in_(users)))  # fmt: skip


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    configure_auth(monkeypatch)
    yield
    load_auth_settings.cache_clear()


async def test_concurrent_organization_creation_stops_at_the_quota(auth, monkeypatch):
    configure(monkeypatch, quotas={"organizations_per_user": 3})
    async with committed_organizations(2) as (ada, bob):
        async with api_client(ada) as client:
            responses = await asyncio.gather(
                *(client.post("/organizations", json={"name": f"Org {i}"}) for i in range(10))
            )
        assert sorted(r.status_code for r in responses) == [201] * 2 + [409] * 8
        detail = assert_quota(
            next(r for r in responses if r.status_code == 409), "organizations_per_user"
        )
        assert detail["message"] == "you have reached your limit of 3 organizations created"
        async with SessionLocal() as s:
            created = await s.scalar(
                select(func.count()).where(Organization.created_by_user_id == ada.user_id)
            )
        assert created == 3  # the one they started with, plus two
        # Another user's quota is their own.
        async with api_client(bob) as client:
            assert (await client.post("/organizations", json={"name": "Bob 2"})).status_code == 201


async def test_organization_creation_is_rate_limited(client: AsyncClient, monkeypatch):
    configure(monkeypatch, {"organization_create.user": "2/3600"}, {"organizations_per_user": 50})
    for i in range(2):
        assert (await client.post("/organizations", json={"name": f"O{i}"})).status_code == 201
    assert_rate_limited(await client.post("/organizations", json={"name": "O3"}))


async def test_invitation_floods_are_limited_per_inviter_and_organization(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    configure(
        monkeypatch,
        {"invitation_create.user": "3/3600", "invitation_create.organization": "5/3600"},
    )
    org = tenant.organization_id
    admin = await make_member(session, organization_id=org, role=OrganizationRole.ADMIN, name="adm")
    member = await make_member(session, organization_id=org, role=OrganizationRole.MEMBER, name="m")
    outsider = await make_member(session, name="outsider")

    def invite(c: AsyncClient, n: str, organization: uuid.UUID = org):
        return c.post(
            f"/organizations/{organization}/invitations", json={"email": f"{n}@example.com"}
        )

    codes = [(await invite(client, f"o{i}")).status_code for i in range(4)]
    assert codes == [201, 201, 201, 429]
    async with api_client(member) as m, api_client(outsider) as o, api_client(admin) as a:
        # Refused for their role or membership: counted for nobody's organization.
        for _ in range(5):
            assert (await invite(m, "m")).status_code == 403
            assert (await invite(o, "o")).status_code == 404
        # The organization's budget (5) is shared by its inviters: 3 used, 2 left.
        assert [(await invite(a, f"a{i}")).status_code for i in range(3)] == [201, 201, 429]
        assert_rate_limited(await invite(a, "a9"))
        # Another organization's budget is untouched.
        assert (await invite(o, "x", outsider.organization_id)).status_code == 201


async def test_pending_invitations_and_members_are_bounded(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    configure(
        monkeypatch,
        quotas={"pending_invitations_per_organization": 2, "members_per_organization": 4},
    )
    url = f"/organizations/{tenant.organization_id}/invitations"
    assert (await client.post(url, json={"email": "a@example.com"})).status_code == 201
    assert (await client.post(url, json={"email": "b@example.com"})).status_code == 201
    # Re-inviting an address replaces its pending invitation, so it does not count twice.
    assert (await client.post(url, json={"email": "a@example.com"})).status_code == 201
    refused = await client.post(url, json={"email": "c@example.com"})
    detail = assert_quota(refused, "pending_invitations_per_organization")
    assert detail["message"].endswith("revoke some first")

    # Members plus pending invitations: 2 members + 2 pending reach 4.
    configure(
        monkeypatch,
        quotas={"pending_invitations_per_organization": 10, "members_per_organization": 4},
    )
    await make_member(session, organization_id=tenant.organization_id, name="m2")
    assert_quota(
        await client.post(url, json={"email": "d@example.com"}), "members_per_organization"
    )


async def test_acceptance_respects_member_and_membership_quotas(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    url = f"/organizations/{tenant.organization_id}/invitations"
    invitee = await make_member(session, name="invitee")
    email = (await session.get(User, invitee.user_id)).email
    created = await client.post(url, json={"email": email})
    token = created.json()["accept_url"].split("#token=")[1]
    async with api_client(invitee) as c:
        configure(monkeypatch, quotas={"members_per_organization": 1})
        assert_quota(
            await c.post("/invitations/accept", json={"token": token}), "members_per_organization"
        )
        configure(monkeypatch, quotas={"memberships_per_user": 1})
        assert_quota(
            await c.post("/invitations/accept", json={"token": token}), "memberships_per_user"
        )
        configure(monkeypatch)
        # Nothing was used up by the refusals: the invitation still works.
        assert (await c.post("/invitations/accept", json={"token": token})).status_code == 200


async def test_acceptance_attempts_are_limited_whatever_the_token(
    session: AsyncSession, client: AsyncClient, monkeypatch
):
    configure(monkeypatch, {"invitation_accept.user": "2/3600"})
    guesser = await make_member(session, name="guesser")
    async with api_client(guesser) as c:
        for _ in range(2):
            assert (
                await c.post("/invitations/accept", json={"token": "t" * 40})
            ).status_code == 410
        # The same refusal whether or not the token exists.
        bogus = await c.post("/invitations/accept", json={"token": "u" * 40})
    assert_rate_limited(bogus)


async def test_concurrent_invitations_stop_at_the_pending_quota(auth, monkeypatch):
    configure(monkeypatch, quotas={"pending_invitations_per_organization": 3})
    async with committed_organizations(1) as (ada,):
        url = f"/organizations/{ada.organization_id}/invitations"
        async with api_client(ada) as client:
            responses = await asyncio.gather(
                *(client.post(url, json={"email": f"p{i}@example.com"}) for i in range(10))
            )
        assert sorted(r.status_code for r in responses) == [201] * 3 + [409] * 7
        async with SessionLocal() as s:
            pending = await s.scalar(
                select(func.count()).where(
                    OrganizationInvitation.organization_id == ada.organization_id,
                    OrganizationInvitation.revoked_at.is_(None),
                )
            )
        assert pending == 3


# --- environments -----------------------------------------------------------------------------


def _environment(organization_id: uuid.UUID, name: str) -> dict:
    return {"name": name, "type": "local", "organization_id": str(organization_id)}


async def test_concurrent_environment_creation_stops_at_the_quota_per_organization(
    auth, monkeypatch
):
    configure(monkeypatch, quotas={"environments_per_organization": 4})
    async with committed_organizations(2) as (ada, bob):
        async with api_client(ada) as a, api_client(bob) as b:
            responses = await asyncio.gather(
                *(
                    a.post("/environments", json=_environment(ada.organization_id, f"e{i}"))
                    for i in range(12)
                ),
                b.post("/environments", json=_environment(bob.organization_id, "bob-1")),
            )
        mine, theirs = responses[:12], responses[12]
        assert sorted(r.status_code for r in mine) == [201] * 4 + [409] * 8
        assert theirs.status_code == 201
        refused = next(r for r in mine if r.status_code == 409)
        detail = assert_quota(refused, "environments_per_organization")
        assert detail["message"] == "this organization has reached its limit of 4 environments"
        assert_discloses_nothing(refused, bob.organization_id, "bob-1")
        async with SessionLocal() as s:
            count = await s.scalar(
                select(func.count()).where(Environment.organization_id == ada.organization_id)
            )
        assert count == 4


async def test_environment_creation_is_rate_limited_per_user_and_organization(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    configure(
        monkeypatch,
        {"environment_create.user": "2/3600", "environment_create.organization": "3/3600"},
    )
    org = tenant.organization_id
    peer = await make_member(session, organization_id=org, name="peer")
    other = await make_member(session, name="other")
    codes = [
        (await client.post("/environments", json=_environment(org, f"t{i}"))).status_code
        for i in range(3)
    ]
    assert codes == [201, 201, 429]
    async with api_client(peer) as p, api_client(other) as o:
        # Outsiders naming this organization are refused before counting.
        for _ in range(3):
            assert (await o.post("/environments", json=_environment(org, "x"))).status_code == 404
        # So the organization's budget (3) still has one left for a colleague.
        assert (await p.post("/environments", json=_environment(org, "p1"))).status_code == 201
        assert_rate_limited(await p.post("/environments", json=_environment(org, "p2")))
        own = await o.post("/environments", json=_environment(other.organization_id, "o"))
        assert own.status_code == 201


# --- investigations and turns -------------------------------------------------------------------


async def _env(session: AsyncSession, owner: Member, name: str | None = None) -> Environment:
    environment = Environment(
        name=name or f"env-{uuid.uuid4().hex[:6]}", type=EnvironmentType.LOCAL,
        metadata_json=SCOPE, **owner.owns,
    )  # fmt: skip
    session.add(environment)
    await session.flush()
    return environment


async def test_investigations_per_day_are_bounded_per_organization(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    configure(monkeypatch, quotas={"investigations_per_organization_per_day": 2})
    mine = await _env(session, tenant)
    other = await make_member(session, name="other")
    theirs = await _env(session, other)
    body = {"environment_id": str(mine.id), "title": "t", "question": "why?"}
    assert [(await client.post("/investigations", json=body)).status_code for _ in range(2)] == [
        201,
        201,
    ]
    refused = await client.post("/investigations", json=body)
    detail = assert_quota(refused, "investigations_per_organization_per_day", status=429)
    assert (
        detail["message"] == "this organization has reached its limit of 2 investigations per day"
    )
    assert 86_000 < int(refused.headers["retry-after"]) <= 86_400
    async with api_client(other) as o:
        response = await o.post("/investigations", json=body | {"environment_id": str(theirs.id)})
    assert response.status_code == 201


async def test_turns_per_day_count_follow_ups(session: AsyncSession, tenant: Member, monkeypatch):
    configure(monkeypatch, quotas={"turns_per_organization_per_day": 2})
    environment = await _env(session, tenant)
    data = InvestigationCreate(environment_id=environment.id, title="t", question="q")
    first = await investigations.create_investigation(session, data)
    await investigations.create_investigation(session, data)
    with pytest.raises(limits.QuotaExceeded) as refused:
        await turns.create_follow_up_turn(session, first.id, "and then?")
    assert refused.value.name == "turns_per_organization_per_day"
    assert refused.value.retry_after and refused.value.retry_after <= 86_400
    await session.rollback()


async def test_starts_and_follow_ups_are_rate_limited(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    configure(monkeypatch, {"turn_submit.user": "2/3600"})
    environment = await _env(session, tenant)
    created = await client.post(
        "/investigations",
        json={"environment_id": str(environment.id), "title": "t", "question": "q"},
    )
    investigation = created.json()["id"]
    # Counted before the credential and runtime checks that refuse these two.
    first = await client.post(f"/investigations/{investigation}/start")
    second = await client.post(f"/investigations/{investigation}/turns", json={"question": "q2"})
    assert first.status_code in (409, 503) and second.status_code in (409, 503)
    assert_rate_limited(await client.post(f"/investigations/{investigation}/start"))


async def test_concurrent_investigation_creation_stops_at_the_daily_quota(auth, monkeypatch):
    configure(monkeypatch, quotas={"investigations_per_organization_per_day": 3})
    async with committed_organizations(1) as (ada,):
        async with SessionLocal() as s:
            environment = await _env(s, ada)
            await s.commit()
        body = {"environment_id": str(environment.id), "title": "t", "question": "q"}
        async with api_client(ada) as client:
            responses = await asyncio.gather(
                *(client.post("/investigations", json=body) for _ in range(10))
            )
        assert sorted(r.status_code for r in responses) == [201] * 3 + [429] * 7
        async with SessionLocal() as s:
            count = await s.scalar(
                select(func.count()).where(Investigation.organization_id == ada.organization_id)
            )
        assert count == 3


# --- credential validation ---------------------------------------------------------------------


class Provider:
    """Mock provider: counts requests; may hold them at a gate or answer slowly."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.gate: asyncio.Event | None = None
        self.arrived = asyncio.Event()
        self.delay = 0.0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.arrived.set()
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(self.delay)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json=QUERY_OK)


@pytest.fixture
def provider(credential_storage) -> Iterator[Provider]:  # noqa: F811
    mock = Provider()
    app.dependency_overrides[provider_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(mock), follow_redirects=False
    )
    yield mock
    app.dependency_overrides.pop(provider_client, None)


def _credentials(environment: Environment, kind: str) -> str:
    return f"/environments/{environment.id}/credentials/{kind}"


async def test_credential_validation_floods_stop_before_the_provider(
    session: AsyncSession, client: AsyncClient, tenant: Member, provider: Provider, monkeypatch
):
    configure(monkeypatch, {"credential_validation.user": "3/3600"})
    environment = await _env(session, tenant)
    member = await make_member(session, organization_id=tenant.organization_id, name="member")
    async with api_client(member) as m:
        # Not the owner: refused before counting or contacting anything.
        for _ in range(5):
            assert (
                await m.put(_credentials(environment, "nebius"), json=NEBIUS_BODY)
            ).status_code == 403
    assert provider.requests == []

    for _ in range(3):
        assert (
            await client.put(_credentials(environment, "nebius"), json=NEBIUS_BODY)
        ).status_code == 200
    stored = await session.get(EnvironmentCredential, (environment.id, CredentialKind.NEBIUS))
    kept = (stored.ciphertext, stored.updated_at)
    refused = await client.put(
        _credentials(environment, "nebius"), json=NEBIUS_BODY | {"api_key": NEBIUS_SECRET + "-new"}
    )
    assert_rate_limited(refused)
    assert_discloses_nothing(refused, NEBIUS_SECRET, "tokenfactory")
    assert len(provider.requests) == 3
    # The working credential is untouched.
    stored = await session.get(
        EnvironmentCredential, (environment.id, CredentialKind.NEBIUS), populate_existing=True
    )
    assert (stored.ciphertext, stored.updated_at) == kept


async def test_validation_budgets_are_per_organization_never_per_endpoint(
    session: AsyncSession, client: AsyncClient, tenant: Member, provider: Provider, monkeypatch
):
    configure(
        monkeypatch,
        {"credential_validation.organization": "2/3600", "credential_validation.server": "4/3600"},
    )
    mine = await _env(session, tenant)
    peer = await make_member(session, organization_id=tenant.organization_id, name="peer")
    peers = await _env(session, peer)
    other = await make_member(session, name="other")
    theirs = await _env(session, other)
    third = await make_member(session, name="third")
    thirds = await _env(session, third)
    for _ in range(2):
        assert (await client.put(_credentials(mine, "signoz"), json=SIGNOZ_BODY)).status_code == 200
    async with api_client(peer) as p, api_client(other) as o, api_client(third) as t:
        # The organization's budget is spent for every owner in it...
        assert_rate_limited(await p.put(_credentials(peers, "signoz"), json=SIGNOZ_BODY))
        # ...but another organization validating the same endpoint is unaffected: nothing
        # about who else uses an endpoint shows in a refusal.
        for _ in range(2):
            same = await o.put(_credentials(theirs, "signoz"), json=SIGNOZ_BODY)
            assert same.status_code in (200, 422), same.text
            assert same.status_code != 429
        # The server-wide budget (4) is spent: a third organization is refused, and the
        # refusal names no one.
        server = await t.put(_credentials(thirds, "nebius"), json=NEBIUS_BODY)
    assert_rate_limited(server)
    assert_discloses_nothing(server, tenant.organization_id, SIGNOZ_ORIGIN, "signoz.test")


async def test_validations_in_flight_are_bounded_per_process(
    session: AsyncSession, client: AsyncClient, tenant: Member, provider: Provider, monkeypatch
):
    configure(monkeypatch, quotas={"credential_validations_in_flight": 1})
    environment = await _env(session, tenant)
    provider.gate = asyncio.Event()
    held = asyncio.create_task(client.put(_credentials(environment, "nebius"), json=NEBIUS_BODY))
    await asyncio.wait_for(provider.arrived.wait(), 5)
    busy = await client.put(_credentials(environment, "signoz"), json=SIGNOZ_BODY)
    detail = assert_quota(busy, "credential_validations_in_flight", status=429)
    assert busy.headers["retry-after"] == str(limits.BUSY_RETRY_SECONDS)
    assert "credential checks are in progress" in detail["message"]
    provider.gate.set()
    assert (await held).status_code == 200
    assert (
        await client.put(_credentials(environment, "signoz"), json=SIGNOZ_BODY)
    ).status_code == 200


async def test_a_slow_provider_cannot_hold_a_validation_open(
    session: AsyncSession, client: AsyncClient, tenant: Member, provider: Provider, monkeypatch
):
    monkeypatch.setattr(credential_routes, "VALIDATION_SECONDS", 0.2)
    environment = await _env(session, tenant)
    provider.delay = 5
    response = await client.put(_credentials(environment, "nebius"), json=NEBIUS_BODY)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unreachable"
    assert await session.get(EnvironmentCredential, (environment.id, CredentialKind.NEBIUS)) is None
    assert limits._validations == 0


# --- event streams --------------------------------------------------------------------------------


async def _investigation(session: AsyncSession, owner: Member) -> uuid.UUID:
    environment = await _env(session, owner)
    data = InvestigationCreate(environment_id=environment.id, title="t", question="q")
    return (await investigations.create_investigation(session, data)).id


async def _open_stream(path: str, member: Member, opened: asyncio.Event, closed: asyncio.Event):
    """An EventSource held open at the ASGI level (httpx's test transport buffers)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": b"", "query_string": b"", "root_path": "",
        "server": ("test", 80), "client": ("127.0.0.1", 1),
        "headers": [(b"host", b"test"), (b"cookie", f"{COOKIE}={member.token}".encode())],
    }  # fmt: skip
    requested = False

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await closed.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            opened.set()

    await app(scope, receive, send)


async def test_open_event_streams_are_bounded_and_released(
    session: AsyncSession, client: AsyncClient, tenant: Member, monkeypatch
):
    configure(monkeypatch, quotas={"sse_streams_per_user": 2, "sse_streams": 3})
    investigation = await _investigation(session, tenant)
    path = f"/investigations/{investigation}/events"
    closed, opened = asyncio.Event(), [asyncio.Event(), asyncio.Event()]
    streams = []
    try:
        # One at a time: they share the rolled-back test session while authorizing.
        for o in opened:
            streams.append(asyncio.create_task(_open_stream(path, tenant, o, closed)))
            await asyncio.wait_for(o.wait(), 5)
        refused = await client.get(path, params={"follow": "false"})
        assert_quota(refused, "sse_streams_per_user", status=429)
        assert refused.headers["retry-after"] == str(limits.BUSY_RETRY_SECONDS)
        # A colleague may open one more; then the process is full for everyone.
        colleague = await make_member(session, organization_id=tenant.organization_id, name="c")
        hold = limits.take_stream_slot(colleague.user_id)
        async with api_client(colleague) as c:
            assert_quota(await c.get(path, params={"follow": "false"}), "sse_streams", status=429)
        hold()
    finally:
        closed.set()
        await asyncio.gather(*streams)
    # Disconnects released every slot; a finished replay releases its own too.
    assert limits._streams == {}
    assert (await client.get(path, params={"follow": "false"})).status_code == 200
    assert limits._streams == {}


# --- retention -------------------------------------------------------------------------------


async def test_prune_deletes_only_what_no_longer_counts(session: AsyncSession, tenant: Member):
    now = datetime.now(UTC)
    session.add_all(
        [
            RateLimitCounter(bucket="a" * 64, window_start=now - timedelta(hours=2), hits=5,
                             expires_at=now - timedelta(hours=1)),
            RateLimitCounter(bucket="b" * 64, window_start=now, hits=5,
                             expires_at=now + timedelta(hours=1)),
            AuthLoginState(state_hash="c" * 64, browser_hash="d" * 64, code_verifier="v",
                           nonce="n", return_to="/", expires_at=now - timedelta(minutes=1)),
            AuthLoginState(state_hash="e" * 64, browser_hash="f" * 64, code_verifier="v",
                           nonce="n", return_to="/", expires_at=now + timedelta(minutes=5)),
            AuthSession(user_id=tenant.user_id, token_hash="g" * 64, csrf_token="x",
                        last_seen_at=now - timedelta(days=30), created_at=now - timedelta(days=30),
                        expires_at=now - timedelta(days=8)),
            AuthSession(user_id=tenant.user_id, token_hash="h" * 64, csrf_token="x",
                        last_seen_at=now, created_at=now - timedelta(days=3),
                        expires_at=now + timedelta(days=1), revoked_at=now - timedelta(days=2)),
        ]
    )  # fmt: skip
    await session.flush()
    await limits.prune(session)
    buckets = set(await session.scalars(select(RateLimitCounter.bucket)))
    states = set(await session.scalars(select(AuthLoginState.state_hash)))
    sessions = set(
        await session.scalars(
            select(AuthSession.token_hash).where(AuthSession.user_id == tenant.user_id)
        )
    )
    assert "a" * 64 not in buckets and "b" * 64 in buckets
    assert states >= {"e" * 64} and "c" * 64 not in states
    assert "g" * 64 not in sessions and "h" * 64 in sessions
    assert len(sessions) == 2  # the tenant's own live session stays


# --- rejected traffic and the connection pool -----------------------------------------------------


async def test_rejected_floods_do_not_exhaust_database_connections(identity_provider, monkeypatch):
    configure(monkeypatch, {"login.ip": "1/60"}, {"pending_logins": 1000})
    async with SessionLocal() as s:
        started_at = await s.scalar(select(func.now()))
    try:
        async with browser() as b:
            # Each refusal is one short transaction on the app's own sessions.
            async with asyncio.timeout(30):
                responses = await asyncio.gather(*(b.get("/auth/login") for _ in range(200)))
        codes = [r.status_code for r in responses]
        assert codes.count(303) == 1 and codes.count(429) == 199
        assert engine.pool.checkedout() == 0
        # The server still answers immediately afterwards.
        async with api_client(None) as anonymous, asyncio.timeout(2):
            assert (await anonymous.get("/health")).status_code == 200
        async with SessionLocal() as s:
            counters = await s.scalar(select(func.count()).select_from(RateLimitCounter))
        assert counters == 1  # one window row for the address, however long the flood
    finally:
        async with SessionLocal() as s, s.begin():
            await s.execute(delete(AuthLoginState).where(AuthLoginState.created_at >= started_at))
