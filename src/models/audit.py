import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, event, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, ImmutableRecordError, UUIDPrimaryKeyMixin


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """Actions that change who owns data or its credentials: operator commands (legacy
    adoption, D032; credential key rotation) and owners' credential changes (D034).
    Append-only. Never holds credentials, URLs or any part of a secret."""

    __tablename__ = "audit_events"

    action: Mapped[str] = mapped_column(String(64))
    # Who ran it: the operator name given to the command plus the OS account, or
    # "user:<id>" for API actions.
    actor: Mapped[str] = mapped_column(String(200))
    # The signed-in user for API actions; NULL for operator commands.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@event.listens_for(AuditEvent, "before_update")
def _reject_audit_update(_mapper, _connection, _target: AuditEvent) -> None:
    raise ImmutableRecordError("audit events are append-only")
