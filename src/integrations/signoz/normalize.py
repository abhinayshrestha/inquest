import json
import math
import re
import statistics
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, TypeAdapter, ValidationError

from src.integrations.signoz.builders import TRACE_STATS_AGGREGATIONS
from src.integrations.signoz.errors import SigNozError, SigNozErrorCode
from src.integrations.signoz.schemas import (
    MAX_ATTR_VALUE_CHARS,
    MAX_ATTRS_PER_ROW,
    MAX_DATA_BYTES,
    MAX_LOG_BODY_CHARS,
    MAX_POINTS_PER_SERIES,
    MAX_SERIES,
    MAX_SUMMARY_CHARS,
    MAX_TRACE_SPANS,
    LogData,
    LogRecord,
    LogVolumeData,
    MetricData,
    QueryStats,
    SeriesStats,
    SpanData,
    SpanRecord,
    TimeRange,
    TraceCompleteness,
    TraceData,
    TraceGroupStats,
    TraceStatsData,
)

MAX_WARNINGS = 5
MAX_WARNING_CHARS = 300
TRUNCATION_MARK = " …[truncated]"

_timestamp = TypeAdapter(AwareDatetime)

# auth.log and app logs can carry credentials; these never reach the model or Postgres.
REDACTIONS = [
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization)"
            r"(\"?\s*[=:]\s*\"?)(?!\[REDACTED)[^\s\"',;&]+"
        ),
        r"\1\2[REDACTED]",
    ),
]


def redact(text: str) -> tuple[str, bool]:
    redacted = text
    for pattern, replacement in REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted, redacted != text


def clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: limit - len(TRUNCATION_MARK)] + TRUNCATION_MARK, True


def _malformed(detail: str) -> SigNozError:
    return SigNozError(SigNozErrorCode.MALFORMED_RESPONSE, detail)


def unwrap(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], QueryStats, list[str]]:
    """Validates the v5 success envelope; returns (results, stats, warnings)."""
    data = payload.get("data")
    if payload.get("status") != "success" or not isinstance(data, dict):
        raise _malformed("unexpected query_range envelope")
    inner = data.get("data") or {}
    results = inner.get("results") if isinstance(inner, dict) else None
    if results is None:
        results = []
    if not isinstance(results, list):
        raise _malformed("query_range results is not a list")

    meta = data.get("meta") or {}
    stats = QueryStats(
        rows_scanned=int(meta.get("rowsScanned") or 0),
        bytes_scanned=int(meta.get("bytesScanned") or 0),
        signoz_duration_ms=int(meta.get("durationMs") or 0),
    )

    warnings: list[str] = []
    warning = data.get("warning")
    if isinstance(warning, dict):
        messages = [warning.get("message")]
        messages += [w.get("message") for w in warning.get("warnings") or [] if isinstance(w, dict)]
        warnings = [clip(str(m), MAX_WARNING_CHARS)[0] for m in messages if m][:MAX_WARNINGS]
    return [r for r in results if isinstance(r, dict)], stats, warnings


def find_result(results: list[dict[str, Any]], query_name: str) -> dict[str, Any]:
    # SigNoz omits a query's entry, or sends nulls, when it matched nothing.
    return next((r for r in results if r.get("queryName") == query_name), {})


def parse_timestamp(value: Any) -> datetime:
    try:
        if isinstance(value, int | float):
            # Raw rows carry epoch nanoseconds; time series values carry milliseconds.
            return datetime.fromtimestamp(value / (1e9 if value > 1e14 else 1e3), UTC)
        return _timestamp.validate_python(value)
    except (ValidationError, ValueError, OverflowError, OSError):
        raise _malformed("unparseable timestamp in SigNoz response") from None


def _num(value: float) -> float:
    return round(value, 4)


