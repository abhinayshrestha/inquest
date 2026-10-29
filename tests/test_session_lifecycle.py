"""Session lifecycle (Phase 7D-2E-2, D043; audit M3): idle and absolute expiry on open
event streams, sign out everywhere, operator disabling and re-enabling. Uses the fake OIDC
provider. Stream and race tests run on committed rows (the stream reads with its own
sessions) and remove them afterwards; the rest run in the rolled-back test session."""

import argparse
import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import admit_user
from src.api import sse
from src.api.routes import auth as auth_routes
from src.auth import deps, oidc
from src.auth import sessions as auth_sessions
from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal, get_session
from src.main import app
from src.models import (
    AuditEvent,
    AuthSession,
    Environment,
    EnvironmentType,
    Investigation,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    ProgressEvent,
    TriggerType,
    User,
    UserAdmission,
)
from src.services import admissions
from src.services import organizations as orgs
from tests.auth_fixtures import ISSUER, PUBLIC_URL, FakeProvider
from tests.database import OwnerSession
from tests.test_admission import (
    COOKIE,
    OPERATOR,
    configure,
    invite,
    owner_with_organization,
    refused,
    sign_in,
    signed_in,
    unique,
    user_for,
)

REVOKED = 'event: access_revoked\ndata: {"reason": "access_revoked"}\n\n'


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeProvider]:
    yield configure(monkeypatch, "approval")
    app.dependency_overrides.pop(oidc.http_client, None)
    load_auth_settings.cache_clear()
    oidc.clear_caches()


