import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.models import AssignmentStatus, InvestigationStatus, Severity, TriggerType
from src.schemas.environment import EnvironmentSummary


class InvestigationCreate(BaseModel):
    environment_id: uuid.UUID
    title: str = Field(min_length=1, max_length=255)
    question: str = Field(min_length=1)
    severity: Severity | None = None


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    round: int
    objective: str
    status: AssignmentStatus
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    environment: EnvironmentSummary


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
