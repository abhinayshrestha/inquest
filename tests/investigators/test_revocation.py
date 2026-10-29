"""Phase 7C-3: revoking credentials stops the turns using them, and an accepted ownership
transfer revokes the previous owner's credentials while history stays. Committed rows,
the real runner, MCP server and executor; SigNoz is a loopback server whose gate holds a
tool call in flight; models are scripted (the factory records what each step used). No
provider is called for real."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import httpx2
import pytest
from sqlalchemy import delete, or_, select, text

from src.api.routes.credentials import provider_client
from src.auth.settings import load_auth_settings
from src.credentials.settings import load_credential_config
from src.db.session import SessionLocal
from src.investigators import runner, runtime
from src.main import app
from src.models import (
    AssignmentStatus,
    AuditEvent,
    Environment,
    EnvironmentCredential,
    Evidence,
    InvestigationAssignment,
    InvestigationReport,
    InvestigationTurn,
    ProgressEvent,
    ToolCall,
    ToolCallStatus,
    TurnStatus,
    User,
)
from src.services import transfers, turns
from tests.credential_fixtures import (
    NEBIUS_URL,
    configure_credentials,
    signoz_server,
    store_committed,
)
from tests.database import OwnerSession
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel
from tests.investigators.test_main_graph import (
    ANSWER,
    Crash,
    MainWorld,
    built_main_world,
    compose,
    investigate,
    local_script,
)
from tests.investigators.test_runtime_credentials import (
    Provider,
    Tenant,
    assert_no_secrets,
    nebius_key,
)
from tests.mcp_server.conftest import mcp_app  # noqa: F401
from tests.tenancy import Member, api_client, committed_members, configure_auth

KEY = nebius_key("A")


@dataclass
class Setup:
    alice: Member  # owns the environment
    bob: Member  # another member of the organization
    world: MainWorld
    origin: str
    seen: list[dict[str, Any]]
    gate: asyncio.Event

    def tenant(self, member: Member) -> Tenant:
        return Tenant("A", member, self.world, self.origin, self.seen)


@pytest.fixture
async def setup(checkpointer, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Setup]:
    configure_auth(monkeypatch)
    gate = asyncio.Event()
    async with committed_members(2) as (alice, bob):
        try:
            async with (
                signoz_server("trace_stats_scalar.json", gate=gate) as (origin, seen),
                built_main_world(checkpointer, alice.owns) as world,
            ):
                configure_credentials(monkeypatch, telemetry=[origin])
                await store_committed(alice.user_id, world.environment_id, tag="A",
                                      signoz_url=origin)  # fmt: skip
                yield Setup(alice, bob, world, origin, seen, gate)
                gate.set()
                # A turn a failing test left running must stop before its rows are deleted.
                await runner.cancel_investigation_task(world.investigation_id)
        finally:
            # Audit rows are append-only for the runtime role.
            async with OwnerSession() as s, s.begin():
                await s.execute(
                    delete(AuditEvent).where(
                        or_(
                            AuditEvent.actor_user_id.in_([alice.user_id, bob.user_id]),
                            AuditEvent.organization_id == alice.organization_id,
                        )
                    )
                )
    app.dependency_overrides.clear()
    load_auth_settings.cache_clear()
    load_credential_config.cache_clear()


def _start(s: Setup, checkpointer, mcp_app, turn_id: uuid.UUID | None = None):  # noqa: F811
    return runner.start_turn_task(
        s.world.investigation_id,
        turn_id or s.world.turn_id,
        checkpointer,
        mcp_settings=MCP_SETTINGS,
        mcp_transport=httpx2.ASGITransport(app=mcp_app),
    )


async def _in_flight(s: Setup, count: int = 1) -> None:
    """Until SigNoz holds `count` requests: a Local Investigator's tool call is under way."""
    for _ in range(1000):
        if len(s.seen) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the tool call never reached SigNoz")


async def _turn(turn_id: uuid.UUID) -> InvestigationTurn:
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, turn_id)
    assert turn is not None
    return turn


