from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from src.integrations.signoz.schemas import (
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    Scope,
    SpanSearchRequest,
    TraceRequest,
    TraceStatsRequest,
)
from src.tools.policy import Rejection
from src.tools.scope import EnvironmentScope, parse_scope, resolve_scope

START = datetime.now(UTC) - timedelta(hours=1)
TR = {"start": START, "end": START + timedelta(minutes=30)}
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
OWN = EnvironmentScope(hosts=["vm-1", "vm-2"], services=["api", "web"])


def resolve(req: BaseModel, own: EnvironmentScope = OWN, others: list[Any] | None = None) -> Scope:
    return resolve_scope(req, own, others or [])


def rejected(req: BaseModel, code: str, **kwargs: Any) -> Rejection:
    with pytest.raises(Rejection) as exc:
        resolve(req, **kwargs)
    assert exc.value.code == code
    return exc.value


@pytest.mark.parametrize(
    ("req", "identity", "values"),
    [
        # Host metrics: authorized hosts only.
        (MetricQueryRequest(time_range=TR, metric="cpu_utilization"), "host", ("vm-1", "vm-2")),
        (
            MetricQueryRequest(time_range=TR, metric="memory_utilization", host="vm-2"),
            "host",
            ("vm-2",),
        ),
        # Application metrics: authorized services only.
        (
            MetricQueryRequest(time_range=TR, metric="http_server_p99_seconds"),
            "service",
            ("api", "web"),
        ),
        (
            MetricQueryRequest(
                time_range=TR,
                custom={
                    "metric_name": "app.queue.depth",
                    "space_aggregation": "max",
                    "identity": "service",
                },
                service="api",
            ),
            "service",
            ("api",),
        ),
        # System logs by host, application logs by service.
        (LogSearchRequest(time_range=TR), "host", ("vm-1", "vm-2")),
        (LogVolumeRequest(time_range=TR, identity="service", service="web"), "service", ("web",)),
        # Traces: services only.
        (SpanSearchRequest(time_range=TR), "service", ("api", "web")),
        (TraceStatsRequest(time_range=TR, service="api"), "service", ("api",)),
        (TraceRequest(trace_id=TRACE_ID, time_range=TR), "service", ("api", "web")),
    ],
)
def test_signal_specific_identity(req, identity, values):
    scope = resolve(req)
    assert (scope.identity, scope.values) == (identity, values)


def test_scope_is_never_hosts_or_services():
    # A host-scoped query never mentions services and vice versa.
    assert resolve(LogSearchRequest(time_range=TR)).identity == "host"
    assert resolve(TraceStatsRequest(time_range=TR)).identity == "service"


@pytest.mark.parametrize(
    "req",
    [
        MetricQueryRequest(time_range=TR, metric="cpu_utilization", host="vm-9"),
        LogSearchRequest(time_range=TR, host="other-host"),
        TraceStatsRequest(time_range=TR, service="other-svc"),
        SpanSearchRequest(time_range=TR, service="API"),
    ],
)
def test_narrowing_outside_scope_is_rejected(req):
    rejected(req, "policy.out_of_scope")


@pytest.mark.parametrize(
    "req",
    [
        MetricQueryRequest(time_range=TR, metric="cpu_utilization", service="api"),
        MetricQueryRequest(time_range=TR, metric="http_server_p99_seconds", host="vm-1"),
        LogSearchRequest(time_range=TR, service="api"),
        LogVolumeRequest(time_range=TR, identity="service", host="vm-1"),
    ],
)
def test_narrowing_by_the_other_identity_is_rejected(req):
    rejected(req, "policy.identity_mismatch")


def test_missing_identity_for_the_signal_is_rejected():
    hosts_only = EnvironmentScope(hosts=["vm-1"])
    rejected(TraceStatsRequest(time_range=TR), "policy.scope_missing_identity", own=hosts_only)
    rejected(
        MetricQueryRequest(time_range=TR, metric="http_server_p99_seconds"),
        "policy.scope_missing_identity",
        own=hosts_only,
    )
    services_only = EnvironmentScope(services=["api"])
    rejected(LogSearchRequest(time_range=TR), "policy.scope_missing_identity", own=services_only)


def test_identity_claimed_by_another_environment_is_ambiguous():
    other = {"signoz": {"hosts": ["vm-2"], "services": []}}
    error = rejected(LogSearchRequest(time_range=TR), "policy.scope_ambiguous", others=[other])
    assert "vm-2" in error.message
    # Narrowing to an unshared host avoids the overlap.
    assert resolve(LogSearchRequest(time_range=TR, host="vm-1"), others=[other]).values == ("vm-1",)


def test_malformed_other_scope_still_claims_its_names():
    other = {"signoz": {"services": ["api"], "unexpected": True}}
    rejected(TraceStatsRequest(time_range=TR), "policy.scope_ambiguous", others=[other])
    # Non-dict or missing configs claim nothing.
    assert resolve(TraceStatsRequest(time_range=TR), others=[{}, {"signoz": "x"}, None])


def test_distinct_deployment_environments_disambiguate_services():
    own = EnvironmentScope(services=["api"], deployment_environment="staging")
    other = {"signoz": {"services": ["api"], "deployment_environment": "prod"}}
    scope = resolve(TraceStatsRequest(time_range=TR), own=own, others=[other])
    assert scope.deployment_environment == "staging"
    # Only one side declaring it is not enough.
    rejected(
        TraceStatsRequest(time_range=TR),
        "policy.scope_ambiguous",
        own=own,
        others=[{"signoz": {"services": ["api"]}}],
    )
    # Hosts are never disambiguated by deployment.environment.
    own_hosts = EnvironmentScope(hosts=["vm-1"], deployment_environment="staging")
    rejected(
        LogSearchRequest(time_range=TR),
        "policy.scope_ambiguous",
        own=own_hosts,
        others=[{"signoz": {"hosts": ["vm-1"], "deployment_environment": "prod"}}],
    )


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        ({}, "policy.scope_missing"),
        (None, "policy.scope_missing"),
        ({"host": "utm"}, "policy.scope_missing"),
        ({"signoz": {"hosts": "vm-1"}}, "policy.scope_invalid"),
        ({"signoz": {"hosts": ["vm-1", "vm-1"]}}, "policy.scope_invalid"),
        ({"signoz": {"hosts": [""]}}, "policy.scope_invalid"),
        ({"signoz": {"hosts": [f"h{i}" for i in range(21)]}}, "policy.scope_invalid"),
        ({"signoz": {"hosts": ["vm-1"], "filter": "1=1"}}, "policy.scope_invalid"),
    ],
)
def test_parse_scope_fails_closed(metadata, code):
    with pytest.raises(Rejection) as exc:
        parse_scope(metadata)
    assert exc.value.code == code
