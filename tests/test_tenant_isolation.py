"""Phase 7B tenant isolation through the API and the database: two organizations, three
users, environments and investigations in each, plus ownerless legacy records.

Organization A: Ada (owner) and Bob (member). Organization B: Eve (owner).
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.settings import load_auth_settings
from src.db.session import get_session
from src.investigators.refs import ref
from src.main import app
from src.models import (
    AuditEvent,
    AuthSession,
    Environment,
    EnvironmentType,
    Evidence,
    EvidenceType,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationReport,
    OrganizationRole,
    TriggerType,
)
from src.services import access, adoption
from tests.credential_fixtures import allow_process_credentials
from tests.records import add_assignment
from tests.tenancy import Member, api_client, configure_auth, make_member


@dataclass
class Tenants:
    ada: Member
    bob: Member
    eve: Member
    a: InvestigationAssignment  # organization A's investigation, with evidence and a report
    evidence: Evidence
    legacy: InvestigationAssignment  # ownerless, from before tenancy

    def reads(self) -> list[str]:
        """Every read endpoint for organization A's records."""
        i, e = self.a.investigation_id, self.a.environment_id
        return [
            f"/environments/{e}",
            f"/environments/{e}/telemetry",
            f"/investigations/{i}",
            f"/investigations/{i}/reports/1",
            f"/investigations/{i}/citations/{ref('evidence', self.evidence.id)}",
            f"/investigations/{i}/turns/{self.a.turn_id}/presentation",
            f"/investigations/{i}/events?follow=false",
        ]

    def lists(self, organization_id: uuid.UUID) -> list[str]:
        q = f"organization_id={organization_id}"
        return [f"/environments?{q}", f"/investigations?{q}", f"/overview?{q}", f"/reports?{q}"]


@pytest.fixture
async def tenants(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Tenants]:
    configure_auth(monkeypatch)
    ada = await make_member(session, name="ada")
    bob = await make_member(
        session, organization_id=ada.organization_id, role=OrganizationRole.MEMBER, name="bob"
    )
    eve = await make_member(session, name="eve")
    a = await add_assignment(session, tenant=ada)
    await add_assignment(session, tenant=eve)  # organization B has data of its own
    legacy = await add_assignment(session)
    await session.flush()
    evidence = Evidence(
        assignment=a,
        evidence_type=EvidenceType.METRIC,
        source_provider="signoz",
        title="cpu",
        summary="cpu 97%",
    )
    session.add(evidence)
    session.add(
        InvestigationReport(
            investigation_id=a.investigation_id,
            turn_id=a.turn_id,
            version=1,
            report_json={"outcome": "inconclusive"},
        )
    )
    await session.flush()
    app.dependency_overrides[get_session] = lambda: session
    yield Tenants(ada, bob, eve, a, evidence, legacy)
    app.dependency_overrides.pop(get_session, None)
    load_auth_settings.cache_clear()


async def _ids(client: AsyncClient, path: str) -> set[str]:
    body = (await client.get(path)).json()
    items = body["items"] if isinstance(body, dict) else body
    return {i.get("investigation_id", i["id"]) for i in items}


# --- collaboration inside one organization ---------------------------------------------------


async def test_members_of_one_organization_share_its_records(tenants: Tenants):
    t = tenants
    async with api_client(t.bob) as bob:
        for path in t.reads():
            status = (await bob.get(path)).status_code
            # The pending turn has no answer to present yet: reachable, not presentable.
            assert status == (409 if path.endswith("/presentation") else 200), path
        assert str(t.a.investigation_id) in await _ids(
            bob, f"/investigations?organization_id={t.ada.organization_id}"
        )
        created = await bob.post(
            "/investigations",
            json={"environment_id": str(t.a.environment_id), "title": "t", "question": "q"},
        )
        assert created.status_code == 201
    investigation = await _investigation(created.json()["id"])
    assert investigation.organization_id == t.ada.organization_id
    assert investigation.created_by_user_id == t.bob.user_id


async def _investigation(investigation_id: str) -> Investigation:
    session = app.dependency_overrides[get_session]()
    return await session.get(Investigation, uuid.UUID(investigation_id))


# --- other organizations -------------------------------------------------------------------


async def test_guessed_ids_from_another_organization_are_not_found(tenants: Tenants):
    t = tenants
    async with api_client(t.eve) as eve:
        for path in t.reads():
            response = await eve.get(path)
            assert response.status_code == 404, path
            # Nothing about the record leaks into the error.
            assert "cpu" not in response.text
        for path in t.lists(t.ada.organization_id):
            assert (await eve.get(path)).status_code == 404, path
        own = t.lists(t.eve.organization_id)
        assert str(t.a.investigation_id) not in await _ids(eve, own[1])
        assert str(t.a.environment_id) not in await _ids(eve, own[0])
        assert str(t.a.investigation_id) not in await _ids(eve, own[3])
        overview = (await eve.get(own[2])).json()
        assert (overview["investigations_total"], overview["environments_total"]) == (1, 1)


