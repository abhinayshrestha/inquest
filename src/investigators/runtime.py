"""In-process investigation runtime for the API (single Uvicorn worker, D020).

Off unless INQUEST_RUNNER_ENABLED=true: a development backend never spends model budget
on its own. When on, the FastAPI lifespan opens one checkpointer, the API submits turns
as background tasks through runner.start_turn_task, and shutdown leaves running turns
running (their checkpoints let the next start resume them). Recovery on startup is a
second opt-in (INQUEST_RUNNER_RECOVER_ON_STARTUP): legacy turns without a checkpoint are
reconciled, never resumed; checkpointed running turns are resumed and accepted-but-never-
started turns are started, as background tasks. Duplicate execution across processes is
prevented by run_turn's advisory lock.
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
from sqlalchemy import select

from src.db.session import SessionLocal
from src.investigators import lifecycle, runner
from src.investigators.checkpoint import open_checkpointer
from src.models import ACTIVE_TURN_STATUSES, InvestigationTurn, TurnStatus
from src.services import turns

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


def current() -> Runtime:
    if _current is None:
        raise RunnerDisabled(
            "investigation runner is disabled; restart the backend with "
            "INQUEST_RUNNER_ENABLED=true to run investigations"
        )
    return _current


def start(runtime: Runtime) -> None:
    global _current
    lifecycle.reset()
    _current = runtime


async def stop() -> None:
    """Cancels this process's turn tasks as a shutdown: their turns stay running."""
    global _current
    lifecycle.begin_shutdown()
    tasks = list(runner._tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _current = None


def submit(investigation_id: uuid.UUID, turn_id: uuid.UUID) -> asyncio.Task[Any]:
    rt = current()
    return runner.start_turn_task(investigation_id, turn_id, rt.checkpointer, **rt.run_kwargs)


async def cancel(investigation_id: uuid.UUID) -> uuid.UUID | None:
    """Cancels the investigation's active turn. A turn running in this process is
    cancelled through its task (which records it); a turn with no task here (queued and
    not yet started, or orphaned) is cancelled in the database. Earlier turns, evidence
    and report versions are untouched. Returns the cancelled turn, or None."""
    async with SessionLocal() as s:
        turn = await s.scalar(
            select(InvestigationTurn).where(
                InvestigationTurn.investigation_id == investigation_id,
                InvestigationTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
    if turn is None:
        return None
    await runner.cancel_investigation_task(investigation_id)
    # Whatever the task managed to record, the turn must end cancelled: a queued turn whose
    # task was cancelled before it started would otherwise be started again by recovery.
    async with SessionLocal() as s:
        current_status = await s.scalar(
            select(InvestigationTurn.status).where(InvestigationTurn.id == turn.id)
        )
        if current_status in ACTIVE_TURN_STATUSES:
            await turns.finish_turn(s, turn.id, TurnStatus.CANCELLED, error_code="cancelled")
    return turn.id


async def recover() -> list[dict[str, Any]]:
    rt = current()
    plan = await runner.recovery_plan(rt.checkpointer)
    for item in plan:
        if item["action"] == "reconcile":
            async with SessionLocal() as s:
                item["done"] = await turns.reconcile_legacy_turn(s, item["turn_id"])
            continue
        try:
            submit(item["investigation_id"], item["turn_id"])
            item["done"] = True
        except runner.RunnerBusy:
            item["done"] = False
    logger.info("recovery: %s", [(str(i["turn_id"]), i["action"]) for i in plan])
    return plan


@asynccontextmanager
async def lifespan(settings: RunnerSettings | None = None) -> AsyncIterator[None]:
    settings = settings or RunnerSettings()
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
