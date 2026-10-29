"""Phase 7D-1 security audit (report: docs/security-audit.md).

Two kinds of checks:

- regression tests for boundaries the audit verified but no existing test pinned down
  (every route's authentication and CSRF dependencies; cross-investigation references
  through alternate paths);
- tests that began as strict xfails reproducing findings and became regular tests when
  the findings were fixed (H1 in 7D-2A, H2 in 7D-2B).

Organization A: Ada (owner). Organization B: Eve (owner).
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx
import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Mount

from src.api.routes.credentials import provider_client
from src.auth.deps import SAFE_METHODS, require_csrf, require_user
from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal, engine, get_session
from src.investigators.refs import ref
from src.investigators.runner import investigation_lock
from src.main import app
from src.models import (
    Environment,
    EnvironmentCredential,
    EnvironmentType,
    Evidence,
    EvidenceType,
    Investigation,
    InvestigationAssignment,
    InvestigationReport,
    TelemetryIsolation,
    TriggerType,
)
from src.services import leases
from tests.credential_fixtures import (
    SIGNOZ_ORIGIN,
    SIGNOZ_SECRET,
    bind_tenant_keys,
    credential_storage,  # noqa: F401  (fixture)
    store_credentials,
)
from tests.records import add_assignment
from tests.tenancy import Member, api_client, committed_members, configure_auth, make_member

# Routes that must answer without a session: liveness and the sign-in flow itself.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/auth/session"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
}
# Writes that come before any session: starting an invitation sign-in (D042). They still
# pass the Origin check middleware, which covers every route.
PUBLIC_WRITES = {("POST", "/auth/login")}


def _calls(dependant: Dependant) -> set[Callable]:
    found = {d.call for d in dependant.dependencies if d.call is not None}
    for sub in dependant.dependencies:
        found |= _calls(sub)
    return found


def test_every_route_needs_a_session_and_every_write_needs_csrf():
    """Structural: the hand-written route lists in other tests miss routes added later.
    Nothing is mounted: MCP has its own loopback listener (D041)."""
    mounts = [r.path for r in app.routes if isinstance(r, Mount)]
    assert mounts == []
    checked = 0
    # Included routers are resolved lazily (FastAPI 0.143); contexts give each effective
    # route with its full path.
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.route, APIRoute):
            continue
        calls = _calls(context.route.dependant)
        for method in context.methods or ():
            if (method, context.path) in PUBLIC:
                assert method in SAFE_METHODS, context.path
                continue
            if (method, context.path) in PUBLIC_WRITES:
                continue
            checked += 1
            assert require_user in calls, f"{method} {context.path} has no session check"
            if method not in SAFE_METHODS:
                assert require_csrf in calls, f"{method} {context.path} has no CSRF check"
    assert checked > 30  # the enumeration really saw the API


# --- two organizations ---------------------------------------------------------------------


@dataclass
class World:
    ada: Member
    eve: Member
    a: InvestigationAssignment
    b: InvestigationAssignment
    evidence: Evidence


@pytest.fixture
async def world(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    configure_auth(monkeypatch)
    ada = await make_member(session, name="ada")
    eve = await make_member(session, name="eve")
    a = await add_assignment(session, tenant=ada)
    b = await add_assignment(session, tenant=eve)
    await session.flush()
    evidence = Evidence(
        assignment=a,
        evidence_type=EvidenceType.METRIC,
        source_provider="signoz",
        title="cpu",
        summary="cpu 97%",
    )
    session.add(evidence)
    for assignment in (a, b):
        session.add(
            InvestigationReport(
                investigation_id=assignment.investigation_id,
                turn_id=assignment.turn_id,
                version=1,
                report_json={"outcome": "inconclusive"},
            )
        )
    await session.flush()
    app.dependency_overrides[get_session] = lambda: session
    yield World(ada, eve, a, b, evidence)
    app.dependency_overrides.clear()
    load_auth_settings.cache_clear()


async def test_references_never_cross_investigations_through_alternate_paths(world: World):
    """Eve may read her own investigation, so these paths pass the investigation check;
    each must still refuse organization A's records named inside them."""
    a, b = world.a, world.b
    mine = f"/investigations/{b.investigation_id}"
    async with api_client(world.eve) as eve:
        # A's evidence ref and A's turn, asked for under Eve's own investigation.
        cited = await eve.get(f"{mine}/citations/{ref('evidence', world.evidence.id)}")
        assert cited.status_code == 404
        shown = await eve.get(f"{mine}/turns/{a.turn_id}/presentation")
        assert shown.status_code == 404
        # Report version 1 under Eve's id is Eve's report, never A's.
        report = (await eve.get(f"{mine}/reports/1")).json()
        assert report["turn_id"] == str(b.turn_id)
        # Filters naming A's environment inside Eve's organization select nothing.
        q = {
            "organization_id": str(world.eve.organization_id),
            "environment_id": str(a.environment_id),
        }
        assert (await eve.get("/investigations", params=q)).json()["items"] == []
        assert (await eve.get("/reports", params=q)).json()["items"] == []
        # A's organization as the selector is refused outright.
        theirs = {"organization_id": str(world.ada.organization_id)}
        for path in ("/investigations", "/reports", "/environments", "/overview"):
            assert (await eve.get(path, params=theirs)).status_code == 404, path
        # A replay cursor cannot reach another investigation's events.
        events = await eve.get(f"{mine}/events", params={"after": 0, "follow": "false"})
        assert str(a.investigation_id) not in events.text


