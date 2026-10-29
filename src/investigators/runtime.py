"""In-process investigation runtime for the API (single Uvicorn worker, D020).

Off unless INQUEST_RUNNER_ENABLED=true: a development backend never spends model budget
on its own. When on, the FastAPI lifespan opens one checkpointer, the API submits turns
as background tasks through runner.start_turn_task, and shutdown leaves running turns
running (their checkpoints let the next start resume them). Recovery on startup is a
second opt-in (INQUEST_RUNNER_RECOVER_ON_STARTUP): legacy turns without a checkpoint are
reconciled, never resumed; checkpointed running turns are resumed and accepted-but-never-
started turns are started, as background tasks. Duplicate execution across processes is
prevented by run_turn's run lease (D036).

Capacity (Phase 7D-2A): the API admits a turn only within the running-plus-queued limits
(services.leases.admit). An admitted turn starts at once when a running slot is free;
otherwise it waits in the database and in `_waiting` (bounded by admission) until the
dispatcher finds a slot: when a run ends, and every BACKLOG_RETRY_SECONDS while turns
wait. At most `max_running` run tasks exist in this process, so queued work holds neither
tasks nor database connections.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from src.credentials import resolver
from src.db.session import SessionLocal
from src.investigators import lifecycle, runner
from src.investigators.checkpoint import open_checkpointer
from src.models import (
    ACTIVE_TURN_STATUSES,
    AuditEvent,
    Investigation,
    InvestigationEnvironment,
    InvestigationTurn,
    RunLease,
    TurnStatus,
)
from src.services import leases, turns
from src.services.investigations import InvalidTransition

logger = logging.getLogger(__name__)


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INQUEST_RUNNER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = False
    recover_on_startup: bool = False


class RunnerDisabled(Exception):
    pass


@dataclass
class Runtime:
    checkpointer: BaseCheckpointSaver
    # Passed to run_turn: model and MCP overrides (tests); empty means configured defaults.
    run_kwargs: dict[str, Any] = field(default_factory=dict)


_current: Runtime | None = None
# Turns this process accepted or recovered that have no run task yet: turn -> investigation.
_waiting: dict[uuid.UUID, uuid.UUID] = {}
_wake = asyncio.Event()
_dispatcher: asyncio.Task[None] | None = None
# While turns wait, the dispatcher looks again this often: slots of other processes and
# leases of crashed runners free up without telling this one.
BACKLOG_RETRY_SECONDS = 5.0


def current() -> Runtime:
    if _current is None:
        raise RunnerDisabled(
            "investigation runner is disabled; restart the backend with "
            "INQUEST_RUNNER_ENABLED=true to run investigations"
        )
    return _current


def start(runtime: Runtime) -> None:
    global _current, _dispatcher
    lifecycle.reset()
    _current = runtime
    _waiting.clear()
    _wake.clear()
    _dispatcher = asyncio.create_task(_dispatch_loop())


async def stop() -> None:
    """Cancels this process's turn tasks as a shutdown: their turns stay running, and
    their leases are released so the next start can resume them at once."""
    global _current, _dispatcher
    lifecycle.begin_shutdown()
    if _dispatcher is not None:
        _dispatcher.cancel()
        await asyncio.gather(_dispatcher, return_exceptions=True)
        _dispatcher = None
    tasks = list(runner._tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _waiting.clear()
    _current = None


def _start(investigation_id: uuid.UUID, turn_id: uuid.UUID) -> asyncio.Task[Any]:
    rt = current()
    task = runner.start_turn_task(investigation_id, turn_id, rt.checkpointer, **rt.run_kwargs)
    _waiting.pop(turn_id, None)

    def ended(done: asyncio.Task[Any]) -> None:
        error = None if done.cancelled() else done.exception()
        if isinstance(error, runner.RunnerBusy):
            # No slot after all, or another runner's lease is still live (a crashed
            # process's expires): retry later. Never started, so nothing was recorded.
            _waiting[turn_id] = investigation_id
        elif error is not None:
            logger.error("run of turn %s ended with %s", turn_id, type(error).__name__)
        _wake.set()

    task.add_done_callback(ended)
    return task


def submit(investigation_id: uuid.UUID, turn_id: uuid.UUID) -> asyncio.Task[Any] | None:
    """For a turn the API just accepted: starts it when this process has a free running
    slot, else leaves it queued for the dispatcher. RunnerBusy if this process already
    runs the investigation."""
    current()
    _waiting[turn_id] = investigation_id
    if len(runner._tasks) < leases.capacity_settings().max_running:
        return _start(investigation_id, turn_id)
    _wake.set()
    return None


async def _drain() -> list[uuid.UUID]:
    """Starts waiting turns, oldest accepted first, while this process has running slots
    and their organization is under its limit (acquisition checks both again). Turns that
    finished, were cancelled or run elsewhere are dropped from the waiting set."""
    if not _waiting:
        return []
    settings = leases.capacity_settings()
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(
                    InvestigationTurn.id,
                    InvestigationTurn.investigation_id,
                    InvestigationTurn.status,
                    Investigation.organization_id,
                )
                .join(Investigation, Investigation.id == InvestigationTurn.investigation_id)
                .where(
                    InvestigationTurn.id.in_(list(_waiting)),
                    InvestigationTurn.status.in_(ACTIVE_TURN_STATUSES),
                )
                .order_by(
                    # Interrupted runs first, then the queue in acceptance order.
                    InvestigationTurn.status != TurnStatus.RUNNING,
                    InvestigationTurn.run_requested_at,
                    InvestigationTurn.id,
                )
            )
        ).all()
        running = dict(
            (
                await s.execute(
                    select(RunLease.organization_id, func.count())
                    .where(RunLease.expires_at > func.now())
                    .group_by(RunLease.organization_id)
                )
            ).all()
        )
        held = set(
            await s.scalars(
                select(RunLease.investigation_id).where(
                    RunLease.investigation_id.in_([r.investigation_id for r in rows]),
                    RunLease.expires_at > func.now(),
                )
            )
        )
    alive = {r.id for r in rows}
    for turn_id in [t for t in _waiting if t not in alive]:
        _waiting.pop(turn_id, None)
    started = []
    for row in rows:
        if row.investigation_id in held or row.investigation_id in runner._tasks:
            continue
        if len(runner._tasks) >= settings.max_running:
            break
        if (
            row.organization_id is not None
            and running.get(row.organization_id, 0) >= settings.max_running_per_organization
        ):
            continue
        try:
            _start(row.investigation_id, row.id)
        except runner.RunnerBusy:
            continue
        running[row.organization_id] = running.get(row.organization_id, 0) + 1
        started.append(row.id)
    return started


async def _dispatch_loop() -> None:
    while True:
        _wake.clear()
        try:
            await _drain()
        except (SQLAlchemyError, OSError) as exc:
            logger.warning("dispatching queued turns failed (%s)", type(exc).__name__)
        try:
            async with asyncio.timeout(BACKLOG_RETRY_SECONDS if _waiting else None):
                await _wake.wait()
        except TimeoutError:
            pass


async def cancel(investigation_id: uuid.UUID, error_code: str = "cancelled") -> uuid.UUID | None:
    """Cancels the investigation's active turn, recording `error_code`. A turn running in
    this process is cancelled through its task (which records it); a turn with no task here
    (queued and not yet started, or orphaned) is cancelled in the database. Earlier turns,
    evidence and report versions are untouched. Idempotent: a turn that ended meanwhile is
    left as it ended. Returns the turn, or None when none was active."""
    async with SessionLocal() as s:
        turn = await s.scalar(
            select(InvestigationTurn).where(
                InvestigationTurn.investigation_id == investigation_id,
                InvestigationTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
    if turn is None:
        return None
    await runner.cancel_investigation_task(investigation_id, error_code)
    # Whatever the task managed to record, the turn must end cancelled: a queued turn whose
    # task was cancelled before it started would otherwise be started again by recovery.
    async with SessionLocal() as s:
        current_status = await s.scalar(
            select(InvestigationTurn.status).where(InvestigationTurn.id == turn.id)
        )
        if current_status in ACTIVE_TURN_STATUSES:
            try:
                await turns.finish_turn(s, turn.id, TurnStatus.CANCELLED, error_code=error_code)
            except InvalidTransition:
                await s.rollback()
    return turn.id


async def cancel_environment_turns(
    environment_id: uuid.UUID,
    error_code: str,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Cancels every active turn whose investigation covers the environment, after its
    credentials were revoked (cleared, or deleted by an ownership transfer). Call it after
    the revocation committed and without holding locks: from then on no model step or tool
    call can resolve the old credentials, and this stops the turns already under way, with
    the usual cancellation lifecycle (assignments cancelled, history kept). A request
    already sent to a provider is not recalled; it completes or times out unused.
    Works with the runner disabled too (queued turns are cancelled in the database)."""
    async with SessionLocal() as s:
        investigation_ids = list(
            await s.scalars(
                select(InvestigationTurn.investigation_id)
                .distinct()
                .join(
                    InvestigationEnvironment,
                    InvestigationEnvironment.investigation_id == InvestigationTurn.investigation_id,
                )
                .where(
                    InvestigationEnvironment.environment_id == environment_id,
                    InvestigationTurn.status.in_(ACTIVE_TURN_STATUSES),
                )
            )
        )
    cancelled = [
        turn_id
        for investigation_id in investigation_ids
        if (turn_id := await cancel(investigation_id, error_code)) is not None
    ]
    if cancelled:
        async with SessionLocal() as s, s.begin():
            s.add(
                AuditEvent(
                    action="turns.cancel",
                    actor=f"user:{actor_user_id}",
                    actor_user_id=actor_user_id,
                    organization_id=organization_id,
                    details_json={
                        "environment_id": str(environment_id),
                        "reason": error_code,
                        "turn_ids": sorted(str(t) for t in cancelled),
                    },
                )
            )
    return cancelled


