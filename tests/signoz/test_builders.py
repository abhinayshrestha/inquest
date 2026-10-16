from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.integrations.signoz import builders
from src.integrations.signoz.schemas import (
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    SpanSearchRequest,
    TimeRange,
    TraceRequest,
    TraceStatsRequest,
)


def specs(body: dict) -> list[dict]:
    return [q["spec"] for q in body["compositeQuery"]["queries"]]


def test_cpu_utilization_is_formula_over_cpu_time(time_range):
    body, label, unit, step = builders.metric_query(
        MetricQueryRequest(time_range=time_range, metric="cpu_utilization", host="vm-1")
    )
    a, b, f = specs(body)

    assert body["requestType"] == "time_series"
    assert body["start"] == 1791986400000
    assert (label, unit, step) == ("cpu_utilization", "percent", 60)
    assert a["aggregations"] == [
        {"metricName": "system.cpu.time", "spaceAggregation": "sum", "timeAggregation": "rate"}
    ]
    assert a["filter"]["expression"] == "host.name = 'vm-1' AND state != 'idle'"
    assert b["filter"]["expression"] == "host.name = 'vm-1'"
    assert a["disabled"] and b["disabled"]
    assert a["groupBy"] == [{"name": "host.name", "fieldContext": "resource"}]
    assert f == {"name": "F", "expression": "A / B * 100"}
    assert body["compositeQuery"]["queries"][2]["type"] == "builder_formula"


def test_only_builder_query_types_are_emitted(time_range):
    for metric in builders.PRESETS:
        body, *_ = builders.metric_query(MetricQueryRequest(time_range=time_range, metric=metric))
        types = {q["type"] for q in body["compositeQuery"]["queries"]}
        assert types <= {"builder_query", "builder_formula"}


def test_histogram_preset_omits_time_aggregation(time_range):
    body, *_ = builders.metric_query(
        MetricQueryRequest(time_range=time_range, metric="http_server_p99_seconds")
    )
    (spec,) = specs(body)
    assert spec["aggregations"] == [
        {"metricName": "http.server.request.duration.bucket", "spaceAggregation": "p99"}
    ]
    assert spec["disabled"] is False


def test_custom_metric_and_user_filter_are_anded(time_range):
    body, label, _, _ = builders.metric_query(
        MetricQueryRequest(
            time_range=time_range,
            custom={"metric_name": "system.paging.usage", "space_aggregation": "max"},
            service="api",
            group_by=["state"],
            filter="state = 'used' OR state = 'free'",
        )
    )
    (spec,) = specs(body)
    assert label == "system.paging.usage"
    assert spec["filter"]["expression"] == (
        "service.name = 'api' AND (state = 'used' OR state = 'free')"
    )
    assert spec["groupBy"] == [{"name": "state"}]


def test_formula_preset_rejects_grouping_by_state(time_range):
    with pytest.raises(ValueError, match="state"):
        builders.metric_query(
            MetricQueryRequest(time_range=time_range, metric="cpu_utilization", group_by=["state"])
        )


def test_quote_escapes_filter_literals():
    assert builders.quote("it's") == "'it\\'s'"
    assert builders.quote("a\\' OR 1=1") == "'a\\\\\\' OR 1=1'"


def test_step_grows_to_bound_point_count():
    week = TimeRange(
        start=datetime(2026, 10, 7, tzinfo=UTC), end=datetime(2026, 10, 14, tzinfo=UTC)
    )
    step = builders.choose_step(week, None)
    assert step == 2520
    assert week.end.timestamp() - week.start.timestamp() <= step * builders.MAX_RAW_POINTS


