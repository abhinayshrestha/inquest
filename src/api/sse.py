"""Server-Sent Events for investigation progress.

Events are read from progress_events, never from memory, so a client that reconnects
with Last-Event-ID (or ?after=) gets exactly what it missed, in order, and a restarted
backend serves the same history. Payloads are the sanitized event payloads: IDs,
statuses, counts and short text; never telemetry, credentials or model reasoning.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from src.db.session import SessionLocal
from src.models import ProgressEvent
from src.services.progress import list_progress_events

POLL_SECONDS = 0.5
KEEPALIVE_SECONDS = 15.0
BATCH = 200
# Client reconnect delay, in milliseconds.
RETRY_MS = 3000


def format_event(event: ProgressEvent) -> str:
    data = {
        "id": event.id,
        "type": event.type,
        "turn_id": str(event.turn_id) if event.turn_id else None,
        "assignment_id": str(event.assignment_id) if event.assignment_id else None,
        "created_at": event.created_at.isoformat(),
        "payload": event.payload_json,
    }
    return f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(data, default=str)}\n\n"


def cursor_from(last_event_id: str | None, after: int | None) -> int:
    """Last-Event-ID (a browser reconnect) wins over ?after=; anything malformed means
    from the beginning."""
    for value in (last_event_id, after):
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0


async def event_stream(
    investigation_id: uuid.UUID,
    *,
    after: int,
    follow: bool,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    yield f"retry: {RETRY_MS}\n\n"
    cursor = after
    quiet_since = time.monotonic()
    while True:
        if await is_disconnected():
            return
        async with SessionLocal() as s:
            events = await list_progress_events(s, investigation_id, after_id=cursor, limit=BATCH)
        for event in events:
            cursor = event.id
            yield format_event(event)
        if len(events) == BATCH:
            continue  # more history to replay before waiting
        if not follow:
            return
        if events:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= KEEPALIVE_SECONDS:
            yield ": keep-alive\n\n"
            quiet_since = time.monotonic()
        await asyncio.sleep(POLL_SECONDS)
