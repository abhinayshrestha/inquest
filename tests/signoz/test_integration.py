"""Runs against the local SigNoz. Enable with SIGNOZ_INTEGRATION=1 and a Viewer key in .env.

Local telemetry is intermittent (the Ubuntu VM is not always running), so these tests check
that SigNoz accepts the queries and the responses normalize, not that data exists. Set
SIGNOZ_IT_START/SIGNOZ_IT_END (ISO 8601) to point at a window known to have data, and
SIGNOZ_IT_HOST/SIGNOZ_IT_SERVICE to the host.name and service.name the data carries.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

from src.integrations.signoz import (
    check_health,
    get_trace,
    log_volume,
    query_metrics,
    search_logs,
    search_spans,
    trace_stats,
)
from src.integrations.signoz.builders import PRESETS
from src.integrations.signoz.schemas import (
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    Scope,
    SpanSearchRequest,
    TimeRange,
    TraceRequest,
    TraceStatsRequest,
)
from src.integrations.signoz.settings import load_signoz_settings

pytestmark = [
    pytest.mark.signoz,
    pytest.mark.skipif(
        os.environ.get("SIGNOZ_INTEGRATION") != "1", reason="set SIGNOZ_INTEGRATION=1"
    ),
]


HOST = Scope(identity="host", values=(os.environ.get("SIGNOZ_IT_HOST", "ubuntu"),))
SERVICE = Scope(identity="service", values=(os.environ.get("SIGNOZ_IT_SERVICE", "inquest-demo"),))


@pytest.fixture(scope="module")
def settings():
    return load_signoz_settings()


@pytest.fixture(scope="module")
def window() -> TimeRange:
    end = (
        datetime.fromisoformat(os.environ["SIGNOZ_IT_END"])
        if "SIGNOZ_IT_END" in os.environ
        else datetime.now(UTC)
    )
    start = (
        datetime.fromisoformat(os.environ["SIGNOZ_IT_START"])
        if "SIGNOZ_IT_START" in os.environ
        else end - timedelta(hours=1)
    )
    return TimeRange(start=start, end=end)


async def test_health(settings):
    assert await check_health(settings=settings)


@pytest.mark.parametrize("metric", list(PRESETS))
async def test_metric_presets_are_accepted(settings, window, metric):
    scope = HOST if PRESETS[metric].identity == "host" else SERVICE
    result = await query_metrics(
        MetricQueryRequest(time_range=window, metric=metric), scope=scope, settings=settings
    )
    assert result.data.metric == metric.value


async def test_logs(settings, window):
    result = await search_logs(
        LogSearchRequest(time_range=window, log_file="syslog", limit=5),
        scope=HOST,
        settings=settings,
    )
    assert len(result.data.records) <= 5
    volume = await log_volume(
        LogVolumeRequest(time_range=window, group_by="log_file"), scope=HOST, settings=settings
    )
    assert volume.data.total_count >= len(result.data.records)


async def test_traces(settings, window):
    spans = await search_spans(
        SpanSearchRequest(time_range=window, limit=5), scope=SERVICE, settings=settings
    )
    await trace_stats(TraceStatsRequest(time_range=window), scope=SERVICE, settings=settings)
    if spans.data.spans:
        trace = await get_trace(
            TraceRequest(trace_id=spans.data.spans[0].trace_id, time_range=window),
            scope=SERVICE,
            settings=settings,
        )
        assert trace.data.span_count >= 1
