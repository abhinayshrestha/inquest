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
    from src.models.investigation import Investigation

MAX_QUESTION_CHARS = 10_000
MAX_ANSWER_JSON_BYTES = 65_536
# Response blocks (D023) are derived from the answer and evidence; charts are bounded
# projections, so this leaves room without letting telemetry be copied in.
MAX_PRESENTATION_JSON_BYTES = 131_072


class TurnKind(StrEnum):
    INITIAL = "initial"
    FOLLOW_UP = "follow_up"


class TurnStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TurnDecision(StrEnum):
    # How the turn was answered: from existing memory, after new assignments, or by
    # asking the user to clarify.
    REUSED_EVIDENCE = "reused_evidence"
    INVESTIGATED = "investigated"
    CLARIFICATION = "clarification"


ACTIVE_TURN_STATUSES = (TurnStatus.PENDING, TurnStatus.RUNNING)


class InvestigationTurn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One question asked of an investigation and its answer (D013). Turn 1 is the
    initial question; later turns are follow-ups. The conversation record, separate from
    evidence and from inferences."""

    __tablename__ = "investigation_turns"
    __table_args__ = (
        UniqueConstraint("investigation_id", "seq", name="uq_investigation_turns_seq"),
        # Target for composite FKs that pin a turn to its investigation.
        UniqueConstraint("investigation_id", "id", name="uq_investigation_turns_scope"),
        # One active turn per investigation: concurrent follow-ups are refused here even if
        # the service-level lock is bypassed.
        Index(
            "uq_investigation_turns_one_active",
            "investigation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("(seq = 1) = (kind = 'initial')", name="initial_is_first"),
        CheckConstraint(
            f"char_length(question) BETWEEN 1 AND {MAX_QUESTION_CHARS}", name="question_length"
        ),
        CheckConstraint("answer_json IS NULL OR status = 'completed'", name="answer_when_done"),
        CheckConstraint(
            f"octet_length(answer_json::text) <= {MAX_ANSWER_JSON_BYTES}", name="answer_json_size"
        ),
        CheckConstraint(COMPLETED_AFTER_STARTED, name="completed_after_started"),
        CheckConstraint(
            "presentation_json IS NULL OR status = 'completed'", name="presentation_when_done"
        ),
        CheckConstraint(
            f"octet_length(presentation_json::text) <= {MAX_PRESENTATION_JSON_BYTES}",
            name="presentation_json_size",
        ),
    )

    # RESTRICT: the conversation is investigation history (D006).
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT")
    )
    seq: Mapped[int]
    kind: Mapped[TurnKind] = mapped_column(str_enum(TurnKind))
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[TurnStatus] = mapped_column(
        str_enum(TurnStatus),
        default=TurnStatus.PENDING,
        server_default=TurnStatus.PENDING.value,
    )
    decision: Mapped[TurnDecision | None] = mapped_column(str_enum(TurnDecision))
    answer_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Response blocks built deterministically from the validated answer and its evidence
    # (services.presentation), stored so reopening an answer never recomputes or calls a
    # model. Derived data: rebuilt when missing, invalid or from an older version.
    presentation_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    # Who asked (NULL for legacy turns and turns created by internal tooling).
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    # Set when the API accepted the turn for execution. Recovery starts pending turns
    # that have it (accepted, never started) and never ones that lack it (created only).
    run_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    investigation: Mapped["Investigation"] = relationship(back_populates="turns", lazy="raise")
    assignments: Mapped[list["InvestigationAssignment"]] = relationship(
        back_populates="turn",
        primaryjoin="InvestigationTurn.id == InvestigationAssignment.turn_id",
        foreign_keys="[InvestigationAssignment.turn_id]",
        passive_deletes="all",
        lazy="raise",
    )
