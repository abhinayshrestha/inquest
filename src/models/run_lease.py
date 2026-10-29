import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class RunLease(Base):
    """The right to run an investigation's active turn, held by one runner at a time
    (D036). Replaces a session advisory lock, which pinned a pooled connection for the
    whole run. Renewed by its holder; once it expires, another runner may take it over.
    `lease_id` changes with every acquisition and is the fencing token: a runner whose
    lease was taken over can no longer move the turn (services.leases.check_fence)."""

    __tablename__ = "run_leases"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), primary_key=True
    )
    # Copied from the investigation for per-organization capacity counts; NULL for
    # ownerless legacy investigations (host tooling).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_turns.id", ondelete="CASCADE")
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    # Which process holds it (host, pid, random suffix): diagnostics only.
    holder: Mapped[str] = mapped_column(String(200))
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
