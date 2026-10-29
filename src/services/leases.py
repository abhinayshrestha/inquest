"""Run leases and execution capacity (Phase 7D-2A, D036).

A runner may drive an investigation's active turn only while it holds that
investigation's lease: a `run_leases` row with a random `lease_id`, an `expires_at` the
holder renews, and nothing held open in between. This replaced a session advisory lock,
which kept one pooled connection checked out for a whole run, so that pool-size running
turns stopped every API request (audit H1).

- Exactly one holder: the row's primary key is the investigation; a live row refuses
  other runners (RunnerBusy). An expired row may be taken over, which changes `lease_id`.
- Fencing: turn transitions (services.turns._locked_turn) call check_fence under the
  investigation row lock; acquisition takes the same row lock. A runner whose lease
  expired or was taken over therefore cannot start, dispatch, complete or fail the turn.
- Capacity: live leases are the running investigations. Acquisition refuses beyond the
  global and per-organization running limits (CapacityFull); API admission refuses beyond
  running plus queued limits (QueueFull). Both count under one transaction-level advisory
  lock, so concurrent requests cannot overshoot. A crashed runner's lease stops counting
  once it expires.

Every function here runs short transactions; none is held across a model or telemetry
call.
"""

import os
import socket
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import ColumnElement, delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Investigation, InvestigationTurn, RunLease, TurnStatus

# A holder renews every RENEW_SECONDS and stops its run once it could not renew for
# TTL_SECONDS - RENEW_SECONDS, before anyone else may take the lease over.
TTL_SECONDS = 30.0
RENEW_SECONDS = 10.0
CAPACITY_KEY = "inquest:runner:capacity"
HOLDER = f"{socket.gethostname()[:120]}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class RunnerBusy(Exception):
    """Another runner (this process or another) holds this investigation's lease."""


class CapacityFull(RunnerBusy):
    """No running slot is free (server-wide or for the organization); the turn stays
    queued and starts when one is."""

    def __init__(self, scope: str, limit: int):
        super().__init__(f"{scope} running limit of {limit} reached")
        self.scope = scope
        self.limit = limit


class QueueFull(Exception):
    """Admission refused: running plus queued turns are at the limit."""

    def __init__(self, scope: str, limit: int):
        super().__init__(f"{scope} limit of {limit} active investigations reached")
        self.scope = scope
        self.limit = limit


class LeaseLost(Exception):
    """This runner's lease expired or was taken over: it must stop without writing."""


class CapacitySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INQUEST_RUNNER_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Running turns. Each uses database connections only for short transactions, so these
    # bound model and telemetry concurrency, not the pool.
    max_running: int = Field(default=4, ge=1, le=64)
    max_running_per_organization: int = Field(default=2, ge=1, le=64)
    # Accepted turns waiting for a running slot; beyond these, start and follow-up
    # requests are refused (429).
    max_queued: int = Field(default=16, ge=0, le=1024)
    max_queued_per_organization: int = Field(default=4, ge=0, le=1024)

    @model_validator(mode="after")
    def _per_organization_within_global(self) -> "CapacitySettings":
        if self.max_running_per_organization > self.max_running:
            raise ValueError("max_running_per_organization exceeds max_running")
        if self.max_queued_per_organization > self.max_queued:
            raise ValueError("max_queued_per_organization exceeds max_queued")
        return self


@lru_cache
def capacity_settings() -> CapacitySettings:
    return CapacitySettings()


@dataclass
class Lease:
    investigation_id: uuid.UUID
    lease_id: uuid.UUID
    # Set when renewal failed or a fenced write found the lease gone.
    lost: bool = False


# The lease of the run in progress, inherited by every task the run starts (graph nodes,
# Local Investigators), so turn transitions can be fenced without passing it around.
current_lease: ContextVar[Lease | None] = ContextVar("inquest_run_lease", default=None)


def _live() -> ColumnElement[bool]:
    return RunLease.expires_at > func.now()


def _expiry() -> ColumnElement:
    return func.now() + timedelta(seconds=TTL_SECONDS)


async def _capacity_lock(session: AsyncSession) -> None:
    # Transaction-level: released at commit or rollback, never held across a run.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(CAPACITY_KEY, 0)))
    )


def _same_organization(column, organization_id: uuid.UUID | None) -> ColumnElement[bool]:
    return column.is_(None) if organization_id is None else column == organization_id


async def _running(session: AsyncSession, organization_id: uuid.UUID | None) -> tuple[int, int]:
    total = await session.scalar(select(func.count()).select_from(RunLease).where(_live()))
    mine = await session.scalar(
        select(func.count())
        .select_from(RunLease)
        .where(_live(), _same_organization(RunLease.organization_id, organization_id))
    )
    return total or 0, mine or 0


