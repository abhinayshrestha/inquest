"""Phase 7C-2: runs use each environment's own credentials (D034). Committed rows; two
organizations with distinct Nebius keys and SigNoz connections. Models are scripted (the
factory is replaced and records what each step was built from); SigNoz is a real HTTP
server on loopback reached through the real destination-guarded client. No provider is
called for real."""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest
from langchain_openai import ChatOpenAI
from sqlalchemy import delete, or_, select, text, update

from src.auth.settings import load_auth_settings
from src.credentials import network, resolver
from src.credentials.network import DestinationNotAllowed, GuardedBackend
from src.credentials.settings import load_credential_config
from src.db.session import SessionLocal
from src.investigators import provider, runner
from src.investigators.local_graph import AssignmentNotRunning, run_local_investigator
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    Evidence,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationTurn,
    ProgressEvent,
    ToolCall,
    ToolCallStatus,
    TurnStatus,
    User,
)
from src.services import credentials
from tests.credential_fixtures import (
    KEY_1,
    KEY_2,
    NEBIUS_SECRET,
    NEBIUS_URL,
    SIGNOZ_SECRET,
    allow_process_credentials,
    configure_credentials,
    signoz_server,
    store_committed,
)
from tests.database import OwnerSession
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel
from tests.investigators.test_main_graph import (
    ANSWER,
    PLAN,
    Crash,
    MainWorld,
    built_main_world,
    compose,
    investigate,
    local_script,
)
from tests.mcp_server.conftest import mcp_app  # noqa: F401
from tests.tenancy import Member, api_client, committed_members, configure_auth
from tests.tools.conftest import World, make_world, replies, trace_stats_args  # noqa: F401

SENTINEL = "SENTINEL"


def nebius_key(tag: str) -> str:
    return f"{NEBIUS_SECRET}-{tag}"


def signoz_key(tag: str) -> str:
    return f"{SIGNOZ_SECRET}-{tag}"


@pytest.fixture
async def owners(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[Member]]:
    """The owners of two organizations, committed. Requested before any world so the
    worlds are deleted before the organizations."""
    configure_auth(monkeypatch)
    async with committed_members() as (a,), committed_members() as (b,):
        try:
            yield [a, b]
        finally:
            # Audit rows are append-only for the runtime role.
            async with OwnerSession() as s, s.begin():
                await s.execute(
                    delete(AuditEvent).where(
                        or_(
                            AuditEvent.actor_user_id.in_([a.user_id, b.user_id]),
                            AuditEvent.organization_id.in_([a.organization_id, b.organization_id]),
                        )
                    )
                )
    load_auth_settings.cache_clear()
    load_credential_config.cache_clear()


@dataclass
class Tenant:
    tag: str
    member: Member
    world: MainWorld
    origin: str
    seen: list[dict[str, Any]]


