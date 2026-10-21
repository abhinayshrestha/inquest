import uuid
from datetime import datetime
from typing import Any

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
