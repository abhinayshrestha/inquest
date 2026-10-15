import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.models import EnvironmentStatus, EnvironmentType


class EnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: EnvironmentType
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class EnvironmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: EnvironmentType
    status: EnvironmentStatus


class EnvironmentRead(EnvironmentSummary):
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
