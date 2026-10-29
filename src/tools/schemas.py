import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models import ToolCallStatus

TelemetryAvailable = Literal["yes", "no", "unknown"]


class ExecutionContext(BaseModel):
    """Bound by the orchestrator when it launches an investigator; never built from tool
    arguments. The executor still re-checks every ID against the locked assignment row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    investigation_id: uuid.UUID
    assignment_id: uuid.UUID
    environment_id: uuid.UUID
    # The tenant the token was issued for; None only for ownerless legacy records (D032).
    organization_id: uuid.UUID | None = None
    # From the agent framework (e.g. LangGraph's tool_call_id) so a replayed call runs once.
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


class Outcome(StrEnum):
    OBSERVATIONS = "observations"
    # The query ran and matched nothing. Never evidence that a system is healthy.
    EMPTY = "empty"
    NOT_FOUND = "not_found"


class ToolError(BaseModel):
    code: str
    message: str
    # Hint for the caller: True means the same call may succeed later; False means change it.
    retryable: bool = False


class ToolExecutionResult(BaseModel):
    # None only when nothing could be persisted (database down, unknown assignment).
    tool_call_id: uuid.UUID | None
    tool_name: str
    status: ToolCallStatus
    outcome: Outcome | None = None
    summary: str | None = None
    # Bounded adapter output; only returned once the ToolCall and Evidence are committed.
    data: dict[str, Any] | None = None
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    telemetry_available: TelemetryAvailable | None = None
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    error: ToolError | None = None
    replayed: bool = False


class ToolDescriptor(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
