import math
from datetime import datetime
from typing import Any, NamedTuple

from src.integrations.signoz.schemas import (
    MAX_RAW_POINTS,
    MAX_SERIES,
    MAX_TRACE_SPANS,
    HostMetric,
    LogFilters,
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    SpanSearchRequest,
    TimeRange,
    TraceRequest,
    TraceStatsRequest,
)

SCHEMA_VERSION = "v1"
MIN_STEP_SECONDS = 60
RESOURCE_KEYS = {"host.name", "service.name", "os.type"}

SPAN_FIELDS = [
    ("timestamp", "span"),
    ("trace_id", "span"),
    ("span_id", "span"),
    ("parent_span_id", "span"),
    ("name", "span"),
    ("kind_string", "span"),
    ("duration_nano", "span"),
    ("has_error", "span"),
    ("status_code_string", "span"),
    ("response_status_code", "span"),
    ("service.name", "resource"),
]

# Aliases double as the order key and the normalizer's lookup names.
TRACE_STATS_AGGREGATIONS = [
    ("count()", "span_count"),
    ("countIf(has_error = true)", "error_count"),
    ("p50(duration_nano)", "p50_duration"),
    ("p95(duration_nano)", "p95_duration"),
    ("p99(duration_nano)", "p99_duration"),
]


class Aggregation(NamedTuple):
    metric_name: str
    time_aggregation: str | None
    space_aggregation: str
    filter: str | None = None


class Preset(NamedTuple):
    unit: str | None
    aggregations: tuple[Aggregation, ...]
    formula: str | None = None
    group_by: tuple[str, ...] = ("host.name",)


# Utilization metrics are disabled by default in the hostmetrics receiver and absent from
# local SigNoz, so percentages are derived with formulas from always-on counters.
PRESETS: dict[HostMetric, Preset] = {
    HostMetric.CPU_UTILIZATION: Preset(
        "percent",
        (
            Aggregation("system.cpu.time", "rate", "sum", "state != 'idle'"),
            Aggregation("system.cpu.time", "rate", "sum"),
        ),
        "A / B * 100",
    ),
    HostMetric.MEMORY_UTILIZATION: Preset(
        "percent",
        (
            Aggregation("system.memory.usage", "avg", "sum", "state = 'used'"),
            # slab_* states overlap the others, so only these four sum to total memory.
            Aggregation(
                "system.memory.usage",
                "avg",
                "sum",
                "state IN ['used', 'free', 'buffered', 'cached']",
            ),
        ),
        "A / B * 100",
    ),
    HostMetric.MEMORY_USED_BYTES: Preset(
        "bytes", (Aggregation("system.memory.usage", "avg", "sum", "state = 'used'"),)
    ),
    HostMetric.LOAD_AVERAGE_1M: Preset(
        None, (Aggregation("system.cpu.load_average.1m", "avg", "avg"),)
    ),
    HostMetric.DISK_IO_BYTES_RATE: Preset(
        "bytes/s", (Aggregation("system.disk.io", "rate", "sum"),), None, ("host.name", "direction")
    ),
    HostMetric.DISK_OPERATIONS_RATE: Preset(
        "ops/s",
        (Aggregation("system.disk.operations", "rate", "sum"),),
        None,
        ("host.name", "direction"),
    ),
    HostMetric.FILESYSTEM_UTILIZATION: Preset(
        "percent",
        (
            Aggregation("system.filesystem.usage", "avg", "sum", "state = 'used'"),
            Aggregation("system.filesystem.usage", "avg", "sum"),
        ),
        "A / B * 100",
        ("host.name", "mountpoint"),
    ),
    HostMetric.NETWORK_IO_BYTES_RATE: Preset(
        "bytes/s",
        (Aggregation("system.network.io", "rate", "sum", "device != 'lo'"),),
        None,
        ("host.name", "direction"),
    ),
    HostMetric.NETWORK_ERRORS_RATE: Preset(
        "errors/s",
        (Aggregation("system.network.errors", "rate", "sum", "device != 'lo'"),),
        None,
        ("host.name", "direction"),
    ),
    HostMetric.NETWORK_DROPPED_RATE: Preset(
        "packets/s",
        (Aggregation("system.network.dropped", "rate", "sum", "device != 'lo'"),),
        None,
        ("host.name", "direction"),
    ),
    HostMetric.HTTP_SERVER_P99_SECONDS: Preset(
        "seconds",
        (Aggregation("http.server.request.duration.bucket", None, "p99"),),
        None,
        ("service.name",),
    ),
}


