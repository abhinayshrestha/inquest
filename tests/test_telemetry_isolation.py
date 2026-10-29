"""Phase 7D-2B (audit H2): SigNoz connection isolation, through the API and the services.

Organization A (Ada) and B (Eve), each with environments claiming the same host and
service names, separate mock credentials, and a mock SigNoz Cloud workspace that applies
the documented key-scoped roles. Rolled-back sessions; no provider is contacted.
"""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.credentials import provider_client
from src.auth.settings import load_auth_settings
from src.credentials import crypto, resolver
from src.credentials.resolver import CredentialRefused, environment_credentials
from src.credentials.settings import load_credential_config
from src.credentials.validation import CredentialValidationFailed
from src.db.session import get_session
from src.integrations.signoz import SigNozSettings, builders
from src.integrations.signoz.schemas import (
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    Scope,
    SpanSearchRequest,
    TraceRequest,
    TraceStatsRequest,
)
from src.main import app
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentType,
    SigNozTenantKey,
    TelemetryIsolation,
    User,
)
from src.services import credentials, telemetry_keys
from tests.credential_fixtures import (
    ANY_KEY,
    SIGNOZ_ORIGIN,
    SIGNOZ_SECRET,
    CloudWorkspace,
    allow_process_credentials,
    bind_tenant_keys,
    configure_credentials,
    store_before_d037,
    store_credentials,
    verify_dedicated,
)
from tests.tenancy import Member, api_client, configure_auth, make_member

pytestmark = pytest.mark.usefixtures("credential_storage")

SCOPE = {"signoz": {"hosts": ["web-1"], "services": ["checkout"]}}
KEY_A, KEY_B = "key-a", "key-b"
API_A, API_B = f"{SIGNOZ_SECRET}-A", f"{SIGNOZ_SECRET}-B"


@pytest.fixture
def credential_storage(monkeypatch: pytest.MonkeyPatch):
    configure_credentials(monkeypatch, telemetry=[SIGNOZ_ORIGIN, "http://127.0.0.1:9999"])
    yield
    load_credential_config.cache_clear()


@dataclass
class Orgs:
    ada: Member
    eve: Member
    a: Environment
    b: Environment


@pytest.fixture
async def orgs(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Orgs]:
    configure_auth(monkeypatch)
    ada = await make_member(session, name="ada")
    eve = await make_member(session, name="eve")
    a = await _environment(session, ada)
    b = await _environment(session, eve)
    app.dependency_overrides[get_session] = lambda: session
    yield Orgs(ada, eve, a, b)
    app.dependency_overrides.clear()
    load_auth_settings.cache_clear()


async def _environment(session: AsyncSession, owner: Member) -> Environment:
    environment = Environment(
        name=f"env-{uuid.uuid4().hex[:8]}",
        type=EnvironmentType.LOCAL,
        metadata_json=SCOPE,
        **owner.owns,
    )
    session.add(environment)
    await session.flush()
    # Nebius is set already; these tests are about the SigNoz connection.
    await store_credentials(session, owner.user_id, environment.id, kinds=(CredentialKind.NEBIUS,))
    return environment


def provider(handler) -> None:
    app.dependency_overrides[provider_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )


async def put_signoz(member: Member, environment: Environment, **body) -> httpx.Response:
    async with api_client(member) as c:
        return await c.put(
            f"/environments/{environment.id}/credentials/signoz",
            json={"base_url": SIGNOZ_ORIGIN} | body,
        )


async def _row(session: AsyncSession, environment: Environment) -> EnvironmentCredential | None:
    return await session.get(
        EnvironmentCredential, (environment.id, CredentialKind.SIGNOZ), populate_existing=True
    )


def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver, "get_settings", lambda: SimpleNamespace(app_env="production"))


def assert_discloses_nothing(response: httpx.Response, *private: str) -> None:
    for value in (SIGNOZ_ORIGIN, "signoz.test", KEY_A, KEY_B, "svc-tenant", "web-1", *private):
        assert value not in response.text


