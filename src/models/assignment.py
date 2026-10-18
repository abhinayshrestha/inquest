import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import (
    COMPLETED_AFTER_STARTED,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)

if TYPE_CHECKING:
    from src.models.environment import Environment
    from src.models.evidence import Evidence
    from src.models.finding import Finding
    from src.models.hypothesis import Hypothesis
    from src.models.investigation import Investigation
    from src.models.tool_call import ToolCall
    from src.models.turn import InvestigationTurn


class AssignmentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


def assignment_scope_fk() -> ForeignKeyConstraint:
    # Records that copy their assignment's (investigation_id, environment_id) use this so
    # the copy can never drift. A new constraint per table: constraints can't be shared.
    return ForeignKeyConstraint(
        ["assignment_id", "investigation_id", "environment_id"],
        [
            "investigation_assignments.id",
            "investigation_assignments.investigation_id",
            "investigation_assignments.environment_id",
        ],
        ondelete="RESTRICT",
    )


class InvestigationAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "investigation_assignments"
    __table_args__ = (
        CheckConstraint("round >= 1", name="round_positive"),
        CheckConstraint(COMPLETED_AFTER_STARTED, name="completed_after_started"),
        # Orchestrator's hot query: assignments of one investigation in one round.
        Index("ix_investigation_assignments_investigation_id_round", "investigation_id", "round"),
        # Target for the scope FKs on evidence, hypotheses and findings: their copied
        # (investigation_id, environment_id) must match their assignment's.
        UniqueConstraint(
            "investigation_id", "environment_id", "id", name="uq_investigation_assignments_scope"
        ),
        # Target for progress_events' (assignment_id, investigation_id) FK.
        UniqueConstraint(
            "investigation_id", "id", name="uq_investigation_assignments_investigation_id_id"
        ),
        # The turn that started the assignment belongs to the same investigation.
        ForeignKeyConstraint(
            ["turn_id", "investigation_id"],
            ["investigation_turns.id", "investigation_turns.investigation_id"],
            ondelete="RESTRICT",
        ),
        # Only environments in the investigation's scope can be assigned. Deferred to commit
        # so the scope row and the assignment can be added in one flush in any order.
        ForeignKeyConstraint(
            ["investigation_id", "environment_id"],
            [
                "investigation_environments.investigation_id",
                "investigation_environments.environment_id",
            ],
            name="fk_investigation_assignments_scope",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_investigation_assignments_turn_id", "turn_id"),
    )

    # RESTRICT on both FKs: deleting an investigation or environment must never erase
    # investigation history (D006).
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT")
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column()
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
    # foreign_keys limits ORM sync to turn_id; `investigation` owns investigation_id and the
    # composite FK verifies the turn shares it.
    turn: Mapped["InvestigationTurn"] = relationship(
        back_populates="assignments",
        primaryjoin="InvestigationAssignment.turn_id == InvestigationTurn.id",
        foreign_keys=[turn_id],
        lazy="raise",
    )
    tool_calls: Mapped[list["ToolCall"]] = relationship(
        back_populates="assignment", passive_deletes="all", lazy="raise"
    )
    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="assignment", passive_deletes="all", lazy="raise"
    )
    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        back_populates="assignment", passive_deletes="all", lazy="raise"
    )
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="assignment", passive_deletes="all", lazy="raise"
    )
