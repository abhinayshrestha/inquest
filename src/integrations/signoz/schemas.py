import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_validator,
)

# Bounds keep each result small enough for a model prompt and for Evidence.data_json (64 KB).
MAX_RANGE = {
    "metrics": timedelta(days=7),
    "logs": timedelta(hours=24),
    "traces": timedelta(hours=24),
}
MAX_RAW_POINTS = 240
MAX_POINTS_PER_SERIES = 60
MAX_SERIES = 10
MAX_ROWS_PER_PAGE = 100
MAX_OFFSET = 1_000
MAX_TRACE_SPANS = 200
MAX_LOG_BODY_CHARS = 500
MAX_ATTRS_PER_ROW = 10
MAX_ATTR_VALUE_CHARS = 200
MAX_CONDITIONS = 5
MAX_CONDITION_VALUES = 10
MAX_CONDITION_VALUE_CHARS = 200
MAX_SCOPE_VALUES = 20
MAX_SUMMARY_CHARS = 4_000
# Headroom under Evidence's 65_536-byte data_json CHECK.
MAX_DATA_BYTES = 60_000
CLOCK_SKEW = timedelta(minutes=1)

Name = Annotated[str, Field(min_length=1, max_length=255)]
FieldKey = Annotated[str, Field(max_length=100, pattern=r"^[A-Za-z0-9_.]+$")]

# Which resource attribute ties a query's telemetry to an environment.
Identity = Literal["host", "service"]
IDENTITY_KEYS: dict[str, str] = {"host": "host.name", "service": "service.name"}
DEPLOYMENT_ENVIRONMENT_KEY = "deployment.environment"
# Identity is chosen through typed fields and the trusted Scope, never through conditions.
PROTECTED_KEYS = frozenset({*IDENTITY_KEYS.values(), DEPLOYMENT_ENVIRONMENT_KEY})
# Filter-grammar keywords; a key spelled like one could change how the expression parses.
RESERVED_WORDS = frozenset(
    {
        "and", "or", "not", "in", "like", "ilike", "between", "exists", "regexp",
        "contains", "has", "hasany", "hasall", "hastoken", "true", "false", "null",
    }
)  # fmt: skip


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ConditionText = Annotated[str, Field(min_length=1, max_length=MAX_CONDITION_VALUE_CHARS)]
Scalar = StrictBool | StrictInt | StrictFloat | ConditionText
Number = StrictInt | StrictFloat