# --- equivalent destinations ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("https://signoz.example.com", "https://SigNoz.Example.COM:443/"),
        ("https://signoz.example.com/base", "https://signoz.example.com./base//"),
        ("https://signoz.example.com/base", "https://signoz.example.com/x/../base/."),
        ("https://signoz.example.com/base", "https://signoz.example.com/%62ase"),
        ("http://127.0.0.1:9999", "http://2130706433:9999"),
        ("http://127.0.0.1:9999", "http://0x7f000001:9999/"),
        ("http://127.0.0.1:9999", "http://127.1:9999"),
        ("http://127.0.0.1:9999", "http://[::ffff:127.0.0.1]:9999"),
        ("http://[::1]:9999", "http://[0:0:0:0:0:0:0:1]:9999"),
    ],
)
def test_equivalent_spellings_share_a_fingerprint(first: str, second: str):
    config = load_credential_config()
    assert crypto.fingerprint(config, first) == crypto.fingerprint(config, second)


def test_other_hosts_and_paths_do_not():
    config = load_credential_config()
    base = crypto.fingerprint(config, "https://signoz.example.com/base")
    for other in (
        "https://signoz.example.org/base",
        "https://signoz.example.com/other",
        "https://signoz.example.com:8443/base",
        "http://signoz.example.com/base",
        # Another DNS name for the same backend cannot be recognized: fingerprints detect
        # reuse, they never prove isolation (provider-scoped mode does not rely on them).
        "https://alias.example.net/base",
    ):
        assert crypto.fingerprint(config, other) != base


# --- dedicated connections -----------------------------------------------------------------


async def test_another_organizations_dedicated_connection_is_refused_and_left_alone(
    orgs: Orgs, session: AsyncSession
):
    workspace = CloudWorkspace({API_A: None, API_B: None})
    provider(workspace.handler)
    assert (await put_signoz(orgs.ada, orgs.a, api_key=API_A)).status_code == 200
    before = await _row(session, orgs.a)
    assert before is not None
    stored = (before.ciphertext, before.updated_at, before.isolation, before.owner_user_id)

    for spelling in (SIGNOZ_ORIGIN, "http://SIGNOZ.test:8080/", "http://signoz.test.:8080/x/.."):
        response = await put_signoz(orgs.eve, orgs.b, base_url=spelling, api_key=API_B)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "connection_not_isolated"
        assert_discloses_nothing(response, str(orgs.a.id), str(orgs.ada.organization_id))

    assert await _row(session, orgs.b) is None
    after = await _row(session, orgs.a)
    assert after is not None
    assert (after.ciphertext, after.updated_at, after.isolation, after.owner_user_id) == stored
    async with api_client(orgs.ada) as ada:
        detail = (await ada.get(f"/environments/{orgs.a.id}")).json()
    assert detail["credentials"] == "environment" and detail["scope"]["problems"] == []


async def test_numeric_spellings_of_a_used_address_are_refused(orgs: Orgs, session: AsyncSession):
    """Service level: these spellings fail the destination policy at the API already."""
    eve = await session.get(User, orgs.eve.user_id)
    await store_credentials(
        session, orgs.ada.user_id, orgs.a.id, signoz_url="http://127.0.0.1:9999"
    )
    for spelling in ("http://2130706433:9999", "http://0x7f000001:9999", "http://127.1:9999"):
        with pytest.raises(CredentialValidationFailed) as refused:
            await credentials.store_credential(
                session, orgs.b.id, eve, CredentialKind.SIGNOZ,  # type: ignore[arg-type]
                secret={"api_key": API_B, "base_url": spelling}, public={},
                config=load_credential_config(), isolation=TelemetryIsolation.DEDICATED,
            )  # fmt: skip
        assert refused.value.code == "connection_not_isolated"


async def test_one_organization_may_reuse_its_own_connection(orgs: Orgs, session: AsyncSession):
    provider(CloudWorkspace({API_A: None}).handler)
    second = await _environment(session, orgs.ada)
    assert (await put_signoz(orgs.ada, orgs.a, api_key=API_A)).status_code == 200
    assert (await put_signoz(orgs.ada, second, api_key=API_A)).status_code == 200
    # Same organization, same identities: reported as ambiguous, as before (attribution).
    async with api_client(orgs.ada) as ada:
        problems = (await ada.get(f"/environments/{orgs.a.id}")).json()["scope"]["problems"]
    assert {p["code"] for p in problems} == {"policy.scope_ambiguous"}