async def _assignments(world: MainWorld) -> list[AssignmentStatus]:
    async with SessionLocal() as s:
        return list(
            await s.scalars(
                select(InvestigationAssignment.status).where(
                    InvestigationAssignment.investigation_id == world.investigation_id
                )
            )
        )


async def _audits(s: Setup, action: str) -> list[AuditEvent]:
    async with SessionLocal() as session:
        return list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.organization_id == s.alice.organization_id,
                    AuditEvent.action == action,
                )
            )
        )


async def _checkpoints(turn_id: uuid.UUID) -> int:
    async with SessionLocal() as session:
        return await session.scalar(
            text("SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t"),
            {"t": f"turn:{turn_id}"},
        )


async def test_clearing_a_credential_cancels_the_turn_during_a_tool_call(
    setup: Setup,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    s = setup
    fake = Provider(
        monkeypatch,
        {
            ("main", KEY): ScriptedModel(structured=[investigate(s.world), ANSWER, compose()]),
            ("local", KEY): local_script(s.world.service),
        },
    )
    task = _start(s, checkpointer, mcp_app)
    await _in_flight(s)
    built = len(fake.built)

    async with api_client(s.alice) as client:
        response = await client.delete(f"/environments/{s.world.environment_id}/credentials/nebius")
    assert response.status_code == 204
    # The DELETE returned only after the turn had recorded its cancellation.
    assert task.done()
    s.gate.set()

    turn = await _turn(s.world.turn_id)
    assert (turn.status, turn.error_code) == (TurnStatus.CANCELLED, "credentials_revoked")
    assert await _assignments(s.world) == [AssignmentStatus.CANCELLED]
    # No model was built after the revocation, and no further SigNoz request was made.
    assert len(fake.built) == built and len(s.seen) == 1
    async with SessionLocal() as session:
        calls = list(
            await session.scalars(
                select(ToolCall.status)
                .join(InvestigationAssignment, InvestigationAssignment.id == ToolCall.assignment_id)
                .where(InvestigationAssignment.investigation_id == s.world.investigation_id)
            )
        )
        events = list(
            await session.scalars(
                select(ProgressEvent.payload_json).where(
                    ProgressEvent.turn_id == turn.id,
                    ProgressEvent.type == "turn.cancelled",
                )
            )
        )
    assert calls == [ToolCallStatus.CANCELLED]
    assert events == [{"decision": None, "error_code": "credentials_revoked"}]
    (audit,) = await _audits(s, "turns.cancel")
    assert audit.details_json == {
        "environment_id": str(s.world.environment_id),
        "reason": "credentials_revoked",
        "turn_ids": [str(turn.id)],
    }
    assert audit.actor_user_id == s.alice.user_id
    # The run's history stays; running it again does nothing.
    assert await _checkpoints(turn.id) > 0
    again = await runner.run_turn(turn.id, checkpointer, mcp_settings=MCP_SETTINGS)
    assert again.status == TurnStatus.CANCELLED and len(fake.built) == built
    await assert_no_secrets(s.tenant(s.alice), caplog.text)


async def test_concurrent_clears_cancel_the_turn_once(
    setup: Setup,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    s = setup
    Provider(
        monkeypatch,
        {
            ("main", KEY): ScriptedModel(structured=[investigate(s.world), ANSWER, compose()]),
            ("local", KEY): local_script(s.world.service),
        },
    )
    task = _start(s, checkpointer, mcp_app)
    await _in_flight(s)
    url = f"/environments/{s.world.environment_id}/credentials"
    async with api_client(s.alice) as client:
        # Both kinds cleared and a user cancel, all at once.
        responses = await asyncio.gather(
            client.delete(f"{url}/nebius"),
            client.delete(f"{url}/signoz"),
            runtime.cancel(s.world.investigation_id),
        )
    assert [r.status_code for r in responses[:2]] == [204, 204]
    assert responses[2] == s.world.turn_id
    assert task.done()
    s.gate.set()

    turn = await _turn(s.world.turn_id)
    assert turn.status == TurnStatus.CANCELLED
    assert turn.error_code in ("credentials_revoked", "cancelled")
    async with SessionLocal() as session:
        cancelled = list(
            await session.scalars(
                select(ProgressEvent.id).where(
                    ProgressEvent.turn_id == turn.id, ProgressEvent.type == "turn.cancelled"
                )
            )
        )
    assert len(cancelled) == 1


async def test_a_revoked_checkpointed_turn_is_cancelled_and_never_recovered(
    setup: Setup,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    s = setup
    s.gate.set()
    Provider(
        monkeypatch,
        {
            ("main", KEY): ScriptedModel(structured=[investigate(s.world), ANSWER, Crash()]),
            ("local", KEY): local_script(s.world.service),
        },
    )
    with pytest.raises(Crash):
        await runner.run_turn(
            s.world.turn_id, checkpointer, mcp_settings=MCP_SETTINGS,
            mcp_transport=httpx2.ASGITransport(app=mcp_app),
        )  # fmt: skip
    assert (await _turn(s.world.turn_id)).status == TurnStatus.RUNNING

    # No task runs it in this process (as after a crash): cancelled in the database.
    async with api_client(s.alice) as client:
        response = await client.delete(f"/environments/{s.world.environment_id}/credentials/signoz")
    assert response.status_code == 204

    turn = await _turn(s.world.turn_id)
    assert (turn.status, turn.error_code) == (TurnStatus.CANCELLED, "credentials_revoked")
    assert all(a != AssignmentStatus.RUNNING for a in await _assignments(s.world))
    assert await _checkpoints(turn.id) > 0
    plan = await runner.recovery_plan(checkpointer)
    assert turn.id not in [item["turn_id"] for item in plan]
    fake = Provider(monkeypatch, {})
    result = await runner.run_turn(turn.id, checkpointer, mcp_settings=MCP_SETTINGS)
    assert result.status == TurnStatus.CANCELLED and fake.built == []
    # A second clear finds nothing to revoke and cancels nothing more.
    async with api_client(s.alice) as client:
        again = await client.delete(f"/environments/{s.world.environment_id}/credentials/signoz")
    assert again.status_code == 204 and len(await _audits(s, "turns.cancel")) == 1


async def test_replacing_a_credential_keeps_the_turn_running_on_the_new_key(
    setup: Setup,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    s = setup
    rotated = "nbk-rotated-0123456789abcdef"
    main = ScriptedModel(structured=[investigate(s.world), ANSWER, compose()])
    local = local_script(s.world.service)
    # One script per role whatever the key: the test checks which key each step used.
    fake = Provider(
        monkeypatch,
        {("main", KEY): main, ("main", rotated): main, ("local", KEY): local,
         ("local", rotated): local},
    )  # fmt: skip
    task = _start(s, checkpointer, mcp_app)
    await _in_flight(s)
    url = f"/environments/{s.world.environment_id}/credentials/nebius"
    body = {"api_key": rotated, "base_url": NEBIUS_URL, "main_model": "main-A",
            "local_model": "local-A"}  # fmt: skip

    def provider(status: int):
        models = {"data": [{"id": "main-A"}, {"id": "local-A"}]}
        app.dependency_overrides[provider_client] = lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json=models))
        )

    async with api_client(s.alice) as client:
        provider(401)
        failed = await client.put(url, json=body)
        assert failed.status_code == 422 and failed.json()["detail"]["code"] == "auth_failed"
        provider(200)
        replaced = await client.put(url, json=body)
        assert replaced.status_code == 200
    assert not task.done()
    s.gate.set()
    result = await task

    assert result.status == TurnStatus.COMPLETED
    assert await _audits(s, "turns.cancel") == []
    assert KEY in fake.keys("main") and fake.keys("main")[-1] == rotated
    assert fake.keys("local")[-1] == rotated


async def test_accepting_a_transfer_cancels_running_turns_and_keeps_history(
    setup: Setup,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    s = setup
    s.gate.set()
    Provider(
        monkeypatch,
        {
            ("main", KEY): ScriptedModel(structured=[investigate(s.world), ANSWER, compose()]),
            ("local", KEY): local_script(s.world.service),
        },
    )
    first = await _start(s, checkpointer, mcp_app)
    assert first.status == TurnStatus.COMPLETED and first.report_version == 1

    # A follow-up is under way (its tool call held) when the owner hands the environment on.
    s.gate.clear()
    fake = Provider(
        monkeypatch,
        {
            ("main", KEY): ScriptedModel(structured=[investigate(s.world, "Look again")]),
            ("local", KEY): local_script(s.world.service),
        },
    )
    async with SessionLocal() as session:
        follow_up = await turns.create_follow_up_turn(
            session, s.world.investigation_id, "and now?", request_run=True
        )
    task = _start(s, checkpointer, mcp_app, follow_up.id)
    await _in_flight(s, count=2)
    built = len(fake.built)
    async with api_client(s.alice) as client:
        proposed = await client.post(
            f"/environments/{s.world.environment_id}/transfers",
            json={"to_user_id": str(s.bob.user_id)},
        )
    async with api_client(s.bob) as client:
        accepted = await client.post(f"/transfers/{proposed.json()['id']}/accept")
    assert accepted.status_code == 200 and task.done()
    s.gate.set()

    second = await _turn(follow_up.id)
    assert (second.status, second.error_code) == (TurnStatus.CANCELLED, "ownership_transferred")
    assert len(fake.built) == built
    # Turn 1, its report, evidence and checkpoints are untouched.
    assert (await _turn(s.world.turn_id)).status == TurnStatus.COMPLETED
    async with SessionLocal() as session:
        environment = await session.get(Environment, s.world.environment_id)
        reports = list(
            await session.scalars(
                select(InvestigationReport.version).where(
                    InvestigationReport.investigation_id == s.world.investigation_id
                )
            )
        )
        evidence = await session.scalar(
            select(Evidence.id).where(Evidence.investigation_id == s.world.investigation_id)
        )
        rows = list(
            await session.scalars(
                select(EnvironmentCredential).where(
                    EnvironmentCredential.environment_id == s.world.environment_id
                )
            )
        )
    assert environment.owner_user_id == s.bob.user_id and rows == []
    assert reports == [1] and evidence is not None
    assert await _checkpoints(s.world.turn_id) > 0
    (audit,) = await _audits(s, "turns.cancel")
    assert audit.details_json["reason"] == "ownership_transferred"
    assert audit.actor_user_id == s.bob.user_id

    async with api_client(s.bob) as client:
        refused = await client.post(
            f"/investigations/{s.world.investigation_id}/turns", json={"question": "more?"}
        )
        report = await client.get(f"/investigations/{s.world.investigation_id}/reports/1")
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("configuration required:")
    assert report.status_code == 200
    await assert_no_secrets(s.tenant(s.bob), caplog.text)


async def test_only_the_recipient_turns_a_transfer_into_a_revocation(
    setup: Setup,
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    s = setup
    Provider(
        monkeypatch,
        {
            ("main", KEY): ScriptedModel(structured=[investigate(s.world), ANSWER, compose()]),
            ("local", KEY): local_script(s.world.service),
        },
    )
    task = _start(s, checkpointer, mcp_app)
    await _in_flight(s)
    async with SessionLocal() as session:
        alice = await session.get(User, s.alice.user_id)
        transfer = await transfers.propose(session, alice, s.world.environment_id, s.bob.user_id)
    async with api_client(s.bob) as client:
        declined = await client.post(f"/transfers/{transfer.id}/decline")
    assert declined.status_code == 200
    # Proposing and declining revoke nothing: the turn completes on the owner's credentials.
    s.gate.set()
    assert (await task).status == TurnStatus.COMPLETED
