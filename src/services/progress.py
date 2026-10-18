"""Append-only progress events. append_progress_event never commits: events are written
in the caller's transaction, so a state change and the event announcing it are durable
together or not at all."""

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ProgressEvent
from src.models.progress_event import MAX_EVENT_PAYLOAD_BYTES

EVENT_TYPES = frozenset(
    {
        "investigation.status_changed",
        "turn.created",
        "turn.started",
        "turn.decision",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "round.started",
        "assignment.started",
        "assignment.finished",
        "hypothesis.recorded",
        "hypothesis.updated",
        "tool_call.started",
        "tool_call.finished",
        "finding.recorded",
        "report.revised",
        "budget.exhausted",
    }
)
MAX_PAYLOAD_KEYS = 32
MAX_KEY_CHARS = 64
# Short text only: anything longer is a sign that raw telemetry is being copied in.
MAX_STRING_CHARS = 500
MAX_LIST_ITEMS = 50
DEFAULT_PAGE = 200

Scalar = str | int | float | bool | None


class InvalidEvent(ValueError):
    pass


def _scalar(value: Any) -> Scalar:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING_CHARS else value[: MAX_STRING_CHARS - 1] + "…"
    if value is None or isinstance(value, bool | int | float):
        return value
    raise InvalidEvent(f"payload values must be scalars, not {type(value).__name__}")


def _value(value: Any, depth: int) -> Any:
    if isinstance(value, list | tuple):
        if len(value) > MAX_LIST_ITEMS:
            raise InvalidEvent(f"payload lists hold at most {MAX_LIST_ITEMS} items")
        return [_scalar(v) for v in value]
    if isinstance(value, dict):
        if depth >= 1:
            raise InvalidEvent("payload nests at most one object level")
        return sanitize_payload(value, depth + 1)
    return _scalar(value)


def sanitize_payload(payload: dict[str, Any] | None, depth: int = 0) -> dict[str, Any]:
    """Small, flat payloads: IDs, statuses, counts and short text. Rejects shapes that
    could carry bulk telemetry rather than silently storing them."""
    payload = payload or {}
    if len(payload) > MAX_PAYLOAD_KEYS:
        raise InvalidEvent(f"payload holds at most {MAX_PAYLOAD_KEYS} keys")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY_CHARS:
            raise InvalidEvent("payload keys are 1-64 character strings")
        clean[key] = _value(value, depth)
    if depth == 0 and len(json.dumps(clean, separators=(",", ":"))) > MAX_EVENT_PAYLOAD_BYTES:
        raise InvalidEvent(f"payload exceeds {MAX_EVENT_PAYLOAD_BYTES} bytes")
    return clean


def append_progress_event(
    session: AsyncSession,
    investigation_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    turn_id: uuid.UUID | None = None,
    assignment_id: uuid.UUID | None = None,
) -> ProgressEvent:
    """Adds an event to the session; the caller's commit makes it durable."""
    if event_type not in EVENT_TYPES:
        raise InvalidEvent(f"unknown event type {event_type!r}")
    event = ProgressEvent(
        investigation_id=investigation_id,
        turn_id=turn_id,
        assignment_id=assignment_id,
        type=event_type,
        payload_json=sanitize_payload(payload),
    )
    session.add(event)
    return event


async def list_progress_events(
    session: AsyncSession,
    investigation_id: uuid.UUID,
    *,
    after_id: int = 0,
    limit: int = DEFAULT_PAGE,
) -> list[ProgressEvent]:
    """Events after a cursor, oldest first: the replay a reconnecting client needs."""
    rows = await session.scalars(
        select(ProgressEvent)
        .where(ProgressEvent.investigation_id == investigation_id, ProgressEvent.id > after_id)
        .order_by(ProgressEvent.id)
        .limit(limit)
    )
    return list(rows)
