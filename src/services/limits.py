"""Abuse limits: request rates and resource quotas (Phase 7D-2C-1, D039; audit H3).

Three mechanisms, chosen by what each limit protects:

- Rate limits shared by every application process: fixed-window counters in PostgreSQL
  (`rate_limit_counters`), one atomic upsert per limited subject (client address, user,
  organization or the whole server), in a short transaction on the request's own session.
  Narrowest subject first: a request refused for its user does not use up its
  organization's or the server's budget.
- Quotas on persistent resources (organizations, memberships, invitations, environments,
  investigations and turns per day, pending logins, sessions): counted from the tables
  themselves under a transaction-level advisory lock per organization or user, in the same
  transaction as the insert, so concurrent requests cannot overshoot. No counter to drift.
- Per-process bounds on what a process holds open (requests per client address before any
  database work, open event streams, credential validations in flight): in memory, since
  they protect that process; with N processes the server allows N times as many.

Defaults are below; operators override any of them in .env (`INQUEST_RATE_LIMITS`,
`INQUEST_QUOTAS`, JSON objects). Refusals carry fixed messages and only the caller's own
limits: never another organization's usage, whether an account exists, or anything about
an endpoint.
"""

import asyncio
import hashlib
import logging
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Select, delete, extract, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AuthLoginState, AuthSession, RateLimitCounter

logger = logging.getLogger(__name__)

# "<count>/<seconds>" per subject. requests.ip is the in-memory first line (per process);
# every other one is shared through PostgreSQL.
RATES: dict[str, str] = {
    "requests.ip": "600/60",
    "login.ip": "10/60",
    "callback.ip": "20/60",
    "organization_create.user": "10/3600",
    "invitation_create.user": "30/3600",
    "invitation_create.organization": "60/3600",
    "invitation_accept.user": "20/3600",
    "environment_create.user": "20/3600",
    "environment_create.organization": "40/3600",
    "investigation_create.user": "30/3600",
    "investigation_create.organization": "60/3600",
    "turn_submit.user": "30/3600",
    "turn_submit.organization": "60/3600",
    "credential_validation.user": "10/3600",
    "credential_validation.organization": "20/3600",
    "credential_validation.server": "200/3600",
    "transfer.user": "30/3600",
    "logout_everywhere.user": "10/3600",
}
QUOTAS: dict[str, int] = {
    "organizations_per_user": 5,
    "memberships_per_user": 50,
    # Members plus pending invitations.
    "members_per_organization": 100,
    "pending_invitations_per_organization": 50,
    "environments_per_organization": 25,
    "investigations_per_organization_per_day": 100,
    # Every turn, the first one of each investigation included.
    "turns_per_organization_per_day": 300,
    "pending_logins": 5000,
    # Beyond this, a new sign-in ends the user's oldest sessions.
    "sessions_per_user": 10,
    "sse_streams_per_user": 8,
    "sse_streams": 200,
    "credential_validations_in_flight": 4,
}
MESSAGES = {
    "organizations_per_user": "you have reached your limit of {n} organizations created",
    "memberships_per_user": "you have reached your limit of {n} organization memberships",
    "members_per_organization": "this organization has reached its limit of {n} members and "
    "pending invitations",
    "pending_invitations_per_organization": "this organization has reached its limit of {n} "
    "pending invitations; revoke some first",
    "environments_per_organization": "this organization has reached its limit of {n} environments",
    "investigations_per_organization_per_day": "this organization has reached its limit of "
    "{n} investigations per day",
    "turns_per_organization_per_day": "this organization has reached its limit of {n} "
    "questions per day",
    "pending_logins": "too many sign-ins are in progress on this server; try again shortly",
    "sse_streams_per_user": "too many open event streams for this account; close some first",
    "sse_streams": "this server has too many open event streams; try again shortly",
    "credential_validations_in_flight": "too many credential checks are in progress; try again "
    "shortly",
}
RATE_LIMITED = "too many requests; try again later"
DAY = timedelta(days=1)
# Retry-After for per-process bounds that free up within seconds.
BUSY_RETRY_SECONDS = 5
# Without this, a flood from new addresses would grow the in-memory table without bound;
# addresses beyond it share one counter.
MAX_TRACKED_ADDRESSES = 100_000
OVERFLOW = "(overflow)"
SESSION_RETENTION = timedelta(days=7)
PRUNE_SECONDS = 300
RATE_SPEC = re.compile(r"^([1-9][0-9]{0,6})/([1-9][0-9]{0,4})$")


