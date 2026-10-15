import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import (
    COMPLETED_AFTER_STARTED,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)

if TYPE_CHECKING:
    from src.models.assignment import InvestigationAssignment
    from src.models.evidence import Evidence

# The tool executor truncates to these before insert; the CHECKs are the backstop that
# keeps raw tool output out of Postgres.
MAX_INPUT_JSON_BYTES = 16_384
MAX_OUTPUT_METADATA_JSON_BYTES = 65_536
MAX_RESULT_SUMMARY_CHARS = 4_000
MAX_ERROR_MESSAGE_CHARS = 2_000


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    # Blocked by the permission layer before execution; kept for audit.
    REJECTED = "rejected"


class ToolCategory(StrEnum):
    TELEMETRY = "telemetry"
    CLOUD_API = "cloud_api"
    RUNTIME = "runtime"
    NETWORK = "network"
    OTHER = "other"


class ToolCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        # Target of evidence's composite FK; its assignment_id prefix also serves
        # "tool calls of assignment X".
        UniqueConstraint("assignment_id", "id", name="uq_tool_calls_assignment_id_id"),
        # Timeout sweeps only care about active calls; finished calls drop out of the index.
        Index(
            "ix_tool_calls_active_status",
            "status",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint("duration_ms >= 0", name="duration_ms_nonnegative"),
        CheckConstraint(COMPLETED_AFTER_STARTED, name="completed_after_started"),
        CheckConstraint(
            f"octet_length(input_json::text) <= {MAX_INPUT_JSON_BYTES}", name="input_json_size"
        ),
        CheckConstraint(
            f"octet_length(output_metadata_json::text) <= {MAX_OUTPUT_METADATA_JSON_BYTES}",
            name="output_metadata_json_size",
        ),
        CheckConstraint(
            f"char_length(result_summary) <= {MAX_RESULT_SUMMARY_CHARS}",
            name="result_summary_length",
        ),
        CheckConstraint(
            f"char_length(error_message) <= {MAX_ERROR_MESSAGE_CHARS}",
            name="error_message_length",
        ),
    )

    # RESTRICT: tool calls are audit history and must outlive any attempted purge.
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigation_assignments.id", ondelete="RESTRICT")
    )
    tool_name: Mapped[str] = mapped_column(String(150))
    tool_category: Mapped[ToolCategory] = mapped_column(str_enum(ToolCategory))
    status: Mapped[ToolCallStatus] = mapped_column(
        str_enum(ToolCallStatus),
        default=ToolCallStatus.PENDING,
        server_default=ToolCallStatus.PENDING.value,
    )
    input_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    result_summary: Mapped[str | None] = mapped_column(Text)
    output_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_number: Mapped[int] = mapped_column(default=1, server_default=text("1"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Measured by the executor with a monotonic clock, so not derived from the timestamps.
    duration_ms: Mapped[int | None]

    assignment: Mapped["InvestigationAssignment"] = relationship(
        back_populates="tool_calls", lazy="raise"
    )
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="tool_call",
        primaryjoin="ToolCall.id == Evidence.tool_call_id",
        foreign_keys="[Evidence.tool_call_id]",
        passive_deletes="all",
        lazy="raise",
    )
