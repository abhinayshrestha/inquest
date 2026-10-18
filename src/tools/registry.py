from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from pydantic import BaseModel

from src.integrations.signoz import (
    get_trace,
    log_volume,
    query_metrics,
    search_logs,
    search_spans,
    trace_stats,
)
from src.integrations.signoz.schemas import (
    LogSearchRequest,
    LogVolumeData,
    LogVolumeRequest,
    MetricData,
    MetricQueryRequest,
    SigNozResult,
    SpanSearchRequest,
    TraceCompleteness,
    TraceRequest,
    TraceStatsData,
    TraceStatsRequest,
)
from src.models import EnvironmentType, EvidenceType, ToolCategory
from src.tools.schemas import Outcome, ToolDescriptor

Operation = Callable[..., Awaitable[SigNozResult[Any]]]

SIGNOZ_ENVIRONMENTS = frozenset({EnvironmentType.LOCAL, EnvironmentType.AWS})


class ToolSpec(NamedTuple):
    name: str
    description: str
    request_model: type[BaseModel]
    run: Operation
    has_observations: Callable[[Any], bool]
    # Coverage query for empty results: same identity and narrowing, every other filter
    # dropped. Tells "nothing matched" apart from "no telemetry arrived at all".
    probe: Callable[[Any], tuple[Operation, BaseModel]]
    evidence_type: EvidenceType
    empty_outcome: Outcome = Outcome.EMPTY
    category: ToolCategory = ToolCategory.TELEMETRY
    environment_types: frozenset[EnvironmentType] = SIGNOZ_ENVIRONMENTS


def _metric_probe(req: MetricQueryRequest) -> tuple[Operation, BaseModel]:
    return query_metrics, req.model_copy(update={"where": [], "group_by": []})


def _log_probe(req: LogSearchRequest | LogVolumeRequest) -> tuple[Operation, BaseModel]:
    return log_volume, LogVolumeRequest(
        time_range=req.time_range, identity=req.identity, host=req.host, service=req.service
    )


def _trace_probe(req: BaseModel) -> tuple[Operation, BaseModel]:
    return trace_stats, TraceStatsRequest(
        time_range=req.time_range,  # type: ignore[attr-defined]
        service=getattr(req, "service", None),
        group_by="service",
    )


def coverage_count(data: BaseModel) -> int:
    if isinstance(data, LogVolumeData):
        return data.total_count
    if isinstance(data, TraceStatsData):
        return sum(g.count for g in data.groups)
    if isinstance(data, MetricData):
        return data.total_series
    raise TypeError(f"no coverage count for {type(data).__name__}")


_SCOPE_NOTE = (
    " Results are restricted to the environment's authorized hosts or services; identities "
    "outside that scope are rejected."
)

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "signoz.query_metrics",
            "Summarize one metric over a time window (min/max/mean/median/last per series). "
            "Prefer `metric` presets: host-scoped cpu_utilization, memory_utilization, "
            "memory_used_bytes, load_average_1m, disk_io_bytes_rate, disk_operations_rate, "
            "filesystem_utilization, network_io_bytes_rate, network_errors_rate, "
            "network_dropped_rate; service-scoped http_server_p99_seconds. Use `custom` only "
            "with an exact metric name already seen in results; a guessed name returns no "
            "data." + _SCOPE_NOTE,
            MetricQueryRequest,
            query_metrics,
            lambda d: bool(d.series),
            _metric_probe,
            EvidenceType.METRIC,
        ),
        ToolSpec(
            "signoz.search_logs",
            "Fetch one page of matching log records (redacted, clipped). identity='host' "
            "(default): system logs of the environment's hosts (syslog, auth.log); narrow with "
            "`host`. identity='service': application logs, present only if the application "
            "exports logs; narrow with `service`. Never set `service` with identity='host'. "
            "Use log_volume for counts." + _SCOPE_NOTE,
            LogSearchRequest,
            search_logs,
            lambda d: bool(d.records),
            _log_probe,
            EvidenceType.LOG,
        ),
        ToolSpec(
            "signoz.log_volume",
            "Count matching logs per time step, optionally grouped, to find spikes. Same "
            "identity rules as search_logs: identity='host' (default) for system logs, "
            "identity='service' for application logs; narrow only with the matching field."
            + _SCOPE_NOTE,
            LogVolumeRequest,
            log_volume,
            lambda d: d.total_count > 0,
            _log_probe,
            EvidenceType.LOG,
        ),
        ToolSpec(
            "signoz.search_spans",
            "Fetch one page of matching spans (slowest or latest first). Use trace_stats for "
            "rates." + _SCOPE_NOTE,
            SpanSearchRequest,
            search_spans,
            lambda d: bool(d.spans),
            _trace_probe,
            EvidenceType.TRACE,
        ),
        ToolSpec(
            "signoz.trace_stats",
            "Span count, error rate and p50/p95/p99 latency grouped by service or operation. "
            "Start here to learn the real operation (endpoint) names." + _SCOPE_NOTE,
            TraceStatsRequest,
            trace_stats,
            lambda d: bool(d.groups),
            _trace_probe,
            EvidenceType.TRACE,
        ),
        ToolSpec(
            "signoz.get_trace",
            "Fetch one trace's spans and judge its completeness. Spans from services outside "
            "the environment's scope are not returned." + _SCOPE_NOTE,
            TraceRequest,
            get_trace,
            lambda d: d.completeness != TraceCompleteness.NOT_FOUND,
            _trace_probe,
            EvidenceType.TRACE,
            empty_outcome=Outcome.NOT_FOUND,
        ),
    )
}


def list_tools() -> list[ToolDescriptor]:
    return [
        ToolDescriptor(
            name=s.name,
            description=s.description,
            input_schema=s.request_model.model_json_schema(),
        )
        for s in TOOLS.values()
    ]
