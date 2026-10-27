import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models import (
    AssignmentStatus,
    InvestigationStatus,
    Severity,
    TriggerType,
    TurnDecision,
    TurnKind,
    TurnStatus,
)
from src.schemas.environment import EnvironmentSummary


class InvestigationCreate(BaseModel):
    environment_id: uuid.UUID
    title: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1)
    severity: Severity | None = None


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turn_id: uuid.UUID
    round: int
    objective: str
    status: AssignmentStatus
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    environment: EnvironmentSummary


class TurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    kind: TurnKind
    question: str
    status: TurnStatus
    decision: TurnDecision | None
    answer_json: dict[str, Any] | None
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turn_id: uuid.UUID
    version: int
    report_json: dict[str, Any]
    created_at: datetime


class ReportRef(BaseModel):
    """A report version without its body; fetch the body by version when needed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    turn_id: uuid.UUID
    version: int
    created_at: datetime


class ProgressEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    turn_id: uuid.UUID | None
    assignment_id: uuid.UUID | None
    type: str
    payload_json: dict[str, Any]
    created_at: datetime


class InvestigationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Only members of this organization can read the investigation (D033).
    organization_id: uuid.UUID | None
    title: str
    question: str
    status: InvestigationStatus
    severity: Severity | None
    trigger_type: TriggerType
    trigger_json: dict[str, Any]
    current_round: int
    started_at: datetime | None
    completed_at: datetime | None
    final_result_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    assignments: list[AssignmentRead]
    turns: list[TurnRead]
    reports: list[ReportRef]


class FollowUpCreate(BaseModel):
    question: str = Field(min_length=1, max_length=10_000)


class TurnAccepted(BaseModel):
    """202 for a turn accepted for execution; progress is on events_url."""

    investigation_id: uuid.UUID
    turn_id: uuid.UUID
    seq: int
    status: str = "queued"
    events_url: str


class TurnCancelled(BaseModel):
    investigation_id: uuid.UUID
    turn_id: uuid.UUID
    status: str = "cancelled"


class InvestigationListItem(BaseModel):
    """One row of the investigation history: enough to list and open it, nothing more."""

    id: uuid.UUID
    title: str
    status: InvestigationStatus
    severity: Severity | None
    trigger_type: TriggerType
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    environments: list[EnvironmentSummary]
    turn_count: int
    latest_report_version: int | None
    # Failed or cancelled turns return the investigation to idle (D016); the latest turn's
    # status is what says whether the last question was answered.
    latest_turn_status: TurnStatus | None
    latest_turn_completed_at: datetime | None


class InvestigationPage(BaseModel):
    items: list[InvestigationListItem]
    # Offset of the next page, or None when this is the last one.
    next_offset: int | None


class CitedEvidenceLink(BaseModel):
    ref: str
    stance: str
    title: str


# Citation details are what a user needs to inspect a reference in an answer: the
# recorded summary and where/when it was observed. Raw tool output (data_json) never
# leaves the backend; only its time window is extracted.
class EvidenceCitation(BaseModel):
    kind: Literal["evidence"] = "evidence"
    ref: str
    evidence_type: str
    title: str
    summary: str
    source_provider: str
    tool_name: str | None
    entity_type: str | None
    entity_identifier: str | None
    observed_at: datetime | None
    collected_at: datetime
    window_start: datetime | None
    window_end: datetime | None
    redaction_applied: bool
    environment: EnvironmentSummary


class FindingCitation(BaseModel):
    kind: Literal["finding"] = "finding"
    ref: str
    title: str
    summary: str
    status: str
    confidence: float | None
    created_at: datetime
    evidence: list[CitedEvidenceLink]
    environment: EnvironmentSummary


class HypothesisCitation(BaseModel):
    kind: Literal["hypothesis"] = "hypothesis"
    ref: str
    statement: str
    status: str
    confidence: float | None
    created_at: datetime
    environment: EnvironmentSummary


Citation = Annotated[
    EvidenceCitation | FindingCitation | HypothesisCitation, Field(discriminator="kind")
]


class OverviewCounts(BaseModel):
    """Counts of persisted records only: no derived health, alert or uptime figures."""

    investigations_total: int
    # Every InvestigationStatus, zero-filled.
    investigations_by_status: dict[InvestigationStatus, int]
    # Investigations grouped by the status of their latest turn (one per investigation).
    latest_turn_by_status: dict[TurnStatus, int]
    turns_total: int
    turns_by_status: dict[TurnStatus, int]
    environments_total: int
    environments_active: int
    reports_total: int
    investigations_with_reports: int


ReportOutcome = Literal["root_cause_identified", "probable_cause", "inconclusive"]


class ReportListItem(BaseModel):
    """One report version, with enough investigation context to find and open it."""

    id: uuid.UUID
    investigation_id: uuid.UUID
    investigation_title: str
    investigation_status: InvestigationStatus
    environments: list[EnvironmentSummary]
    version: int
    is_latest: bool
    turn_id: uuid.UUID
    turn_seq: int
    created_at: datetime
    # None for a stored report whose outcome is missing or not one of the known values.
    outcome: ReportOutcome | None
    conclusion_count: int


class ReportListPage(BaseModel):
    items: list[ReportListItem]
    next_offset: int | None
