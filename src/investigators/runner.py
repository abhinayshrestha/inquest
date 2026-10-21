"""Turn runner: starts, resumes, recovers and cancels Main Investigator turns.

- One runner per investigation at a time, across processes: a PostgreSQL advisory lock
  held for the whole run (the in-process task registry is only a convenience).
- A pending turn runs from the start. A running turn resumes only from its own
  checkpoint (thread turn:{turn_id}). A running turn without one, from the Phase 2
  backfill or the legacy start_investigation path, is never resumed: there is no
  workflow to continue. It is failed explicitly (legacy_no_checkpoint) and the
  investigation returns to idle; its evidence and findings stay.
- Wall clock: TURN_WALL_CLOCK_SECONDS from turn.started_at, across restarts.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import select, text

from src.db.session import SessionLocal, engine
from src.investigators import lifecycle, policy
from src.investigators.llm import local_model, main_model
from src.investigators.main_graph import MainContext, build_graph
from src.mcp_server import McpSettings
from src.models import InvestigationReport, InvestigationTurn, TurnStatus
from src.services import turns

logger = logging.getLogger(__name__)

FINISHED = (TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED)


class RunnerBusy(Exception):
    """Another runner (this process or another) is driving this investigation."""


class TurnNotFound(Exception):
    pass


@dataclass(frozen=True)
class TurnRunResult:
    turn_id: uuid.UUID
    status: TurnStatus
    error_code: str | None
    resumed: bool
    reconciled: bool
    report_version: int | None
    outcome: str | None
    rounds: int
    model_calls: int
    tokens: int
    problems: list[str]
    validation_audit: list[str]


def turn_thread_id(turn_id: object) -> str:
    return f"turn:{turn_id}"


@asynccontextmanager
async def investigation_lock(investigation_id: uuid.UUID) -> AsyncIterator[None]:
    """Session-level advisory lock on a dedicated connection, held while the turn runs.
    Session-level so it survives the many short transactions of a run."""
    key = f"inquest:investigation:{investigation_id}"
    async with engine.connect() as conn:
        locked = await conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": key}
        )
        await conn.commit()
        if not locked:
            raise RunnerBusy(f"investigation {investigation_id} is already being run")
        try:
            yield
        finally:
            await conn.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key}
            )
            await conn.commit()


async def _load(turn_id: uuid.UUID) -> InvestigationTurn:
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, turn_id)
    if turn is None:
        raise TurnNotFound(turn_id)
    return turn


async def _result(
    turn_id: uuid.UUID, values: dict[str, Any], *, resumed: bool, reconciled: bool
) -> TurnRunResult:
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, turn_id)
        report = await s.scalar(
            select(InvestigationReport).where(InvestigationReport.turn_id == turn_id)
        )
    assert turn is not None
    return TurnRunResult(
        turn_id=turn_id,
        status=turn.status,
        error_code=turn.error_code,
        resumed=resumed,
        reconciled=reconciled,
        report_version=report.version if report else None,
        outcome=report.report_json.get("outcome") if report else None,
        rounds=values.get("rounds", 0),
        model_calls=values.get("model_calls", 0),
        tokens=values.get("tokens", 0),
        problems=list(values.get("problems", [])),
        validation_audit=list(values.get("validation_audit", [])),
    )


async def _fail(turn_id: uuid.UUID, error_code: str) -> None:
    async with SessionLocal() as s:
        await turns.fail_turn(s, turn_id, error_code)


async def _cancel(turn_id: uuid.UUID) -> None:
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, turn_id)
        if turn is not None and turn.status not in FINISHED:
            await turns.finish_turn(s, turn_id, TurnStatus.CANCELLED)


async def has_checkpoint(checkpointer: BaseCheckpointSaver, turn_id: uuid.UUID) -> bool:
    graph = build_graph().compile(checkpointer=checkpointer)
    snapshot = await graph.aget_state({"configurable": {"thread_id": turn_thread_id(turn_id)}})
    return bool(snapshot.values) and bool(snapshot.next)


async def run_turn(
    turn_id: uuid.UUID,
    checkpointer: BaseCheckpointSaver,
    *,
    main: BaseChatModel | None = None,
    local: BaseChatModel | None = None,
    mcp_settings: McpSettings | None = None,
    mcp_transport: httpx2.AsyncBaseTransport | None = None,
) -> TurnRunResult:
    """Runs a pending turn, resumes a checkpointed running one, reconciles a running one
    without a checkpoint, and does nothing for a finished one."""
    turn = await _load(turn_id)
    async with investigation_lock(turn.investigation_id):
        turn = await _load(turn_id)
        graph = build_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": turn_thread_id(turn_id)}}
        snapshot = await graph.aget_state(config)
        if turn.status in FINISHED:
            return await _result(turn_id, dict(snapshot.values), resumed=False, reconciled=False)
        resumable = bool(snapshot.values) and bool(snapshot.next)
        if turn.status == TurnStatus.RUNNING and not resumable:
            async with SessionLocal() as s:
                await turns.reconcile_legacy_turn(s, turn_id)
            return await _result(turn_id, {}, resumed=False, reconciled=True)

        started = turn.started_at or datetime.now(UTC)
        remaining = (
            started + timedelta(seconds=policy.TURN_WALL_CLOCK_SECONDS) - datetime.now(UTC)
        ).total_seconds()
        run_context = MainContext(
            main_model=main or main_model(),
            local_model=local or local_model(),
            checkpointer=checkpointer,
            mcp_settings=mcp_settings,
            mcp_transport=mcp_transport,
        )
        deadline = asyncio.timeout(max(remaining, 0))
        try:
            async with deadline:
                await graph.ainvoke(
                    None if resumable else {"turn_id": str(turn_id)},
                    config,
                    context=run_context,
                    durability="sync",
                )
        except TimeoutError:
            if not deadline.expired():
                logger.exception("turn %s failed", turn_id)
            await _fail(turn_id, "turn_timeout" if deadline.expired() else "main_error")
        except asyncio.CancelledError:
            # Shutdown is not a user cancel: leave the turn running for recovery.
            if not lifecycle.shutting_down():
                await asyncio.shield(_cancel(turn_id))
            raise
        except Exception:
            # Exception text may carry provider or database details: log, don't store.
            logger.exception("turn %s failed", turn_id)
            await _fail(turn_id, "main_error")
        values = dict((await graph.aget_state(config)).values)
        return await _result(turn_id, values, resumed=resumable, reconciled=False)


# --- in-process lifecycle ------------------------------------------------------------------

_tasks: dict[uuid.UUID, asyncio.Task[TurnRunResult]] = {}


def start_turn_task(
    investigation_id: uuid.UUID,
    turn_id: uuid.UUID,
    checkpointer: BaseCheckpointSaver,
    **kwargs: Any,
) -> asyncio.Task[TurnRunResult]:
    """Runs a turn in the background (single worker, D020). Refused while this process
    already runs a turn of the investigation; the advisory lock covers other processes."""
    running = _tasks.get(investigation_id)
    if running is not None and not running.done():
        raise RunnerBusy(f"investigation {investigation_id} already has a running task")
    task = asyncio.create_task(run_turn(turn_id, checkpointer, **kwargs))
    _tasks[investigation_id] = task
    task.add_done_callback(lambda _: _tasks.pop(investigation_id, None))
    return task


async def cancel_investigation_task(investigation_id: uuid.UUID) -> bool:
    task = _tasks.get(investigation_id)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return True


async def recovery_plan(checkpointer: BaseCheckpointSaver) -> list[dict[str, Any]]:
    """What boot recovery would do: resume a running turn with a checkpoint, reconcile a
    running turn without one (legacy: never resumed), start a pending turn the API had
    accepted but that never started. Pending turns that were only created are left
    alone. Read-only."""
    async with SessionLocal() as s:
        candidates = list(
            await s.scalars(
                select(InvestigationTurn)
                .where(
                    (InvestigationTurn.status == TurnStatus.RUNNING)
                    | (
                        (InvestigationTurn.status == TurnStatus.PENDING)
                        & InvestigationTurn.run_requested_at.is_not(None)
                    )
                )
                .order_by(InvestigationTurn.created_at)
            )
        )
    plan = []
    for t in candidates:
        if t.status == TurnStatus.PENDING:
            action = "start"
        elif await has_checkpoint(checkpointer, t.id):
            action = "resume"
        else:
            action = "reconcile"
        plan.append(
            {
                "turn_id": t.id,
                "investigation_id": t.investigation_id,
                "seq": t.seq,
                "action": action,
            }
        )
    return plan


async def recover(
    checkpointer: BaseCheckpointSaver, *, resume: bool = False, **kwargs: Any
) -> list[dict[str, Any]]:
    """Applies the recovery plan. Legacy turns are always reconciled (no model calls).
    Checkpointed turns are resumed only when `resume` is set, because resuming spends
    model budget; otherwise they are left for an explicit run."""
    plan = await recovery_plan(checkpointer)
    for item in plan:
        if item["action"] == "reconcile":
            async with SessionLocal() as s:
                item["done"] = await turns.reconcile_legacy_turn(s, item["turn_id"])
        elif resume and item["action"] in ("resume", "start"):
            try:
                item["result"] = await run_turn(item["turn_id"], checkpointer, **kwargs)
            except RunnerBusy:
                item["result"] = "busy"
    return plan