def to_epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def quote(value: str) -> str:
    # SigNoz filter grammar uses backslash escapes inside single-quoted literals.
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def and_filters(*parts: str | None, user_filter: str | None = None) -> str | None:
    clauses = [p for p in parts if p]
    if user_filter:
        clauses.append(f"({user_filter})")
    return " AND ".join(clauses) or None


def choose_step(time_range: TimeRange, requested: int | None) -> int:
    seconds = (time_range.end - time_range.start).total_seconds()
    return max(requested or MIN_STEP_SECONDS, math.ceil(seconds / MAX_RAW_POINTS))


def field_key(name: str, context: str | None = None) -> dict[str, str]:
    context = context or ("resource" if name in RESOURCE_KEYS else None)
    return {"name": name, "fieldContext": context} if context else {"name": name}


def envelope(time_range: TimeRange, request_type: str, queries: list[dict], **extra: Any) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "start": to_epoch_ms(time_range.start),
        "end": to_epoch_ms(time_range.end),
        "requestType": request_type,
        "compositeQuery": {"queries": queries},
        **extra,
    }


def builder_query(spec: dict[str, Any]) -> dict[str, Any]:
    return {"type": "builder_query", "spec": {k: v for k, v in spec.items() if v is not None}}


def filter_spec(expression: str | None) -> dict[str, str] | None:
    return {"expression": expression} if expression else None


def metric_query(req: MetricQueryRequest) -> tuple[dict[str, Any], str, str | None, int]:
    """Returns (body, metric label, unit, step_seconds)."""
    if req.metric is not None:
        preset = PRESETS[req.metric]
        label = req.metric.value
    else:
        assert req.custom is not None
        c = req.custom
        preset = Preset(
            None, (Aggregation(c.metric_name, c.time_aggregation, c.space_aggregation),), None, ()
        )
        label = c.metric_name

    if preset.formula and "state" in req.group_by:
        # Both formula operands must share labels; grouping by the filtered key breaks the join.
        raise ValueError(f"{label} cannot be grouped by state")

    group_by = list(dict.fromkeys([*preset.group_by, *req.group_by]))
    step = choose_step(req.time_range, req.step_seconds)
    scope = and_filters(
        f"host.name = {quote(req.host)}" if req.host else None,
        f"service.name = {quote(req.service)}" if req.service else None,
        user_filter=req.filter,
    )

    queries = []
    for name, agg in zip("ABCDEFGH", preset.aggregations, strict=False):
        aggregation: dict[str, Any] = {
            "metricName": agg.metric_name,
            "spaceAggregation": agg.space_aggregation,
        }
        if agg.time_aggregation:
            aggregation["timeAggregation"] = agg.time_aggregation
        queries.append(
            builder_query(
                {
                    "name": name,
                    "signal": "metrics",
                    "stepInterval": step,
                    "aggregations": [aggregation],
                    "filter": filter_spec(and_filters(scope, agg.filter)),
                    "groupBy": [field_key(k) for k in group_by] or None,
                    # Formula operands are hidden so only the derived series comes back.
                    "disabled": bool(preset.formula),
                }
            )
        )
    if preset.formula:
        queries.append(
            {"type": "builder_formula", "spec": {"name": "F", "expression": preset.formula}}
        )
    return envelope(req.time_range, "time_series", queries), label, preset.unit, step


def log_filter(req: LogFilters) -> str | None:
    severity = None
    if req.severity:
        severity = "severity_text IN [" + ", ".join(quote(s) for s in req.severity) + "]"
    return and_filters(
        f"host.name = {quote(req.host)}" if req.host else None,
        f"service.name = {quote(req.service)}" if req.service else None,
        f"log.file.name = {quote(req.log_file)}" if req.log_file else None,
        f"body CONTAINS {quote(req.body_contains)}" if req.body_contains else None,
        severity,
        user_filter=req.filter,
    )


