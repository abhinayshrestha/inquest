import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from src.db.session import SessionLocal
from src.models import AssignmentStatus, InvestigationAssignment, ToolCallStatus
from src.tools import ToolExecutionResult, policy
from src.tools import executor as executor_module
from tests.signoz.conftest import load_fixture
from tests.tools.conftest import World, trace_stats_args


async def until(predicate: Callable[[], bool]) -> None:
    # Polls state owned by other tasks (task.done(), request lists); no event to wait on.
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def gated() -> tuple[Any, asyncio.Event, list[httpx.Request]]:
    """A SigNoz that holds every request until the gate opens."""
    gate = asyncio.Event()
    arrived: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        arrived.append(request)
        await gate.wait()
        return httpx.Response(200, json=load_fixture("trace_stats_scalar.json"))

    return handler, gate, arrived


def start(world: World, handler: Any, **kwargs: Any) -> "asyncio.Task[ToolExecutionResult]":
    return asyncio.create_task(
        world.run("signoz.trace_stats", kwargs.pop("args", trace_stats_args()), handler, **kwargs)
    )


async def test_parallel_calls_respect_the_concurrency_limit(world: World):
    handler, gate, arrived = gated()
    tasks = [start(world, handler) for _ in range(5)]
    await until(lambda: len(arrived) == 3 and sum(t.done() for t in tasks) == 2)

    early = [t.result() for t in tasks if t.done()]
    assert all(r.error.code == "policy.concurrency_limit" and r.error.retryable for r in early)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert sorted(r.status for r in results) == sorted(
        [ToolCallStatus.SUCCEEDED] * 3 + [ToolCallStatus.REJECTED] * 2
    )
    assert len(arrived) == 3


async def test_parallel_calls_never_exceed_the_call_budget(world: World, monkeypatch):
    monkeypatch.setattr(policy, "MAX_TOOL_CALLS_PER_ASSIGNMENT", 2)
    handler, gate, arrived = gated()
    gate.set()
    results = await asyncio.gather(*(start(world, handler) for _ in range(6)))

    assert sum(r.status is ToolCallStatus.SUCCEEDED for r in results) == 2
    assert {r.error.code for r in results if r.error} == {"policy.call_budget_exhausted"}
    assert len(arrived) == 2
    calls = await world.tool_calls()
    assert sum(c.status is not ToolCallStatus.REJECTED for c in calls) == 2


async def test_parallel_duplicates_execute_once(world: World):
    handler, gate, arrived = gated()
    args = trace_stats_args()
    tasks = [start(world, handler, args=args, key="lg-dup") for _ in range(3)]
    await until(lambda: len(arrived) == 1 and sum(t.done() for t in tasks) == 2)

    for duplicate in (t.result() for t in tasks if t.done()):
        assert duplicate.replayed and duplicate.status is ToolCallStatus.RUNNING
        assert duplicate.error.code == "executor.duplicate_in_progress"
    gate.set()
    results = await asyncio.gather(*tasks)

    assert len({r.tool_call_id for r in results}) == 1
    assert len(await world.tool_calls()) == 1
    assert len(arrived) == 1
    # Once finished, a late duplicate replays the stored result.
    late = await world.run("signoz.trace_stats", args, handler, key="lg-dup")
    assert late.replayed and late.status is ToolCallStatus.SUCCEEDED
    assert late.evidence_ids == next(r for r in results if not r.replayed).evidence_ids


async def test_cancellation_during_the_signoz_call_is_recorded(world: World):
    handler, _, arrived = gated()
    task = start(world, handler)
    await until(lambda: len(arrived) == 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (call,) = await world.tool_calls()
    assert (call.status, call.error_code) == (ToolCallStatus.CANCELLED, "executor.cancelled")
    assert call.started_at is not None and call.completed_at >= call.started_at
    assert call.duration_ms is not None
    assert await world.evidence() == []


async def test_cancellation_during_admission_still_leaves_a_final_row(world: World):
    handler, _, arrived = gated()
    async with SessionLocal() as holder:
        await holder.begin()
        await holder.execute(
            select(InvestigationAssignment)
            .where(InvestigationAssignment.id == world.assignment_id)
            .with_for_update()
        )
        task = start(world, handler)
        await asyncio.sleep(0.1)  # admission is now blocked on the assignment lock
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # it waits for admission to finish instead of abandoning it
        await holder.commit()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert arrived == []
    (call,) = await world.tool_calls()
    assert call.status is ToolCallStatus.CANCELLED
    assert call.started_at is not None and call.completed_at is not None


async def test_cancellation_during_finalization_still_commits(world: World, monkeypatch):
    entered = asyncio.Event()
    real = executor_module._finalize_in

    async def slow_finalize(*args: Any, **kwargs: Any) -> ToolExecutionResult:
        entered.set()
        await asyncio.sleep(0.1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(executor_module, "_finalize_in", slow_finalize)
    handler, gate, _ = gated()
    gate.set()
    task = start(world, handler)
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (call,) = await world.tool_calls()
    assert call.status is ToolCallStatus.SUCCEEDED
    assert len(await world.evidence()) == 1


async def test_status_change_while_queued_on_the_lock_is_respected(world: World):
    handler, _, arrived = gated()
    async with SessionLocal() as holder:
        await holder.begin()
        assignment = await holder.scalar(
            select(InvestigationAssignment)
            .where(InvestigationAssignment.id == world.assignment_id)
            .with_for_update()
        )
        task = start(world, handler)
        await asyncio.sleep(0.1)
        assignment.status = AssignmentStatus.CANCELLED
        await holder.commit()

    result = await task
    assert result.error.code == "policy.assignment_not_running"
    assert arrived == []


async def test_assignment_closed_before_finalization_withholds_the_result(world: World):
    handler, gate, arrived = gated()
    task = start(world, handler)
    await until(lambda: len(arrived) == 1)
    await world.set_status(assignment=AssignmentStatus.TIMED_OUT)
    gate.set()
    result = await task

    assert result.status is ToolCallStatus.CANCELLED
    assert result.error.code == "executor.assignment_closed"
    assert result.data is None and result.evidence_ids == []
    assert await world.evidence() == []