# --- provider-scoped connections -----------------------------------------------------------


async def test_provider_scoped_needs_operator_assigned_keys_before_any_request(orgs: Orgs):
    workspace = CloudWorkspace({API_A: {KEY_A}})
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return workspace.handler(request)

    provider(handler)
    response = await put_signoz(orgs.ada, orgs.a, api_key=API_A, isolation="provider_scoped")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "tenant_keys_missing"
    assert seen == []  # nothing about the workspace was revealed or asked
    # The ingestion key id is never something the owner can send.
    response = await put_signoz(
        orgs.ada, orgs.a, api_key=API_A, isolation="provider_scoped", key_ids=[KEY_A]
    )
    assert response.status_code == 422 and KEY_A not in response.text


async def test_a_confined_key_is_verified_and_stored(orgs: Orgs, session: AsyncSession):
    workspace = CloudWorkspace({API_A: {KEY_A}})
    provider(workspace.handler)
    await bind_tenant_keys(session, orgs.ada.organization_id, SIGNOZ_ORIGIN, KEY_A)

    response = await put_signoz(orgs.ada, orgs.a, api_key=API_A, isolation="provider_scoped")

    assert response.status_code == 200, response.text
    assert response.json()["isolation"] == "provider_scoped"
    # Its own key answered; no filter, a foreign key, PromQL and ClickHouse SQL were denied.
    assert workspace.admitted == [(API_A, (KEY_A,))]
    assert workspace.denied == [API_A] * 4
    resolved = await environment_credentials(session, orgs.a)
    assert (resolved.isolation, resolved.tenant_key_ids) == (
        TelemetryIsolation.PROVIDER_SCOPED,
        (KEY_A,),
    )


@pytest.mark.parametrize(
    ("role", "code"),
    [
        (None, "scope_not_enforced"),  # a managed role: reads every tenant
        (ANY_KEY, "scope_not_enforced"),  # any ingestion key: not a tenant boundary
        ({KEY_A, KEY_B}, "scope_not_enforced"),  # also another tenant's key
        ({KEY_B}, "auth_failed"),  # someone else's key, not this organization's
    ],
)
async def test_keys_not_confined_to_the_organization_are_refused(
    orgs: Orgs, session: AsyncSession, role, code: str
):
    workspace = CloudWorkspace({API_A: role})
    provider(workspace.handler)
    await bind_tenant_keys(session, orgs.ada.organization_id, SIGNOZ_ORIGIN, KEY_A)
    await bind_tenant_keys(session, orgs.eve.organization_id, SIGNOZ_ORIGIN, KEY_B)

    response = await put_signoz(orgs.ada, orgs.a, api_key=API_A, isolation="provider_scoped")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert_discloses_nothing(response, API_A)
    assert await _row(session, orgs.a) is None


async def test_organizations_on_one_workspace_are_scoped_and_harmless(
    orgs: Orgs, session: AsyncSession
):
    workspace = CloudWorkspace({API_A: {KEY_A}, API_B: {KEY_B}})
    provider(workspace.handler)
    await bind_tenant_keys(session, orgs.ada.organization_id, SIGNOZ_ORIGIN, KEY_A)
    put = await put_signoz(orgs.ada, orgs.a, api_key=API_A, isolation="provider_scoped")
    assert put.status_code == 200
    stored = (await _row(session, orgs.a)).updated_at  # type: ignore[union-attr]
    await bind_tenant_keys(session, orgs.eve.organization_id, SIGNOZ_ORIGIN, KEY_B)

    response = await put_signoz(orgs.eve, orgs.b, api_key=API_B, isolation="provider_scoped")

    assert response.status_code == 200
    assert (await _row(session, orgs.a)).updated_at == stored  # type: ignore[union-attr]
    for member, environment in ((orgs.ada, orgs.a), (orgs.eve, orgs.b)):
        async with api_client(member) as c:
            detail = (await c.get(f"/environments/{environment.id}")).json()
        # Both claim web-1/checkout; neither is made ambiguous by the other organization.
        assert detail["credentials"] == "environment" and detail["scope"]["problems"] == []


