from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.integrations.signoz import builders
from src.integrations.signoz.schemas import (
    Condition,
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    Scope,
    SpanSearchRequest,
    TimeRange,
    TraceRequest,
    TraceStatsRequest,
)
from tests.signoz.conftest import HOST_SCOPE, SERVICE_SCOPE

SCOPES = {"host": HOST_SCOPE, "service": SERVICE_SCOPE}


def specs(body: dict) -> list[dict]:
    return [q["spec"] for q in body["compositeQuery"]["queries"]]


def test_cpu_utilization_is_formula_over_cpu_time(time_range):
    body, label, unit, step = builders.metric_query(
        MetricQueryRequest(time_range=time_range, metric="cpu_utilization", host="vm-1"),
        HOST_SCOPE,
    )
    a, b, f = specs(body)

    assert body["requestType"] == "time_series"
    assert body["start"] == 1791986400000
    assert (label, unit, step) == ("cpu_utilization", "percent", 60)
    assert a["aggregations"] == [
        {"metricName": "system.cpu.time", "spaceAggregation": "sum", "timeAggregation": "rate"}
    ]
    assert a["filter"]["expression"] == (
        "(host.name IN ['vm-1']) AND host.name = 'vm-1' AND state != 'idle'"
    )
    assert b["filter"]["expression"] == "(host.name IN ['vm-1']) AND host.name = 'vm-1'"
    assert a["disabled"] and b["disabled"]
    assert a["groupBy"] == [{"name": "host.name", "fieldContext": "resource"}]
    assert f == {"name": "F", "expression": "A / B * 100"}
    assert body["compositeQuery"]["queries"][2]["type"] == "builder_formula"


def test_only_builder_query_types_are_emitted(time_range):
    for metric, preset in builders.PRESETS.items():
        body, *_ = builders.metric_query(
            MetricQueryRequest(time_range=time_range, metric=metric), SCOPES[preset.identity]
        )
        types = {q["type"] for q in body["compositeQuery"]["queries"]}
        assert types <= {"builder_query", "builder_formula"}


def test_every_metric_query_is_scoped(time_range):
    for metric, preset in builders.PRESETS.items():
        body, *_ = builders.metric_query(
            MetricQueryRequest(time_range=time_range, metric=metric), SCOPES[preset.identity]
        )
        clause = builders.scope_clause(SCOPES[preset.identity])
        for spec in specs(body):
            if "filter" in spec:
                assert spec["filter"]["expression"].startswith(clause)


def test_histogram_preset_omits_time_aggregation(time_range):
    body, *_ = builders.metric_query(
        MetricQueryRequest(time_range=time_range, metric="http_server_p99_seconds"),
        SERVICE_SCOPE,
    )
    (spec,) = specs(body)
    assert spec["aggregations"] == [
        {"metricName": "http.server.request.duration.bucket", "spaceAggregation": "p99"}
    ]
    assert spec["disabled"] is False


def test_custom_metric_and_conditions_are_anded(time_range):
    body, label, _, _ = builders.metric_query(
        MetricQueryRequest(
            time_range=time_range,
            custom={
                "metric_name": "system.paging.usage",
                "space_aggregation": "max",
                "identity": "service",
            },
            service="api",
            group_by=["state"],
            where=[{"key": "state", "op": "in", "value": ["used", "free"]}],
        ),
        Scope(identity="service", values=("api",)),
    )
    (spec,) = specs(body)
    assert label == "system.paging.usage"
    assert spec["filter"]["expression"] == (
        "(service.name IN ['api']) AND service.name = 'api' AND state IN ['used', 'free']"
    )
    assert spec["groupBy"] == [{"name": "state"}]


def test_formula_preset_rejects_grouping_by_state(time_range):
    with pytest.raises(ValueError, match="state"):
        builders.metric_query(
            MetricQueryRequest(time_range=time_range, metric="cpu_utilization", group_by=["state"]),
            HOST_SCOPE,
        )


