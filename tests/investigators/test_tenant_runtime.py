"""Phase 7B tenant isolation below the API: the Tool Executor, MCP tokens, the turn runner,
crash recovery, Local Investigators and open event streams. Rows are committed (these
components use their own sessions); nothing calls a model or SigNoz for real."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import httpx2
import pytest
from sqlalchemy import delete, select, update

from src.api import sse
from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal
from src.investigators import runner
from src.investigators.local_graph import AssignmentNotRunning, run_local_investigator
from src.models import (
    AssignmentStatus,
    Environment,
    Investigation,
    InvestigationAssignment,
    InvestigationTurn,
    OrganizationMembership,
    ToolCallStatus,
    TurnStatus,
)
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel
from tests.investigators.test_main_graph import MainWorld, built_main_world
from tests.mcp_server.conftest import connect, mcp_app, signoz, token_for  # noqa: F401
from tests.tenancy import Member, api_client, committed_members, configure_auth
from tests.tools.conftest import World, make_world, replies, trace_stats_args  # noqa: F401


@pytest.fixture
async def members(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[Member]]:
    """Owner and member of one organization, committed. Requested before any world so
    the worlds are deleted before the organization."""
    configure_auth(monkeypatch)
    async with committed_members(2) as found:
        yield found
    load_auth_settings.cache_clear()


TenantWorld = Callable[..., Awaitable[World]]


@pytest.fixture
def tenant_world(members: list[Member], make_world) -> TenantWorld:  # noqa: F811
    async def make(*, grant: bool) -> World:
        world = await make_world()
        owner = members[0]
        async with SessionLocal() as s, s.begin():
            # Moving ownerless rows into the organization, as adoption does.
            await s.execute(
                update(Environment)
                .where(Environment.id == world.environment_id)
                .values(**owner.owns, process_credentials=grant)
            )
            await s.execute(
                update(Investigation)
                .where(Investigation.id == world.investigation_id)
                .values(organization_id=owner.organization_id)
            )
        return world

    return make


# --- Tool Executor -------------------------------------------------------------------------


async def test_executor_binds_calls_to_the_tokens_organization(
    tenant_world: TenantWorld, members: list[Member]
):
    world = await tenant_world(grant=True)
    handler, seen = replies("trace_stats_scalar.json")
    args = trace_stats_args(service=world.service)
    org = members[0].organization_id

    for wrong in (None, uuid.uuid4()):
        refused = await world.run(
            "signoz.trace_stats", args, handler, context=world.context(organization_id=wrong)
        )
        assert refused.status is ToolCallStatus.REJECTED
        assert refused.error.code == "policy.context_mismatch"
    assert seen == []  # nothing reached SigNoz

    ok = await world.run(
        "signoz.trace_stats", args, handler, context=world.context("k1", organization_id=org)
    )
    assert ok.status is ToolCallStatus.SUCCEEDED and len(ok.evidence_ids) == 1
    (evidence,) = await world.evidence()
    assert (evidence.investigation_id, evidence.environment_id) == (
        world.investigation_id, world.environment_id,
    )  # fmt: skip


async def test_executor_refuses_environments_without_credentials_even_on_replay(
    tenant_world: TenantWorld, members: list[Member]
):
    world = await tenant_world(grant=True)
    handler, seen = replies("trace_stats_scalar.json")
    context = world.context("same-key", organization_id=members[0].organization_id)
    args = trace_stats_args(service=world.service)
    assert (await world.run("signoz.trace_stats", args, handler, context=context)).replayed is False

    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(Environment)
            .where(Environment.id == world.environment_id)
            .values(process_credentials=False)
        )
    replay = await world.run("signoz.trace_stats", args, handler, context=context)
    fresh = await world.run(
        "signoz.trace_stats",
        args,
        handler,
        context=world.context(organization_id=context.organization_id),
    )
    for result in (replay, fresh):
        assert result.status is ToolCallStatus.REJECTED
        assert result.error.code == "policy.credentials_not_configured"
    assert len(seen) == 1  # only the first, granted call queried SigNoz


async def test_mcp_token_for_the_wrong_organization_runs_nothing(
    mcp_app,  # noqa: F811
    tenant_world: TenantWorld,
    members: list[Member],
    signoz,  # noqa: F811
):
    world = await tenant_world(grant=True)
    seen = signoz("trace_stats_scalar.json")

    async def call(token: str):
        async with connect(mcp_app, token) as client:
            return await client.call_tool("signoz_trace_stats", trace_stats_args())

    for wrong in (None, uuid.uuid4()):
        result = await call(token_for(world, organization_id=wrong))
        assert result.structured_content["error"]["code"] == "policy.context_mismatch"
    # A token for another environment of the same organization does not fit either.
    other = await tenant_world(grant=True)
    stolen = token_for(
        World(world.investigation_id, other.environment_id, world.assignment_id, "", ""),
        organization_id=members[0].organization_id,
    )
    assert (await call(stolen)).structured_content["error"]["code"] == "policy.context_mismatch"
    assert seen == []

    good = await call(token_for(world, organization_id=members[0].organization_id))
    assert good.structured_content["status"] == "succeeded"


# --- runner, recovery and Local Investigators ----------------------------------------------


@pytest.fixture
async def ungranted(checkpointer, members: list[Member]) -> AsyncIterator[MainWorld]:
    async with built_main_world(checkpointer, members[0].owns) as world:
        yield world


def _no_model() -> ScriptedModel:
    return ScriptedModel()  # any call fails: "model called more often than scripted"


async def test_runner_never_runs_a_tenant_turn_without_credentials(
    ungranted: MainWorld, checkpointer
):
    main, local = _no_model(), _no_model()
    result = await runner.run_turn(
        ungranted.turn_id, checkpointer, main=main, local=local, mcp_settings=MCP_SETTINGS
    )

    assert result.error_code == "configuration_required"
    assert result.status in (TurnStatus.CANCELLED, TurnStatus.FAILED)
    assert main.structured_prompts == [] and local.agent_prompts == []


async def test_recovery_ends_unrunnable_tenant_turns_without_model_calls(
    ungranted: MainWorld, checkpointer
):
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(InvestigationTurn)
            .where(InvestigationTurn.id == ungranted.turn_id)
            .values(run_requested_at=InvestigationTurn.created_at)
        )
    plan = await runner.recovery_plan(checkpointer)
    assert {"turn_id": ungranted.turn_id, "action": "start"}.items() <= next(
        p for p in plan if p["turn_id"] == ungranted.turn_id
    ).items()

    main = _no_model()
    await runner.recover(checkpointer, resume=True, main=main, local=_no_model(),
                         mcp_settings=MCP_SETTINGS)  # fmt: skip

    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, ungranted.turn_id)
    assert turn.error_code == "configuration_required" and main.structured_prompts == []


async def test_local_investigator_refuses_before_any_model_or_token(
    tenant_world: TenantWorld,
    checkpointer,
    mcp_app,  # noqa: F811
):
    world = await tenant_world(grant=False)
    model = _no_model()
    with pytest.raises(AssignmentNotRunning, match="configuration_required"):
        await run_local_investigator(
            world.assignment_id,
            checkpointer,
            model=model,  # type: ignore[arg-type]
            mcp_settings=MCP_SETTINGS,
            mcp_transport=httpx2.ASGITransport(app=mcp_app),
        )
    async with SessionLocal() as s:
        status = await s.scalar(
            select(InvestigationAssignment.status).where(
                InvestigationAssignment.id == world.assignment_id
            )
        )
    assert status == AssignmentStatus.FAILED and model.agent_prompts == []
    assert await world.tool_calls() == []


# --- event streams -------------------------------------------------------------------------


@pytest.fixture
async def granted(checkpointer, members: list[Member]) -> AsyncIterator[MainWorld]:
    async with built_main_world(
        checkpointer, members[0].owns | {"process_credentials": True}
    ) as world:
        yield world


async def test_open_stream_closes_when_membership_is_revoked(
    granted: MainWorld, members: list[Member], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(sse, "RECHECK_SECONDS", 0.05)
    owner, member = members
    url = f"/investigations/{granted.investigation_id}/events"

    async with api_client(member) as client:
        streaming = asyncio.create_task(client.get(url, timeout=10))
        await asyncio.sleep(0.3)
        assert not streaming.done()  # still following while a member
        async with SessionLocal() as s, s.begin():
            await s.execute(
                delete(OrganizationMembership).where(
                    OrganizationMembership.organization_id == member.organization_id,
                    OrganizationMembership.user_id == member.user_id,
                )
            )
        response: httpx.Response = await asyncio.wait_for(streaming, 5)

    assert response.status_code == 200
    assert response.text.rstrip().endswith('data: {"reason": "access_revoked"}')
    async with api_client(member) as client:
        assert (await client.get(url, params={"follow": "false"})).status_code == 404
    async with api_client(owner) as client:
        assert (await client.get(url, params={"follow": "false"})).status_code == 200
