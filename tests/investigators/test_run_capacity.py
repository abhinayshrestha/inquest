"""Phase 7D-2A (audit H1): run leases, capacity limits and connection lifetimes, through the
real API, runtime, runner and graphs. Models are mocked and gated, so runs sit inside a
"slow model call" for as long as a test needs; nothing calls a provider.

Two organizations, A and B, each with environments on the operator's development grant.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import delete, func, select, update

from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal, engine
from src.investigators import lifecycle, policy, runner, runtime
from src.main import app
from src.models import InvestigationTurn, ProgressEvent, RunLease, TurnStatus
from src.services import leases, turns
from src.services.leases import LeaseLost
from tests.credential_fixtures import allow_process_credentials
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel, _Runnable
from tests.investigators.test_api_runtime import happy
from tests.investigators.test_main_graph import MainWorld, built_main_world
from tests.mcp_server.conftest import signoz  # noqa: F401
from tests.tenancy import COOKIE, Member, api_client, committed_members, configure_auth


class GatedModel(ScriptedModel):
    """Every run's first Main call waits at the gate (a slow provider), then fails like an
    unavailable provider would. Shared by all runs, unlike a ScriptedModel's queue."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.waiting = 0

    def with_structured_output(self, schema: type, *, method: str, include_raw: bool):
        async def call(_: list[Any]) -> Any:
            self.waiting += 1
            try:
                await self.gate.wait()
            finally:
                self.waiting -= 1
            raise RuntimeError("provider unavailable")

        return _Runnable(call)


async def eventually(check: Callable[[], Awaitable[bool]]) -> None:
    # Polls database state that no in-process event announces.
    async with asyncio.timeout(15):
        while not await check():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def live_leases(organization_id: uuid.UUID | None = None) -> int:
    async with SessionLocal() as s:
        query = select(func.count()).select_from(RunLease).where(RunLease.expires_at > func.now())
        if organization_id is not None:
            query = query.where(RunLease.organization_id == organization_id)
        return await s.scalar(query) or 0


async def status_of(world: MainWorld) -> TurnStatus:
    async with SessionLocal() as s:
        return await s.scalar(
            select(InvestigationTurn.status).where(InvestigationTurn.id == world.turn_id)
        )  # type: ignore[return-value]


def finished(*worlds: MainWorld) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return all([await status_of(w) in turns.FINISHED for w in worlds])

    return check


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch):
    def set_limits(running: int, per_org: int, queued: int = 0, queued_per_org: int = 0) -> None:
        for name, value in (
            ("MAX_RUNNING", running),
            ("MAX_RUNNING_PER_ORGANIZATION", per_org),
            ("MAX_QUEUED", queued),
            ("MAX_QUEUED_PER_ORGANIZATION", queued_per_org),
        ):
            monkeypatch.setenv(f"INQUEST_RUNNER_{name}", str(value))
        leases.capacity_settings.cache_clear()

    yield set_limits
    leases.capacity_settings.cache_clear()


@pytest.fixture
async def orgs(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Member, Member]]:
    configure_auth(monkeypatch)
    allow_process_credentials(monkeypatch)
    async with committed_members() as [a], committed_members() as [b]:
        yield a, b
    load_auth_settings.cache_clear()


@pytest.fixture
async def worlds(checkpointer, orgs) -> AsyncIterator[Callable[..., Awaitable[list[MainWorld]]]]:
    stack = AsyncExitStack()

    async def make(member: Member, count: int) -> list[MainWorld]:
        return [
            await stack.enter_async_context(
                built_main_world(checkpointer, member.owns | {"process_credentials": True})
            )
            for _ in range(count)
        ]

    async with stack:
        yield make
        # Rows of leases taken over in a test (another "holder") go with their investigation.


