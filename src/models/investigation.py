import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
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
    from src.models.environment import Environment
    from src.models.report import InvestigationReport
    from src.models.turn import InvestigationTurn


class InvestigationStatus(StrEnum):
    # Lifecycle only. Outcome (e.g. inconclusive) lives in final_result_json.
    PENDING = "pending"
    INVESTIGATING = "investigating"
    CORRELATING = "correlating"
    # The latest turn finished and a report exists; follow-up questions are accepted (D016).
    IDLE = "idle"
    # Closed: no further follow-ups.
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
    __table_args__ = (
        CheckConstraint("current_round >= 1", name="current_round_positive"),
        CheckConstraint(COMPLETED_AFTER_STARTED, name="completed_after_started"),
        CheckConstraint(
            "created_by_user_id IS NULL OR organization_id IS NOT NULL",
            name="creator_with_organization",
        ),
        # Organization history pages: newest first within one organization.
        Index("ix_investigations_organization_id_created_at", "organization_id", "created_at"),
    )

    # NULL for ownerless records from before tenancy (D032). Every environment in the
    # scope must belong to the same organization: a deferred trigger checks
    # investigation_environments at commit (migration f2b7c1d9a4e3).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
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

    # passive_deletes="all": let the RESTRICT FK reject the delete instead of the ORM
    # trying to NULL out a non-nullable investigation_id.
    assignments: Mapped[list["InvestigationAssignment"]] = relationship(
        back_populates="investigation", passive_deletes="all", lazy="raise"
    )
    turns: Mapped[list["InvestigationTurn"]] = relationship(
        back_populates="investigation",
        order_by="InvestigationTurn.seq",
        passive_deletes="all",
        lazy="raise",
    )
    environments: Mapped[list["InvestigationEnvironment"]] = relationship(
        back_populates="investigation", passive_deletes="all", lazy="raise"
    )
    # Read-only: versions are written by services.turns, never through this collection.
    reports: Mapped[list["InvestigationReport"]] = relationship(
        order_by="InvestigationReport.version", viewonly=True, lazy="raise"
    )


class InvestigationEnvironment(Base):
    """An environment in an investigation's scope. Assignments may only target these (a
    composite FK on investigation_assignments enforces it)."""

    __tablename__ = "investigation_environments"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="RESTRICT"), primary_key=True
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environments.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    investigation: Mapped["Investigation"] = relationship(
        back_populates="environments", lazy="raise"
    )
    environment: Mapped["Environment"] = relationship(lazy="raise")