class Condition(_Request):
    """One structured predicate. Rendered by trusted code with every value quoted, so no
    model-supplied text can change the filter's structure."""

    key: str = Field(max_length=100, pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")
    op: Literal[
        "eq", "neq", "gt", "gte", "lt", "lte",
        "in", "not_in", "contains", "not_contains", "exists", "not_exists",
    ]  # fmt: skip
    value: Scalar | list[Scalar] | None = None

    @model_validator(mode="after")
    def _check(self) -> "Condition":
        if self.key.lower() in RESERVED_WORDS:
            raise ValueError(f"condition key {self.key!r} is a reserved word")
        if self.key.lower() in PROTECTED_KEYS:
            raise ValueError(
                f"condition key {self.key!r} is an identity key; use the host/service fields"
            )
        value = self.value
        if self.op in ("exists", "not_exists"):
            ok = value is None
        elif self.op in ("in", "not_in"):
            ok = isinstance(value, list) and 1 <= len(value) <= MAX_CONDITION_VALUES
        elif self.op in ("gt", "gte", "lt", "lte"):
            ok = isinstance(value, int | float) and not isinstance(value, bool)
        elif self.op in ("contains", "not_contains"):
            ok = isinstance(value, str)
        else:
            ok = value is not None and not isinstance(value, list)
        if not ok:
            raise ValueError(f"value does not fit operator {self.op!r}")
        values = value if isinstance(value, list) else [value]
        if any(isinstance(v, float) and not math.isfinite(v) for v in values):
            raise ValueError("numeric values must be finite")
        return self


Where = Annotated[list[Condition], Field(default_factory=list, max_length=MAX_CONDITIONS)]


class Scope(_Request):
    """Mandatory environment restriction. Built only by trusted code (the tool executor);
    never a field of a request model, so a model cannot supply or widen it."""

    identity: Identity
    values: tuple[Name, ...] = Field(min_length=1, max_length=MAX_SCOPE_VALUES)
    deployment_environment: Name | None = None

    @model_validator(mode="after")
    def _check(self) -> "Scope":
        if self.deployment_environment and self.identity != "service":
            raise ValueError("deployment_environment applies to service identity only")
        return self


class TimeRange(_Request):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _check_order(self) -> "TimeRange":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.start > datetime.now(UTC) + CLOCK_SKEW:
            raise ValueError("start is in the future")
        return self

    def check_span(self, signal: str) -> None:
        if self.end - self.start > MAX_RANGE[signal]:
            raise ValueError(f"{signal} time range may not exceed {MAX_RANGE[signal]}")


class HostMetric(StrEnum):
    CPU_UTILIZATION = "cpu_utilization"
    MEMORY_UTILIZATION = "memory_utilization"
    MEMORY_USED_BYTES = "memory_used_bytes"
    LOAD_AVERAGE_1M = "load_average_1m"
    DISK_IO_BYTES_RATE = "disk_io_bytes_rate"
    DISK_OPERATIONS_RATE = "disk_operations_rate"
    FILESYSTEM_UTILIZATION = "filesystem_utilization"
    NETWORK_IO_BYTES_RATE = "network_io_bytes_rate"
    NETWORK_ERRORS_RATE = "network_errors_rate"
    NETWORK_DROPPED_RATE = "network_dropped_rate"
    HTTP_SERVER_P99_SECONDS = "http_server_p99_seconds"


class CustomMetric(_Request):
    """Escape hatch for metrics without a preset; aggregations restricted to SigNoz enums."""

    metric_name: str = Field(max_length=200, pattern=r"^[A-Za-z0-9_.]+$")
    # Presets know whether they are host or service telemetry; a custom metric must say.
    identity: Identity
    time_aggregation: (
        Literal["latest", "sum", "avg", "min", "max", "count", "rate", "increase"] | None
    ) = None
    space_aggregation: Literal["sum", "avg", "min", "max", "count", "p50", "p90", "p95", "p99"]


class MetricQueryRequest(_Request):
    time_range: TimeRange
    metric: HostMetric | None = None
    custom: CustomMetric | None = None
    host: Name | None = None
    service: Name | None = None
    group_by: list[FieldKey] = Field(default_factory=list, max_length=3)
    where: Where
    step_seconds: int | None = Field(default=None, ge=10, le=3600)

    @model_validator(mode="after")
    def _check(self) -> "MetricQueryRequest":
        if (self.metric is None) == (self.custom is None):
            raise ValueError("exactly one of metric or custom is required")
        self.time_range.check_span("metrics")
        return self


class LogFilters(_Request):
    time_range: TimeRange
    # System logs (syslog, auth.log) are host telemetry; application logs are service telemetry.
    identity: Identity = "host"
    host: Name | None = None
    service: Name | None = None
    log_file: Name | None = None
    body_contains: str | None = Field(default=None, min_length=1, max_length=200)
    severity: list[Annotated[str, Field(min_length=1, max_length=20)]] | None = Field(
        default=None, min_length=1, max_length=6
    )
    where: Where

    @model_validator(mode="after")
    def _check(self) -> "LogFilters":
        self.time_range.check_span("logs")
        return self


class LogSearchRequest(LogFilters):
    order: Literal["desc", "asc"] = "desc"
    limit: int = Field(default=50, ge=1, le=MAX_ROWS_PER_PAGE)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)


class LogVolumeRequest(LogFilters):
    group_by: Literal["host", "log_file", "service"] | None = None
    step_seconds: int | None = Field(default=None, ge=10, le=3600)


class SpanSearchRequest(_Request):
    time_range: TimeRange
    service: Name | None = None
    operation: Name | None = None
    errors_only: bool = False
    min_duration_ms: float | None = Field(default=None, ge=0)
    where: Where
    order_by: Literal["timestamp", "duration"] = "timestamp"
    limit: int = Field(default=50, ge=1, le=MAX_ROWS_PER_PAGE)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)

    @model_validator(mode="after")
    def _check(self) -> "SpanSearchRequest":
        self.time_range.check_span("traces")
        return self