def log_search_query(req: LogSearchRequest) -> dict[str, Any]:
    # No selectFields: SigNoz then returns body, severity and the attribute/resource maps
    # without failing on keys that some log sources lack.
    spec = {
        "name": "A",
        "signal": "logs",
        "filter": filter_spec(log_filter(req)),
        "order": [
            {"key": {"name": "timestamp", "fieldContext": "log"}, "direction": req.order},
            {"key": {"name": "id"}, "direction": req.order},
        ],
        # One extra row reveals whether another page exists.
        "limit": req.limit + 1,
        "offset": req.offset,
    }
    return envelope(req.time_range, "raw", [builder_query(spec)])


LOG_VOLUME_GROUP_KEYS = {
    "host": "host.name",
    "log_file": "log.file.name",
    "service": "service.name",
}


def log_volume_query(req: LogVolumeRequest) -> tuple[dict[str, Any], int]:
    step = choose_step(req.time_range, req.step_seconds)
    group = LOG_VOLUME_GROUP_KEYS.get(req.group_by) if req.group_by else None
    spec = {
        "name": "A",
        "signal": "logs",
        "stepInterval": step,
        "aggregations": [{"expression": "count()", "alias": "log_count"}],
        "filter": filter_spec(log_filter(req)),
        "groupBy": [field_key(group)] if group else None,
        "order": [{"key": {"name": "log_count"}, "direction": "desc"}] if group else None,
        "limit": MAX_SERIES + 1 if group else None,
    }
    # Zero-filled buckets keep the median baseline honest for spike detection.
    body = envelope(
        req.time_range, "time_series", [builder_query(spec)], formatOptions={"fillGaps": True}
    )
    return body, step


def span_search_query(req: SpanSearchRequest) -> dict[str, Any]:
    min_duration = None
    if req.min_duration_ms is not None:
        min_duration = f"duration_nano > {int(req.min_duration_ms * 1_000_000)}"
    order_key = "duration_nano" if req.order_by == "duration" else "timestamp"
    spec = {
        "name": "A",
        "signal": "traces",
        "filter": filter_spec(
            and_filters(
                f"service.name = {quote(req.service)}" if req.service else None,
                f"name = {quote(req.operation)}" if req.operation else None,
                "has_error = true" if req.errors_only else None,
                min_duration,
                user_filter=req.filter,
            )
        ),
        "selectFields": [field_key(n, c) for n, c in SPAN_FIELDS],
        "order": [{"key": field_key(order_key, "span"), "direction": "desc"}],
        "limit": req.limit + 1,
        "offset": req.offset,
    }
    return envelope(req.time_range, "raw", [builder_query(spec)])


def trace_stats_query(req: TraceStatsRequest) -> dict[str, Any]:
    group = "service.name" if req.group_by == "service" else "name"
    spec = {
        "name": "A",
        "signal": "traces",
        "aggregations": [{"expression": e, "alias": a} for e, a in TRACE_STATS_AGGREGATIONS],
        "filter": filter_spec(
            and_filters(
                f"service.name = {quote(req.service)}" if req.service else None,
                user_filter=req.filter,
            )
        ),
        "groupBy": [field_key(group, "resource" if group == "service.name" else "span")],
        "order": [{"key": {"name": "p99_duration"}, "direction": "desc"}],
        "limit": MAX_SERIES + 1,
    }
    return envelope(req.time_range, "scalar", [builder_query(spec)])


def trace_query(req: TraceRequest) -> dict[str, Any]:
    spec = {
        "name": "A",
        "signal": "traces",
        "filter": filter_spec(f"trace_id = {quote(req.trace_id)}"),
        "selectFields": [field_key(n, c) for n, c in SPAN_FIELDS],
        "order": [{"key": field_key("timestamp", "span"), "direction": "asc"}],
        "limit": MAX_TRACE_SPANS + 1,
    }
    return envelope(req.time_range, "raw", [builder_query(spec)])
