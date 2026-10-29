"""Sign-up admission control (Phase 7D-2E-1, D042; audit H3): a valid OIDC sign-in is not
enough under INQUEST_AUTH_ADMISSION=approval. Uses the fake OIDC provider; most tests run
in the rolled-back test session, the concurrency tests on committed rows they remove."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import oidc
from src.auth.settings import AuthNotConfigured, load_auth_settings
from src.db.session import SessionLocal, get_session
from src.deployment import deployment_problems
from src.main import app
from src.mcp_server import McpSettings
from src.models import (
    AuditEvent,
    AuthLoginState,
    AuthSession,
    Environment,
    EnvironmentType,
    Investigation,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    TriggerType,
    User,
    UserAdmission,
)
from src.services import access, admissions
from src.services import organizations as orgs
from tests.auth_fixtures import CLIENT_ID, CLIENT_SECRET, ISSUER, PUBLIC_URL, FakeProvider
from tests.database import OwnerSession

COOKIE = "__Host-inquest_session"
OPERATOR = "operator:test (pytest)"


def configure(monkeypatch: pytest.MonkeyPatch, admission: str | None) -> FakeProvider:
    fake = FakeProvider()
    for key, value in {
        "ISSUER": ISSUER, "CLIENT_ID": CLIENT_ID, "CLIENT_SECRET": CLIENT_SECRET,
        "PUBLIC_URL": PUBLIC_URL,
    }.items():  # fmt: skip
        monkeypatch.setenv(f"INQUEST_AUTH_{key}", value)
    if admission is None:
        monkeypatch.delenv("INQUEST_AUTH_ADMISSION", raising=False)
    else:
        monkeypatch.setenv("INQUEST_AUTH_ADMISSION", admission)
    load_auth_settings.cache_clear()
    oidc.clear_caches()

    async def http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            yield c

    app.dependency_overrides[oidc.http_client] = http
    return fake


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


def unique(name: str) -> tuple[str, str]:
    """A fresh (subject, email) pair."""
    suffix = uuid.uuid4().hex[:8]
    return f"{name}-{suffix}", f"{name}-{suffix}@example.com"


async def sign_in(
    browser: AsyncClient,
    provider: FakeProvider,
    *,
    sub: str,
    email: str,
    verified: bool = True,
    invitation: str | None = None,
) -> httpx.Response:
    """The browser flow, optionally started from an invitation link; the callback response."""
    if invitation is None:
        start = await browser.get("/auth/login", params={"return_to": "/"})
        url = start.headers["location"]
    else:
        start = await browser.post(
            "/auth/login",
            json={"invitation_token": invitation, "return_to": "/invitations/accept"},
        )
        assert start.status_code == 200, start.text
        url = start.json()["authorization_url"]
    code = provider.authorize(url, sub=sub, email=email, email_verified=verified)
    state = parse_qs(urlsplit(url).query)["state"][0]
    return await browser.get("/auth/callback", params={"code": code, "state": state})


def refused(response: httpx.Response) -> bool:
    return (
        response.status_code == 303
        and response.headers["location"] == f"{PUBLIC_URL}/?auth_error=not_admitted"
        and COOKIE not in response.headers.get("set-cookie", "")
    )


async def signed_in(browser: AsyncClient) -> bool:
    return (await browser.get("/auth/session")).json()["authenticated"]


async def user_for(session: AsyncSession, sub: str) -> User | None:
    return await session.scalar(select(User).where(User.issuer == ISSUER, User.subject == sub))


async def owner_with_organization(session: AsyncSession) -> tuple[User, Organization]:
    sub, email = unique("owner")
    user = User(issuer=ISSUER, subject=sub, email=email, email_verified=True)
    session.add(user)
    await session.flush()
    session.add(UserAdmission(user_id=user.id, source="operator"))
    organization = await orgs.create_organization(session, user, f"Org {sub}")
    await session.commit()
    return user, organization


async def invite(session: AsyncSession, inviter: User, organization: Organization, email: str):
    invitation, token = await orgs.create_invitation(
        session, organization.id, inviter, email, OrganizationRole.MEMBER
    )
    await session.commit()
    return invitation, token


# --- policy and startup ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "must be set to approval"),
        ("", "must be set to approval"),
        ("open", "open is not allowed"),
        ("anyone", "ADMISSION"),
    ],
)
def test_hosted_sign_in_needs_an_approval_policy(monkeypatch, value, expected):
    configure(monkeypatch, value)
    hosted = load_auth_settings.__wrapped__  # uncached, with production settings below
    from src.config import get_settings

    production = get_settings().model_copy(update={"app_env": "production"})
    monkeypatch.setattr("src.auth.settings.get_settings", lambda: production)
    with pytest.raises(AuthNotConfigured, match=expected):
        hosted()
    load_auth_settings.cache_clear()
    monkeypatch.setattr("src.deployment.load_credential_config", lambda: None)
    problems = deployment_problems(production, McpSettings())
    assert any(p.startswith("sign-in:") and expected in p for p in problems), problems
    app.dependency_overrides.pop(oidc.http_client, None)


def test_hosted_approval_policy_is_accepted(monkeypatch):
    configure(monkeypatch, "approval")
    from src.config import get_settings

    production = get_settings().model_copy(update={"app_env": "production"})
    monkeypatch.setattr("src.auth.settings.get_settings", lambda: production)
    assert load_auth_settings.__wrapped__().admission == "approval"
    app.dependency_overrides.pop(oidc.http_client, None)


async def test_local_development_stays_open_by_default(monkeypatch, browser, session):
    provider = configure(monkeypatch, None)
    assert load_auth_settings().admission == "open"
    sub, email = unique("dev")
    response = await sign_in(browser, provider, sub=sub, email=email)
    assert response.status_code == 303 and await signed_in(browser)
    assert await session.scalar(select(func.count()).select_from(UserAdmission)) == 0
    app.dependency_overrides.pop(oidc.http_client, None)
    load_auth_settings.cache_clear()


# --- refusals --------------------------------------------------------------------------------


async def test_an_unknown_identity_is_refused_and_nothing_is_stored(provider, browser, session):
    sub, email = unique("stranger")
    users = await session.scalar(select(func.count()).select_from(User))
    response = await sign_in(browser, provider, sub=sub, email=email)
    assert refused(response)
    assert not await signed_in(browser)
    assert await session.scalar(select(func.count()).select_from(User)) == users
    assert await session.scalar(select(func.count()).select_from(AuthSession).join(User).where(
        User.subject == sub)) == 0  # fmt: skip
    # No organization access either: protected routes stay closed.
    assert (await browser.get("/me")).status_code == 401
    assert (await browser.post("/organizations", json={"name": "x"})).status_code in (401, 403)


async def test_an_unverified_email_never_claims_an_approval(provider, browser, session):
    sub, email = unique("unverified")
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    response = await sign_in(browser, provider, sub=sub, email=email, verified=False)
    assert refused(response)
    assert await user_for(session, sub) is None
    pending = await session.scalar(select(UserAdmission).where(UserAdmission.email == email))
    assert pending.user_id is None


async def test_repeated_refusals_store_nothing_and_stay_rate_limited(provider, browser, session):
    users = await session.scalar(select(func.count()).select_from(User))
    outcomes = []
    for _ in range(10):
        sub, email = unique("flood")
        outcomes.append(refused(await sign_in(browser, provider, sub=sub, email=email)))
    assert all(outcomes)
    assert await session.scalar(select(func.count()).select_from(User)) == users
    # The existing per-address sign-in limit (D039) still applies.
    assert (await browser.get("/auth/login")).status_code == 429


async def test_a_refusal_says_nothing_about_why(provider, browser, session):
    """Unknown, unverified, withdrawn, wrong invitation: one redirect, one code."""
    owner, organization = await owner_with_organization(session)
    _, token = await invite(session, owner, organization, "someone-else@example.com")
    withdrawn_sub, withdrawn_email = unique("withdrawn")
    user = User(issuer=ISSUER, subject=withdrawn_sub, email=withdrawn_email, email_verified=True)
    session.add(user)
    await session.flush()
    await admissions.revoke_user(session, user.id, actor=OPERATOR)
    await session.commit()
    cases = [
        dict(zip(("sub", "email"), unique("a"), strict=True)),
        {**dict(zip(("sub", "email"), unique("b"), strict=True)), "verified": False},
        {"sub": withdrawn_sub, "email": withdrawn_email},
        {**dict(zip(("sub", "email"), unique("c"), strict=True)), "invitation": token},
    ]
    locations = {(await sign_in(browser, provider, **case)).headers["location"] for case in cases}
    assert locations == {f"{PUBLIC_URL}/?auth_error=not_admitted"}


# --- operator approval -----------------------------------------------------------------------


async def test_an_approved_first_user_signs_in_and_creates_an_organization(
    provider, browser, session
):
    sub, email = unique("first")
    change = await admissions.approve_email(session, f"  {email.upper()} ", actor=OPERATOR)
    await session.commit()
    response = await sign_in(browser, provider, sub=sub, email=email)
    assert response.status_code == 303 and response.headers["location"] == f"{PUBLIC_URL}/"
    assert await signed_in(browser)
    user = await user_for(session, sub)
    admission = await session.get(UserAdmission, change.admission_id)
    assert admission.user_id == user.id and admission.source == "operator"
    actions = list(await session.scalars(select(AuditEvent.action).where(
        AuditEvent.details_json["admission_id"].astext == str(admission.id))))  # fmt: skip
    assert actions == ["admission.approve", "admission.claim"]
    csrf = (await browser.get("/auth/session")).json()["csrf_token"]
    created = await browser.post(
        "/organizations", json={"name": "First"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == 201


async def test_an_approval_belongs_to_the_first_account_that_claims_it(provider, browser, session):
    sub, email = unique("claimed")
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    assert not refused(await sign_in(browser, provider, sub=sub, email=email))
    # Another account at the provider with the same verified address gets nothing.
    other, _ = unique("impostor")
    assert refused(await sign_in(browser, provider, sub=other, email=email))
    assert await user_for(session, other) is None
    # Nor can the operator be tricked into a second email approval for that address.
    with pytest.raises(admissions.AdmissionRefused, match="already has an admission"):
        await admissions.approve_email(session, email, actor=OPERATOR)
        await session.commit()


async def test_existing_users_need_an_explicit_operator_decision(provider, browser, session):
    """Turning approval on admits nobody by itself; admit-members is the explicit policy."""
    member_sub, member_email = unique("old-member")
    loner_sub, loner_email = unique("old-loner")
    member = User(issuer=ISSUER, subject=member_sub, email=member_email, email_verified=True)
    loner = User(issuer=ISSUER, subject=loner_sub, email=loner_email, email_verified=True)
    session.add_all([member, loner])
    await session.flush()
    organization = Organization(name="Old", slug=f"old-{member_sub}", created_by_user_id=member.id)
    session.add(organization)
    await session.flush()
    session.add(OrganizationMembership(organization_id=organization.id, user_id=member.id,
                                       role=OrganizationRole.OWNER))  # fmt: skip
    member_id, loner_id = member.id, loner.id
    await session.commit()
    assert refused(await sign_in(browser, provider, sub=member_sub, email=member_email))
    change = await admissions.admit_members(session, actor=OPERATOR)
    await session.commit()
    assert member_id in change.users and loner_id not in change.users
    assert not refused(await sign_in(browser, provider, sub=member_sub, email=member_email))
    assert refused(await sign_in(browser, provider, sub=loner_sub, email=loner_email))


async def test_sessions_from_before_approval_stop_working(monkeypatch, provider, browser, session):
    sub, email = unique("before")
    open_provider = configure(monkeypatch, "open")
    assert not refused(await sign_in(browser, open_provider, sub=sub, email=email))
    assert await signed_in(browser)
    configure(monkeypatch, "approval")
    assert not await signed_in(browser)
    assert (await browser.get("/me")).status_code == 401


# --- invitations -----------------------------------------------------------------------------


async def test_an_invited_teammate_is_admitted_by_signing_in_from_the_link(
    provider, browser, session
):
    owner, organization = await owner_with_organization(session)
    sub, email = unique("teammate")
    invitation, token = await invite(session, owner, organization, email)
    response = await sign_in(browser, provider, sub=sub, email=email, invitation=token)
    assert response.headers["location"] == (
        f"{PUBLIC_URL}/invitations/accept?joined={organization.id}"
    )
    assert await signed_in(browser)
    user = await user_for(session, sub)
    assert await orgs.membership_role(session, organization.id, user) == OrganizationRole.MEMBER
    await session.refresh(invitation)
    assert invitation.accepted_by_user_id == user.id
    admission = await session.scalar(select(UserAdmission).where(UserAdmission.user_id == user.id))
    assert admission.source == "invitation"
    audit = await session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "admission.invitation", AuditEvent.actor_user_id == user.id
        )
    )
    assert audit.organization_id == organization.id
    assert set(audit.details_json) == {"admission_id", "source"}
    # The teammate sees their organization and nothing about anyone else's.
    me = (await browser.get("/me")).json()
    assert [m["organization_id"] for m in me["memberships"]] == [str(organization.id)]
    # Members cannot invite or manage (roles unchanged by how they were admitted).
    csrf = (await browser.get("/auth/session")).json()["csrf_token"]
    forbidden = await browser.post(
        f"/organizations/{organization.id}/invitations",
        json={"email": "x@example.com", "role": "member"},
        headers={"X-CSRF-Token": csrf},
    )
    assert forbidden.status_code == 403


async def test_the_invitation_must_be_for_the_verified_identity(provider, browser, session):
    owner, organization = await owner_with_organization(session)
    _, invited_email = unique("invited")
    invitation, token = await invite(session, owner, organization, invited_email)
    sub, other_email = unique("holder")
    assert refused(await sign_in(browser, provider, sub=sub, email=other_email, invitation=token))
    # Even the invited address, unverified, is not enough.
    sub2, _ = unique("unverified-holder")
    response = await sign_in(
        browser, provider, sub=sub2, email=invited_email, verified=False, invitation=token
    )
    assert refused(response)
    await session.refresh(invitation)
    assert orgs.invitation_status(invitation, datetime.now(UTC)) == "pending"  # not consumed
    assert await user_for(session, sub) is None and await user_for(session, sub2) is None


@pytest.mark.parametrize("ending", ["expired", "revoked", "accepted"])
async def test_ended_invitations_admit_nobody(provider, browser, session, ending):
    owner, organization = await owner_with_organization(session)
    sub, email = unique(ending)
    invitation, token = await invite(session, owner, organization, email)
    now = datetime.now(UTC)
    if ending == "expired":
        invitation.expires_at = now - timedelta(seconds=1)
    elif ending == "revoked":
        invitation.revoked_at = now
    else:
        invitation.accepted_at, invitation.accepted_by_user_id = now, owner.id
    await session.commit()
    assert refused(await sign_in(browser, provider, sub=sub, email=email, invitation=token))
    assert await user_for(session, sub) is None


async def test_an_invitation_cannot_be_replayed(provider, browser, session):
    owner, organization = await owner_with_organization(session)
    sub, email = unique("once")
    _, token = await invite(session, owner, organization, email)
    assert not refused(await sign_in(browser, provider, sub=sub, email=email, invitation=token))
    other, _ = unique("replay")
    assert refused(await sign_in(browser, provider, sub=other, email=email, invitation=token))


async def test_admitted_users_still_accept_invitations_themselves(provider, browser, session):
    """Signing in from a link does not join an organization for someone already admitted:
    they confirm on the invitation page as before."""
    owner, organization = await owner_with_organization(session)
    sub, email = unique("admitted")
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    invitation, token = await invite(session, owner, organization, email)
    response = await sign_in(browser, provider, sub=sub, email=email, invitation=token)
    assert response.headers["location"] == f"{PUBLIC_URL}/invitations/accept"
    await session.refresh(invitation)
    assert invitation.accepted_at is None
    csrf = (await browser.get("/auth/session")).json()["csrf_token"]
    accepted = await browser.post(
        "/invitations/accept", json={"token": token}, headers={"X-CSRF-Token": csrf}
    )
    assert accepted.status_code == 200


async def test_invitation_admission_respects_the_organizations_quota(
    provider, browser, session, monkeypatch
):
    owner, organization = await owner_with_organization(session)
    sub, email = unique("over-quota")
    _, token = await invite(session, owner, organization, email)
    # The organization fills up (members plus pending) after the invitation was sent.
    monkeypatch.setattr("src.services.limits.quota", lambda name: 1)
    assert refused(await sign_in(browser, provider, sub=sub, email=email, invitation=token))
    assert await user_for(session, sub) is None


async def test_starting_an_invitation_sign_in_reveals_nothing(provider, browser, session):
    owner, organization = await owner_with_organization(session)
    _, token = await invite(session, owner, organization, "real@example.com")
    answers = []
    for candidate in (token, "x" * 43):
        start = await browser.post("/auth/login", json={"invitation_token": candidate})
        answers.append((start.status_code, set(start.json())))
    assert answers == [(200, {"authorization_url"})] * 2
    stored = list(await session.scalars(select(AuthLoginState.invitation_hash)))
    assert token not in str(stored) and all(h is None or len(h) == 64 for h in stored)
    # A browser on another site cannot start one (Origin check).
    foreign = await browser.post(
        "/auth/login", json={"invitation_token": token}, headers={"origin": "https://evil.test"}
    )
    assert foreign.status_code == 403


# --- withdrawal ------------------------------------------------------------------------------


async def test_withdrawal_ends_access_and_keeps_memberships_and_ownership(
    provider, browser, session
):
    sub, email = unique("leaving")
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    assert not refused(await sign_in(browser, provider, sub=sub, email=email))
    user = await user_for(session, sub)
    user_id = user.id
    organization = await orgs.create_organization(session, user, "Leaving Org")
    environment = Environment(
        name="env", type=EnvironmentType.LOCAL, organization_id=organization.id,
        owner_user_id=user.id,
    )  # fmt: skip
    session.add(environment)
    investigation = Investigation(
        title="i", question="why?", trigger_type=TriggerType.MANUAL, organization_id=organization.id
    )
    session.add(investigation)
    await session.flush()
    auth_session_id = await session.scalar(
        select(AuthSession.id).where(AuthSession.user_id == user.id)
    )
    assert await access.still_allowed(session, auth_session_id, investigation.id)

    change = await admissions.revoke_user(session, user.id, actor=OPERATOR)

    await session.commit()
    assert (change.memberships, change.owned_environments, change.sessions_ended) == (1, 1, 1)
    assert not await signed_in(browser)
    assert not await access.still_allowed(session, auth_session_id, investigation.id)
    # Nothing moved: the environment keeps its owner, the organization its owner member.
    await session.refresh(environment)
    assert environment.owner_user_id == user.id
    assert await orgs.membership_role(session, organization.id, user) == OrganizationRole.OWNER
    # Neither an email approval for their address nor an invitation brings them back (the
    # operator command refuses that approval; inserted directly to prove the rule holds).
    with pytest.raises(admissions.AdmissionRefused):
        await admissions.approve_email(session, email, actor=OPERATOR)
    session.add(UserAdmission(email=email, source="operator"))
    await session.commit()
    other_owner, other_org = await owner_with_organization(session)
    _, token = await invite(session, other_owner, other_org, email)
    assert refused(await sign_in(browser, provider, sub=sub, email=email, invitation=token))
    # Only the operator restores it.
    await admissions.approve_user(session, user_id, actor=OPERATOR)
    await session.commit()
    assert not refused(await sign_in(browser, provider, sub=sub, email=email))


async def test_a_withdrawn_admission_ends_open_streams_by_itself(provider, browser, session):
    """Even where sessions were not ended with it (a withdrawal made in the database, or
    approval switched on over existing sessions), streams stop at their next re-check."""
    sub, email = unique("streaming")
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    assert not refused(await sign_in(browser, provider, sub=sub, email=email))
    user = await user_for(session, sub)
    organization = await orgs.create_organization(session, user, "Streaming Org")
    investigation = Investigation(
        title="i", question="why?", trigger_type=TriggerType.MANUAL, organization_id=organization.id
    )
    session.add(investigation)
    await session.flush()
    auth_session_id = await session.scalar(
        select(AuthSession.id).where(AuthSession.user_id == user.id)
    )
    assert await access.still_allowed(session, auth_session_id, investigation.id)
    await session.execute(
        update(UserAdmission).where(UserAdmission.user_id == user.id).values(
            revoked_at=datetime.now(UTC))
    )  # fmt: skip
    assert not await access.still_allowed(session, auth_session_id, investigation.id)
    assert not await signed_in(browser)


async def test_withdrawing_a_user_without_a_decision_is_recorded(provider, browser, session):
    sub, email = unique("never-admitted")
    user = User(issuer=ISSUER, subject=sub, email=email, email_verified=True)
    session.add(user)
    await session.flush()
    # An approval for their address made earlier does not outlast the withdrawal.
    await admissions.approve_email(session, email, actor=OPERATOR)
    await admissions.revoke_user(session, user.id, actor=OPERATOR)
    await session.commit()
    assert refused(await sign_in(browser, provider, sub=sub, email=email))


async def test_unclaimed_approvals_can_be_withdrawn(provider, browser, session):
    sub, email = unique("unclaimed")
    await admissions.approve_email(session, email, actor=OPERATOR)
    await session.commit()
    await admissions.revoke_email(session, email, actor=OPERATOR)
    assert refused(await sign_in(browser, provider, sub=sub, email=email))
    with pytest.raises(admissions.AdmissionRefused):
        await admissions.revoke_email(session, email, actor=OPERATOR)
        await session.commit()


# --- concurrency (committed rows) ------------------------------------------------------------


@pytest.fixture
async def committed(monkeypatch) -> AsyncIterator[tuple[FakeProvider, list[str], list[str]]]:
    """The real app and sessions; the subjects and emails used are removed afterwards."""
    fake = configure(monkeypatch, "approval")
    subjects: list[str] = []
    emails: list[str] = []
    yield fake, subjects, emails
    app.dependency_overrides.pop(oidc.http_client, None)
    load_auth_settings.cache_clear()
    oidc.clear_caches()
    async with OwnerSession() as s, s.begin():
        users = select(User.id).where(User.issuer == ISSUER, User.subject.in_(subjects))
        organizations = select(Organization.id).where(Organization.created_by_user_id.in_(users))
        operator = AuditEvent.actor.like("operator:test%")
        await s.execute(delete(AuditEvent).where(AuditEvent.actor_user_id.in_(users) | operator))
        await s.execute(delete(OrganizationInvitation).where(
            OrganizationInvitation.organization_id.in_(organizations)))  # fmt: skip
        await s.execute(delete(OrganizationMembership).where(
            OrganizationMembership.organization_id.in_(organizations)))  # fmt: skip
        await s.execute(delete(Organization).where(Organization.id.in_(organizations)))
        await s.execute(delete(UserAdmission).where(UserAdmission.email.in_(emails)))
        await s.execute(delete(User).where(User.id.in_(users)))


async def committed_sign_in(provider: FakeProvider, **kwargs) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as browser:
        return await sign_in(browser, provider, **kwargs)


async def test_concurrent_first_sign_ins_of_one_identity_make_one_user(committed):
    provider, subjects, emails = committed
    sub, email = unique("racer")
    subjects.append(sub), emails.append(email)
    async with SessionLocal() as s:
        await admissions.approve_email(s, email, actor=OPERATOR)
        await s.commit()
    results = await asyncio.gather(
        *(committed_sign_in(provider, sub=sub, email=email) for _ in range(5))
    )
    assert all(
        r.status_code == 303
        and r.headers["location"] == f"{PUBLIC_URL}/"
        and COOKIE in r.headers.get("set-cookie", "")
        for r in results
    ), [(r.status_code, r.headers.get("location")) for r in results]
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).where(User.subject == sub)) == 1
        bound = await s.scalar(select(func.count()).select_from(UserAdmission).join(
            User, User.id == UserAdmission.user_id).where(User.subject == sub))  # fmt: skip
        assert bound == 1


async def test_overlapping_first_sign_ins_of_one_identity_both_succeed(committed, monkeypatch):
    """Forces the overlap: both callbacks are held just before writing the user. Without the
    per-identity lock the second would find the approval already claimed and be refused."""
    provider, subjects, emails = committed
    sub, email = unique("overlap")
    subjects.append(sub), emails.append(email)
    async with SessionLocal() as s:
        await admissions.approve_email(s, email, actor=OPERATOR)
        await s.commit()
    from src.auth import admission

    barrier, original = asyncio.Barrier(2), admission.upsert_user

    async def held(session, identity):
        try:
            await asyncio.wait_for(barrier.wait(), 1)
        except TimeoutError:
            pass  # serialized by the lock: the other request is still waiting for it
        return await original(session, identity)

    monkeypatch.setattr(admission, "upsert_user", held)
    results = await asyncio.gather(
        *(committed_sign_in(provider, sub=sub, email=email) for _ in range(2))
    )
    assert [r.headers["location"] for r in results] == [f"{PUBLIC_URL}/"] * 2


async def test_two_accounts_racing_for_one_approval_admit_one(committed):
    provider, subjects, emails = committed
    _, email = unique("contested")
    emails.append(email)
    a, b = f"{email}-a", f"{email}-b"
    subjects.extend([a, b])
    async with SessionLocal() as s:
        await admissions.approve_email(s, email, actor=OPERATOR)
        await s.commit()
    results = await asyncio.gather(
        committed_sign_in(provider, sub=a, email=email),
        committed_sign_in(provider, sub=b, email=email),
    )
    assert sorted(refused(r) for r in results) == [False, True]
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).where(User.subject.in_([a, b]))) == 1


async def test_concurrent_acceptance_of_one_invitation_admits_one(committed):
    provider, subjects, emails = committed
    owner_sub, owner_email = unique("inviter")
    _, email = unique("contested-invite")
    a, b = f"{email}-a", f"{email}-b"
    subjects.extend([owner_sub, a, b])
    emails.append(owner_email)
    async with SessionLocal() as s:
        await admissions.approve_email(s, owner_email, actor=OPERATOR)
        await s.commit()
    first = await committed_sign_in(provider, sub=owner_sub, email=owner_email)
    assert not refused(first)
    async with SessionLocal() as s:
        owner = await user_for(s, owner_sub)
        organization = await orgs.create_organization(s, owner, f"Race {owner_sub}")
        invitation, token = await orgs.create_invitation(
            s, organization.id, owner, email, OrganizationRole.MEMBER
        )
        await s.commit()
    results = await asyncio.gather(
        committed_sign_in(provider, sub=a, email=email, invitation=token),
        committed_sign_in(provider, sub=b, email=email, invitation=token),
    )
    assert sorted(refused(r) for r in results) == [False, True]
    async with SessionLocal() as s:
        members = await s.scalar(select(func.count()).where(
            OrganizationMembership.organization_id == organization.id))  # fmt: skip
        assert members == 2  # the owner and exactly one teammate
        assert await s.scalar(select(func.count()).where(User.subject.in_([a, b]))) == 1