# --- open findings (strict xfail) ----------------------------------------------------------

SCOPE = {"signoz": {"hosts": ["web-1"]}}


async def _scoped_environment(session: AsyncSession, member: Member) -> Environment:
    environment = Environment(
        name=f"env-{uuid.uuid4().hex[:8]}",
        type=EnvironmentType.LOCAL,
        metadata_json=SCOPE,
        **member.owns,
    )
    session.add(environment)
    await session.flush()
    return environment


@pytest.mark.usefixtures("credential_storage")
async def test_another_organization_cannot_block_or_observe_my_telemetry_scope(
    world: World, session: AsyncSession
):
    """SEC-H2 (fixed in 7D-2B): B's claim on A's host, on A's SigNoz, used to make A's
    host ambiguous (A's queries for it rejected) and told A someone else claims it. Now
    only environments of one organization can make each other's identities ambiguous."""
    mine = await _scoped_environment(session, world.ada)
    twin = await _scoped_environment(session, world.ada)
    theirs = await _scoped_environment(session, world.eve)
    await store_credentials(session, world.ada.user_id, mine.id, tag="A")
    await store_credentials(session, world.ada.user_id, twin.id, tag="A")

    async def problems() -> list[str]:
        async with api_client(world.ada) as ada:
            detail = (await ada.get(f"/environments/{mine.id}")).json()
        return [p["code"] for p in detail["scope"]["problems"]]

    # Within one organization the overlap is reported (attribution), as before.
    assert await problems() == ["policy.scope_ambiguous"]
    await session.execute(
        delete(EnvironmentCredential).where(EnvironmentCredential.environment_id == twin.id)
    )
    # B on the same backend, the only way still accepted: provider-scoped with its own keys.
    await bind_tenant_keys(session, world.eve.organization_id, SIGNOZ_ORIGIN, "key-b")
    await store_credentials(
        session, world.eve.user_id, theirs.id, tag="B",
        isolation=TelemetryIsolation.PROVIDER_SCOPED,
    )  # fmt: skip
    assert await problems() == []


@pytest.mark.usefixtures("credential_storage")
async def test_signoz_connection_of_another_organization_is_refused(
    world: World, session: AsyncSession
):
    """SEC-H2 (fixed in 7D-2B): a dedicated connection another organization already uses
    is refused, without saying whose, and A's configuration is untouched."""
    mine = await _scoped_environment(session, world.ada)
    await store_credentials(session, world.ada.user_id, mine.id, tag="A")
    theirs = await _scoped_environment(session, world.eve)
    query_ok = {"status": "success", "data": {"type": "scalar", "data": {"results": []}}}
    app.dependency_overrides[provider_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=query_ok)),
        follow_redirects=False,
    )
    async with api_client(world.eve) as eve:
        response = await eve.put(
            f"/environments/{theirs.id}/credentials/signoz",
            json={"base_url": SIGNOZ_ORIGIN, "api_key": f"{SIGNOZ_SECRET}-B"},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "connection_not_isolated"
    for private in ("signoz.test", str(mine.id), str(world.ada.organization_id), "web-1"):
        assert private not in response.text
    async with api_client(world.ada) as ada:
        detail = (await ada.get(f"/environments/{mine.id}")).json()
    assert detail["credentials"] == "environment" and detail["scope"]["problems"] == []


async def test_running_turns_cannot_starve_the_api_connection_pool(
    monkeypatch: pytest.MonkeyPatch,
):
    """SEC-H1 (fixed in 7D-2A): pool-size running turns (15 by default) used to hold one
    pooled connection each for their advisory lock and stop every API request. Run leases
    hold none; see tests/investigators/test_run_capacity.py for full runs."""
    capacity = engine.pool.size() + engine.pool._max_overflow  # type: ignore[attr-defined]
    monkeypatch.setenv("INQUEST_RUNNER_MAX_RUNNING", str(capacity))
    monkeypatch.setenv("INQUEST_RUNNER_MAX_RUNNING_PER_ORGANIZATION", str(capacity))
    leases.capacity_settings.cache_clear()
    configure_auth(monkeypatch)
    async with committed_members() as [member]:
        async with SessionLocal() as s:
            running = [
                Investigation(
                    title="t",
                    question="q",
                    trigger_type=TriggerType.MANUAL,
                    organization_id=member.organization_id,
                )
                for _ in range(capacity)
            ]
            s.add_all(running)
            await s.commit()
        try:
            async with AsyncExitStack() as held:
                for investigation in running:
                    await held.enter_async_context(investigation_lock(investigation.id))
                assert engine.pool.checkedout() == 0
                async with asyncio.timeout(3), SessionLocal() as s:
                    assert await s.scalar(text("SELECT 1")) == 1
        finally:
            async with SessionLocal() as s, s.begin():
                ids = [i.id for i in running]
                await s.execute(delete(Investigation).where(Investigation.id.in_(ids)))
            leases.capacity_settings.cache_clear()
