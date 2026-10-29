import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, UUIDPrimaryKeyMixin, str_enum


class TransferStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class EnvironmentTransfer(UUIDPrimaryKeyMixin, Base):
    """A proposal by an environment's owner to hand it to another member (D029, D035).
    Nothing changes until the recipient accepts; then ownership moves and the previous
    owner's credentials are deleted, never handed over. Kept after resolution as history."""

    __tablename__ = "environment_transfers"
    __table_args__ = (
        # One open proposal per environment: a second one could be accepted after the first
        # changed the owner it was proposed by.
        Index(
            "uq_environment_transfers_pending",
            "environment_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint("from_user_id <> to_user_id", name="different_users"),
        CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL AND resolved_by_user_id IS NULL)",
            name="resolved_when_closed",
        ),
    )

    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    from_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[TransferStatus] = mapped_column(
        str_enum(TransferStatus),
        default=TransferStatus.PENDING,
        server_default=TransferStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
