import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from src.models.assignment import InvestigationAssignment
    from src.models.tool_call import ToolCall

MAX_SUMMARY_CHARS = 4_000
MAX_DATA_JSON_BYTES = 65_536


class EvidenceType(StrEnum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    EVENT = "event"
    RESOURCE_STATE = "resource_state"
    DIAGNOSTIC = "diagnostic"
    CONFIGURATION = "configuration"
    OTHER = "other"


class Evidence(UUIDPrimaryKeyMixin, Base):
    """A verifiable observation backed by tool output. Never an LLM conclusion.

    Immutable after insert, so there is no updated_at.
    """

    __tablename__ = "evidence"
    __table_args__ = (
        # Composite FK: when tool_call_id is set, the tool call must belong to the same
        # assignment. MATCH SIMPLE skips the check when tool_call_id is NULL.
        ForeignKeyConstraint(
            ["tool_call_id", "assignment_id"],
            ["tool_calls.id", "tool_calls.assignment_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_evidence_assignment_id_observed_at", "assignment_id", "observed_at"),
        CheckConstraint(f"char_length(summary) <= {MAX_SUMMARY_CHARS}", name="summary_length"),
        CheckConstraint(
            f"octet_length(data_json::text) <= {MAX_DATA_JSON_BYTES}", name="data_json_size"
        ),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigation_assignments.id", ondelete="RESTRICT")
    )
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    evidence_type: Mapped[EvidenceType] = mapped_column(str_enum(EvidenceType))
    # Free text so a new telemetry provider needs no migration.
    source_provider: Mapped[str] = mapped_column(String(100))
    source_reference: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(100))
    entity_identifier: Mapped[str | None] = mapped_column(String(255))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    data_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    redaction_applied: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    assignment: Mapped["InvestigationAssignment"] = relationship(
        back_populates="evidence", lazy="raise"
    )
    # foreign_keys limits ORM sync to tool_call_id; otherwise the composite FK would make
    # this relationship and `assignment` both write assignment_id. Callers set
    # assignment_id; the DB verifies it matches the tool call.
    tool_call: Mapped["ToolCall | None"] = relationship(
        back_populates="evidence",
        primaryjoin="Evidence.tool_call_id == ToolCall.id",
        foreign_keys=[tool_call_id],
        lazy="raise",
    )


class EvidenceImmutableError(Exception):
    pass


@event.listens_for(Evidence, "before_update")
def _reject_evidence_update(_mapper, _connection, _target: Evidence) -> None:
    raise EvidenceImmutableError("evidence is immutable once recorded")
