import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, ImmutableRecordError

MAX_EVENT_PAYLOAD_BYTES = 8_192
EVENT_TYPE_PATTERN = r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$"


class ProgressEvent(Base):
    """Append-only activity record for the frontend and the audit trail. The bigint id is
    the replay cursor (Last-Event-ID). Small payloads only: IDs, statuses, counts and short
    text, never raw telemetry (it stays in SigNoz)."""

    __tablename__ = "progress_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["turn_id", "investigation_id"],
            ["investigation_turns.id", "investigation_turns.investigation_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["assignment_id", "investigation_id"],
            ["investigation_assignments.id", "investigation_assignments.investigation_id"],
            ondelete="RESTRICT",
        ),
        # Replay of one investigation's events after a cursor.
        Index("ix_progress_events_investigation_id_id", "investigation_id", "id"),
        CheckConstraint(f"type ~ '{EVENT_TYPE_PATTERN}'", name="type_format"),
        CheckConstraint(
            f"octet_length(payload_json::text) <= {MAX_EVENT_PAYLOAD_BYTES}", name="payload_size"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT")
    )
    turn_id: Mapped[uuid.UUID | None]
    assignment_id: Mapped[uuid.UUID | None]
    type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@event.listens_for(ProgressEvent, "before_update")
def _reject_event_update(_mapper, _connection, _target: ProgressEvent) -> None:
    raise ImmutableRecordError("progress events are append-only")