@pytest.mark.parametrize(
    ("build", "req", "scope"),
    [
        (
            builders.metric_query,
            lambda tr: MetricQueryRequest(time_range=tr, metric="cpu_utilization"),
            SERVICE_SCOPE,
        ),
        (
            builders.metric_query,
            lambda tr: MetricQueryRequest(time_range=tr, metric="http_server_p99_seconds"),
            HOST_SCOPE,
        ),
        (builders.log_search_query, lambda tr: LogSearchRequest(time_range=tr), SERVICE_SCOPE),
        (
            builders.log_volume_query,
            lambda tr: LogVolumeRequest(time_range=tr, identity="service"),
            HOST_SCOPE,
        ),
        (builders.span_search_query, lambda tr: SpanSearchRequest(time_range=tr), HOST_SCOPE),
        (builders.trace_stats_query, lambda tr: TraceStatsRequest(time_range=tr), HOST_SCOPE),
        (
            builders.trace_query,
            lambda tr: TraceRequest(trace_id="4bf92f3577b34da6a3ce929d0e0e4736", time_range=tr),
            HOST_SCOPE,
        ),
    ],
)
def test_scope_identity_must_fit_the_signal(time_range, build, req, scope):
    with pytest.raises(ValueError, match="scope"):
        build(req(time_range), scope)


def test_quote_escapes_filter_literals():
    assert builders.quote("it's") == "'it\\'s'"
    assert builders.quote("a\\' OR 1=1") == "'a\\\\\\' OR 1=1'"


def test_scope_clause_is_one_identity_never_an_or():
    scope = Scope(identity="service", values=("a", "b'c"), deployment_environment="prod")
    clause = builders.scope_clause(scope)
    assert clause == "(service.name IN ['a', 'b\\'c'] AND deployment.environment = 'prod')"
    assert " OR " not in clause


def test_scope_rejects_deployment_environment_for_hosts():
    with pytest.raises(ValidationError):
        Scope(identity="host", values=("vm-1",), deployment_environment="prod")
    with pytest.raises(ValidationError):
        Scope(identity="host", values=())


@pytest.mark.parametrize(
    ("condition", "rendered"),
    [
        ({"key": "http.route", "op": "eq", "value": "/checkout"}, "http.route = '/checkout'"),
        ({"key": "status", "op": "neq", "value": 200}, "status != 200"),
        ({"key": "duration", "op": "gte", "value": 1.5}, "duration >= 1.5"),
        ({"key": "duration", "op": "lt", "value": 1e-07}, "duration < 0.0000001"),
        ({"key": "has_error", "op": "eq", "value": True}, "has_error = true"),
        ({"key": "code", "op": "not_in", "value": [500, 503]}, "code NOT IN [500, 503]"),
        ({"key": "body", "op": "contains", "value": "oom"}, "body CONTAINS 'oom'"),
        ({"key": "user.id", "op": "exists"}, "user.id EXISTS"),
    ],
)
def test_conditions_render_with_quoted_literals(condition, rendered):
    assert builders.render_condition(Condition(**condition)) == rendered


@pytest.mark.parametrize(
    "payload",
    [
        "x') OR (1=1",
        "x' OR host.name = 'other",
        "x\\' OR 1=1 --",
        "a) OR (service.name IN ['other-svc']",
    ],
)
def test_injection_attempts_stay_inside_one_literal(time_range, payload):
    body = builders.trace_stats_query(
        TraceStatsRequest(
            time_range=time_range, where=[{"key": "http.route", "op": "eq", "value": payload}]
        ),
        SERVICE_SCOPE,
    )
    (spec,) = specs(body)
    expression = spec["filter"]["expression"]
    assert (
        expression
        == f"(service.name IN ['inquest-demo']) AND http.route = {builders.quote(payload)}"
    )
    # The literal is the last token: nothing after the closing quote but end of input.
    assert expression.endswith("'")


@pytest.mark.parametrize(
    "condition",
    [
        {"key": "host.name", "op": "eq", "value": "other"},
        {"key": "Service.Name", "op": "eq", "value": "other"},
        {"key": "deployment.environment", "op": "eq", "value": "prod"},
        {"key": "OR", "op": "exists"},
        {"key": "not", "op": "exists"},
        {"key": "a OR b", "op": "exists"},
        {"key": "a)", "op": "exists"},
        {"key": "a'b", "op": "exists"},
        {"key": "1abc", "op": "exists"},
        {"key": "a..b", "op": "exists"},
        {"key": "status", "op": "eq"},
        {"key": "status", "op": "exists", "value": "x"},
        {"key": "status", "op": "in", "value": []},
        {"key": "status", "op": "in", "value": list(range(11))},
        {"key": "status", "op": "gt", "value": "10"},
        {"key": "status", "op": "gt", "value": True},
        {"key": "status", "op": "gt", "value": float("inf")},
        {"key": "status", "op": "contains", "value": 5},
        {"key": "status", "op": "eq", "value": "x" * 201},
        {"key": "status", "op": "like", "value": "%"},
    ],
)
def test_invalid_conditions_are_rejected(condition):
    with pytest.raises(ValidationError):
        Condition(**condition)


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
        ),
        HOST_SCOPE,
    )
    (spec,) = specs(body)
    assert body["requestType"] == "raw"
    assert spec["filter"]["expression"] == (
        "(host.name IN ['vm-1']) AND host.name = 'vm-1' AND log.file.name = 'syslog' "
        "AND body CONTAINS 'oom'"
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
        ),
        HOST_SCOPE,
    )
    (spec,) = specs(body)
    assert step == 60
    assert body["formatOptions"] == {"fillGaps": True}
    assert spec["aggregations"] == [{"expression": "count()", "alias": "log_count"}]
    assert spec["groupBy"] == [{"name": "log.file.name"}]
    assert spec["filter"]["expression"] == (
        "(host.name IN ['vm-1']) AND body CONTAINS 'error' AND severity_text IN ['ERROR']"
    )


