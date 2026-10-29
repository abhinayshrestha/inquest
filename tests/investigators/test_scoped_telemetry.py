"""Phase 7D-2B: provider-scoped SigNoz connections at run time, through the real Tool
Executor, resolver and destination-guarded client, against a mock SigNoz Cloud workspace
(a real loopback server applying the documented key-scoped roles).

Organizations A and B share one workspace and claim the same host and service. Each has
its own API key (role: only its own ingestion key) and operator-assigned key id.
Committed rows, deleted afterwards.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from src.credentials.validation import CredentialValidationFailed
from src.db.session import SessionLocal
from src.models import (
    CredentialKind,
    Environment,
    EnvironmentCredential,
    Evidence,
    Investigation,
    SigNozTenantKey,
    TelemetryIsolation,
    ToolCall,
    ToolCallStatus,
)
from tests.credential_fixtures import (
    SIGNOZ_SECRET,
    CloudWorkspace,
    bind_tenant_keys,
    configure_credentials,
    signoz_server,
    store_committed,
)
from tests.investigators.test_runtime_credentials import owners  # noqa: F401
from tests.tenancy import Member
from tests.tools.conftest import World, make_world, replies, window  # noqa: F401

HOST, SERVICE = "abbyvm-QEMU-Virtual-Machine", "inquest-demo"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ARGUMENTS = {
    "signoz.query_metrics": {"metric": "cpu_utilization"},
    "signoz.search_logs": {},
    "signoz.log_volume": {},
    "signoz.search_spans": {},
    "signoz.trace_stats": {"group_by": "operation"},
    "signoz.get_trace": {"trace_id": TRACE_ID},
}


@dataclass
class Shared:
    workspace: CloudWorkspace
    origin: str
    seen: list[dict[str, Any]]
    tenants: list["Tenant"]


@dataclass
class Tenant:
    tag: str
    world: World
    owner: Member
    key_id: str
    api_key: str

    async def call(self, tool: str, extra: dict[str, Any] | None = None):
        handler, ignored = replies(500)
        context = self.world.context(organization_id=self.owner.organization_id)
        arguments = {"time_range": window(), **ARGUMENTS[tool], **(extra or {})}
        result = await self.world.run(tool, arguments, handler, context=context)
        # The test-only process connection is never used for an environment's own one.
        assert ignored == []
        return result


@pytest.fixture
async def shared(
    owners: list[Member],  # noqa: F811
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Shared]:
    workspace = CloudWorkspace({f"{SIGNOZ_SECRET}-A": {"key-a"}, f"{SIGNOZ_SECRET}-B": {"key-b"}})
    async with signoz_server(workspace) as (origin, seen):
        configure_credentials(monkeypatch, telemetry=[origin])
        found = []
        for tag, owner in (("A", owners[0]), ("B", owners[1])):
            world = await make_world(signoz={"hosts": [HOST], "services": [SERVICE]})
            async with SessionLocal() as s, s.begin():
                await s.execute(
                    update(Environment)
                    .where(Environment.id == world.environment_id)
                    .values(**owner.owns)
                )
                await s.execute(
                    update(Investigation)
                    .where(Investigation.id == world.investigation_id)
                    .values(organization_id=owner.organization_id)
                )
            async with SessionLocal() as s:
                await bind_tenant_keys(s, owner.organization_id, origin, f"key-{tag.lower()}")
                await s.commit()
            await store_committed(
                owner.user_id, world.environment_id, tag=tag, signoz_url=origin,
                isolation=TelemetryIsolation.PROVIDER_SCOPED,
            )  # fmt: skip
            found.append(Tenant(tag, world, owner, f"key-{tag.lower()}", f"{SIGNOZ_SECRET}-{tag}"))
        yield Shared(workspace, origin, seen, found)


def _filters(request: dict[str, Any]) -> list[str]:
    body = json.loads(request["body"])
    return [
        q["spec"]["filter"]["expression"]
        for q in body["compositeQuery"]["queries"]
        if q["type"] == "builder_query"
    ]


async def _texts(tenant: Tenant) -> str:
    """Everything stored for the tenant's assignment: evidence and tool call records."""
    assignment = tenant.world.assignment_id
    async with SessionLocal() as s:
        evidence = list(
            await s.scalars(select(Evidence).where(Evidence.assignment_id == assignment))
        )
        calls = list(await s.scalars(select(ToolCall).where(ToolCall.assignment_id == assignment)))
    return json.dumps(
        [(e.summary, e.data_json) for e in evidence]
        + [(c.result_summary, c.output_metadata_json) for c in calls],
        default=str,
    )