async def test_an_unscoped_key_on_a_shared_workspace_is_refused(orgs: Orgs, session: AsyncSession):
    """A dedicated connection to a workspace with other organizations' assigned keys reads
    them all: refused (D038), the provider-scoped peer unaffected."""
    await store_credentials(session, orgs.ada.user_id, orgs.a.id)  # dedicated, before keys
    await bind_tenant_keys(session, orgs.eve.organization_id, SIGNOZ_ORIGIN, KEY_B)
    # The assignment alone makes it a shared workspace, before B saves anything.
    assert await _refused(session, orgs.a) == "telemetry_isolation_unverified"
    second = await _environment(session, orgs.ada)
    ada = await session.get(User, orgs.ada.user_id)
    with pytest.raises(CredentialValidationFailed) as refused:
        await credentials.store_credential(
            session, second.id, ada, CredentialKind.SIGNOZ,  # type: ignore[arg-type]
            secret={"api_key": API_A, "base_url": SIGNOZ_ORIGIN}, public={},
            config=load_credential_config(), isolation=TelemetryIsolation.DEDICATED,
        )  # fmt: skip
    assert refused.value.code == "connection_not_isolated"
    await store_credentials(
        session, orgs.eve.user_id, orgs.b.id, isolation=TelemetryIsolation.PROVIDER_SCOPED
    )
    assert await _refused(session, orgs.a) == "telemetry_isolation_unverified"
    assert (await environment_credentials(session, orgs.b)).tenant_key_ids == (KEY_B,)


async def test_removing_the_assignment_stops_the_connection(orgs: Orgs, session: AsyncSession):
    await bind_tenant_keys(session, orgs.ada.organization_id, SIGNOZ_ORIGIN, KEY_A)
    await store_credentials(
        session, orgs.ada.user_id, orgs.a.id, isolation=TelemetryIsolation.PROVIDER_SCOPED
    )
    assert (await environment_credentials(session, orgs.a)).tenant_key_ids == (KEY_A,)
    for row in await session.scalars(select(SigNozTenantKey)):
        await session.delete(row)
    await session.flush()
    with pytest.raises(CredentialRefused) as refused:
        await environment_credentials(session, orgs.a)
    assert refused.value.code == "telemetry_isolation_unverified"
    async with api_client(orgs.ada) as ada:
        detail = (await ada.get(f"/environments/{orgs.a.id}")).json()
    assert detail["credentials"] == "unavailable"


# --- operator assignments ------------------------------------------------------------------


async def test_assignments_are_operator_only_exclusive_and_audited(
    orgs: Orgs, session: AsyncSession
):
    config = load_credential_config()
    a, b = orgs.ada.organization_id, orgs.eve.organization_id
    dry = await telemetry_keys.bind(
        session, config, a, SIGNOZ_ORIGIN, [KEY_A], actor="op", apply=False
    )
    assert (dry.add, dry.applied) == ([KEY_A], False)
    assert await resolver.bound_key_ids(session, a, dry.fingerprint) == ()
    done = await telemetry_keys.bind(
        session, config, a, "http://SIGNOZ.test:8080/", [KEY_A], actor="op", apply=True
    )
    assert done.applied and await resolver.bound_key_ids(session, a, done.fingerprint) == (KEY_A,)
    # A key id belongs to one organization per workspace.
    with pytest.raises(telemetry_keys.BindingRefused):
        await telemetry_keys.bind(
            session, config, b, SIGNOZ_ORIGIN, [KEY_A], actor="op", apply=True
        )
    with pytest.raises(telemetry_keys.BindingRefused):
        await telemetry_keys.bind(session, config, b, SIGNOZ_ORIGIN, ["x' OR '1"], actor="op",
                                  apply=True)  # fmt: skip
    removed = await telemetry_keys.unbind(
        session, config, a, SIGNOZ_ORIGIN, [KEY_A], actor="op", apply=True
    )
    assert removed.remove == [KEY_A]
    audits = list(await session.scalars(select(AuditEvent).where(AuditEvent.organization_id == a)))
    telemetry = [e for e in audits if e.action.startswith("telemetry.")]
    # One transaction: same created_at, so compare without order.
    assert sorted(e.action for e in telemetry) == ["telemetry.bind", "telemetry.unbind"]
    assert all("signoz.test" not in str(e.details_json) for e in audits)


