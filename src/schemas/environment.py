import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models import EnvironmentStatus, EnvironmentType

# Environment configuration is small (the SigNoz scope is a few names): bounded so one
# record cannot hold arbitrary data or nesting.
MAX_METADATA_BYTES = 16_384
MAX_METADATA_DEPTH = 8


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max(map(_depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(_depth, value), default=0)
    return 0


class EnvironmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: EnvironmentType
    metadata_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata_json")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        # The request body's own nesting limit (src/api/security.py) keeps this recursion
        # shallow; this bound is the record's.
        if _depth(value) > MAX_METADATA_DEPTH:
            raise ValueError(f"metadata_json is nested deeper than {MAX_METADATA_DEPTH} levels")
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
            raise ValueError(f"metadata_json exceeds {MAX_METADATA_BYTES} bytes")
        return value


class EnvironmentCreateRequest(EnvironmentCreate):
    # Which of the caller's organizations to create it in. Only selects: the server checks
    # the caller's membership and records the caller as owner.
    organization_id: uuid.UUID


class EnvironmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: EnvironmentType
    status: EnvironmentStatus


class EnvironmentRead(EnvironmentSummary):
    organization_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class EnvironmentListItem(EnvironmentSummary):
    created_at: datetime
    investigation_count: int
    last_investigation_at: datetime | None


class ScopeProblem(BaseModel):
    # Same codes the Tool Executor rejects queries with (src/tools/scope.py).
    code: str
    message: str


class TelemetryScopeRead(BaseModel):
    """The SigNoz identities this environment's investigations may query: the v1 isolation
    mechanism. Identity names only; no credentials."""

    status: Literal["configured", "missing", "invalid"]
    hosts: list[str]
    services: list[str]
    deployment_environment: str | None
    problems: list[ScopeProblem]


class ProviderStatus(BaseModel):
    """Whether a provider is configured, never its credentials. `source` says where the
    configuration lives; per-environment credentials are planned (D026), so today it is
    always the backend's process-wide development settings."""

    provider: str
    # process_environment: the backend's development settings (legacy or operator grant);
    # environment: this environment's own configuration, which does not exist before 7C.
    source: Literal["process_environment", "environment"]
    configured: bool
    # Field names that are missing or invalid (e.g. "NEBIUS_API_KEY"), never values.
    issues: list[str]


class AiProviderStatus(ProviderStatus):
    main_model: str | None
    local_model: str | None


class EnvironmentStats(BaseModel):
    investigations: int
    active_investigations: int
    evidence: int
    tool_calls: int
    last_investigation_at: datetime | None


class EnvironmentDetail(EnvironmentSummary):
    created_at: datetime
    updated_at: datetime
    organization_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    # environment: runs with its own credentials (D034). process_development: with the
    # backend's development credentials (operator grant, permitted on this server).
    # configuration_required: credentials missing or incomplete. unavailable: stored
    # credentials this server cannot read or whose endpoint is no longer allowed.
    credentials: Literal[
        "environment", "process_development", "configuration_required", "unavailable"
    ]
    scope: TelemetryScopeRead
    stats: EnvironmentStats
    telemetry_provider: ProviderStatus
    ai_provider: AiProviderStatus
    # Kept for the existing contract; credential state is in `credentials` and in
    # GET /environments/{id}/credentials. Configuration forms arrive in 7C-4.
    per_environment_credentials: Literal["not_available"] = "not_available"


class SignalCoverage(BaseModel):
    """What the latest successful query of one signal for one scope identity observed.
    History recorded by investigations, not a live health check."""

    signal: Literal["metrics", "logs", "traces"]
    identity: str
    value: str
    status: Literal["observed", "no_data", "unknown"]
    window_start: datetime | None
    window_end: datetime | None
    checked_at: datetime
    investigation_id: uuid.UUID


class TelemetryCoverage(BaseModel):
    signals: list[SignalCoverage]
    queries_checked: int
    queries_failed: int