@pytest.fixture
async def browser(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


async def session_row(session: AsyncSession, user_id: uuid.UUID) -> AuthSession:
    return await session.scalar(
        select(AuthSession)
        .where(AuthSession.user_id == user_id)
        .order_by(AuthSession.created_at.desc())
        .execution_options(populate_existing=True)
    )


async def csrf(client: AsyncClient) -> str:
    return (await client.get("/auth/session")).json()["csrf_token"]


async def write(client: AsyncClient, path: str, token: str | None = None) -> httpx.Response:
    headers = {"X-CSRF-Token": token if token is not None else await csrf(client)}
    return await client.post(path, headers=headers)


async def approved_sign_in(
    session: AsyncSession, client: AsyncClient, fake: FakeProvider, name: str
) -> User:
    sub, email = unique(name)
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    assert not refused(await sign_in(client, fake, sub=sub, email=email))
    return await user_for(session, sub)


def another_browser() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


# --- idle deadline -----------------------------------------------------------------------------


async def test_reads_never_move_the_idle_deadline_and_activity_does(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "reader")
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    await session.execute(
        update(AuthSession).where(AuthSession.user_id == user.id).values(last_seen_at=earlier)
    )
    await session.commit()
    for path in ("/auth/session", "/me", "/auth/session", "/me"):
        assert (await browser.get(path)).status_code == 200
    assert (await session_row(session, user.id)).last_seen_at == earlier

    assert (await write(browser, "/auth/activity")).status_code == 204
    moved = (await session_row(session, user.id)).last_seen_at
    assert moved > datetime.now(UTC) - timedelta(minutes=1)


async def test_activity_needs_csrf_and_cannot_revive_an_ended_session(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "idle")
    earlier = datetime.now(UTC) - timedelta(minutes=30)
    await session.execute(
        update(AuthSession).where(AuthSession.user_id == user.id).values(last_seen_at=earlier)
    )
    await session.commit()
    assert (await write(browser, "/auth/activity", token="")).status_code == 403
    assert (await write(browser, "/auth/activity", token="not-the-token")).status_code == 403
    assert (await session_row(session, user.id)).last_seen_at == earlier

    token = await csrf(browser)
    gone = datetime.now(UTC) - timedelta(hours=3)  # beyond the 120-minute idle timeout
    await session.execute(
        update(AuthSession).where(AuthSession.user_id == user.id).values(last_seen_at=gone)
    )
    await session.commit()
    response = await write(browser, "/auth/activity", token=token)
    assert response.status_code == 401 and response.json() == {"detail": "sign in required"}
    assert (await session_row(session, user.id)).last_seen_at == gone
    assert not await signed_in(browser)


# --- sign out everywhere -------------------------------------------------------------------


async def test_sign_out_everywhere_ends_every_session_of_the_user_only(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "everywhere")
    organization = await orgs.create_organization(session, user, "Everywhere Org")
    investigation = Investigation(
        title="kept", question="why?", trigger_type=TriggerType.MANUAL,
        organization_id=organization.id,
    )  # fmt: skip
    session.add(investigation)
    await session.commit()
    others = [another_browser() for _ in range(2)]
    for other in others:
        await sign_in(other, provider, sub=user.subject, email=user.email)
    # A teammate in the same organization keeps their session.
    teammate_browser = another_browser()
    teammate = await approved_sign_in(session, teammate_browser, provider, "teammate")
    session.add(
        OrganizationMembership(
            organization_id=organization.id, user_id=teammate.id, role=OrganizationRole.MEMBER
        )
    )
    await session.commit()
    assert all([await signed_in(b) for b in (browser, *others, teammate_browser)])

    response = await write(browser, "/auth/logout-everywhere")

    assert response.status_code == 204
    assert f"{COOKIE}=" in response.headers["set-cookie"]  # deleted in this browser
    for b in (browser, *others):
        assert not await signed_in(b)
        assert (await b.get("/me")).status_code == 401
    assert await signed_in(teammate_browser)
    live = await session.scalar(
        select(func.count()).where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
    )
    assert live == 0
    # Nothing but sessions changed.
    assert await orgs.membership_role(session, organization.id, user) == OrganizationRole.OWNER
    assert await session.get(Investigation, investigation.id) is not None
    assert await session.scalar(select(UserAdmission.revoked_at).where(
        UserAdmission.user_id == user.id)) is None  # fmt: skip
    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "session.revoke_all", AuditEvent.actor_user_id == user.id
        )
    )
    assert audit.details_json == {"sessions_ended": 3}
    # Signing in again needs only the provider.
    assert not refused(await sign_in(browser, provider, sub=user.subject, email=user.email))
    assert await signed_in(browser)
    for b in (*others, teammate_browser):
        await b.aclose()