def test_log_search_uses_convenience_filters_and_fetches_one_extra(time_range):
    body = builders.log_search_query(
        LogSearchRequest(
            time_range=time_range,
            host="vm-1",
            log_file="syslog",
            body_contains="oom",
            limit=20,
            offset=40,
        )
    )
    (spec,) = specs(body)
    assert body["requestType"] == "raw"
    assert spec["filter"]["expression"] == (
        "host.name = 'vm-1' AND log.file.name = 'syslog' AND body CONTAINS 'oom'"
    )
    assert (spec["limit"], spec["offset"]) == (21, 40)
    assert "selectFields" not in spec
    assert spec["order"][0] == {
        "key": {"name": "timestamp", "fieldContext": "log"},
        "direction": "desc",
    }


def test_log_volume_counts_per_step_with_gap_filling(time_range):
    body, step = builders.log_volume_query(
        LogVolumeRequest(
            time_range=time_range, body_contains="error", severity=["ERROR"], group_by="log_file"
        )
    )
    (spec,) = specs(body)
    assert step == 60
    assert body["formatOptions"] == {"fillGaps": True}
    assert spec["aggregations"] == [{"expression": "count()", "alias": "log_count"}]
    assert spec["groupBy"] == [{"name": "log.file.name"}]
    assert spec["filter"]["expression"] == "body CONTAINS 'error' AND severity_text IN ['ERROR']"


def test_span_search_selects_intrinsic_fields(time_range):
    body = builders.span_search_query(
        SpanSearchRequest(
            time_range=time_range,
            service="inquest-demo",
            errors_only=True,
            min_duration_ms=500,
            order_by="duration",
        )
    )
    (spec,) = specs(body)
    assert spec["filter"]["expression"] == (
        "service.name = 'inquest-demo' AND has_error = true AND duration_nano > 500000000"
    )
    names = [f["name"] for f in spec["selectFields"]]
    assert {"trace_id", "span_id", "duration_nano", "has_error", "service.name"} <= set(names)
    assert spec["order"] == [
        {"key": {"name": "duration_nano", "fieldContext": "span"}, "direction": "desc"}
    ]


def test_trace_stats_is_scalar_ordered_by_p99(time_range):
    body = builders.trace_stats_query(TraceStatsRequest(time_range=time_range, group_by="service"))
    (spec,) = specs(body)
    assert body["requestType"] == "scalar"
    assert spec["groupBy"] == [{"name": "service.name", "fieldContext": "resource"}]
    assert spec["order"] == [{"key": {"name": "p99_duration"}, "direction": "desc"}]
    assert [a["alias"] for a in spec["aggregations"]][-1] == "p99_duration"


def test_get_trace_filters_by_id(time_range):
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    (spec,) = specs(builders.trace_query(TraceRequest(trace_id=trace_id, time_range=time_range)))
    assert spec["filter"]["expression"] == f"trace_id = '{trace_id}'"
    assert spec["limit"] == 201


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time_range": {"start": "2026-10-14T15:00:00Z", "end": "2026-10-14T14:00:00Z"}},
        {"time_range": {"start": "2026-10-13T00:00:00Z", "end": "2026-10-14T01:00:00Z"}},
        {"limit": 101},
        {"offset": 1001},
        {"filter": "x" * 501},
        {"unknown": 1},
    ],
)
def test_log_search_rejects_unbounded_requests(time_range, kwargs):
    with pytest.raises(ValidationError):
        LogSearchRequest(**{"time_range": time_range, **kwargs})


def test_time_range_rejects_future_and_naive_times():
    future = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(ValidationError):
        TimeRange(start=future, end=future + timedelta(minutes=5))
    with pytest.raises(ValidationError):
        TimeRange(start=datetime(2026, 10, 14), end=datetime(2026, 10, 14, 1))


def test_metric_request_needs_exactly_one_metric(time_range):
    with pytest.raises(ValidationError):
        MetricQueryRequest(time_range=time_range)
    with pytest.raises(ValidationError):
        MetricQueryRequest(
            time_range=time_range,
            metric="cpu_utilization",
            custom={"metric_name": "x", "space_aggregation": "sum"},
        )


def test_trace_request_validates_trace_id(time_range):
    with pytest.raises(ValidationError):
        TraceRequest(trace_id="'; DROP", time_range=time_range)