async def stop_environment_runs(
    environment_id: uuid.UUID,
    reason: str,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    """cancel_environment_turns for API routes, after a committed revocation. A failure is
    logged, not raised: the revocation stands, and the turns are refused at their next
    model step or tool call regardless."""
    try:
        await cancel_environment_turns(
            environment_id, reason, organization_id=organization_id, actor_user_id=actor_user_id
        )
    except Exception as exc:
        logger.warning(
            "cancelling turns of environment %s failed (%s)", environment_id, type(exc).__name__
        )


async def recover() -> list[dict[str, Any]]:
    rt = current()
    plan = await runner.recovery_plan(rt.checkpointer)
    for item in plan:
        if item["action"] == "reconcile":
            async with SessionLocal() as s:
                item["done"] = await turns.reconcile_legacy_turn(s, item["turn_id"])
        else:
            _waiting[item["turn_id"]] = item["investigation_id"]
    # Within capacity now; the rest when slots free up (or a dead runner's lease expires).
    started = set(await _drain())
    _wake.set()  # the dispatcher retries what could not start yet
    for item in plan:
        if item["action"] != "reconcile":
            item["done"] = item["turn_id"] in started
    logger.info("recovery: %s", [(str(i["turn_id"]), i["action"]) for i in plan])
    return plan


async def log_credential_readiness() -> None:
    try:
        async with SessionLocal() as s:
            problems = await resolver.startup_problems(s)
    except Exception as exc:
        # Startup must not depend on this report; runs check credentials themselves.
        logger.warning("credential readiness check failed (%s)", type(exc).__name__)
        return
    for problem in problems:
        logger.warning("credentials: %s", problem)


@asynccontextmanager
async def lifespan(settings: RunnerSettings | None = None) -> AsyncIterator[None]:
    settings = settings or RunnerSettings()
    await log_credential_readiness()
    if not settings.enabled:
        yield
        return
    async with open_checkpointer() as checkpointer:
        start(Runtime(checkpointer))
        try:
            if settings.recover_on_startup:
                await recover()
            yield
        finally:
            await stop()