async def test_sign_out_everywhere_needs_this_sessions_csrf_token(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "csrf")
    other = another_browser()
    someone = await approved_sign_in(session, other, provider, "someone")
    their_token = await csrf(other)
    assert (await write(browser, "/auth/logout-everywhere", token="")).status_code == 403
    assert (await write(browser, "/auth/logout-everywhere", token=their_token)).status_code == 403
    cross_site = await browser.post(
        "/auth/logout-everywhere",
        headers={"X-CSRF-Token": await csrf(browser), "Origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403
    assert await signed_in(browser) and await signed_in(other)
    actions = await session.scalars(
        select(AuditEvent.action).where(AuditEvent.actor_user_id.in_([user.id, someone.id]))
    )
    assert "session.revoke_all" not in list(actions)
    await other.aclose()


async def test_sign_out_everywhere_is_rate_limited(provider, browser, session, monkeypatch):
    monkeypatch.setenv("INQUEST_RATE_LIMITS", json.dumps({"logout_everywhere.user": "1/3600"}))
    from src.services import limits

    limits.limit_settings.cache_clear()
    try:
        user = await approved_sign_in(session, browser, provider, "limited")
        assert (await write(browser, "/auth/logout-everywhere")).status_code == 204
        await sign_in(browser, provider, sub=user.subject, email=user.email)
        assert (await write(browser, "/auth/logout-everywhere")).status_code == 429
        assert await signed_in(browser)
    finally:
        limits.limit_settings.cache_clear()


async def test_single_sign_out_still_ends_only_this_session(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "single")
    other = another_browser()
    await sign_in(other, provider, sub=user.subject, email=user.email)
    assert (await write(browser, "/auth/logout")).status_code == 204
    assert not await signed_in(browser) and await signed_in(other)
    await other.aclose()


# --- disabling ---------------------------------------------------------------------------


async def test_disabling_ends_sessions_and_keeps_everything_else(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "disabled")
    user_id = user.id
    organization = await orgs.create_organization(session, user, "Disabled Org")
    environment = Environment(
        name="env", type=EnvironmentType.LOCAL, organization_id=organization.id,
        owner_user_id=user.id,
    )  # fmt: skip
    session.add(environment)
    await session.commit()
    other = another_browser()
    await sign_in(other, provider, sub=user.subject, email=user.email)

    change = await admissions.disable_user(session, user_id, actor=OPERATOR)
    await session.commit()

    assert change.action == "disabled"
    assert (change.sessions_ended, change.memberships, change.owned_environments) == (2, 1, 1)
    assert not await signed_in(browser) and not await signed_in(other)
    await session.refresh(environment)
    assert environment.owner_user_id == user_id
    assert await session.get(Organization, organization.id) is not None
    assert await session.scalar(select(UserAdmission.revoked_at).where(
        UserAdmission.user_id == user_id)) is None  # fmt: skip
    # No sign-in works, with any policy.
    response = await sign_in(browser, provider, sub=user.subject, email=user.email)
    assert response.headers["location"] == f"{PUBLIC_URL}/?auth_error=account_disabled"
    assert not await signed_in(browser)
    audit = await session.scalar(select(AuditEvent).where(
        AuditEvent.action == "user.disable", AuditEvent.details_json["user_id"].astext == str(
            user_id)))  # fmt: skip
    assert audit.details_json == {"user_id": str(user_id), "sessions_ended": 2}
    assert audit.actor == OPERATOR
    # Disabling twice changes nothing and audits nothing more.
    again = await admissions.disable_user(session, user_id, actor=OPERATOR)
    assert again.action.startswith("unchanged")
    await other.aclose()


async def test_disabled_accounts_are_refused_under_the_open_policy(monkeypatch, browser, session):
    fake = configure(monkeypatch, "open")
    try:
        sub, email = unique("open-disabled")
        await sign_in(browser, fake, sub=sub, email=email)
        user = await user_for(session, sub)
        await admissions.disable_user(session, user.id, actor=OPERATOR)
        await session.commit()
        assert not await signed_in(browser)
        response = await sign_in(browser, fake, sub=sub, email=email)
        assert response.headers["location"] == f"{PUBLIC_URL}/?auth_error=account_disabled"
        assert not await signed_in(browser)
    finally:
        app.dependency_overrides.pop(oidc.http_client, None)
        load_auth_settings.cache_clear()


async def test_a_disabled_user_cannot_return_through_an_invitation(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "uninvitable")
    sub, email = user.subject, user.email
    await admissions.disable_user(session, user.id, actor=OPERATOR)
    await session.commit()
    owner, organization = await owner_with_organization(session)
    invitation, token = await invite(session, owner, organization, email)
    invitation_id = invitation.id

    response = await sign_in(browser, provider, sub=sub, email=email, invitation=token)

    assert response.headers["location"] == f"{PUBLIC_URL}/?auth_error=account_disabled"
    assert not await signed_in(browser)
    still = await session.scalar(
        select(OrganizationInvitation.accepted_at).where(OrganizationInvitation.id == invitation_id)
    )
    assert still is None
    members = await session.scalar(select(func.count()).where(
        OrganizationMembership.organization_id == organization.id))  # fmt: skip
    assert members == 1


async def test_re_enabling_brings_no_session_back(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "paused")
    user_id = user.id
    other = another_browser()
    await sign_in(other, provider, sub=user.subject, email=user.email)
    await admissions.disable_user(session, user_id, actor=OPERATOR)
    await session.commit()
    # A session row that slipped in while disabled (inserted directly) is ended too.
    session.add(
        AuthSession(
            user_id=user_id, token_hash=auth_sessions.digest("slipped"), csrf_token="x",
            last_seen_at=datetime.now(UTC), created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )  # fmt: skip
    await session.commit()

    change = await admissions.enable_user(session, user_id, actor=OPERATOR)
    await session.commit()

    assert change.sessions_ended == 1
    assert not await signed_in(browser) and not await signed_in(other)
    live = await session.scalar(
        select(func.count()).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    )
    assert live == 0
    assert not refused(await sign_in(browser, provider, sub=user.subject, email=user.email))
    assert await signed_in(browser)
    actions = list(
        await session.scalars(
            select(AuditEvent.action)
            .where(AuditEvent.details_json["user_id"].astext == str(user_id))
            .order_by(AuditEvent.created_at)
        )
    )
    assert actions[-2:] == ["user.disable", "user.enable"]
    await other.aclose()


async def test_enabling_does_not_restore_a_withdrawn_admission(provider, browser, session):
    user = await approved_sign_in(session, browser, provider, "both")
    user_id, sub, email = user.id, user.subject, user.email
    await admissions.revoke_user(session, user_id, actor=OPERATOR)
    await admissions.disable_user(session, user_id, actor=OPERATOR)
    await session.commit()
    await admissions.enable_user(session, user_id, actor=OPERATOR)
    await session.commit()
    assert refused(await sign_in(browser, provider, sub=sub, email=email))
    await admissions.approve_user(session, user_id, actor=OPERATOR)
    await session.commit()
    assert not refused(await sign_in(browser, provider, sub=sub, email=email))


# --- event streams (committed rows) ------------------------------------------------------------


@dataclass
class Browser:
    client: AsyncClient
    user_id: uuid.UUID
    sub: str
    email: str


@dataclass
class StreamWorld:
    provider: FakeProvider
    organization_id: uuid.UUID
    investigation_id: uuid.UUID
    owner: Browser
    teammate: Browser
    subjects: list[str]
    emails: list[str]

    @property
    def url(self) -> str:
        return f"/investigations/{self.investigation_id}/events"

    async def browser_for(self, name: str) -> Browser:
        sub, email = unique(name)
        self.subjects.append(sub), self.emails.append(email)
        async with SessionLocal() as s:
            await admissions.approve_email(s, email, actor=OPERATOR)
            await s.commit()
        client = another_browser()
        assert not refused(await sign_in(client, self.provider, sub=sub, email=email))
        async with SessionLocal() as s:
            user_id = await s.scalar(select(User.id).where(User.subject == sub))
        return Browser(client, user_id, sub, email)

    async def again(self, who: Browser) -> Browser:
        """Another browser signed in as the same user."""
        client = another_browser()
        await sign_in(client, self.provider, sub=who.sub, email=who.email)
        return Browser(client, who.user_id, who.sub, who.email)

    def stream(self, who: Browser) -> asyncio.Task[httpx.Response]:
        return asyncio.create_task(who.client.get(self.url, timeout=20))

    async def add_events(self, count: int = 1) -> int:
        async with SessionLocal() as s:
            for _ in range(count):
                s.add(ProgressEvent(investigation_id=self.investigation_id, type="turn.queued"))
            await s.commit()
            return await s.scalar(
                select(func.max(ProgressEvent.id)).where(
                    ProgressEvent.investigation_id == self.investigation_id
                )
            )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[StreamWorld]:
    fake = configure(monkeypatch, "approval")
    monkeypatch.setattr(sse, "RECHECK_SECONDS", 0.2)
    created = StreamWorld(fake, uuid.uuid4(), uuid.uuid4(), None, None, [], [])  # type: ignore[arg-type]
    try:
        created.owner = await created.browser_for("streamer")
        created.teammate = await created.browser_for("watcher")
        async with SessionLocal() as s:
            owner = await s.get(User, created.owner.user_id)
            organization = await orgs.create_organization(s, owner, "Stream Org")
            s.add(OrganizationMembership(
                organization_id=organization.id, user_id=created.teammate.user_id,
                role=OrganizationRole.MEMBER))  # fmt: skip
            investigation = Investigation(
                title="streamed", question="why?", trigger_type=TriggerType.MANUAL,
                organization_id=organization.id,
            )  # fmt: skip
            s.add(investigation)
            await s.commit()
            created.organization_id, created.investigation_id = organization.id, investigation.id
        yield created
    finally:
        app.dependency_overrides.pop(oidc.http_client, None)
        load_auth_settings.cache_clear()
        oidc.clear_caches()
        async with OwnerSession() as s, s.begin():
            users = select(User.id).where(User.issuer == ISSUER, User.subject.in_(created.subjects))
            await s.execute(delete(ProgressEvent).where(
                ProgressEvent.investigation_id == created.investigation_id))  # fmt: skip
            await s.execute(delete(Investigation).where(
                Investigation.id == created.investigation_id))  # fmt: skip
            await s.execute(delete(AuditEvent).where(
                AuditEvent.actor_user_id.in_(users)
                | AuditEvent.actor.like("operator:test%")
                | (AuditEvent.organization_id == created.organization_id)))  # fmt: skip
            await s.execute(delete(OrganizationMembership).where(
                OrganizationMembership.organization_id == created.organization_id))  # fmt: skip
            await s.execute(delete(Organization).where(
                Organization.id == created.organization_id))  # fmt: skip
            await s.execute(delete(UserAdmission).where(
                UserAdmission.user_id.in_(users)
                | UserAdmission.email.in_(created.emails)))  # fmt: skip
            await s.execute(delete(AuthSession).where(AuthSession.user_id.in_(users)))
            await s.execute(delete(User).where(User.id.in_(users)))
        for b in (created.owner, created.teammate):
            if b is not None:
                await b.client.aclose()


async def ended(task: asyncio.Task[httpx.Response], seconds: float = 5) -> str:
    response = await asyncio.wait_for(task, seconds)
    assert response.status_code == 200
    return response.text


async def set_session(world: StreamWorld, who: Browser, **values) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(AuthSession).where(AuthSession.user_id == who.user_id).values(**values)
        )
        await s.commit()


async def test_an_idle_stream_closes_when_the_session_goes_idle(world):
    streaming = world.stream(world.owner)
    await asyncio.sleep(0.5)
    assert not streaming.done()
    await set_session(world, world.owner, last_seen_at=datetime.now(UTC) - timedelta(hours=3))
    started = asyncio.get_running_loop().time()
    text = await ended(streaming)
    # Detection within one re-check plus one poll (both shortened in this test).
    assert asyncio.get_running_loop().time() - started < 2
    assert text.endswith(REVOKED)
    assert (await world.owner.client.get(world.url, params={"follow": "false"})).status_code == 401


async def test_flowing_events_never_extend_the_idle_deadline(world):
    async with SessionLocal() as s:
        signed_at = await s.scalar(
            select(AuthSession.last_seen_at).where(AuthSession.user_id == world.owner.user_id)
        )
    streaming = world.stream(world.owner)
    for _ in range(10):
        await world.add_events(2)
        await asyncio.sleep(0.1)
    async with SessionLocal() as s:
        seen = await s.scalar(
            select(AuthSession.last_seen_at).where(AuthSession.user_id == world.owner.user_id)
        )
    assert seen == signed_at  # streaming and its events moved nothing
    await set_session(world, world.owner, last_seen_at=datetime.now(UTC) - timedelta(hours=3))

    async def keep_flowing():
        while True:
            await world.add_events()
            await asyncio.sleep(0.05)

    flowing = asyncio.create_task(keep_flowing())
    try:
        text = await ended(streaming)
    finally:
        flowing.cancel()
    assert text.count("event: turn.queued") >= 20
    assert text.endswith(REVOKED)


async def test_a_stream_ends_at_the_absolute_expiry(world):
    streaming = world.stream(world.owner)
    await asyncio.sleep(0.3)
    now = datetime.now(UTC)
    await set_session(
        world,
        world.owner,
        created_at=now - timedelta(hours=13),
        expires_at=now - timedelta(seconds=1),
    )
    assert (await ended(streaming)).endswith(REVOKED)


async def test_withdrawal_and_disabling_end_open_streams(world):
    second = await world.again(world.owner)
    withdrawn = world.stream(world.teammate)
    disabled = [world.stream(world.owner), world.stream(second)]
    await asyncio.sleep(0.4)
    async with SessionLocal() as s:
        await admissions.revoke_user(s, world.teammate.user_id, actor=OPERATOR)
        await s.commit()
    assert (await ended(withdrawn)).endswith(REVOKED)
    assert not any(t.done() for t in disabled)  # another user's streams carry on
    async with SessionLocal() as s:
        await admissions.disable_user(s, world.owner.user_id, actor=OPERATOR)
        await s.commit()
    for task in disabled:
        assert (await ended(task)).endswith(REVOKED)
    await second.client.aclose()


async def test_disabling_alone_ends_open_streams(world):
    """Even where sessions were not ended with it (disabled in the database directly)."""
    streaming = world.stream(world.owner)
    await asyncio.sleep(0.3)
    async with SessionLocal() as s:
        await s.execute(update(User).where(User.id == world.owner.user_id).values(
            disabled_at=datetime.now(UTC)))  # fmt: skip
        await s.commit()
    assert (await ended(streaming)).endswith(REVOKED)


async def test_sign_out_everywhere_ends_streams_in_every_browser(world):
    browsers = [world.owner, await world.again(world.owner), await world.again(world.owner)]
    streams = [world.stream(b) for b in browsers]
    teammate = world.stream(world.teammate)
    await asyncio.sleep(0.4)
    assert (await write(browsers[0].client, "/auth/logout-everywhere")).status_code == 204
    for task in streams:
        assert (await ended(task)).endswith(REVOKED)
    await asyncio.sleep(0.5)
    assert not teammate.done()
    teammate.cancel()
    for b in browsers[1:]:
        await b.client.aclose()


async def test_no_event_read_after_access_ends_is_sent(world, monkeypatch):
    """Each batch is authorized after it is read, not on a timer: with the default
    re-check interval, an event appearing just as access ends is never sent."""
    monkeypatch.setattr(sse, "RECHECK_SECONDS", 5.0)
    allowed = True

    async def still_allowed() -> bool:
        return allowed

    async def connected() -> bool:
        return False

    stream = sse.event_stream(
        world.investigation_id, after=0, follow=True, is_disconnected=connected,
        still_allowed=still_allowed,
    )  # fmt: skip
    assert (await anext(stream)).startswith("retry:")
    first = await world.add_events()
    assert f"id: {first}\n" in await asyncio.wait_for(anext(stream), 3)
    second = await world.add_events()
    allowed = False
    chunk = await asyncio.wait_for(anext(stream), 3)
    assert chunk == REVOKED and f"id: {second}" not in chunk
    await stream.aclose()


async def test_replay_and_reconnect_still_work(world):
    first = await world.add_events(3)
    replay = await world.owner.client.get(world.url, params={"follow": "false"})
    assert replay.text.count("event: turn.queued") == 3
    resumed = await world.owner.client.get(
        world.url, params={"follow": "false"}, headers={"Last-Event-ID": str(first - 1)}
    )
    assert resumed.text.count("event: turn.queued") == 1 and f"id: {first}\n" in resumed.text


# --- races (committed rows) --------------------------------------------------------------------


async def live_sessions(user_id: uuid.UUID) -> int:
    async with SessionLocal() as s:
        return await s.scalar(
            select(func.count()).where(
                AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
            )
        )


async def test_requests_racing_sign_out_everywhere_cannot_revive_a_session(world, monkeypatch):
    """A write already past its session check when sign-out everywhere commits finishes,
    and its idle-deadline update leaves the session ended."""
    browsers = [world.owner, await world.again(world.owner), await world.again(world.owner)]
    tokens = [await csrf(b.client) for b in browsers]
    await set_session(world, world.owner, last_seen_at=datetime.now(UTC) - timedelta(minutes=30))
    held, release = asyncio.Event(), asyncio.Event()
    real = deps.record_activity

    async def slow_activity(session, auth):
        held.set()
        await release.wait()
        await real(session, auth)

    monkeypatch.setattr(deps, "record_activity", slow_activity)
    in_flight = asyncio.create_task(write(browsers[1].client, "/auth/activity", tokens[1]))
    await asyncio.wait_for(held.wait(), 5)
    monkeypatch.setattr(deps, "record_activity", real)
    racing = [write(browsers[2].client, "/auth/activity", tokens[2]) for _ in range(5)]
    racing += [browsers[2].client.get("/me") for _ in range(5)]
    signed_out, *_ = await asyncio.gather(
        write(browsers[0].client, "/auth/logout-everywhere", tokens[0]), *racing
    )
    assert signed_out.status_code == 204
    release.set()
    assert (await in_flight).status_code == 204  # was authorized before the sign-out
    assert await live_sessions(world.owner.user_id) == 0
    for b, token in zip(browsers, tokens, strict=True):
        assert not await signed_in(b.client)
        assert (await write(b.client, "/auth/activity", token)).status_code == 401
    for b in browsers[1:]:
        await b.client.aclose()


@pytest.mark.parametrize(
    ("stop", "restore"),
    [
        (admissions.disable_user, admissions.enable_user),
        (admissions.revoke_user, admissions.approve_user),
    ],
    ids=["disable", "withdraw"],
)
async def test_a_sign_in_racing_the_operator_leaves_no_session(world, monkeypatch, stop, restore):
    """The sign-in is held just before its session is created while the operator disables
    the account (or withdraws its admission), then let go: the session is refused, and none
    appears after re-enabling (or restoring)."""
    held, release = asyncio.Event(), asyncio.Event()
    real = auth_routes.create_session

    async def slow_create(session, user, settings):
        held.set()
        await release.wait()
        return await real(session, user, settings)

    monkeypatch.setattr(auth_routes, "create_session", slow_create)
    client = another_browser()
    signing_in = asyncio.create_task(
        sign_in(client, world.provider, sub=world.owner.sub, email=world.owner.email)
    )
    await asyncio.wait_for(held.wait(), 5)
    async with SessionLocal() as s:
        await stop(s, world.owner.user_id, actor=OPERATOR)
        await s.commit()
    release.set()
    response = await signing_in
    assert response.headers["location"] == f"{PUBLIC_URL}/?auth_error=not_admitted"
    assert await live_sessions(world.owner.user_id) == 0
    async with SessionLocal() as s:
        await restore(s, world.owner.user_id, actor=OPERATOR)
        await s.commit()
    assert not await signed_in(client) and not await signed_in(world.owner.client)
    await client.aclose()


async def test_a_sign_in_waits_for_a_disabling_in_progress(world):
    """The operator's transaction holds the per-user lock: a sign-in finishing meanwhile
    waits for it and then sees the account disabled."""
    client = another_browser()
    async with SessionLocal() as s:
        await admissions.disable_user(s, world.owner.user_id, actor=OPERATOR)
        signing_in = asyncio.create_task(
            sign_in(client, world.provider, sub=world.owner.sub, email=world.owner.email)
        )
        await asyncio.sleep(0.5)
        assert not signing_in.done()
        await s.commit()
    response = await asyncio.wait_for(signing_in, 5)
    assert "auth_error=" in response.headers["location"]
    assert await live_sessions(world.owner.user_id) == 0
    await client.aclose()


async def test_concurrent_sign_ins_and_disabling_leave_no_usable_session(world):
    clients = [another_browser() for _ in range(4)]

    async def disable():
        await asyncio.sleep(0.01)
        async with SessionLocal() as s:
            await admissions.disable_user(s, world.owner.user_id, actor=OPERATOR)
            await s.commit()

    await asyncio.gather(
        *(sign_in(c, world.provider, sub=world.owner.sub, email=world.owner.email)
          for c in clients),
        disable(),
    )  # fmt: skip
    assert await live_sessions(world.owner.user_id) == 0
    for c in clients:
        assert not await signed_in(c)
        await c.aclose()


# --- no leakage ----------------------------------------------------------------------------


async def test_audit_rows_and_refusals_hold_no_secrets(world):
    async with SessionLocal() as s:
        secrets = (await s.execute(
            select(AuthSession.token_hash, AuthSession.csrf_token)
            .where(AuthSession.user_id == world.owner.user_id))).one()  # fmt: skip
    streaming = world.stream(world.owner)
    await asyncio.sleep(0.3)
    signed_out = await write(world.owner.client, "/auth/logout-everywhere")
    text = await ended(streaming)
    async with SessionLocal() as s:
        await admissions.disable_user(s, world.owner.user_id, actor=OPERATOR)
        await s.commit()
        await admissions.enable_user(s, world.owner.user_id, actor=OPERATOR)
        await s.commit()
        rows = list(await s.scalars(select(AuditEvent).where(
            AuditEvent.action.in_(["session.revoke_all", "user.disable", "user.enable"]),
            (AuditEvent.actor_user_id == world.owner.user_id)
            | (AuditEvent.details_json["user_id"].astext == str(world.owner.user_id))
        )))  # fmt: skip
    assert {r.action for r in rows} == {"session.revoke_all", "user.disable", "user.enable"}
    refusal = await world.owner.client.get("/me")
    exposed = json.dumps([r.details_json for r in rows]) + text + refusal.text + signed_out.text
    for secret in (*secrets, world.owner.email, world.owner.sub):
        assert secret not in exposed
    for row in rows:
        assert set(row.details_json) <= {"user_id", "sessions_ended"}
    assert refusal.json() == {"detail": "sign in required"}


# --- operator command (committed rows) ---------------------------------------------------------


def command(name: str, user_id: uuid.UUID, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        command=name, user=str(user_id), email=None, operator="test", apply=apply
    )


async def disabled_at(user_id: uuid.UUID) -> datetime | None:
    async with SessionLocal() as s:
        return await s.scalar(select(User.disabled_at).where(User.id == user_id))


async def test_operator_command_plans_by_default_and_applies_on_request(world, capsys):
    user_id = world.owner.user_id
    await admit_user._change(command("disable", user_id, apply=False))
    assert "plan (dry run" in capsys.readouterr().out
    assert await disabled_at(user_id) is None and await signed_in(world.owner.client)

    await admit_user._change(command("disable", user_id, apply=True))
    out = capsys.readouterr().out
    assert "applied: disabled" in out and "sessions ended: 1" in out
    assert "1 membership(s), 0 owned environment(s), 0 pending transfer(s)" in out
    assert await disabled_at(user_id) is not None and not await signed_in(world.owner.client)

    await admit_user._change(command("enable", user_id, apply=True))
    assert "applied: enabled" in capsys.readouterr().out
    assert await disabled_at(user_id) is None and not await signed_in(world.owner.client)