@pytest.fixture
async def tenants(
    owners: list[Member], checkpointer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[list[Tenant]]:
    """Organization A and B, one environment and investigation each, with their own
    credentials and their own SigNoz server (allowlisted, as a local SigNoz would be)."""
    async with (
        signoz_server("trace_stats_scalar.json") as (origin_a, seen_a),
        signoz_server("trace_stats_scalar.json") as (origin_b, seen_b),
        built_main_world(checkpointer, owners[0].owns) as world_a,
        built_main_world(checkpointer, owners[1].owns) as world_b,
    ):
        configure_credentials(monkeypatch, telemetry=[origin_a, origin_b])
        found = [
            Tenant("A", owners[0], world_a, origin_a, seen_a),
            Tenant("B", owners[1], world_b, origin_b, seen_b),
        ]
        for t in found:
            await store_committed(t.member.user_id, t.world.environment_id, tag=t.tag,
                                  signoz_url=t.origin)  # fmt: skip
        yield found


class Provider:
    """Stands in for the Nebius model factory: records what each model step was built from
    and hands out the scripted model for that (role, key). A step built from a key nobody
    scripted fails the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, models: dict[tuple[str, str], Any]):
        self.models = models
        self.built: list[tuple[str, str, str | None, httpx.AsyncClient | None]] = []
        monkeypatch.setattr(provider, "main_model", self._builder("main"))
        monkeypatch.setattr(provider, "local_model", self._builder("local"))

    def _builder(self, role: str):
        def build(settings=None, http_async_client=None):
            key = settings.api_key.get_secret_value() if settings else "process"
            model = getattr(settings, f"{role}_model") if settings else None
            self.built.append((role, key, model, http_async_client))
            return self.models[(role, key)]

        return build

    def keys(self, role: str) -> list[str]:
        return [k for r, k, _, _ in self.built if r == role]


def happy(t: Tenant, key: str | None = None) -> dict[tuple[str, str], ScriptedModel]:
    key = key or nebius_key(t.tag)
    return {
        ("main", key): ScriptedModel(structured=[investigate(t.world), ANSWER, compose()]),
        ("local", key): local_script(t.world.service),
    }


async def run(world: MainWorld, checkpointer, app) -> runner.TurnRunResult:
    return await runner.run_turn(
        world.turn_id,
        checkpointer,
        mcp_settings=MCP_SETTINGS,
        mcp_transport=httpx2.ASGITransport(app=app),
    )


async def _threads(world: MainWorld) -> list[str]:
    async with SessionLocal() as s:
        assignments = await s.scalars(
            select(InvestigationAssignment.id).where(
                InvestigationAssignment.investigation_id == world.investigation_id
            )
        )
        return [f"turn:{world.turn_id}", *(f"asg:{a}" for a in assignments)]


async def assert_no_secrets(t: Tenant, logs: str) -> None:
    """No decrypted key anywhere it could persist or be shown: checkpoints, progress
    events, tool-call records, evidence, audit events, logs and API responses."""
    world = t.world
    threads = await _threads(world)
    async with SessionLocal() as s:
        stored: list[Any] = []
        for table, columns in (
            ("checkpoints", "checkpoint::text, metadata::text"),
            ("checkpoint_blobs", "blob"),
            ("checkpoint_writes", "blob"),
        ):
            rows = await s.execute(
                text(f"SELECT {columns} FROM langgraph.{table} WHERE thread_id = ANY(:t)"),
                {"t": threads},
            )
            stored += [v for row in rows for v in row]
        assert stored, "the run wrote checkpoints"
        stored += list(
            await s.scalars(
                select(ProgressEvent.payload_json).where(
                    ProgressEvent.investigation_id == world.investigation_id
                )
            )
        )
        calls = await s.execute(
            select(ToolCall.input_json, ToolCall.output_metadata_json, ToolCall.error_message,
                   ToolCall.result_summary)
            .join(InvestigationAssignment, InvestigationAssignment.id == ToolCall.assignment_id)
            .where(InvestigationAssignment.investigation_id == world.investigation_id)
        )  # fmt: skip
        stored += [v for row in calls for v in row]
        stored += list(
            await s.scalars(
                select(Evidence.data_json).where(
                    Evidence.investigation_id == world.investigation_id
                )
            )
        )
        stored += list(
            await s.scalars(
                select(AuditEvent.details_json).where(
                    AuditEvent.organization_id == t.member.organization_id
                )
            )
        )
    for value in stored:
        blob = value if isinstance(value, bytes) else str(value).encode()
        assert SENTINEL.encode() not in blob
    assert SENTINEL not in logs

    async with api_client(t.member) as client:
        inv, env = world.investigation_id, world.environment_id
        for url in (f"/investigations/{inv}", f"/investigations/{inv}/events?follow=false",
                    f"/environments/{env}", f"/environments/{env}/credentials"):  # fmt: skip
            response = await client.get(url)
            assert response.status_code == 200, url
            assert SENTINEL not in response.text
            if url.startswith("/environments"):
                assert t.origin.removeprefix("http://") not in response.text


# --- end to end ----------------------------------------------------------------------------


async def test_each_organization_runs_on_its_own_credentials(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    fake = Provider(monkeypatch, happy(tenants[0]) | happy(tenants[1]))

    with caplog.at_level(logging.DEBUG):
        # Side by side: nothing is shared between the two runs.
        results = await asyncio.gather(*(run(t.world, checkpointer, mcp_app) for t in tenants))

    assert [r.status for r in results] == [TurnStatus.COMPLETED] * 2
    for t in tenants:
        mine = {(role, model) for role, key, model, _ in fake.built if key == nebius_key(t.tag)}
        # Main through its investigation's environment (the Ultra role), Local through its
        # assignment's (the Super role): this tenant's ids and key only.
        assert mine == {("main", f"main-{t.tag}"), ("local", f"local-{t.tag}")}
        assert {r["headers"]["signoz-api-key"] for r in t.seen} == {signoz_key(t.tag)}
        async with SessionLocal() as s:
            (evidence,) = await s.scalars(
                select(Evidence).where(Evidence.investigation_id == t.world.investigation_id)
            )
        assert evidence.environment_id == t.world.environment_id
    assert "process" not in fake.keys("main") + fake.keys("local")
    # A fresh guarded client per model step, closed when the step ends.
    clients = [c for *_, c in fake.built]
    assert len({id(c) for c in clients}) == len(clients) == len(fake.built)
    for client in clients:
        assert client is not None and client.is_closed
        backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
        assert isinstance(backend, GuardedBackend) and backend._allowlisted_only
    for t in tenants:
        await assert_no_secrets(t, caplog.text)


async def test_a_missing_kind_refuses_the_turn_without_process_fallback(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a = tenants[0]
    allow_process_credentials(monkeypatch)
    async with SessionLocal() as s, s.begin():
        # Even with an operator grant and grants permitted: own rows exclude `.env`.
        await s.execute(
            update(Environment)
            .where(Environment.id == a.world.environment_id)
            .values(process_credentials=True)
        )
        await s.execute(
            delete(EnvironmentCredential).where(
                EnvironmentCredential.environment_id == a.world.environment_id,
                EnvironmentCredential.kind == CredentialKind.SIGNOZ,
            )
        )
    fake = Provider(monkeypatch, {})

    result = await run(a.world, checkpointer, mcp_app)

    # Refused by the runner before the turn started: never running, no model step.
    assert result.error_code == "configuration_required"
    assert result.status == TurnStatus.CANCELLED
    assert fake.built == [] and a.seen == []


async def test_unreadable_ciphertext_and_missing_keys_fail_closed(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a, b = tenants
    fake = Provider(monkeypatch, {})
    async with SessionLocal() as s, s.begin():
        row = await s.get(EnvironmentCredential, (a.world.environment_id, CredentialKind.NEBIUS))
        row.ciphertext = bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:]
    assert (await run(a.world, checkpointer, mcp_app)).error_code == "credentials_unavailable"

    configure_credentials(monkeypatch, keys={"k2": KEY_2}, active="k2",
                          telemetry=[a.origin, b.origin])  # fmt: skip
    assert (await run(b.world, checkpointer, mcp_app)).error_code == "credentials_unavailable"
    assert fake.built == [] and a.seen == b.seen == []


async def test_a_rotated_key_is_used_from_the_next_model_step(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a = tenants[0]

    async def rotate_then_investigate(_: Any):
        await store_committed(a.member.user_id, a.world.environment_id, tag="A2",
                              signoz_url=a.origin, kinds=(CredentialKind.NEBIUS,))  # fmt: skip
        return investigate(a.world)

    fake = Provider(
        monkeypatch,
        {
            ("main", nebius_key("A")): ScriptedModel(structured=[rotate_then_investigate]),
            ("main", nebius_key("A2")): ScriptedModel(structured=[ANSWER, compose()]),
            ("local", nebius_key("A2")): local_script(a.world.service),
        },
    )

    result = await run(a.world, checkpointer, mcp_app)

    assert result.status == TurnStatus.COMPLETED
    assert fake.keys("main") == [nebius_key("A")] + [nebius_key("A2")] * 2
    assert set(fake.keys("local")) == {nebius_key("A2")}


async def test_a_local_investigator_resolves_its_credentials_for_every_step(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a = tenants[0]

    async def rotate_then_plan(_: Any):
        await store_committed(a.member.user_id, a.world.environment_id, tag="A2",
                              signoz_url=a.origin, kinds=(CredentialKind.NEBIUS,))  # fmt: skip
        return PLAN

    rest = local_script(a.world.service)
    rest.structured.pop(0)  # the plan, answered before the rotation
    fake = Provider(
        monkeypatch,
        {
            ("main", nebius_key("A")): ScriptedModel(structured=[investigate(a.world)]),
            ("local", nebius_key("A")): ScriptedModel(structured=[rotate_then_plan]),
            ("local", nebius_key("A2")): rest,
            ("main", nebius_key("A2")): ScriptedModel(structured=[ANSWER, compose()]),
        },
    )

    result = await run(a.world, checkpointer, mcp_app)

    assert result.status == TurnStatus.COMPLETED
    local = fake.keys("local")
    assert local[0] == nebius_key("A") and set(local[1:]) == {nebius_key("A2")} and len(local) > 2


async def test_deleting_a_credential_blocks_the_next_provider_call(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a = tenants[0]

    async def delete_then_investigate(_: Any):
        async with SessionLocal() as s:
            await credentials.clear_credential(
                s, a.world.environment_id, await s.get(User, a.member.user_id),
                CredentialKind.NEBIUS,
            )  # fmt: skip
        return investigate(a.world)

    fake = Provider(
        monkeypatch,
        {("main", nebius_key("A")): ScriptedModel(structured=[delete_then_investigate])},
    )

    result = await run(a.world, checkpointer, mcp_app)

    # The Local Investigator was refused at admission and Main's next step at its model.
    assert result.status == TurnStatus.FAILED and result.error_code == "configuration_required"
    assert fake.keys("main") == [nebius_key("A")] and fake.keys("local") == []
    assert a.seen == []
    async with SessionLocal() as s:
        statuses = list(
            await s.scalars(
                select(InvestigationAssignment.status).where(
                    InvestigationAssignment.investigation_id == a.world.investigation_id
                )
            )
        )
    assert statuses == ["failed"]


async def test_resume_after_rotation_uses_the_new_key_and_key_ring(
    tenants: list[Tenant],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a, b = tenants
    first = Provider(
        monkeypatch,
        {
            ("main", nebius_key("A")): ScriptedModel(
                structured=[investigate(a.world), ANSWER, Crash()]
            ),
            ("local", nebius_key("A")): local_script(a.world.service),
        },
    )
    with pytest.raises(Crash):
        await run(a.world, checkpointer, mcp_app)
    assert len(a.seen) == 1 and first.keys("main")[-1] == nebius_key("A")

    # The owner replaces the key; the operator rotates the encryption key and drops k1.
    await store_committed(a.member.user_id, a.world.environment_id, tag="A2",
                          signoz_url=a.origin, kinds=(CredentialKind.NEBIUS,))  # fmt: skip
    origins = [a.origin, b.origin]
    configure_credentials(monkeypatch, keys={"k1": KEY_1, "k2": KEY_2}, active="k2",
                          telemetry=origins)  # fmt: skip
    actor = f"runtime-test-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        report = await credentials.rekey(s, load_credential_config(), actor=actor, apply=True)
    # An operator event: no user or organization, so the fixtures would not remove it.
    async with OwnerSession() as s, s.begin():
        await s.execute(delete(AuditEvent).where(AuditEvent.actor == actor))
    assert report.applied and report.unreadable == []
    configure_credentials(monkeypatch, keys={"k2": KEY_2}, active="k2", telemetry=origins)

    second = Provider(
        monkeypatch, {("main", nebius_key("A2")): ScriptedModel(structured=[compose()])}
    )
    result = await run(a.world, checkpointer, mcp_app)

    assert result.resumed and result.status == TurnStatus.COMPLETED
    assert second.keys("main") == [nebius_key("A2")] and len(a.seen) == 1


async def test_recovery_refuses_turns_whose_credentials_became_unusable(
    tenants: list[Tenant], checkpointer, monkeypatch: pytest.MonkeyPatch
):
    a = tenants[0]
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(InvestigationTurn)
            .where(InvestigationTurn.id == a.world.turn_id)
            .values(run_requested_at=InvestigationTurn.created_at)
        )
    configure_credentials(monkeypatch, nebius=["https://other.example"], telemetry=[a.origin])
    fake = Provider(monkeypatch, {})

    await runner.recover(checkpointer, resume=True, mcp_settings=MCP_SETTINGS)

    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, a.world.turn_id)
    assert turn.error_code == "destination_not_allowed" and fake.built == []


async def test_several_own_credential_environments_never_pick_an_owner(
    owners: list[Member], checkpointer, monkeypatch: pytest.MonkeyPatch
):
    configure_credentials(monkeypatch)
    owner = owners[0]
    async with built_main_world(checkpointer, owner.owns) as world:
        await store_committed(owner.user_id, world.environment_id)
        async with SessionLocal() as s, s.begin():
            second = Environment(name=f"second-{uuid.uuid4().hex[:6]}", type="local",
                                 **owner.owns)  # fmt: skip
            s.add(second)
            await s.flush()
            s.add(InvestigationEnvironment(investigation_id=world.investigation_id,
                                           environment_id=second.id))  # fmt: skip
        await store_committed(owner.user_id, second.id, tag="B")
        fake = Provider(monkeypatch, {})
        try:
            result = await runner.run_turn(world.turn_id, checkpointer, mcp_settings=MCP_SETTINGS)
            async with api_client(owner) as client:
                start = await client.post(f"/investigations/{world.investigation_id}/turns",
                                          json={"question": "and now?"})  # fmt: skip
        finally:
            async with SessionLocal() as s, s.begin():
                await s.execute(
                    delete(InvestigationEnvironment).where(
                        InvestigationEnvironment.environment_id == second.id
                    )
                )
                await s.execute(delete(Environment).where(Environment.id == second.id))
    assert result.error_code == "multi_environment_unsupported" and fake.built == []
    assert start.status_code == 409 and "multi environment unsupported" in start.json()["detail"]


# --- legacy and process grants -------------------------------------------------------------


def _production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver, "get_settings", lambda: SimpleNamespace(app_env="production"))


async def test_legacy_runs_stay_host_only_and_never_run_in_production(
    owners: list[Member],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    async with built_main_world(checkpointer) as legacy:
        async with api_client(owners[0]) as client:
            # Not reachable through the authenticated API, whoever asks (D032).
            found = await client.get(f"/investigations/{legacy.investigation_id}")
        assert found.status_code == 404
        _production(monkeypatch)
        fake = Provider(monkeypatch, {})
        result = await run(legacy, checkpointer, mcp_app)
    assert result.error_code == "process_credentials_not_permitted" and fake.built == []


async def test_granted_environments_need_the_development_opt_in(
    owners: list[Member],
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    granted = owners[0].owns | {"process_credentials": True}
    async with built_main_world(checkpointer, granted) as world:
        fake = Provider(monkeypatch, {})
        refused = await run(world, checkpointer, mcp_app)
        assert refused.error_code == "process_credentials_not_permitted" and fake.built == []

    allow_process_credentials(monkeypatch)
    _production(monkeypatch)
    async with built_main_world(checkpointer, granted) as world:
        refused = await run(world, checkpointer, mcp_app)
    assert refused.error_code == "process_credentials_not_permitted" and fake.built == []


# --- Local Investigator ----------------------------------------------------------------------


@pytest.fixture
async def own_world(
    owners: list[Member],
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    configure_credentials(monkeypatch)
    world = await make_world()
    owner = owners[0]
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(Environment).where(Environment.id == world.environment_id).values(**owner.owns)
        )
        await s.execute(
            update(Investigation)
            .where(Investigation.id == world.investigation_id)
            .values(organization_id=owner.organization_id)
        )
    await store_committed(owner.user_id, world.environment_id)
    return world


async def test_local_admission_refuses_unreadable_credentials_before_model_or_token(
    own_world: World,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    async with SessionLocal() as s, s.begin():
        row = await s.get(EnvironmentCredential, (own_world.environment_id, CredentialKind.SIGNOZ))
        row.ciphertext = row.ciphertext[:-1] + bytes([row.ciphertext[-1] ^ 1])
    fake = Provider(monkeypatch, {})
    with pytest.raises(AssignmentNotRunning, match="credentials_unavailable"):
        await run_local_investigator(
            own_world.assignment_id,
            checkpointer,
            mcp_settings=MCP_SETTINGS,
            mcp_transport=httpx2.ASGITransport(app=mcp_app),
        )
    assert fake.built == [] and await own_world.tool_calls() == []


async def test_models_are_built_from_the_environment_through_the_allowlist_guard(
    own_world: World,
):
    async with provider.environment_model(own_world.environment_id) as local:
        assert isinstance(local, ChatOpenAI)
        assert (local.model_name, local.openai_api_base) == ("local-A", NEBIUS_URL)
        assert local.openai_api_key.get_secret_value() == nebius_key("A")
        client = local.http_async_client
        assert isinstance(client, httpx.AsyncClient) and not client.follow_redirects
        # Only the allowlisted Nebius origin connects; refused before any lookup.
        with pytest.raises(DestinationNotAllowed):
            await client.get("https://evil.example/v1/models")
        with pytest.raises(RuntimeError, match="synchronous"):
            local.http_client.get(NEBIUS_URL)
    assert client.is_closed
    async with provider.investigation_model(own_world.investigation_id) as main:
        assert main.model_name == "main-A" and main.max_tokens == 16_384


# --- Tool Executor -------------------------------------------------------------------------


@pytest.fixture
async def exec_tenants(
    owners: list[Member],
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[tuple[World, Member, str, list[dict[str, Any]]]]]:
    async with (
        signoz_server("trace_stats_scalar.json") as (origin_a, seen_a),
        signoz_server("trace_stats_scalar.json") as (origin_b, seen_b),
    ):
        configure_credentials(monkeypatch, telemetry=[origin_a, origin_b])
        found = []
        for tag, owner, origin, seen in (("A", owners[0], origin_a, seen_a),
                                         ("B", owners[1], origin_b, seen_b)):  # fmt: skip
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
            await store_committed(owner.user_id, world.environment_id, tag=tag, signoz_url=origin)
            found.append((world, owner, origin, seen))
        yield found


def _call(world: World, owner: Member, key: str | None = None, args: Any = None, **overrides: Any):
    handler, ignored = replies("trace_stats_scalar.json")
    context = world.context(key, organization_id=owner.organization_id, **overrides)
    args = args or trace_stats_args(service=world.service)
    return world.run("signoz.trace_stats", args, handler, context=context), ignored


async def test_executor_uses_each_environments_own_connection(exec_tenants):
    (a, owner_a, _, seen_a), (b, owner_b, _, seen_b) = exec_tenants
    args = trace_stats_args(service=a.service)
    (call_a, ignored_a), (call_b, ignored_b) = (
        _call(a, owner_a, "k", args),
        _call(b, owner_b, "k", trace_stats_args(service=b.service)),
    )
    # Side by side, with the same idempotency key: neither replays the other.
    ok_a, ok_b = await asyncio.gather(call_a, call_b)

    for result in (ok_a, ok_b):
        assert result.status is ToolCallStatus.SUCCEEDED and not result.replayed
    assert [r["headers"]["signoz-api-key"] for r in seen_a] == [signoz_key("A")]
    assert [r["headers"]["signoz-api-key"] for r in seen_b] == [signoz_key("B")]
    # The test-only settings and client overrides never redirect an environment's own one.
    assert ignored_a == ignored_b == []

    replay, _ = _call(a, owner_a, "k", args)
    assert (await replay).replayed and len(seen_a) == 1
    (evidence,) = await a.evidence()
    assert (evidence.environment_id, evidence.investigation_id) == (
        a.environment_id, a.investigation_id,
    )  # fmt: skip


async def test_executor_refuses_wrong_assignments_and_foreign_tokens(exec_tenants):
    (a, owner_a, _, seen_a), (b, owner_b, _, seen_b) = exec_tenants
    for overrides in (
        {"environment_id": b.environment_id},
        {"investigation_id": b.investigation_id},
    ):
        call, _ = _call(a, owner_a, **overrides)
        assert (await call).error.code == "policy.context_mismatch"
    call, _ = _call(a, owner_b)
    assert (await call).error.code == "policy.context_mismatch"
    assert seen_a == seen_b == []


async def test_executor_refuses_unusable_credentials_even_on_replay(
    exec_tenants, monkeypatch: pytest.MonkeyPatch
):
    (a, owner_a, origin_a, seen_a), (b, owner_b, origin_b, _) = exec_tenants
    args = trace_stats_args(service=a.service)
    call, _ = _call(a, owner_a, "same", args)
    assert (await call).status is ToolCallStatus.SUCCEEDED

    async with SessionLocal() as s, s.begin():
        row = await s.get(EnvironmentCredential, (a.environment_id, CredentialKind.SIGNOZ))
        row.ciphertext = bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:]
    for key in ("same", None):
        call, _ = _call(a, owner_a, key, args)
        assert (await call).error.code == "policy.credentials_unavailable"

    async with SessionLocal() as s, s.begin():
        await s.execute(
            delete(EnvironmentCredential).where(
                EnvironmentCredential.environment_id == a.environment_id,
                EnvironmentCredential.kind == CredentialKind.SIGNOZ,
            )
        )
    call, _ = _call(a, owner_a, "same", args)
    assert (await call).error.code == "policy.credentials_not_configured"

    # The operator removed B's origin from the allowlist since it was saved.
    configure_credentials(monkeypatch, telemetry=[origin_a])
    call, _ = _call(b, owner_b)
    assert (await call).error.code == "policy.destination_not_allowed"
    assert len(seen_a) == 1


async def test_runtime_telemetry_requests_are_vetted_at_connection(
    owners: list[Member],
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    configure_credentials(monkeypatch, telemetry=[])
    owner = owners[0]
    world = await make_world()
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(Environment).where(Environment.id == world.environment_id).values(**owner.owns)
        )
        await s.execute(
            update(Investigation)
            .where(Investigation.id == world.investigation_id)
            .values(organization_id=owner.organization_id)
        )
    # A public https URL when saved; it now resolves to a private address (rebinding).
    await store_committed(owner.user_id, world.environment_id,
                          signoz_url="https://telemetry.example.com")  # fmt: skip
    lookups: list[str] = []

    async def rebound(host: str, port: int) -> list[str]:
        lookups.append(host)
        return ["10.0.0.7"]

    monkeypatch.setattr(network, "resolve", rebound)
    call, _ = _call(world, owner)
    result = await call

    assert result.status is ToolCallStatus.FAILED
    assert result.error.code == "policy.destination_not_allowed"
    assert lookups and set(lookups) == {"telemetry.example.com"}
    (row,) = await world.tool_calls()
    assert row.error_code == "policy.destination_not_allowed"


async def test_runtime_redirects_are_not_followed_and_echoed_keys_are_scrubbed(
    owners: list[Member],
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    owner = owners[0]
    async with signoz_server("trace_stats_scalar.json") as (elsewhere, seen_elsewhere):
        echo = {"status": "error", "error": {"message": f"bad key {signoz_key('A')}"}}
        async with signoz_server(
            (302, {}, {"Location": f"{elsewhere}/api/v5/query_range"}), (400, echo, {})
        ) as (origin, seen):
            configure_credentials(monkeypatch, telemetry=[origin, elsewhere])
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
            await store_committed(owner.user_id, world.environment_id, signoz_url=origin)
            redirected, _ = _call(world, owner)
            assert (await redirected).status is ToolCallStatus.FAILED
            echoed, _ = _call(world, owner)
            result = await echoed
    assert seen_elsewhere == [] and len(seen) == 2
    assert SENTINEL not in result.model_dump_json()
    for row in await world.tool_calls():
        assert SENTINEL not in f"{row.error_message} {row.output_metadata_json}"