async def test_another_organization_cannot_create_start_ask_or_cancel(
    tenants: Tenants, session: AsyncSession
):
    t = tenants
    i = t.a.investigation_id
    before = await session.scalar(select(func.count()).select_from(Investigation))
    async with api_client(t.eve) as eve:
        attempts = [
            ("/investigations", {"environment_id": str(t.a.environment_id), "title": "x",
                                 "question": "x"}),
            ("/environments", {"organization_id": str(t.ada.organization_id), "name": "x",
                               "type": "local"}),
            (f"/investigations/{i}/start", None),
            (f"/investigations/{i}/turns", {"question": "x"}),
            (f"/investigations/{i}/cancel", None),
        ]  # fmt: skip
        for path, body in attempts:
            assert (await eve.post(path, json=body)).status_code == 404, path
    assert await session.scalar(select(func.count()).select_from(Investigation)) == before


async def test_revoked_membership_loses_access_at_once(tenants: Tenants, session: AsyncSession):
    t = tenants
    async with api_client(t.bob) as bob, api_client(t.ada) as ada:
        assert (await bob.get(f"/investigations/{t.a.investigation_id}")).status_code == 200
        assert await access.still_allowed(session, t.bob.session_id, t.a.investigation_id)

        removed = await ada.delete(
            f"/organizations/{t.ada.organization_id}/members/{t.bob.user_id}"
        )
        assert removed.status_code == 204

        # Same browser session, next request: no membership, no data.
        for path in t.reads():
            assert (await bob.get(path)).status_code == 404, path
        for path in t.lists(t.ada.organization_id):
            assert (await bob.get(path)).status_code == 404, path
    # An open event stream checks the same thing and closes (see test_sse_access).
    assert not await access.still_allowed(session, t.bob.session_id, t.a.investigation_id)


async def test_signed_out_sessions_stop_open_streams(tenants: Tenants, session: AsyncSession):
    t = tenants
    assert await access.still_allowed(session, t.ada.session_id, t.a.investigation_id)
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == t.ada.session_id)
        .values(revoked_at=datetime.now(UTC))
    )
    assert not await access.still_allowed(session, t.ada.session_id, t.a.investigation_id)


# --- authentication ------------------------------------------------------------------------


async def test_every_route_needs_a_session(tenants: Tenants):
    t = tenants
    writes = [
        ("/environments", {"organization_id": str(t.ada.organization_id), "name": "x",
                           "type": "local"}),
        ("/investigations", {"environment_id": str(t.a.environment_id), "title": "x",
                             "question": "x"}),
        (f"/investigations/{t.a.investigation_id}/start", None),
        (f"/investigations/{t.a.investigation_id}/turns", {"question": "x"}),
        (f"/investigations/{t.a.investigation_id}/cancel", None),
    ]  # fmt: skip
    # A direct request from this machine is no longer special: no session, no access.
    async with api_client(None) as anonymous:
        for path in t.reads() + t.lists(t.ada.organization_id):
            assert (await anonymous.get(path)).status_code == 401, path
        for path, body in writes:
            assert (await anonymous.post(path, json=body)).status_code == 401, path
    async with api_client(t.ada) as ada:
        # Writes also need the session's CSRF token, not just its cookie.
        del ada.headers["X-CSRF-Token"]
        for path, body in writes:
            assert (await ada.post(path, json=body)).status_code == 403, path


async def test_without_authentication_configured_everything_fails_closed(
    tenants: Tenants, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("INQUEST_AUTH_ISSUER")
    load_auth_settings.cache_clear()
    async with api_client(tenants.ada) as ada:
        for path in tenants.reads() + tenants.lists(tenants.ada.organization_id):
            assert (await ada.get(path)).status_code == 401, path


# --- legacy records ------------------------------------------------------------------------


async def test_ownerless_legacy_records_are_quarantined(tenants: Tenants):
    t = tenants
    legacy_reads = [
        f"/environments/{t.legacy.environment_id}",
        f"/investigations/{t.legacy.investigation_id}",
        f"/investigations/{t.legacy.investigation_id}/events?follow=false",
    ]
    for member in (t.ada, t.eve):
        async with api_client(member) as client:
            for path in legacy_reads:
                assert (await client.get(path)).status_code == 404, path
            created = await client.post(
                "/investigations",
                json={"environment_id": str(t.legacy.environment_id), "title": "x",
                      "question": "x"},
            )  # fmt: skip
            assert created.status_code == 404
            listed = await _ids(client, f"/investigations?organization_id={member.organization_id}")
            assert str(t.legacy.investigation_id) not in listed


# --- configuration boundary ----------------------------------------------------------------


async def test_tenant_environments_never_run_on_process_credentials(
    tenants: Tenants, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    t = tenants
    url = f"/investigations/{t.a.investigation_id}/start"
    async with api_client(t.ada) as ada:
        refused = await ada.post(url)
        assert refused.status_code == 409 and "configuration required" in refused.json()["detail"]
        follow_up = await ada.post(
            f"/investigations/{t.a.investigation_id}/turns", json={"question": "x"}
        )
        assert follow_up.status_code == 409
        # Only an operator grant changes that, where the server permits grants; then the
        # (disabled) runner is the limit.
        await session.execute(
            update(Environment)
            .where(Environment.id == t.a.environment_id)
            .values(process_credentials=True)
        )
        refused = await ada.post(url)
        assert refused.status_code == 409
        assert "process credentials not permitted" in refused.json()["detail"]
        allow_process_credentials(monkeypatch)
        assert (await ada.post(url)).status_code == 503


# --- database invariants -------------------------------------------------------------------


async def _check_now(session: AsyncSession) -> None:
    # The tenant trigger is deferred to commit; tests never commit, so force it.
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))