def test_service_logs_are_scoped_by_service(time_range):
    body, _ = builders.log_volume_query(
        LogVolumeRequest(time_range=time_range, identity="service"), SERVICE_SCOPE
    )
    (spec,) = specs(body)
    assert spec["filter"]["expression"] == "(service.name IN ['inquest-demo'])"


def test_span_search_selects_intrinsic_fields(time_range):
    body = builders.span_search_query(
        SpanSearchRequest(
            time_range=time_range,
            service="inquest-demo",
            errors_only=True,
            min_duration_ms=500,
            order_by="duration",
        ),
        SERVICE_SCOPE,
    )
    (spec,) = specs(body)
    assert spec["filter"]["expression"] == (
        "(service.name IN ['inquest-demo']) AND service.name = 'inquest-demo' "
        "AND has_error = true AND duration_nano > 500000000"
    )
    names = [f["name"] for f in spec["selectFields"]]
    assert {"trace_id", "span_id", "duration_nano", "has_error", "service.name"} <= set(names)
    assert spec["order"] == [
        {"key": {"name": "duration_nano", "fieldContext": "span"}, "direction": "desc"}
    ]


def test_trace_stats_is_scalar_ordered_by_p99(time_range):
    body = builders.trace_stats_query(
        TraceStatsRequest(time_range=time_range, group_by="service"), SERVICE_SCOPE
    )
    (spec,) = specs(body)
    assert body["requestType"] == "scalar"
    assert spec["groupBy"] == [{"name": "service.name", "fieldContext": "resource"}]
    assert spec["order"] == [{"key": {"name": "p99_duration"}, "direction": "desc"}]
    assert [a["alias"] for a in spec["aggregations"]][-1] == "p99_duration"
    assert spec["filter"]["expression"] == "(service.name IN ['inquest-demo'])"


def test_get_trace_filters_by_id_within_scope(time_range):
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    (spec,) = specs(
        builders.trace_query(TraceRequest(trace_id=trace_id, time_range=time_range), SERVICE_SCOPE)
    )
    assert spec["filter"]["expression"] == (
        f"(service.name IN ['inquest-demo']) AND trace_id = '{trace_id}'"
    )
    assert spec["limit"] == 201


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time_range": {"start": "2026-10-14T15:00:00Z", "end": "2026-10-14T14:00:00Z"}},
        {"time_range": {"start": "2026-10-13T00:00:00Z", "end": "2026-10-14T01:00:00Z"}},
        {"limit": 101},
        {"offset": 1001},
        # Free-form filter expressions are not accepted anywhere.
        {"filter": "severity_text = 'ERROR'"},
        {"where": [{"key": "a", "op": "exists"}] * 6},
        {"severity": ["x" * 21]},
        {"identity": "cluster"},
        {"unknown": 1},
    ],
)
def test_log_search_rejects_unbounded_requests(time_range, kwargs):
    with pytest.raises(ValidationError):
        LogSearchRequest(**{"time_range": time_range, **kwargs})


@pytest.mark.parametrize(
    "model", [MetricQueryRequest, LogSearchRequest, LogVolumeRequest, SpanSearchRequest,
              TraceStatsRequest, TraceRequest]
)  # fmt: skip
def test_request_models_expose_no_filter_or_scope(model):
    assert not {"filter", "scope"} & set(model.model_fields)


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
            custom={"metric_name": "x", "space_aggregation": "sum", "identity": "host"},
        )


def test_custom_metric_requires_identity(time_range):
    with pytest.raises(ValidationError):
        MetricQueryRequest(
            time_range=time_range, custom={"metric_name": "x", "space_aggregation": "sum"}
        )


def test_trace_request_validates_trace_id(time_range):
    with pytest.raises(ValidationError):
        TraceRequest(trace_id="'; DROP", time_range=time_range)