def downsample(points: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Keeps first, last, and each bucket's min and max, so spikes and drops both survive.

    Expects chronologically sorted points. Buckets split by point count, not time, so the
    budget holds even with irregular scrape intervals.
    """
    if len(points) <= MAX_POINTS_PER_SERIES:
        return points
    first, *interior, last = points
    size = math.ceil(len(interior) / ((MAX_POINTS_PER_SERIES - 2) // 2))
    kept = [first]
    for i in range(0, len(interior), size):
        chunk = interior[i : i + size]
        extremes = {min(chunk, key=lambda p: p[1]), max(chunk, key=lambda p: p[1])}
        kept.extend(sorted(extremes))
    kept.append(last)
    return kept


def series_stats(
    labels: dict[str, str], unit: str | None, points: list[tuple[datetime, float]]
) -> SeriesStats:
    points = sorted(points)
    values = [v for _, v in points]
    peak_at, peak = max(points, key=lambda p: p[1])
    return SeriesStats(
        labels=labels,
        unit=unit,
        min=_num(min(values)),
        max=_num(peak),
        mean=_num(statistics.fmean(values)),
        median=_num(statistics.median(values)),
        last=_num(values[-1]),
        sum=_num(math.fsum(values)),
        max_at=peak_at,
        points=[(t, _num(v)) for t, v in downsample(points)],
    )


def _label_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def time_series(result: dict[str, Any], unit: str | None) -> list[SeriesStats]:
    out: list[SeriesStats] = []
    for bucket in result.get("aggregations") or []:
        bucket_unit = (bucket.get("meta") or {}).get("unit") or unit
        for series in bucket.get("series") or []:
            labels = {
                str(label["key"]["name"]): _label_value(label.get("value"))
                for label in series.get("labels") or []
                if isinstance(label.get("key"), dict) and label["key"].get("name")
            }
            points = [
                (parse_timestamp(v["timestamp"]), float(v["value"]))
                for v in series.get("values") or []
                if isinstance(v.get("value"), int | float) and math.isfinite(v["value"])
            ]
            if points:
                out.append(series_stats(labels, bucket_unit, points))
    return out


def cap_series(series: list[SeriesStats]) -> tuple[list[SeriesStats], bool]:
    # Highest peaks first: the series most likely to explain an incident survive the cap.
    ranked = sorted(series, key=lambda s: s.max, reverse=True)
    return ranked[:MAX_SERIES], len(ranked) > MAX_SERIES


def json_size(value: Any) -> int:
    # Postgres measures jsonb as text with ", " and ": " separators, which compact JSON
    # undercounts by about two bytes per key; measure the way the CHECK constraint does.
    return len(json.dumps(value, ensure_ascii=False).encode())


def fit(model: BaseModel, items: list) -> bool:
    """Drops trailing items until the model fits in Evidence.data_json."""
    cut = False
    while items and json_size(model.model_dump(mode="json")) > MAX_DATA_BYTES:
        items.pop()
        cut = True
    return cut


def metric_data(
    results: list[dict[str, Any]], label: str, unit: str | None, step: int, query_name: str
) -> tuple[MetricData, bool]:
    series = time_series(find_result(results, query_name), unit)
    kept, capped = cap_series(series)
    data = MetricData(metric=label, step_seconds=step, series=kept, total_series=len(series))
    return data, fit(data, data.series) or capped


def log_volume_data(results: list[dict[str, Any]], step: int) -> tuple[LogVolumeData, bool]:
    series = time_series(find_result(results, "A"), "count")
    kept, capped = cap_series(series)
    data = LogVolumeData(
        step_seconds=step,
        series=kept,
        total_series=len(series),
        total_count=int(math.fsum(s.sum for s in series)),
    )
    return data, fit(data, data.series) or capped


def _rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = find_result(results, "A").get("rows") or []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("data"), dict)]


def _merged(data: dict[str, Any], *columns: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for column in columns:
        if isinstance(data.get(column), dict):
            merged.update(data[column])
    return merged


def log_record(row: dict[str, Any]) -> tuple[LogRecord, bool, bool]:
    """Returns (record, clipped, redacted)."""
    data = row["data"]
    attributes = _merged(
        data, "attributes", "attributes_string", "attributes_number", "attributes_bool"
    )
    resources = _merged(data, "resource", "resources_string")
    host = resources.pop("host.name", None)
    service = resources.pop("service.name", None)
    log_file = attributes.pop("log.file.name", None)

    body = data.get("body")
    body = body if isinstance(body, str) else json.dumps(body, default=str)
    body, redacted = redact(body)
    body, clipped = clip(body, MAX_LOG_BODY_CHARS)

    extra: dict[str, str] = {}
    for key in sorted({**resources, **attributes}):
        value = attributes.get(key, resources.get(key))
        text, was_redacted = redact(_label_value(value))
        extra[key[:100]] = clip(text, MAX_ATTR_VALUE_CHARS)[0]
        redacted |= was_redacted
    clipped |= len(extra) > MAX_ATTRS_PER_ROW

    record = LogRecord(
        id=str(data["id"]) if data.get("id") else None,
        timestamp=parse_timestamp(row.get("timestamp") or data.get("timestamp")),
        severity=data.get("severity_text") or None,
        host=host or None,
        service=service or None,
        log_file=log_file or None,
        body=body,
        attributes=dict(list(extra.items())[:MAX_ATTRS_PER_ROW]),
    )
    return record, clipped, redacted


def log_data(results: list[dict[str, Any]], limit: int, offset: int) -> tuple[LogData, bool, bool]:
    """Returns (data, truncated, redacted)."""
    rows = _rows(results)
    has_more = len(rows) > limit
    records, truncated, redacted = [], False, False
    for row in rows[:limit]:
        record, was_clipped, was_redacted = log_record(row)
        records.append(record)
        truncated |= was_clipped
        redacted |= was_redacted
    data = LogData(records=records, offset=offset, has_more=has_more, next_offset=None)
    if fit(data, data.records):
        truncated = True
        data.has_more = True
    if data.has_more:
        data.next_offset = offset + len(data.records)
    return data, truncated, redacted


def _http_status(value: Any) -> int | None:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return None
    return status or None


def span_record(row: dict[str, Any]) -> SpanRecord:
    data = row["data"]
    return SpanRecord(
        timestamp=parse_timestamp(row.get("timestamp") or data.get("timestamp")),
        trace_id=str(data.get("trace_id") or ""),
        span_id=str(data.get("span_id") or ""),
        parent_span_id=data.get("parent_span_id") or None,
        service=data.get("service.name") or None,
        name=redact(str(data.get("name") or ""))[0],
        kind=data.get("kind_string") or None,
        duration_ms=_num(float(data.get("duration_nano") or 0) / 1e6),
        has_error=bool(data.get("has_error")),
        status_code=data.get("status_code_string") or None,
        http_status=_http_status(data.get("response_status_code")),
    )


def span_data(results: list[dict[str, Any]], limit: int, offset: int) -> tuple[SpanData, bool]:
    rows = _rows(results)
    data = SpanData(
        spans=[span_record(r) for r in rows[:limit]],
        offset=offset,
        has_more=len(rows) > limit,
        next_offset=None,
    )
    truncated = fit(data, data.spans)
    data.has_more |= truncated
    if data.has_more:
        data.next_offset = offset + len(data.spans)
    return data, truncated


def trace_stats_data(results: list[dict[str, Any]], group_by: str) -> tuple[TraceStatsData, bool]:
    result = find_result(results, "A")
    columns = result.get("columns") or []
    group_index = next((i for i, c in enumerate(columns) if c.get("columnType") == "group"), None)
    # aggregationIndex follows the order aggregations were sent, so aliases map by position.
    agg_index = {
        c.get("aggregationIndex"): i
        for i, c in enumerate(columns)
        if c.get("columnType") == "aggregation"
    }
    names = [alias for _, alias in TRACE_STATS_AGGREGATIONS]

    groups = []
    for row in result.get("data") or []:
        values = {
            names[a]: float(row[i] or 0)
            for a, i in agg_index.items()
            if isinstance(a, int) and a < len(names) and i < len(row)
        }
        count = int(values.get("span_count", 0))
        errors = int(values.get("error_count", 0))
        groups.append(
            TraceGroupStats(
                group=str(row[group_index]) if group_index is not None else "all",
                count=count,
                error_count=errors,
                error_rate=_num(errors / count) if count else 0.0,
                p50_ms=_num(values.get("p50_duration", 0) / 1e6),
                p95_ms=_num(values.get("p95_duration", 0) / 1e6),
                p99_ms=_num(values.get("p99_duration", 0) / 1e6),
            )
        )
    groups.sort(key=lambda g: g.p99_ms, reverse=True)
    data = TraceStatsData(
        group_by=group_by, groups=groups[:MAX_SERIES], has_more=len(groups) > MAX_SERIES
    )
    return data, fit(data, data.groups) or data.has_more


def trace_data(results: list[dict[str, Any]], trace_id: str) -> tuple[TraceData, bool]:
    rows = _rows(results)
    spans = sorted((span_record(r) for r in rows[:MAX_TRACE_SPANS]), key=lambda s: s.timestamp)
    if not spans:
        data = TraceData(
            trace_id=trace_id,
            span_count=0,
            root_service=None,
            duration_ms=None,
            error_spans=0,
            spans=[],
        )
        return data, False

    # Completeness is judged on every fetched span, before the size cap drops any.
    span_ids = {s.span_id for s in spans}
    root = next((s for s in spans if s.parent_span_id is None), None)
    missing_parents = {
        s.parent_span_id for s in spans if s.parent_span_id and s.parent_span_id not in span_ids
    }
    reasons = []
    if len(rows) > MAX_TRACE_SPANS:
        reasons.append("span_limit_reached")
    if root is None:
        # Also happens when the caller is uninstrumented and the root has a remote parent.
        reasons.append("root_span_missing")
    if missing_parents:
        reasons.append("parent_spans_missing")

    start_ms = spans[0].timestamp.timestamp() * 1000
    end_ms = max(s.timestamp.timestamp() * 1000 + s.duration_ms for s in spans)
    data = TraceData(
        trace_id=trace_id,
        span_count=len(spans),
        root_service=root.service if root else None,
        duration_ms=root.duration_ms if root else None,
        observed_window_ms=_num(end_ms - start_ms),
        root_found=root is not None,
        completeness=TraceCompleteness.INCOMPLETE if reasons else TraceCompleteness.CONSISTENT,
        missing_parent_spans=len(missing_parents),
        incomplete_reasons=reasons,
        error_spans=sum(s.has_error for s in spans),
        spans=spans,
    )
    cut = fit(data, data.spans)
    data.spans_omitted = data.span_count - len(data.spans)
    return data, cut or len(rows) > MAX_TRACE_SPANS


def window(time_range: TimeRange) -> str:
    fmt = "%Y-%m-%dT%H:%MZ"
    return (
        f"{time_range.start.astimezone(UTC).strftime(fmt)}–"
        f"{time_range.end.astimezone(UTC).strftime(fmt)}"
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _labels(labels: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items()) or "all"


def finish(summary: str) -> str:
    return clip(summary, MAX_SUMMARY_CHARS)[0]


def summarize_metrics(data: MetricData, time_range: TimeRange) -> str:
    head = f"{data.metric} {window(time_range)}, step {data.step_seconds}s: "
    if not data.series:
        return head + "no data points."
    lines = [
        f"[{_labels(s.labels)}] mean {s.mean}, median {s.median}, max {s.max} at "
        f"{_iso(s.max_at)}, last {s.last} {s.unit or ''}".rstrip()
        for s in data.series[:5]
    ]
    return finish(head + f"{data.total_series} series. " + "; ".join(lines))


def summarize_log_volume(data: LogVolumeData, time_range: TimeRange) -> str:
    head = f"log volume {window(time_range)}, step {data.step_seconds}s: "
    if not data.series:
        return head + "no matching logs."
    lines = []
    for s in data.series[:5]:
        ratio = f"{_num(s.max / s.median)}x median" if s.median else "median 0"
        lines.append(
            f"[{_labels(s.labels)}] {int(s.sum)} logs, peak {int(s.max)}/step at "
            f"{_iso(s.max_at)} ({ratio}), median {s.median}/step"
        )
    return finish(
        head + f"{data.total_count} logs in {data.total_series} series. " + "; ".join(lines)
    )


def _page(returned: int, offset: int, has_more: bool, next_offset: int | None, noun: str) -> str:
    # Raw queries carry no total, so the summary must never read as the whole match set.
    span = f"{noun} {offset + 1}–{offset + returned}" if returned else f"no {noun}"
    if has_more:
        return f"returned {span} of an unknown total; more match (next_offset={next_offset})"
    return f"returned {span}; no further matches"


def summarize_logs(data: LogData, time_range: TimeRange, redacted: bool) -> str:
    head = f"logs {window(time_range)}: "
    if not data.records and data.offset == 0:
        return head + "no matching records."
    page = _page(len(data.records), data.offset, data.has_more, data.next_offset, "records")
    if not data.records:
        return finish(f"{head}{page}.")
    times = sorted(r.timestamp for r in data.records)
    samples = " | ".join(clip(r.body, 160)[0] for r in data.records[:3])
    note = " Redactions applied." if redacted else ""
    return finish(
        f"{head}{page}. Returned records span {_iso(times[0])} to {_iso(times[-1])}.{note} "
        f"Use log_volume for match counts. Samples: {samples}"
    )


def summarize_spans(data: SpanData, time_range: TimeRange) -> str:
    head = f"spans {window(time_range)}: "
    if not data.spans and data.offset == 0:
        return head + "no matching spans."
    page = _page(len(data.spans), data.offset, data.has_more, data.next_offset, "spans")
    if not data.spans:
        return finish(f"{head}{page}.")
    errors = sum(s.has_error for s in data.spans)
    slowest = sorted(data.spans, key=lambda s: s.duration_ms, reverse=True)[:3]
    tops = "; ".join(f"{s.service}/{s.name} {s.duration_ms}ms trace {s.trace_id}" for s in slowest)
    # A ratio from one page is not an error rate, so only the raw count is given.
    return finish(
        f"{head}{page}. {errors} of the {len(data.spans)} returned spans have errors "
        f"(sample count, not an error rate; use trace_stats for rates). "
        f"Slowest returned: {tops}"
    )


# Groups named first in a trace-stats summary. Models see summaries clipped (evidence
# catalogs, investigation memory), so what matters must come before what does not.
TRACE_ERROR_GROUPS = 3
TRACE_SLOW_GROUPS = 3
TRACE_LISTED_GROUPS = 10


def _ms(value: float) -> str:
    return f"{value:.1f}ms" if value < 100 else f"{value:.0f}ms"


def _group_line(g: TraceGroupStats, *, errors: bool) -> str:
    spans = f"{g.count} span{'' if g.count == 1 else 's'}"
    if errors:
        rate = f"{_num(g.error_rate * 100)}%"
        return f"[{g.group}] {spans}, {g.error_count} errors ({rate}), p95 {_ms(g.p95_ms)}"
    return f"[{g.group}] {spans}, p95 {_ms(g.p95_ms)}, p99 {_ms(g.p99_ms)}"


def summarize_trace_stats(data: TraceStatsData, time_range: TimeRange) -> str:
    """Totals, then the operations with errors (most errors first), then the slowest by
    p95, then the rest by span count. Query order says nothing about importance: in the
    first live run the only failing operation came last and was cut from model context."""
    head = f"trace stats by {data.group_by} {window(time_range)}: "
    if not data.groups:
        return head + "no spans."
    spans = sum(g.count for g in data.groups)
    errors = sum(g.error_count for g in data.groups)
    rate = f"{_num(errors / spans * 100)}%" if spans else "0%"
    parts = [f"{len(data.groups)} groups, {spans} spans, {errors} errors ({rate})."]
    failing = sorted(
        (g for g in data.groups if g.error_count), key=lambda g: (-g.error_count, -g.error_rate)
    )[:TRACE_ERROR_GROUPS]
    if failing:
        parts.append("Errors: " + "; ".join(_group_line(g, errors=True) for g in failing) + ".")
    else:
        parts.append("No span errors.")
    listed = {g.group for g in failing}
    slowest = [g for g in sorted(data.groups, key=lambda g: -g.p95_ms) if g.group not in listed][
        :TRACE_SLOW_GROUPS
    ]
    if slowest:
        parts.append(
            "Slowest p95: " + "; ".join(_group_line(g, errors=False) for g in slowest) + "."
        )
    listed |= {g.group for g in slowest}
    rest = sorted((g for g in data.groups if g.group not in listed), key=lambda g: -g.count)
    room = max(TRACE_LISTED_GROUPS - len(listed), 0)
    if rest[:room]:
        parts.append(
            "Others: " + "; ".join(_group_line(g, errors=False) for g in rest[:room]) + "."
        )
    if len(rest) > room:
        parts.append(f"+{len(rest) - room} more groups not listed.")
    if data.has_more:
        parts.append("More groups exist beyond this result.")
    return finish(head + " ".join(parts))


def summarize_trace(data: TraceData) -> str:
    if data.completeness == TraceCompleteness.NOT_FOUND:
        return f"trace {data.trace_id}: not found in time range."
    head = f"trace {data.trace_id}: {data.span_count} spans fetched"
    if data.spans_omitted:
        head += (
            f" ({data.spans_omitted} omitted from this result for size; completeness judged "
            "on all fetched spans)"
        )
    errors = f"{data.error_spans} error spans among fetched spans"
    if data.completeness == TraceCompleteness.CONSISTENT:
        return finish(
            f"{head}, root service {data.root_service}, root span duration "
            f"{data.duration_ms}ms, {errors}. No missing root or parent spans detected; "
            "missing leaf spans or spans outside the query window cannot be ruled out."
        )
    duration = (
        f"root span duration {data.duration_ms}ms (root service {data.root_service})"
        if data.root_found
        else f"no root span; trace duration unknown, at least {data.observed_window_ms}ms observed"
    )
    return finish(
        f"{head}. INCOMPLETE ({', '.join(data.incomplete_reasons)}): {duration}, "
        f"{data.missing_parent_spans} missing parent spans, {errors}."
    )