# --- hosted servers: allowlisted private origins need a grant ------------------------------


async def test_hosted_servers_allow_private_origins_only_to_granted_organizations(
    orgs: Orgs, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    provider(CloudWorkspace({API_A: None, API_B: None}).handler)
    await store_credentials(session, orgs.ada.user_id, orgs.a.id)  # saved while local
    production(monkeypatch)

    response = await put_signoz(orgs.eve, orgs.b, api_key=API_B, base_url="http://127.0.0.1:9999")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "destination_not_allowed"
    with pytest.raises(CredentialRefused) as refused:
        await environment_credentials(session, orgs.a)
    assert refused.value.code == "destination_not_allowed"

    monkeypatch.setenv(
        "INQUEST_TELEMETRY_ORIGIN_ORGANIZATIONS",
        f'{{"{SIGNOZ_ORIGIN}": ["{orgs.ada.organization_id}"]}}',
    )
    load_credential_config.cache_clear()
    # A granted origin is a network decision; isolation still needs the verification.
    with pytest.raises(CredentialRefused) as refused:
        await environment_credentials(session, orgs.a)
    assert refused.value.code == "telemetry_isolation_unverified"
    await verify_dedicated(session, orgs.ada.organization_id, SIGNOZ_ORIGIN)
    resolved = await environment_credentials(session, orgs.a)
    assert resolved.config is not None
    assert resolved.config.telemetry_origins == {"http://signoz.test:8080"}


# --- the mandatory tenant filter -----------------------------------------------------------

TIME = {
    "start": "2026-10-28T10:00:00+00:00",
    "end": "2026-10-28T10:30:00+00:00",
}
HOST_SCOPE = Scope(identity="host", values=["web-1"])
SERVICE_SCOPE = Scope(identity="service", values=["checkout"])
BODIES = {
    "query_metrics": lambda: builders.metric_query(
        MetricQueryRequest(time_range=TIME, metric="cpu_utilization"), HOST_SCOPE
    )[0],
    "search_logs": lambda: builders.log_search_query(LogSearchRequest(time_range=TIME), HOST_SCOPE),
    "log_volume": lambda: builders.log_volume_query(LogVolumeRequest(time_range=TIME), HOST_SCOPE)[
        0
    ],
    "search_spans": lambda: builders.span_search_query(
        SpanSearchRequest(time_range=TIME), SERVICE_SCOPE
    ),
    "trace_stats": lambda: builders.trace_stats_query(
        TraceStatsRequest(time_range=TIME, group_by="operation"), SERVICE_SCOPE
    ),
    "get_trace": lambda: builders.trace_query(
        TraceRequest(time_range=TIME, trace_id="4bf92f3577b34da6a3ce929d0e0e4736"), SERVICE_SCOPE
    ),
}


def _ask(workspace: CloudWorkspace, api_key: str, body: dict) -> int:
    status, _ = workspace({"headers": {"signoz-api-key": api_key}, "body": json.dumps(body)})
    return status


@pytest.mark.parametrize("tool", sorted(BODIES))
def test_every_tool_query_is_confined_to_the_organizations_keys(tool: str):
    workspace = CloudWorkspace({API_A: {KEY_A}})
    body = BODIES[tool]()
    # Unfiltered (a bug, or an unrestricted path) is denied by a scoped role.
    assert _ask(workspace, API_A, body) == 403
    scoped = builders.tenant_scoped(body, (KEY_A,))
    assert _ask(workspace, API_A, scoped) == 200
    # Another tenant's key id, or both, is denied.
    assert _ask(workspace, API_A, builders.tenant_scoped(body, (KEY_B,))) == 403
    assert _ask(workspace, API_A, builders.tenant_scoped(body, (KEY_A, KEY_B))) == 403
    # Every builder query carries the clause at the top level, formulas included.
    for query in scoped["compositeQuery"]["queries"]:
        if query["type"] == "builder_query":
            expression = query["spec"]["filter"]["expression"]
            assert expression.startswith("signoz.workspace.key.id = 'key-a'")
    assert body != scoped  # the builder's own body is left as it was


def test_queries_that_cannot_be_confined_are_never_sent():
    for kind in ("promql", "clickhouse_sql", "builder_trace_operator", "builder_join", None):
        body = {"compositeQuery": {"queries": [{"type": kind, "spec": {"query": "x"}}]}}
        with pytest.raises(ValueError):
            builders.tenant_scoped(body, (KEY_A,))
    with pytest.raises(ValueError):
        builders.tenant_scoped({"compositeQuery": {"queries": []}}, (KEY_A,))
    for bad in ((), ("x' OR signoz.workspace.key.id != '",), ("",)):
        with pytest.raises(ValueError):
            builders.tenant_clause(bad)  # type: ignore[arg-type]
    # A model-chosen condition on the key attribute stays inside the parenthesized part.
    body = {"compositeQuery": {"queries": [{"type": "builder_query", "spec": {
        "signal": "logs", "filter": {"expression": "signoz.workspace.key.id = 'key-b'"},
    }}]}}  # fmt: skip
    expression = builders.tenant_scoped(body, (KEY_A,))["compositeQuery"]["queries"][0]["spec"]
    assert expression["filter"]["expression"] == (
        "signoz.workspace.key.id = 'key-a' AND (signoz.workspace.key.id = 'key-b')"
    )


# --- closeout (D038): legacy reuse, operator verification, aliases, development grants -----

ALIAS = "http://alias.signoz.test:8080"  # another DNS name for the same mock backend


def grant(monkeypatch: pytest.MonkeyPatch, orgs: Orgs, *origins: str) -> None:
    """Hosted network policy: both organizations may reach these origins, so what is
    tested is the isolation policy alone."""
    configure_credentials(monkeypatch, telemetry=list(origins))
    members = [str(orgs.ada.organization_id), str(orgs.eve.organization_id)]
    monkeypatch.setenv(
        "INQUEST_TELEMETRY_ORIGIN_ORGANIZATIONS",
        json.dumps({origin: members for origin in origins}),
    )
    load_credential_config.cache_clear()


async def _refused(session: AsyncSession, environment: Environment) -> str:
    with pytest.raises(CredentialRefused) as refused:
        await environment_credentials(session, environment)
    return refused.value.code


async def _detail(member: Member, environment: Environment, *private: str) -> dict:
    """The environment's detail for its own member; names nothing of the others."""
    async with api_client(member) as c:
        response = await c.get(f"/environments/{environment.id}")
    for value in (SIGNOZ_ORIGIN, "signoz.test", *private):
        assert value not in response.text
    return response.json()


async def test_legacy_cross_organization_reuse_fails_closed_without_disclosure(
    orgs: Orgs, session: AsyncSession
):
    carol = await make_member(session, name="carol")
    c = await _environment(session, carol)
    await store_credentials(session, carol.user_id, c.id, signoz_url="http://127.0.0.1:9999")
    await store_before_d037(session, orgs.ada.user_id, orgs.a.id)
    await store_before_d037(session, orgs.eve.user_id, orgs.b.id, tag="B")
    signoz = select(EnvironmentCredential).where(EnvironmentCredential.kind == "signoz")
    stored = {(r.environment_id, r.ciphertext, r.updated_at) for r in await session.scalars(signoz)}

    pairs = ((orgs.ada, orgs.a, orgs.eve, orgs.b), (orgs.eve, orgs.b, orgs.ada, orgs.a))
    for member, environment, other, theirs in pairs:
        assert await _refused(session, environment) == "telemetry_isolation_unverified"
        detail = await _detail(member, environment, str(other.organization_id), str(theirs.id))
        assert detail["credentials"] == "unavailable"
    # An unrelated organization's connection is untouched; nothing was deleted or changed.
    assert (await environment_credentials(session, c)).isolation is TelemetryIsolation.DEDICATED
    assert {
        (r.environment_id, r.ciphertext, r.updated_at)
        for r in await session.scalars(signoz.execution_options(populate_existing=True))
    } == stored

    # Operators can find them: the review names the reason, the startup log only counts.
    reviewed = {r.environment_id: r.problem for r in await resolver.isolation_review(session)}
    assert reviewed[orgs.a.id] == reviewed[orgs.b.id] == "connection_shared"
    assert reviewed[c.id] is None
    report = " ".join(await resolver.startup_problems(session))
    assert "2 SigNoz connections are refused" in report and "review" in report
    for private in (str(orgs.a.id), str(orgs.ada.organization_id), "signoz.test"):
        assert private not in report


async def test_the_operator_resolves_legacy_reuse_for_one_organization(
    orgs: Orgs, session: AsyncSession
):
    config = load_credential_config()
    a, b = orgs.ada.organization_id, orgs.eve.organization_id
    await store_before_d037(session, orgs.ada.user_id, orgs.a.id)
    await store_before_d037(session, orgs.eve.user_id, orgs.b.id, tag="B")

    dry = await telemetry_keys.verify_dedicated(
        session, config, a, SIGNOZ_ORIGIN, actor="op", apply=False
    )
    assert (dry.change, dry.others_refused, dry.applied) == (True, 1, False)
    for environment in (orgs.a, orgs.b):
        await session.refresh(environment)  # expired by the dry run's rollback
        assert await _refused(session, environment) == "telemetry_isolation_unverified"

    done = await telemetry_keys.verify_dedicated(
        session, config, a, "http://SIGNOZ.test:8080/", actor="op", apply=True
    )
    assert done.applied
    # Verified: A's connection runs; B's reuse of it stays refused, and B learns nothing.
    assert (await environment_credentials(session, orgs.a)).source == "environment"
    assert await _refused(session, orgs.b) == "telemetry_isolation_unverified"
    assert (await _detail(orgs.eve, orgs.b, str(a), str(orgs.a.id)))["credentials"] == (
        "unavailable"
    )
    # One organization per verified connection, and never a workspace shared by keys.
    with pytest.raises(telemetry_keys.BindingRefused):
        await telemetry_keys.verify_dedicated(
            session, config, b, SIGNOZ_ORIGIN, actor="op", apply=True
        )
    with pytest.raises(telemetry_keys.BindingRefused):
        await telemetry_keys.bind(session, config, b, SIGNOZ_ORIGIN, [KEY_B], actor="op",
                                  apply=True)  # fmt: skip
    await bind_tenant_keys(session, b, "http://127.0.0.1:9999", KEY_B)
    with pytest.raises(telemetry_keys.BindingRefused):
        await telemetry_keys.verify_dedicated(
            session, config, a, "http://127.0.0.1:9999", actor="op", apply=True
        )
    # B's owner replaces the connection with B's own: B runs again, A is unaffected.
    await store_credentials(session, orgs.eve.user_id, orgs.b.id, tag="B",
                            signoz_url="https://telemetry-b.example.com")  # fmt: skip
    assert (await environment_credentials(session, orgs.b)).source == "environment"
    assert (await environment_credentials(session, orgs.a)).source == "environment"

    removed = await telemetry_keys.unverify_dedicated(
        session, config, a, SIGNOZ_ORIGIN, actor="op", apply=True
    )
    assert removed.applied
    actions = sorted(
        e.action
        for e in await session.scalars(select(AuditEvent).where(AuditEvent.organization_id == a))
        if e.action.startswith("telemetry.")
    )
    assert actions == ["telemetry.unverify_dedicated", "telemetry.verify_dedicated"]
    audits = await session.scalars(select(AuditEvent).where(AuditEvent.organization_id == a))
    assert all("signoz.test" not in str(e.details_json) for e in audits)


async def test_hosted_dedicated_connections_need_operator_verification(
    orgs: Orgs, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    hosts: list[str] = []
    workspace = CloudWorkspace({API_A: None, API_B: None})

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return workspace.handler(request)

    provider(handler)
    production(monkeypatch)
    grant(monkeypatch, orgs, SIGNOZ_ORIGIN, ALIAS)

    response = await put_signoz(orgs.ada, orgs.a, api_key=API_A)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "dedication_unverified"
    assert await _row(session, orgs.a) is None
    await verify_dedicated(session, orgs.ada.organization_id, SIGNOZ_ORIGIN)
    assert (await put_signoz(orgs.ada, orgs.a, api_key=API_A)).status_code == 200

    # Another DNS name for the same backend, or the same name: the same answer, which
    # says nothing about A. A URL an owner types is never proof of dedication.
    for url in (ALIAS, SIGNOZ_ORIGIN):
        response = await put_signoz(orgs.eve, orgs.b, base_url=url, api_key=API_B)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "dedication_unverified"
        assert_discloses_nothing(response, str(orgs.ada.organization_id), str(orgs.a.id))
    assert await _row(session, orgs.b) is None
    assert {"signoz.test", "alias.signoz.test"} <= set(hosts)  # one backend, two names
    detail = await _detail(orgs.ada, orgs.a)
    assert detail["credentials"] == "environment" and detail["scope"]["problems"] == []


async def test_aliases_of_one_backend_are_not_recognized_and_need_verification_when_hosted(
    orgs: Orgs, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    hosts: list[str] = []
    workspace = CloudWorkspace({API_A: None, API_B: None})

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return workspace.handler(request)

    provider(handler)
    configure_credentials(monkeypatch, telemetry=[SIGNOZ_ORIGIN, ALIAS])
    # Local single-tenant development: the alias is a different fingerprint, so both are
    # accepted although they reach one backend. This is the limit D038 closes for hosting.
    assert (await put_signoz(orgs.ada, orgs.a, api_key=API_A)).status_code == 200
    assert (await put_signoz(orgs.eve, orgs.b, base_url=ALIAS, api_key=API_B)).status_code == 200
    assert hosts == ["signoz.test", "alias.signoz.test"]

    production(monkeypatch)
    grant(monkeypatch, orgs, SIGNOZ_ORIGIN, ALIAS)
    for environment in (orgs.a, orgs.b):
        assert await _refused(session, environment) == "telemetry_isolation_unverified"
    await verify_dedicated(session, orgs.ada.organization_id, SIGNOZ_ORIGIN)
    assert (await environment_credentials(session, orgs.a)).source == "environment"
    assert await _refused(session, orgs.b) == "telemetry_isolation_unverified"
    reviewed = {r.environment_id: r.problem for r in await resolver.isolation_review(session)}
    assert (reviewed[orgs.a.id], reviewed[orgs.b.id]) == (None, "dedication_unverified")


async def test_development_grants_never_reach_another_organizations_connection(
    orgs: Orgs, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    allow_process_credentials(monkeypatch)
    monkeypatch.setattr(
        resolver,
        "load_signoz_settings",
        lambda: SigNozSettings(base_url=SIGNOZ_ORIGIN, api_key="x" * 10),
    )

    async def granted(owner: Member) -> Environment:
        environment = Environment(name=f"grant-{uuid.uuid4().hex[:8]}", type="local",
                                  process_credentials=True, **owner.owns)  # fmt: skip
        session.add(environment)
        await session.flush()
        return environment

    # The operator's own development connection, local only: grants may share it.
    ga, gb = await granted(orgs.ada), await granted(orgs.eve)
    for environment in (ga, gb):
        assert (await environment_credentials(session, environment)).source == "process"
    gb.process_credentials = False
    await session.flush()

    # It is not a tenant's connection: B cannot claim it as dedicated while A's grant
    # reaches it.
    eve = await session.get(User, orgs.eve.user_id)
    with pytest.raises(CredentialValidationFailed) as refused:
        await credentials.store_credential(
            session, orgs.b.id, eve, CredentialKind.SIGNOZ,  # type: ignore[arg-type]
            secret={"api_key": API_B, "base_url": SIGNOZ_ORIGIN}, public={},
            config=load_credential_config(), isolation=TelemetryIsolation.DEDICATED,
        )  # fmt: skip
    assert refused.value.code == "connection_not_isolated"

    # Saved before D037: both sides fail closed. A refused own connection never falls
    # back to a grant.
    await store_before_d037(session, orgs.eve.user_id, orgs.b.id, tag="B")
    orgs.b.process_credentials = True
    await session.flush()
    assert await _refused(session, orgs.b) == "telemetry_isolation_unverified"
    assert await _refused(session, ga) == "telemetry_isolation_unverified"