def _parse(spec: str) -> tuple[int, int]:
    match = RATE_SPEC.match(spec)
    if match is None or int(match[2]) > 86_400:
        raise ValueError("rate limits are '<count>/<seconds>', at most one day")
    return int(match[1]), int(match[2])


class LimitSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INQUEST_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Overrides by name, e.g. {"login.ip": "5/60"}; unknown names refuse to start.
    rate_limits: dict[str, str] = {}
    quotas: dict[str, int] = {}
    # Request bodies, checked before routing, parsing or database work.
    max_request_bytes: int = Field(default=131_072, ge=4096, le=16_777_216)
    max_json_depth: int = Field(default=32, ge=4, le=256)

    @field_validator("rate_limits")
    @classmethod
    def _known_rates(cls, value: dict[str, str]) -> dict[str, str]:
        for name, spec in value.items():
            if name not in RATES:
                raise ValueError(f"unknown rate limit {name!r}")
            _parse(spec)
        return value

    @field_validator("quotas")
    @classmethod
    def _known_quotas(cls, value: dict[str, int]) -> dict[str, int]:
        for name, limit in value.items():
            if name not in QUOTAS:
                raise ValueError(f"unknown quota {name!r}")
            if not 1 <= limit <= 1_000_000:
                raise ValueError(f"quota {name!r} must be between 1 and 1000000")
        return value


@lru_cache
def limit_settings() -> LimitSettings:
    return LimitSettings()


def rate(name: str) -> tuple[int, int]:
    return _parse(limit_settings().rate_limits.get(name, RATES[name]))


def quota(name: str) -> int:
    return limit_settings().quotas.get(name, QUOTAS[name])


class RateLimited(Exception):
    def __init__(self, retry_after: int, message: str = RATE_LIMITED):
        super().__init__(message)
        self.retry_after = max(1, retry_after)
        self.message = message


class QuotaExceeded(Exception):
    """A quota on persistent resources. With retry_after (quotas per day, pending logins)
    the request succeeds later by itself; without, only fewer resources or a higher limit
    help."""

    def __init__(self, name: str, retry_after: int | None = None):
        super().__init__(name)
        self.name = name
        self.retry_after = None if retry_after is None else max(1, retry_after)
        self.message = MESSAGES[name].format(n=quota(name))


# --- shared rate limits ------------------------------------------------------------------


def _bucket(name: str, subject: object) -> str:
    return hashlib.sha256(f"{name}|{subject}".encode()).hexdigest()


async def _count(session: AsyncSession, bucket: str, seconds: int) -> tuple[int, int]:
    """Hits in the current window including this one, and seconds until it ends. Statement
    time, not transaction time: the window is the one this request falls in."""
    now = func.statement_timestamp()
    start = func.to_timestamp(func.floor(extract("epoch", now) / seconds) * seconds)
    end = start + timedelta(seconds=seconds)
    statement = (
        insert(RateLimitCounter)
        .values(bucket=bucket, window_start=start, hits=1, expires_at=end)
        .on_conflict_do_update(
            index_elements=[RateLimitCounter.bucket, RateLimitCounter.window_start],
            set_={"hits": RateLimitCounter.hits + 1},
        )
        .returning(
            RateLimitCounter.hits,
            func.ceil(extract("epoch", RateLimitCounter.expires_at - now)),
        )
    )
    hits, remaining = (await session.execute(statement)).one()
    return hits, int(remaining)


async def hit(
    session: AsyncSession,
    action: str,
    *,
    ip: str | None = None,
    user: uuid.UUID | None = None,
    organization: uuid.UUID | None = None,
    server: bool = False,
) -> None:
    """Counts one request of `action` for each subject given that has a limit, and commits
    (call it before the request's own writes). RateLimited once any is over its limit.
    Callers count an organization only after authorizing the caller for it, so outsiders
    cannot use up another organization's budget."""
    subjects = {"ip": ip, "user": user, "organization": organization, "server": server or None}
    for scope, subject in subjects.items():
        name = f"{action}.{scope}"
        if subject is None or name not in RATES:
            continue
        limit, seconds = rate(name)
        hits, remaining = await _count(session, _bucket(name, subject), seconds)
        if hits > limit:
            # Refused attempts count too: a flood keeps its window full.
            await session.commit()
            raise RateLimited(remaining)
    await session.commit()