async def test_all_six_tools_read_only_their_organizations_telemetry(shared: Shared):
    workspace, seen, (a, b) = shared.workspace, shared.seen, shared.tenants
    for tool in ARGUMENTS:
        # Side by side: overlapping identities, one workspace, two tenants.
        result_a, result_b = await asyncio.gather(a.call(tool), b.call(tool))
        for result in (result_a, result_b):
            assert result.status is ToolCallStatus.SUCCEEDED, (tool, result.error)

    # Every request was confined by the provider to exactly the caller's own key.
    assert workspace.denied == []
    assert sorted(set(workspace.admitted)) == [
        (a.api_key, ("key-a",)),
        (b.api_key, ("key-b",)),
    ]
    for request in seen:
        mine = "key-a" if request["headers"]["signoz-api-key"] == a.api_key else "key-b"
        for expression in _filters(request):
            assert expression.startswith(f"signoz.workspace.key.id = '{mine}'")
    # What each organization stored holds its own tenant's telemetry, never the other's.
    text_a, text_b = await _texts(a), await _texts(b)
    assert "[tenant key-a]" in text_a and "key-b" not in text_a
    assert "[tenant key-b]" in text_b and "key-a" not in text_b


async def test_a_model_cannot_widen_the_tenant_filter(shared: Shared):
    workspace, seen, (a, _) = shared.workspace, shared.seen, shared.tenants
    sneaky = {"where": [{"key": "signoz.workspace.key.id", "op": "eq", "value": "key-b"}]}
    result = await a.call("signoz.search_logs", sneaky)
    # Refused by the tool's argument rules: the attribute is set by trusted code only.
    assert result.status is ToolCallStatus.REJECTED
    assert result.error.code == "policy.invalid_arguments"
    assert seen == [] and workspace.admitted == []


async def test_unassigned_keys_stop_queries_without_fallback(shared: Shared):
    seen, (a, b) = shared.seen, shared.tenants
    async with SessionLocal() as s, s.begin():
        await s.execute(
            delete(SigNozTenantKey).where(
                SigNozTenantKey.organization_id == a.owner.organization_id
            )
        )
    result = await a.call("signoz.trace_stats")
    assert result.status is ToolCallStatus.REJECTED
    assert result.error.code == "policy.telemetry_isolation_unverified"
    assert seen == []  # nothing sent, not even unscoped
    # B is unaffected by A's change.
    assert (await b.call("signoz.trace_stats")).status is ToolCallStatus.SUCCEEDED


async def test_rotated_keys_stay_confined(shared: Shared):
    workspace, seen, (a, _) = shared.workspace, shared.seen, shared.tenants
    rotated = f"{SIGNOZ_SECRET}-A2"
    workspace.roles[rotated] = {"key-a"}
    await store_committed(
        a.owner.user_id, a.world.environment_id, tag="A2", signoz_url=shared.origin,
        isolation=TelemetryIsolation.PROVIDER_SCOPED,
    )  # fmt: skip
    assert (await a.call("signoz.trace_stats")).status is ToolCallStatus.SUCCEEDED
    assert seen[-1]["headers"]["signoz-api-key"] == rotated
    assert _filters(seen[-1])[0].startswith("signoz.workspace.key.id = 'key-a'")
    # A rotation to a key that reads other tenants is denied by the provider at run time;
    # the executor reports the failure and never retries without the filter.
    workspace.roles[rotated] = {"key-b"}
    result = await a.call("signoz.trace_stats")
    assert result.status is ToolCallStatus.FAILED and result.error.code == "signoz_auth"
    assert "key-b" not in json.dumps(result.model_dump(mode="json"))


async def test_racing_dedicated_claims_admit_one_organization(
    owners: list[Member],  # noqa: F811
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    url = "http://127.0.0.1:9999"
    configure_credentials(monkeypatch, telemetry=[url])
    worlds = []
    for owner in owners:
        world = await make_world()
        async with SessionLocal() as s, s.begin():
            await s.execute(
                update(Environment)
                .where(Environment.id == world.environment_id)
                .values(**owner.owns)
            )
            await s.execute(
                update(Investigation)
                .where(Investigation.id == world.investigation_id)
                .values(organization_id=owner.organization_id)
            )
        worlds.append(world)

    async def claim(owner: Member, world: World) -> str:
        try:
            await store_committed(owner.user_id, world.environment_id, signoz_url=url,
                                  kinds=(CredentialKind.SIGNOZ,))  # fmt: skip
        except CredentialValidationFailed as exc:
            return exc.code
        return "stored"

    for _ in range(5):
        outcomes = await asyncio.gather(*(claim(o, w) for o, w in zip(owners, worlds, strict=True)))
        assert sorted(outcomes) == ["connection_not_isolated", "stored"]
        async with SessionLocal() as s:
            organizations = set(
                await s.scalars(
                    select(Environment.organization_id)
                    .join(EnvironmentCredential)
                    .where(EnvironmentCredential.kind == CredentialKind.SIGNOZ)
                    .where(Environment.id.in_([w.environment_id for w in worlds]))
                )
            )
        assert len(organizations) == 1  # never both organizations on one dedicated connection
        async with SessionLocal() as s, s.begin():
            await s.execute(
                delete(EnvironmentCredential).where(
                    EnvironmentCredential.environment_id.in_([w.environment_id for w in worlds])
                )
            )
