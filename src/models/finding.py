import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
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
from src.models.evidence import MAX_SUMMARY_CHARS, EvidenceStance

if TYPE_CHECKING:
    from src.models.assignment import InvestigationAssignment
    from src.models.evidence import Evidence
    from src.models.hypothesis import Hypothesis


class FindingStatus(StrEnum):
    PRELIMINARY = "preliminary"
    CONFIRMED = "confirmed"
    INCONCLUSIVE = "inconclusive"


class Finding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A structured conclusion for the Main Investigator, traceable to linked evidence."""

    __tablename__ = "findings"
    __table_args__ = (
        assignment_scope_fk(),
        # When set, the hypothesis must belong to the same assignment as the finding.
        ForeignKeyConstraint(
            ["hypothesis_id", "assignment_id"],
            ["hypotheses.id", "hypotheses.assignment_id"],
            ondelete="RESTRICT",
        ),
        # Target for finding_evidence's FK; the investigation_id prefix also serves the
        # Main Investigator reading all findings of an investigation.
        UniqueConstraint("investigation_id", "environment_id", "id", name="uq_findings_scope"),
        CheckConstraint(f"char_length(summary) <= {MAX_SUMMARY_CHARS}", name="summary_length"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(index=True)
    investigation_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    # Uncalibrated model estimate; never present it as a probability.
    confidence: Mapped[float | None]
    status: Mapped[FindingStatus] = mapped_column(
        str_enum(FindingStatus),
        default=FindingStatus.PRELIMINARY,
        server_default=FindingStatus.PRELIMINARY.value,
    )

    assignment: Mapped["InvestigationAssignment"] = relationship(
        back_populates="findings", lazy="raise"
    )
    # foreign_keys limits ORM sync to hypothesis_id; `assignment` owns assignment_id and
    # the DB verifies the hypothesis shares it.
    hypothesis: Mapped["Hypothesis | None"] = relationship(
        back_populates="findings",
        primaryjoin="Finding.hypothesis_id == Hypothesis.id",
        foreign_keys=[hypothesis_id],
        lazy="raise",
    )
    evidence_links: Mapped[list["FindingEvidence"]] = relationship(
        back_populates="finding", passive_deletes="all", lazy="raise"
    )


class FindingEvidence(Base):
    """Insert-only link, same scope rule as HypothesisEvidence. Contradicting evidence is
    linked too so a finding never hides what argues against it."""

    __tablename__ = "finding_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["finding_id", "investigation_id", "environment_id"],
            ["findings.id", "findings.investigation_id", "findings.environment_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "investigation_id", "environment_id"],
            ["evidence.id", "evidence.investigation_id", "evidence.environment_id"],
            ondelete="RESTRICT",
        ),
    )

    finding_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, index=True)
    investigation_id: Mapped[uuid.UUID]
    environment_id: Mapped[uuid.UUID]
    stance: Mapped[EvidenceStance] = mapped_column(str_enum(EvidenceStance))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    finding: Mapped["Finding"] = relationship(back_populates="evidence_links", lazy="raise")
    evidence: Mapped["Evidence"] = relationship(
        back_populates="finding_links",
        primaryjoin="FindingEvidence.evidence_id == Evidence.id",
        foreign_keys=[evidence_id],
        lazy="raise",
    )


@event.listens_for(FindingEvidence, "before_update")
def _reject_finding_link_update(_mapper, _connection, _target: FindingEvidence) -> None:
    raise ImmutableRecordError("finding evidence links are insert-only")
