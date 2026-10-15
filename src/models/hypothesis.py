import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import (
    Base,
    ImmutableRecordError,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)
from src.models.assignment import assignment_scope_fk
from src.models.evidence import EvidenceStance

if TYPE_CHECKING:
    from src.models.assignment import InvestigationAssignment
    from src.models.evidence import Evidence
    from src.models.finding import Finding

MAX_STATEMENT_CHARS = 1_000


class HypothesisStatus(StrEnum):
    ACTIVE = "active"
    SUPPORTED = "supported"
    WEAKENED = "weakened"
    REJECTED = "rejected"


class Hypothesis(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A testable claim made by a Local Investigator. Stores the claim, not reasoning."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        assignment_scope_fk(),
        # Target for findings.hypothesis_id's same-assignment FK; also serves assignment
        # lookups.
        UniqueConstraint("assignment_id", "id", name="uq_hypotheses_assignment_id_id"),
        UniqueConstraint("investigation_id", "environment_id", "id", name="uq_hypotheses_scope"),
        CheckConstraint(
            f"char_length(statement) <= {MAX_STATEMENT_CHARS}", name="statement_length"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    assignment_id: Mapped[uuid.UUID]
    investigation_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    statement: Mapped[str] = mapped_column(Text)
    status: Mapped[HypothesisStatus] = mapped_column(
        str_enum(HypothesisStatus),
        default=HypothesisStatus.ACTIVE,
        server_default=HypothesisStatus.ACTIVE.value,
    )
    # Uncalibrated model estimate; never present it as a probability.
    confidence: Mapped[float | None]

    assignment: Mapped["InvestigationAssignment"] = relationship(
        back_populates="hypotheses", lazy="raise"
    )
    evidence_links: Mapped[list["HypothesisEvidence"]] = relationship(
        back_populates="hypothesis", passive_deletes="all", lazy="raise"
    )
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="hypothesis",
        primaryjoin="Hypothesis.id == Finding.hypothesis_id",
        foreign_keys="[Finding.hypothesis_id]",
        passive_deletes="all",
        lazy="raise",
    )


class HypothesisEvidence(Base):
    """Insert-only link. Evidence may come from any assignment of the same investigation
    and environment; cross-environment correlation is the Main Investigator's job."""

    __tablename__ = "hypothesis_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["hypothesis_id", "investigation_id", "environment_id"],
            ["hypotheses.id", "hypotheses.investigation_id", "hypotheses.environment_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "investigation_id", "environment_id"],
            ["evidence.id", "evidence.investigation_id", "evidence.environment_id"],
            ondelete="RESTRICT",
        ),
    )

    hypothesis_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    investigation_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    stance: Mapped[EvidenceStance] = mapped_column(str_enum(EvidenceStance))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Scope columns are filled from the hypothesis; the DB checks the evidence matches.
    hypothesis: Mapped["Hypothesis"] = relationship(back_populates="evidence_links", lazy="raise")
    evidence: Mapped["Evidence"] = relationship(
        back_populates="hypothesis_links",
        primaryjoin="HypothesisEvidence.evidence_id == Evidence.id",
        foreign_keys=[evidence_id],
        lazy="raise",
    )


@event.listens_for(Hypothesis, "before_update")
def _reject_statement_change(_mapper, _connection, target: Hypothesis) -> None:
    # A different claim is a different hypothesis; only status and confidence evolve.
    if inspect(target).attrs.statement.history.has_changes():
        raise ImmutableRecordError("hypothesis statement is immutable; record a new hypothesis")


@event.listens_for(HypothesisEvidence, "before_update")
def _reject_hypothesis_link_update(_mapper, _connection, _target: HypothesisEvidence) -> None:
    raise ImmutableRecordError("hypothesis evidence links are insert-only")