def queued_turns():
    """Accepted turns no runner holds yet, oldest first: the queue."""
    return (
        select(
            InvestigationTurn.id, InvestigationTurn.investigation_id, Investigation.organization_id
        )
        .join(Investigation, Investigation.id == InvestigationTurn.investigation_id)
        .where(
            InvestigationTurn.status == TurnStatus.PENDING,
            InvestigationTurn.run_requested_at.is_not(None),
            ~exists().where(
                RunLease.investigation_id == InvestigationTurn.investigation_id, _live()
            ),
        )
        .order_by(InvestigationTurn.run_requested_at, InvestigationTurn.id)
    )


async def _queued(session: AsyncSession, organization_id: uuid.UUID | None) -> tuple[int, int]:
    queue = queued_turns().subquery()
    total = await session.scalar(select(func.count()).select_from(queue))
    mine = await session.scalar(
        select(func.count())
        .select_from(queue)
        .where(_same_organization(queue.c.organization_id, organization_id))
    )
    return total or 0, mine or 0


async def admit(session: AsyncSession, organization_id: uuid.UUID | None) -> None:
    """Admission for one more accepted turn, inside the caller's transaction and before
    it locks the investigation (lock order: capacity, then investigation). QueueFull when
    running plus queued turns are at the organization's or the server's limit."""
    settings = capacity_settings()
    await _capacity_lock(session)
    running, running_mine = await _running(session, organization_id)
    queued, queued_mine = await _queued(session, organization_id)
    if organization_id is not None:
        limit = settings.max_running_per_organization + settings.max_queued_per_organization
        if running_mine + queued_mine >= limit:
            raise QueueFull("organization", limit)
    limit = settings.max_running + settings.max_queued
    if running + queued >= limit:
        raise QueueFull("server", limit)


async def acquire(
    session: AsyncSession, investigation_id: uuid.UUID, turn_id: uuid.UUID | None = None
) -> Lease:
    """Takes the investigation's lease in one short transaction, or raises RunnerBusy
    (live lease elsewhere) or CapacityFull. Locks the investigation row so a fenced write
    of a previous holder is either committed before this or refused after it."""
    settings = capacity_settings()
    await _capacity_lock(session)
    organization_id = (
        await session.execute(
            select(Investigation.organization_id)
            .where(Investigation.id == investigation_id)
            .with_for_update()
        )
    ).scalar_one()
    held = await session.scalar(
        select(RunLease.lease_id).where(RunLease.investigation_id == investigation_id, _live())
    )
    if held is not None:
        await session.rollback()
        raise RunnerBusy(f"investigation {investigation_id} is already being run")
    running, running_mine = await _running(session, organization_id)
    full = None
    if running >= settings.max_running:
        full = CapacityFull("server", settings.max_running)
    elif organization_id is not None and running_mine >= settings.max_running_per_organization:
        full = CapacityFull("organization", settings.max_running_per_organization)
    if full is not None:
        await session.rollback()
        raise full
    lease = Lease(investigation_id, uuid.uuid4())
    values = {
        "organization_id": organization_id,
        "turn_id": turn_id,
        "lease_id": lease.lease_id,
        "holder": HOLDER,
        "acquired_at": func.now(),
        "expires_at": _expiry(),
    }
    await session.execute(
        insert(RunLease)
        .values(investigation_id=investigation_id, **values)
        .on_conflict_do_update(index_elements=[RunLease.investigation_id], set_=values)
    )
    await session.commit()
    return lease


async def renew(session: AsyncSession, lease: Lease) -> bool:
    """Extends a lease that is still this holder's and still live; False otherwise."""
    renewed = await session.scalar(
        update(RunLease)
        .where(
            RunLease.investigation_id == lease.investigation_id,
            RunLease.lease_id == lease.lease_id,
            _live(),
        )
        .values(expires_at=_expiry())
        .returning(RunLease.lease_id)
    )
    await session.commit()
    return renewed is not None


async def release(session: AsyncSession, lease: Lease) -> None:
    """Frees the slot; a lease already taken over is left to its new holder."""
    await session.execute(
        delete(RunLease).where(
            RunLease.investigation_id == lease.investigation_id,
            RunLease.lease_id == lease.lease_id,
        )
    )
    await session.commit()


async def check_fence(session: AsyncSession, investigation_id: uuid.UUID) -> None:
    """For writes that move a turn, after the caller locked the investigation row. Inside
    a run: LeaseLost unless that run still holds a live lease. Outside a run (API cancel,
    operators, recovery bookkeeping) there is no lease to check."""
    lease = current_lease.get()
    if lease is None or lease.investigation_id != investigation_id:
        return
    held = await session.scalar(
        select(RunLease.lease_id).where(
            RunLease.investigation_id == investigation_id,
            RunLease.lease_id == lease.lease_id,
            _live(),
        )
    )
    if held is None:
        lease.lost = True
        raise LeaseLost(f"run lease of investigation {investigation_id} is no longer held")
