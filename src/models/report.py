import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, ImmutableRecordError, UUIDPrimaryKeyMixin

MAX_REPORT_JSON_BYTES = 262_144


class InvestigationReport(UUIDPrimaryKeyMixin, Base):
    """One immutable version of an investigation's report. A revision is a new row with
    the next version; investigations.final_result_json mirrors the latest one."""

    __tablename__ = "investigation_reports"
    __table_args__ = (
        # The turn that produced the revision must belong to the same investigation.
        ForeignKeyConstraint(
            ["turn_id", "investigation_id"],
            ["investigation_turns.id", "investigation_turns.investigation_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("investigation_id", "version", name="uq_investigation_reports_version"),
        # At most one revision per turn.
        UniqueConstraint("turn_id", name="uq_investigation_reports_turn_id"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            f"octet_length(report_json::text) <= {MAX_REPORT_JSON_BYTES}", name="report_json_size"
        ),
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT")
    )
    turn_id: Mapped[uuid.UUID]
    version: Mapped[int]
    report_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@event.listens_for(InvestigationReport, "before_update")
def _reject_report_update(_mapper, _connection, _target: InvestigationReport) -> None:
    raise ImmutableRecordError("report versions are immutable; record a new revision")