class TraceStatsRequest(_Request):
    time_range: TimeRange
    service: Name | None = None
    group_by: Literal["service", "operation"] = "operation"
    where: Where

    @model_validator(mode="after")
    def _check(self) -> "TraceStatsRequest":
        self.time_range.check_span("traces")
        return self


class TraceRequest(_Request):
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    time_range: TimeRange

    @model_validator(mode="after")
    def _check(self) -> "TraceRequest":
        self.time_range.check_span("traces")
        return self


class QueryStats(BaseModel):
    rows_scanned: int = 0
    bytes_scanned: int = 0
    signoz_duration_ms: int = 0


class SeriesStats(BaseModel):
    labels: dict[str, str]
    unit: str | None
    min: float
    max: float
    mean: float
    median: float
    last: float
    sum: float
    max_at: AwareDatetime
    # Downsampled by bucket max so short spikes survive.
    points: list[tuple[AwareDatetime, float]]


class MetricData(BaseModel):
    metric: str
    step_seconds: int
    series: list[SeriesStats]
    total_series: int


class LogVolumeData(BaseModel):
    step_seconds: int
    series: list[SeriesStats]
    total_series: int
    total_count: int


class LogRecord(BaseModel):
    id: str | None
    timestamp: AwareDatetime
    severity: str | None
    host: str | None
    service: str | None
    log_file: str | None
    body: str
    attributes: dict[str, str]


class LogData(BaseModel):
    """One page of matching records, not the full match set. Raw queries return no total;
    use log_volume for counts."""

    records: list[LogRecord]
    offset: int = 0
    has_more: bool
    next_offset: int | None


class SpanRecord(BaseModel):
    timestamp: AwareDatetime
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str | None
    name: str
    kind: str | None
    duration_ms: float
    has_error: bool
    status_code: str | None
    http_status: int | None


class SpanData(BaseModel):
    """One page of matching spans, not the full match set. Use trace_stats for counts and
    error rates."""

    spans: list[SpanRecord]
    offset: int = 0
    has_more: bool
    next_offset: int | None


class TraceGroupStats(BaseModel):
    group: str
    count: int
    error_count: int
    error_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


class TraceStatsData(BaseModel):
    group_by: str
    groups: list[TraceGroupStats]
    has_more: bool


class TraceCompleteness(StrEnum):
    # Root present and every referenced parent present. Missing leaf spans or spans
    # outside the query window cannot be detected, so this is not proof of completeness.
    CONSISTENT = "consistent"
    INCOMPLETE = "incomplete"
    NOT_FOUND = "not_found"


class TraceData(BaseModel):
    trace_id: str
    # Spans fetched (at most MAX_TRACE_SPANS); `spans` may hold fewer after the size cap.
    span_count: int
    root_service: str | None
    # The root span's own duration; None without a root, since then it is unknowable.
    duration_ms: float | None
    # Earliest start to latest end among fetched spans; a lower bound on the trace duration.
    observed_window_ms: float = 0.0
    root_found: bool = False
    # completeness and incomplete_reasons describe the trace as fetched from SigNoz, judged
    # on all fetched spans; CONSISTENT exactly when incomplete_reasons is empty.
    completeness: TraceCompleteness = TraceCompleteness.NOT_FOUND
    missing_parent_spans: int = 0
    incomplete_reasons: list[str] = Field(default_factory=list)
    # Fetched spans left out of `spans` by the size cap: a gap in this result, not the trace.
    spans_omitted: int = 0
    error_spans: int
    spans: list[SpanRecord]


class SigNozResult[T: BaseModel](BaseModel):
    """Bounded, normalized output. Maps onto ToolCall (request, summary, stats) and
    optionally Evidence (data, source_reference, redaction_applied)."""

    signal: Literal["metrics", "logs", "traces"]
    operation: str
    request: dict[str, Any]
    data: T
    summary: str = Field(max_length=MAX_SUMMARY_CHARS)
    stats: QueryStats
    truncated: bool
    redaction_applied: bool
    source_reference: str = Field(max_length=500)
    # Timestamp of a returned record or span; None for aggregated results, whose window is
    # request["time_range"].
    observed_at: AwareDatetime | None
    warnings: list[str]