# --- quotas ------------------------------------------------------------------------------


async def lock(session: AsyncSession, kind: str, subject: object) -> None:
    """Serializes quota checks for one organization or user until the transaction ends.
    Lock order: organization before user, and both before capacity and row locks."""
    key = f"inquest:quota:{kind}:{subject}"
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


def require(name: str, used: int | None) -> None:
    if (used or 0) >= quota(name):
        raise QuotaExceeded(name)


async def require_daily(session: AsyncSession, name: str, created: Select) -> None:
    """`created` selects the creation time of everything the quota counts; only the last
    day counts. Hold lock() for the subject first."""
    column = created.selected_columns[0]
    recent = created.where(column > func.now() - DAY).subquery()
    used, wait = (
        await session.execute(
            select(func.count(), func.min(recent.c[0]) + DAY - func.now()).select_from(recent)
        )
    ).one()
    if used >= quota(name):
        raise QuotaExceeded(name, math.ceil(wait.total_seconds()))


# --- per-process bounds ------------------------------------------------------------------

_requests: dict[str, int] = {}
_requests_window = 0
_streams: dict[uuid.UUID, int] = {}
_validations = 0


def allow_request(address: str) -> int | None:
    """The first line, before any database or body work: None when this process may serve
    another request from `address`, else seconds until its window ends."""
    global _requests_window
    limit, seconds = rate("requests.ip")
    now = time.time()
    window = int(now // seconds)
    if window != _requests_window:
        # Fixed windows: everything counted so far is stale at once, which also bounds memory.
        _requests.clear()
        _requests_window = window
    if address not in _requests and len(_requests) >= MAX_TRACKED_ADDRESSES:
        address = OVERFLOW
    _requests[address] = _requests.get(address, 0) + 1
    if _requests[address] > limit:
        return max(1, math.ceil((window + 1) * seconds - now))
    return None


def take_stream_slot(user_id: uuid.UUID) -> Callable[[], None]:
    """One open event stream for `user_id`; QuotaExceeded when this process holds too many
    (for the user or in all). The returned release is idempotent."""
    if sum(_streams.values()) >= quota("sse_streams"):
        raise QuotaExceeded("sse_streams", BUSY_RETRY_SECONDS)
    if _streams.get(user_id, 0) >= quota("sse_streams_per_user"):
        raise QuotaExceeded("sse_streams_per_user", BUSY_RETRY_SECONDS)
    _streams[user_id] = _streams.get(user_id, 0) + 1
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        if _streams[user_id] > 1:
            _streams[user_id] -= 1
        else:
            del _streams[user_id]

    return release


@asynccontextmanager
async def validation_slot() -> AsyncIterator[None]:
    """Bounds outbound credential validations this process runs at once; refuses rather
    than queues, so waiting requests hold no connection or task."""
    global _validations
    if _validations >= quota("credential_validations_in_flight"):
        raise QuotaExceeded("credential_validations_in_flight", BUSY_RETRY_SECONDS)
    _validations += 1
    try:
        yield
    finally:
        _validations -= 1


def reset_process_state() -> None:
    """Tests only."""
    global _requests_window, _validations
    _requests.clear()
    _requests_window = 0
    _streams.clear()
    _validations = 0


# --- retention ---------------------------------------------------------------------------


async def prune(session: AsyncSession) -> None:
    """Deletes what no longer limits or authenticates anything: past rate windows, expired
    login attempts, and sessions ended more than SESSION_RETENTION ago."""
    ended = func.now() - SESSION_RETENTION
    await session.execute(delete(RateLimitCounter).where(RateLimitCounter.expires_at < func.now()))
    await session.execute(delete(AuthLoginState).where(AuthLoginState.expires_at < func.now()))
    await session.execute(
        delete(AuthSession).where(
            or_(AuthSession.expires_at < ended, AuthSession.revoked_at < ended)
        )
    )
    await session.commit()


async def prune_periodically(session_factory: Callable[[], AsyncSession]) -> None:
    """One task per process for its lifetime; each pass is a few short deletes."""
    while True:
        try:
            async with session_factory() as session:
                await prune(session)
        except Exception as exc:  # noqa: BLE001 (keep pruning; the next pass retries)
            logger.warning("limit cleanup failed: %s", type(exc).__name__)
        await asyncio.sleep(PRUNE_SECONDS)
