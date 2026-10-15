import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from src.models.environment import Environment
    from src.models.investigation import Investigation


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class InvestigationAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "investigation_assignments"
    __table_args__ = (
        CheckConstraint("round >= 1", name="round_positive"),
        # Orchestrator's hot query: assignments of one investigation in one round.
        Index("ix_investigation_assignments_investigation_id_round", "investigation_id", "round"),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE")
    )
    # RESTRICT: deleting an environment must never erase investigation history.
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), index=True
    )
    round: Mapped[int]
    objective: Mapped[str] = mapped_column(Text)
    status: Mapped[AssignmentStatus] = mapped_column(
        str_enum(AssignmentStatus),
        default=AssignmentStatus.PENDING,
        server_default=AssignmentStatus.PENDING.value,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    investigation: Mapped["Investigation"] = relationship(
        back_populates="assignments", lazy="raise"
    )
    environment: Mapped["Environment"] = relationship(back_populates="assignments", lazy="raise")