async def test_database_keeps_investigations_inside_one_organization(
    tenants: Tenants, session: AsyncSession
):
    t = tenants
    # Check the fixture's own rows first, outside the savepoints below, so each block's
    # failure is its own.
    await _check_now(session)
    foreign = Investigation(
        title="x", question="x", trigger_type=TriggerType.MANUAL,
        organization_id=t.eve.organization_id,
    )  # fmt: skip
    with pytest.raises(IntegrityError, match="different organizations"):
        async with session.begin_nested():
            session.add(foreign)
            await session.flush()
            session.add(
                InvestigationEnvironment(
                    investigation_id=foreign.id, environment_id=t.a.environment_id
                )
            )
            await session.flush()
            await _check_now(session)

    with pytest.raises(IntegrityError, match="cannot change"):
        async with session.begin_nested():
            await session.execute(
                update(Environment)
                .where(Environment.id == t.a.environment_id)
                .values(organization_id=t.eve.organization_id, owner_user_id=t.eve.user_id)
            )
            await _check_now(session)

    with pytest.raises(IntegrityError, match="owner_with_organization"):
        async with session.begin_nested():
            session.add(
                Environment(
                    name="orphan",
                    type=EnvironmentType.LOCAL,
                    organization_id=t.ada.organization_id,
                )  # fmt: skip
            )
            await session.flush()


# --- operator adoption of legacy records ---------------------------------------------------


async def test_adoption_is_explicit_audited_and_operator_only(
    tenants: Tenants, session: AsyncSession
):
    t = tenants
    assert not [r.path for r in app.routes if "adopt" in getattr(r, "path", "")]

    def plan(owner: uuid.UUID, *envs: uuid.UUID):
        return adoption.plan_adoption(
            session,
            environment_ids=list(envs),
            organization_id=t.ada.organization_id,
            owner_user_id=owner,
            process_credentials=True,
        )

    # The owner must already be a member; owned environments cannot be taken over.
    assert any(
        "not a member" in p for p in (await plan(t.eve.user_id, t.legacy.environment_id)).problems
    )
    assert any(
        "already belongs" in p for p in (await plan(t.ada.user_id, t.a.environment_id)).problems
    )
    # An investigation spanning another legacy environment would be split: refused.
    spare = Environment(name=f"spare-{uuid.uuid4().hex[:6]}", type=EnvironmentType.LOCAL)
    session.add(spare)
    session.add(
        InvestigationEnvironment(investigation_id=t.legacy.investigation_id, environment=spare)
    )
    await session.flush()
    split = await plan(t.ada.user_id, t.legacy.environment_id)
    assert any("not being adopted" in p for p in split.problems)
    with pytest.raises(adoption.AdoptionRefused):
        await adoption.adopt(session, split, actor="test")

    whole = await plan(t.ada.user_id, t.legacy.environment_id, spare.id)
    assert whole.problems == [] and whole.investigation_ids == [t.legacy.investigation_id]
    event = await adoption.adopt(session, whole, actor="Operator (os:test)")
    await _check_now(session)

    environment = await session.get(Environment, t.legacy.environment_id)
    assert (environment.organization_id, environment.owner_user_id) == (
        t.ada.organization_id, t.ada.user_id,
    )  # fmt: skip
    assert environment.process_credentials is True
    stored = await session.get(AuditEvent, event.id)
    assert stored.action == "legacy.adopt" and stored.actor == "Operator (os:test)"
    assert stored.details_json["investigation_ids"] == [str(t.legacy.investigation_id)]
    async with api_client(t.ada) as ada:
        assert (await ada.get(f"/investigations/{t.legacy.investigation_id}")).status_code == 200
    async with api_client(t.eve) as eve:
        assert (await eve.get(f"/investigations/{t.legacy.investigation_id}")).status_code == 404
