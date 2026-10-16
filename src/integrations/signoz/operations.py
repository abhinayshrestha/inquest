import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from src.integrations.signoz import builders, normalize
from src.integrations.signoz.client import HEALTH_PATH, QUERY_RANGE_PATH, request_json
from src.integrations.signoz.errors import SigNozError, SigNozErrorCode
from src.integrations.signoz.schemas import (
    LogData,
    LogSearchRequest,
    LogVolumeData,
    LogVolumeRequest,
    MetricData,
    MetricQueryRequest,
    QueryStats,
    Scope,
    SigNozResult,
    SpanData,
    SpanSearchRequest,
    TimeRange,
    TraceData,
    TraceRequest,
    TraceStatsData,
    TraceStatsRequest,
)
from src.integrations.signoz.settings import SigNozSettings, load_signoz_settings


async def _query(
    body: dict[str, Any], settings: SigNozSettings | None, client: httpx.AsyncClient | None
) -> tuple[list[dict[str, Any]], QueryStats, list[str]]:
    settings = settings or load_signoz_settings()
    payload = await request_json(settings, "POST", QUERY_RANGE_PATH, body, client)
    return normalize.unwrap(payload)


def _reference(signal: str, body: dict[str, Any], time_range: TimeRange) -> str:
    # Hash of the exact query so the evidence can be re-run against SigNoz later.
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    start, end = builders.to_epoch_ms(time_range.start), builders.to_epoch_ms(time_range.end)
    return f"signoz:{signal}:{digest}@{start}-{end}"


def _trace_reference(req: TraceRequest) -> str:
    # The window is part of provenance: a trace fetched over a narrower range can differ.
    start, end = (
        builders.to_epoch_ms(req.time_range.start),
        builders.to_epoch_ms(req.time_range.end),
    )
    return f"signoz:trace:{req.trace_id}@{start}-{end}"


def _result[T](
    *,
    signal: str,
    operation: str,
    request: Any,
    data: T,
    summary: str,
    stats: QueryStats,
    truncated: bool,
    source_reference: str,
    observed_at: datetime | None,
    warnings: list[str],
    redaction_applied: bool = False,
) -> SigNozResult[T]:
    return SigNozResult[type(data)](  # type: ignore[misc]
        signal=signal,
        operation=operation,
        request=request.model_dump(mode="json"),
        data=data,
        summary=summary,
        stats=stats,
        truncated=truncated,
        redaction_applied=redaction_applied,
        source_reference=source_reference,
        observed_at=observed_at,
        warnings=warnings,
    )


def _build[R](builder: Callable[..., R], req: Any, scope: Scope) -> R:
    try:
        return builder(req, scope)
    except ValueError as exc:
        raise SigNozError(SigNozErrorCode.INVALID_QUERY, str(exc)) from None


async def query_metrics(
    req: MetricQueryRequest,
    *,
    scope: Scope,
    settings: SigNozSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SigNozResult[MetricData]:
    body, label, unit, step = _build(builders.metric_query, req, scope)
    has_formula = req.metric is not None and builders.PRESETS[req.metric].formula is not None
    results, stats, warnings = await _query(body, settings, client)
    data, truncated = normalize.metric_data(results, label, unit, step, "F" if has_formula else "A")
    return _result(
        signal="metrics",
        operation="signoz.query_metrics",
        request=req,
        data=data,
        summary=normalize.summarize_metrics(data, req.time_range),
        stats=stats,
        truncated=truncated,
        source_reference=_reference("metrics", body, req.time_range),
        # Aggregated buckets across series have no single observation time; each series'
        # max_at is in data and the window stays in request.time_range.
        observed_at=None,
        warnings=warnings,
    )


async def search_logs(
    req: LogSearchRequest,
    *,
    scope: Scope,
    settings: SigNozSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SigNozResult[LogData]:
    body = _build(builders.log_search_query, req, scope)
    results, stats, warnings = await _query(body, settings, client)
    data, truncated, redacted = normalize.log_data(results, req.limit, req.offset)
    return _result(
        signal="logs",
        operation="signoz.search_logs",
        request=req,
        data=data,
        summary=normalize.summarize_logs(data, req.time_range, redacted),
        stats=stats,
        truncated=truncated,
        redaction_applied=redacted,
        source_reference=_reference("logs", body, req.time_range),
        observed_at=max((r.timestamp for r in data.records), default=None),
        warnings=warnings,
    )


async def log_volume(
    req: LogVolumeRequest,
    *,
    scope: Scope,
    settings: SigNozSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SigNozResult[LogVolumeData]:
    body, step = _build(builders.log_volume_query, req, scope)
    results, stats, warnings = await _query(body, settings, client)
    data, truncated = normalize.log_volume_data(results, step)
    return _result(
        signal="logs",
        operation="signoz.log_volume",
        request=req,
        data=data,
        summary=normalize.summarize_log_volume(data, req.time_range),
        stats=stats,
        truncated=truncated,
        source_reference=_reference("logs", body, req.time_range),
        observed_at=None,
        warnings=warnings,
    )


async def search_spans(
    req: SpanSearchRequest,
    *,
    scope: Scope,
    settings: SigNozSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SigNozResult[SpanData]:
    body = _build(builders.span_search_query, req, scope)
    results, stats, warnings = await _query(body, settings, client)
    data, truncated = normalize.span_data(results, req.limit, req.offset)
    return _result(
        signal="traces",
        operation="signoz.search_spans",
        request=req,
        data=data,
        summary=normalize.summarize_spans(data, req.time_range),
        stats=stats,
        truncated=truncated,
        source_reference=_reference("traces", body, req.time_range),
        observed_at=max((s.timestamp for s in data.spans), default=None),
        warnings=warnings,
    )


async def trace_stats(
    req: TraceStatsRequest,
    *,
    scope: Scope,
    settings: SigNozSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SigNozResult[TraceStatsData]:
    body = _build(builders.trace_stats_query, req, scope)
    results, stats, warnings = await _query(body, settings, client)
    data, truncated = normalize.trace_stats_data(results, req.group_by)
    return _result(
        signal="traces",
        operation="signoz.trace_stats",
        request=req,
        data=data,
        summary=normalize.summarize_trace_stats(data, req.time_range),
        stats=stats,
        truncated=truncated,
        source_reference=_reference("traces", body, req.time_range),
        observed_at=None,
        warnings=warnings,
    )


async def get_trace(
    req: TraceRequest,
    *,
    scope: Scope,
    settings: SigNozSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SigNozResult[TraceData]:
    body = _build(builders.trace_query, req, scope)
    results, stats, warnings = await _query(body, settings, client)
    data, truncated = normalize.trace_data(results, req.trace_id)
    return _result(
        signal="traces",
        operation="signoz.get_trace",
        request=req,
        data=data,
        summary=normalize.summarize_trace(data),
        stats=stats,
        truncated=truncated,
        source_reference=_trace_reference(req),
        # Start of the earliest returned span. It is the trace start only when the root was
        # fetched; for incomplete traces earlier spans may exist outside what was returned.
        observed_at=data.spans[0].timestamp if data.spans else None,
        warnings=warnings,
    )


async def check_health(
    *, settings: SigNozSettings | None = None, client: httpx.AsyncClient | None = None
) -> bool:
    """True when SigNoz and its ClickHouse store answer. The endpoint is open access, so
    this does not prove SIGNOZ_API_KEY is valid; a query does."""
    settings = settings or load_signoz_settings()
    payload = await request_json(settings, "GET", HEALTH_PATH, client=client)
    return payload.get("status") == "ok"