@pytest.fixture
async def rt(checkpointer, mcp_app, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(runtime, "BACKLOG_RETRY_SECONDS", 0.2)
    started = runtime.Runtime(
        checkpointer,
        run_kwargs={"mcp_settings": MCP_SETTINGS,
                    "mcp_transport": httpx2.ASGITransport(app=mcp_app)},
    )  # fmt: skip
    runtime.start(started)
    yield started
    await runtime.stop()
    lifecycle.reset()
    assert engine.pool.checkedout() == 0  # every run and request returned its connections


async def start(member: Member, world: MainWorld) -> Any:
    async with api_client(member) as api:
        return await api.post(f"/investigations/{world.investigation_id}/start")


# --- H1 ------------------------------------------------------------------------------------


async def test_running_turns_hold_no_connection_and_the_api_stays_responsive(
    rt, orgs, worlds, limits
):
    """The audit's H1 scenario: as many running turns as the pool has connections (15),
    all inside a slow model call. Before 7D-2A each held a connection for its advisory lock
    and the API stopped; now none is held and ordinary requests keep succeeding."""
    a, _ = orgs
    capacity = engine.pool.size() + engine.pool._max_overflow  # type: ignore[attr-defined]
    limits(running=capacity, per_org=capacity)
    model = GatedModel()
    rt.run_kwargs |= {"main": model, "local": ScriptedModel()}
    many = await worlds(a, capacity)
    for world in many:
        assert (await start(a, world)).status_code == 202

    async def all_waiting() -> bool:
        return model.waiting == capacity

    await eventually(all_waiting)
    assert await live_leases(a.organization_id) == capacity
    assert engine.pool.checkedout() == 0  # nothing held during model calls

    async with api_client(a) as api, asyncio.timeout(10):
        reads = await asyncio.gather(
            *[api.get(f"/investigations/{w.investigation_id}") for w in many],
            *[api.get("/investigations", params={"organization_id": str(a.organization_id)})
              for _ in range(capacity)],
        )  # fmt: skip
    assert {r.status_code for r in reads} == {200}

    model.gate.set()
    await eventually(finished(*many))
    assert {await status_of(w) for w in many} == {TurnStatus.FAILED}
    await eventually(_no_leases)  # a finished turn's run releases its lease right after


async def test_limits_queue_and_refuse_without_letting_one_tenant_monopolize(
    rt, orgs, worlds, limits
):
    a, b = orgs
    limits(running=3, per_org=2, queued=4, queued_per_org=1)
    model = GatedModel()
    rt.run_kwargs |= {"main": model, "local": ScriptedModel()}
    a1, a2, a3, a4 = await worlds(a, 4)
    (b1,) = await worlds(b, 1)

    for world in (a1, a2, a3):
        assert (await start(a, world)).status_code == 202
    # A's third is admitted but queued: A may run only 2 of the 3 server slots.
    await eventually(lambda: _waiting_is(model, 2))
    assert await status_of(a3) == TurnStatus.PENDING
    await eventually(lambda: _tasks_are(2))
    # A's fourth would exceed its running + queued allowance (2 + 1): refused, unchanged.
    refused = await start(a, a4)
    assert refused.status_code == 429 and refused.headers["retry-after"]
    assert "organization limit of 3" in refused.json()["detail"]
    assert await status_of(a4) == TurnStatus.PENDING
    async with SessionLocal() as s:
        assert (await s.get(InvestigationTurn, a4.turn_id)).run_requested_at is None  # type: ignore[union-attr]
    # B still gets the free slot ahead of A's queue.
    assert (await start(b, b1)).status_code == 202
    await eventually(lambda: _waiting_is(model, 3))
    assert (await live_leases(a.organization_id), await live_leases(b.organization_id)) == (2, 1)
    assert await status_of(a3) == TurnStatus.PENDING
    assert engine.pool.checkedout() == 0

    model.gate.set()  # runs end (failing); the dispatcher then starts A's queued turn
    await eventually(finished(a1, a2, a3, b1))
    async with SessionLocal() as s:
        started = await s.scalar(
            select(func.count()).where(
                ProgressEvent.investigation_id == a3.investigation_id,
                ProgressEvent.type == "turn.started",
            )
        )
    assert started == 1
    await eventually(_no_leases)  # a finished turn's run releases its lease right after


async def _waiting_is(model: GatedModel, count: int) -> bool:
    return model.waiting == count


async def _tasks_are(count: int) -> bool:
    return len(runner._tasks) == count


async def test_concurrent_acquisitions_never_duplicate_or_exceed_limits(orgs, worlds, limits):
    a, b = orgs
    limits(running=3, per_org=2)
    a_worlds = await worlds(a, 4)
    b_worlds = await worlds(b, 2)

    async def acquire(world: MainWorld) -> leases.Lease | Exception:
        try:
            async with SessionLocal() as s:
                return await leases.acquire(s, world.investigation_id, world.turn_id)
        except leases.RunnerBusy as exc:
            return exc

    # One investigation, ten racing runners: exactly one wins.
    same = await asyncio.gather(*(acquire(a_worlds[0]) for _ in range(10)))
    winners = [r for r in same if isinstance(r, leases.Lease)]
    assert len(winners) == 1
    assert all(type(r) is leases.RunnerBusy for r in same if r not in winners)
    # Everyone else at once: the server and organization limits hold exactly.
    rest = await asyncio.gather(*(acquire(w) for w in a_worlds[1:] + b_worlds))
    assert await live_leases() == 3
    assert await live_leases(a.organization_id) <= 2
    assert sum(isinstance(r, leases.CapacityFull) for r in rest) == 3
    async with SessionLocal() as s:
        for held in [*winners, *(r for r in rest if isinstance(r, leases.Lease))]:
            await leases.release(s, held)
    assert await live_leases() == 0


# --- leases: loss, fencing, crash recovery --------------------------------------------------


async def test_a_stale_holder_cannot_move_the_turn(orgs, worlds):
    a, _ = orgs
    (world,) = await worlds(a, 1)
    async with runner.investigation_lock(world.investigation_id, world.turn_id):
        async with SessionLocal() as s:
            await s.execute(
                update(RunLease)
                .where(RunLease.investigation_id == world.investigation_id)
                .values(lease_id=uuid.uuid4(), holder="another runner")
            )
            await s.commit()
        async with SessionLocal() as s:
            with pytest.raises(LeaseLost):
                await turns.fail_turn(s, world.turn_id, "stale")
    assert await status_of(world) == TurnStatus.PENDING
    # The new holder's lease survives the old holder's release.
    assert await live_leases() == 1
    # Outside any run (the API's cancel, operators) nothing is fenced.
    async with SessionLocal() as s:
        await turns.fail_turn(s, world.turn_id, "cancelled by user")
        await s.execute(delete(RunLease).where(RunLease.investigation_id == world.investigation_id))
        await s.commit()
    assert await status_of(world) == TurnStatus.CANCELLED


async def test_a_lost_lease_stops_the_run_and_records_nothing(
    rt, orgs, worlds, monkeypatch: pytest.MonkeyPatch
):
    a, _ = orgs
    monkeypatch.setattr(leases, "RENEW_SECONDS", 0.1)
    model = GatedModel()
    rt.run_kwargs |= {"main": model, "local": ScriptedModel()}
    (world,) = await worlds(a, 1)
    assert (await start(a, world)).status_code == 202
    await eventually(lambda: _waiting_is(model, 1))
    task = runner._tasks[world.investigation_id]

    async with SessionLocal() as s:  # another runner took the lease over
        await s.execute(
            update(RunLease)
            .where(RunLease.investigation_id == world.investigation_id)
            .values(lease_id=uuid.uuid4(), holder="another runner")
        )
        await s.commit()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert await status_of(world) == TurnStatus.RUNNING  # neither cancelled nor failed by us
    assert await live_leases() == 1  # the new holder's
    async with SessionLocal() as s:
        await s.execute(delete(RunLease).where(RunLease.investigation_id == world.investigation_id))
        await s.commit()


async def test_a_crashed_runner_is_resumed_once_after_its_lease_expires(
    rt,
    orgs,
    worlds,
    signoz,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a, _ = orgs
    signoz("trace_stats_scalar.json")
    model = GatedModel()
    rt.run_kwargs |= {"main": model, "local": ScriptedModel()}
    (world,) = await worlds(a, 1)
    assert (await start(a, world)).status_code == 202
    await eventually(lambda: _waiting_is(model, 1))

    # The process dies: its run and heartbeat stop, but nothing releases the lease.
    release = runner._release

    async def kept(_: Any, heartbeat: asyncio.Task[None]) -> None:
        heartbeat.cancel()

    monkeypatch.setattr(runner, "_release", kept)
    await runtime.stop()
    monkeypatch.setattr(runner, "_release", release)
    assert await status_of(world) == TurnStatus.RUNNING
    assert await live_leases() == 1

    lifecycle.reset()  # restart with recovery
    runtime.start(rt)
    main, local = happy(world)
    rt.run_kwargs |= {"main": main, "local": local}
    plan = await runtime.recover()
    item = next(p for p in plan if p["turn_id"] == world.turn_id)
    assert (item["action"], item["done"]) == ("resume", False)  # dead runner's lease is live
    await asyncio.sleep(0.5)
    assert await status_of(world) == TurnStatus.RUNNING and not runner._tasks

    async with SessionLocal() as s:  # ... until it expires
        await s.execute(update(RunLease).values(expires_at=func.now() - timedelta(seconds=1)))
        await s.commit()
    await eventually(finished(world))
    assert await status_of(world) == TurnStatus.COMPLETED
    async with SessionLocal() as s:
        events = list(
            await s.scalars(
                select(ProgressEvent.type).where(
                    ProgressEvent.investigation_id == world.investigation_id
                )
            )
        )
    assert events.count("turn.started") == 1 and events.count("turn.completed") == 1
    await eventually(_no_leases)  # a finished turn's run releases its lease right after


# --- capacity is released on every ending ---------------------------------------------------


async def test_cancel_timeout_error_and_shutdown_release_capacity(
    rt, orgs, worlds, monkeypatch: pytest.MonkeyPatch
):
    a, _ = orgs
    model = GatedModel()
    rt.run_kwargs |= {"main": model, "local": ScriptedModel()}
    cancelled, timed_out, failed, interrupted = await worlds(a, 4)

    assert (await start(a, cancelled)).status_code == 202
    await eventually(lambda: _waiting_is(model, 1))
    async with api_client(a) as api:
        assert (
            await api.post(f"/investigations/{cancelled.investigation_id}/cancel")
        ).status_code == 200
    assert await status_of(cancelled) == TurnStatus.CANCELLED
    await eventually(lambda: _no_leases())

    wall_clock = policy.TURN_WALL_CLOCK_SECONDS
    monkeypatch.setattr(policy, "TURN_WALL_CLOCK_SECONDS", 0.5)
    assert (await start(a, timed_out)).status_code == 202
    await eventually(finished(timed_out))
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, timed_out.turn_id)
    assert (turn.status, turn.error_code) == (TurnStatus.FAILED, "turn_timeout")  # type: ignore[union-attr]
    await eventually(lambda: _no_leases())
    monkeypatch.setattr(policy, "TURN_WALL_CLOCK_SECONDS", wall_clock)

    model.gate.set()  # the provider fails: the turn fails and frees its slot
    assert (await start(a, failed)).status_code == 202
    await eventually(finished(failed))
    assert await status_of(failed) == TurnStatus.FAILED
    await eventually(lambda: _no_leases())

    model.gate.clear()
    assert (await start(a, interrupted)).status_code == 202
    await eventually(lambda: _waiting_is(model, 1))
    await runtime.stop()  # shutdown: the turn stays running for recovery, the slot is freed
    assert await status_of(interrupted) == TurnStatus.RUNNING
    assert await live_leases() == 0 and not runner._tasks  # stop() waits for the release
    async with SessionLocal() as s:  # not resumed in this test
        await turns.fail_turn(s, interrupted.turn_id, "test cleanup")
    lifecycle.reset()
    runtime.start(rt)


async def _no_leases() -> bool:
    return await live_leases() == 0 and not runner._tasks


async def test_admission_refuses_start_and_follow_up_beyond_the_queue(orgs, worlds, limits):
    a, _ = orgs
    limits(running=1, per_org=1)
    busy, start_me, ask_me = await worlds(a, 3)
    async with SessionLocal() as s:
        held = await leases.acquire(s, busy.investigation_id, busy.turn_id)
    try:
        async with SessionLocal() as s:
            with pytest.raises(leases.QueueFull):
                await turns.request_turn_run(s, start_me.turn_id)
        async with SessionLocal() as s:
            with pytest.raises(leases.QueueFull):
                await turns.create_follow_up_turn(
                    s, ask_me.investigation_id, "and now?", request_run=True
                )
        async with SessionLocal() as s:
            turn = await s.get(InvestigationTurn, start_me.turn_id)
            assert turn is not None and turn.run_requested_at is None
            count = await s.scalar(
                select(func.count()).where(
                    InvestigationTurn.investigation_id == ask_me.investigation_id
                )
            )
            assert count == 1
    finally:
        async with SessionLocal() as s:
            await leases.release(s, held)


async def _open_stream(
    investigation_id: uuid.UUID, token: str, opened: asyncio.Event, closed: asyncio.Event
) -> None:
    """A browser's EventSource at the ASGI level (httpx's test transport buffers whole
    responses, so it cannot hold a stream open)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": f"/investigations/{investigation_id}/events",
        "raw_path": b"", "query_string": b"", "root_path": "", "server": ("test", 80),
        "client": ("127.0.0.1", 1), "headers": [(b"host", b"test"),
                                                (b"cookie", f"{COOKIE}={token}".encode())],
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


async def test_open_progress_streams_hold_no_connection(orgs, worlds):
    a, _ = orgs
    watched = await worlds(a, 3)
    closed = asyncio.Event()
    opened = [asyncio.Event() for _ in watched]
    streams = [
        asyncio.create_task(_open_stream(w.investigation_id, a.token, o, closed))
        for w, o in zip(watched, opened, strict=True)
    ]
    try:
        async with asyncio.timeout(5):
            await asyncio.gather(*(o.wait() for o in opened))
        await asyncio.sleep(0.6)  # past a poll: each stream reads with short sessions
        assert engine.pool.checkedout() == 0
    finally:
        closed.set()
        await asyncio.gather(*streams)
    assert engine.pool.checkedout() == 0
