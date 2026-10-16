import uuid
from typing import Any

from src.integrations.signoz.normalize import window
from src.integrations.signoz.schemas import (
    LogData,
    LogVolumeData,
    MetricData,
    Scope,
    SigNozResult,
    SpanData,
    TimeRange,
    TraceData,
    TraceStatsData,
)
from src.models import Evidence, EvidenceType, InvestigationAssignment, ToolCall
from src.tools.schemas import TelemetryAvailable

EVIDENCE_PROVIDER = "signoz"
MAX_TITLE_CHARS = 255


def _label(data: Any) -> str:
    if isinstance(data, MetricData):
        return f"Metric {data.metric}"
    if isinstance(data, LogData):
        return "Log records"
    if isinstance(data, LogVolumeData):
        return "Log volume"
    if isinstance(data, SpanData):
        return "Spans"
    if isinstance(data, TraceStatsData):
        return f"Trace stats by {data.group_by}"
    if isinstance(data, TraceData):
        return f"Trace {data.trace_id}"
    return "Telemetry"


def _scope_text(scope: Scope) -> str:
    text = f"{scope.identity} in [{', '.join(scope.values)}]"
    if scope.deployment_environment:
        text += f" ({scope.deployment_environment})"
    return text


def evidence_data(result: SigNozResult[Any], scope: Scope) -> dict[str, Any]:
    # Scope travels with the data so the Main Investigator knows what the observation covers.
    return {
        "data": result.data.model_dump(mode="json"),
        "time_range": result.request["time_range"],
        "scope": scope.model_dump(mode="json"),
        "truncated": result.truncated,
        "warnings": result.warnings,
    }


def build_evidence(
    *,
    evidence_type: EvidenceType,
    result: SigNozResult[Any],
    time_range: TimeRange,
    scope: Scope,
    summary: str,
    data_json: dict[str, Any],
    assignment: InvestigationAssignment,
    tool_call: ToolCall,
) -> Evidence:
    if isinstance(result.data, TraceData):
        entity: tuple[str | None, str | None] = ("trace", result.data.trace_id)
    elif len(scope.values) == 1:
        entity = (scope.identity, scope.values[0])
    else:
        entity = (None, None)
    title = f"{_label(result.data)}, {_scope_text(scope)}, {window(time_range)}"
    # Relationships, not IDs: the composite FKs then fill investigation/environment from the
    # assignment and the database verifies the tool call belongs to the same assignment.
    return Evidence(
        # Set here, not at flush, because the ToolCall's metadata lists it in the same commit.
        id=uuid.uuid4(),
        assignment=assignment,
        tool_call=tool_call,
        evidence_type=evidence_type,
        source_provider=EVIDENCE_PROVIDER,
        source_reference=result.source_reference,
        title=title[:MAX_TITLE_CHARS],
        summary=summary,
        entity_type=entity[0],
        entity_identifier=entity[1],
        observed_at=result.observed_at,
        data_json=data_json,
        redaction_applied=result.redaction_applied,
    )


def empty_summary(summary: str, scope: Scope, telemetry_available: TelemetryAvailable) -> str:
    return (
        f"{summary} Scope: {_scope_text(scope)}. Telemetry for this scope in the window: "
        f"{telemetry_available}. An empty result is not evidence that the system is healthy."
    )
