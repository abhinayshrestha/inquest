from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from src.models.assignment import InvestigationAssignment


class InvestigationStatus(StrEnum):
    # Lifecycle only. Outcome (e.g. inconclusive) lives in final_result_json.
    PENDING = "pending"
    INVESTIGATING = "investigating"
    CORRELATING = "correlating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TriggerType(StrEnum):
    MANUAL = "manual"
    ALERT = "alert"


class Investigation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "investigations"
    __table_args__ = (CheckConstraint("current_round >= 1", name="current_round_positive"),)

    title: Mapped[str] = mapped_column(String(255))
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[InvestigationStatus] = mapped_column(
        str_enum(InvestigationStatus),
        default=InvestigationStatus.PENDING,
        server_default=InvestigationStatus.PENDING.value,
    )
    severity: Mapped[Severity | None] = mapped_column(str_enum(Severity))
    trigger_type: Mapped[TriggerType] = mapped_column(str_enum(TriggerType))
    trigger_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    current_round: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # passive_deletes: the DB's ON DELETE CASCADE removes children without loading them.
    assignments: Mapped[list["InvestigationAssignment"]] = relationship(
        back_populates="investigation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )
