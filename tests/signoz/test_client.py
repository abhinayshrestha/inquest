import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from src.integrations.signoz import (
    SigNozError,
    SigNozErrorCode,
    check_health,
    get_trace,
    log_volume,
    query_metrics,
    search_logs,
    search_spans,
    trace_stats,
)
from src.integrations.signoz.client import request_json
from src.integrations.signoz.schemas import (
    LogSearchRequest,
    LogVolumeRequest,
    MetricQueryRequest,
    SpanSearchRequest,
    TraceRequest,
    TraceStatsRequest,
)
from src.integrations.signoz.settings import load_signoz_settings
from tests.signoz.conftest import HOST_SCOPE, SERVICE_SCOPE, TEST_API_KEY, load_fixture

Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def error_body(code: str, message: str) -> dict:
    return {"status": "error", "error": {"type": code, "code": code, "message": message}}


async def raises(settings, handler: Handler) -> SigNozError:
    async with mock_client(handler) as client:
        with pytest.raises(SigNozError) as exc:
            await request_json(settings, "POST", "/api/v5/query_range", {}, client)
    # The key must never leak through any error surface the executor might persist.
    assert TEST_API_KEY not in str(exc.value)
    assert TEST_API_KEY not in exc.value.message
    assert TEST_API_KEY not in repr(exc.value)
    return exc.value


async def test_query_metrics_sends_api_key_and_normalizes(settings, time_range):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=load_fixture("metrics_formula.json"))

    async with mock_client(handler) as client:
        result = await query_metrics(
            MetricQueryRequest(time_range=time_range, metric="cpu_utilization"),
            scope=HOST_SCOPE,
            settings=settings,
            client=client,
        )

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "http://signoz.test/api/v5/query_range"
    assert request.headers["SIGNOZ-API-KEY"] == TEST_API_KEY
    assert json.loads(request.content)["compositeQuery"]["queries"][2]["spec"]["name"] == "F"

    assert result.operation == "signoz.query_metrics"
    assert result.data.series[0].max == 97.25
    assert result.stats.rows_scanned == 28800
    assert result.request["metric"] == "cpu_utilization"
    assert result.source_reference.startswith("signoz:metrics:")
    assert "max 97.25" in result.summary
    assert TEST_API_KEY not in result.model_dump_json()


async def test_search_logs_end_to_end_marks_redaction(settings, time_range):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("logs_raw.json"))

    async with mock_client(handler) as client:
        result = await search_logs(
            LogSearchRequest(time_range=time_range, log_file="auth.log", limit=5),
            scope=HOST_SCOPE,
            settings=settings,
            client=client,
        )
    assert result.redaction_applied
    assert len(result.data.records) == 3 and not result.data.has_more
    assert result.observed_at == result.data.records[0].timestamp


async def test_log_volume_and_trace_stats_end_to_end(settings, time_range):
    fixtures = iter(["log_volume.json", "trace_stats_scalar.json"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture(next(fixtures)))

    async with mock_client(handler) as client:
        volume = await log_volume(
            LogVolumeRequest(time_range=time_range, group_by="log_file"),
            scope=HOST_SCOPE,
            settings=settings,
            client=client,
        )
        stats = await trace_stats(
            TraceStatsRequest(time_range=time_range),
            scope=SERVICE_SCOPE,
            settings=settings,
            client=client,
        )
    assert volume.data.total_count == 525
    assert stats.data.groups[0].group == "GET /api/orders"


async def test_invalid_query_surfaces_truncated_server_message(settings):
    message = "key `sevrity` not found; did you mean `severity_text`? " + "x" * 1_000

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=error_body("invalid_input", message))

    error = await raises(settings, handler)
    assert error.code == SigNozErrorCode.INVALID_QUERY
    assert "did you mean `severity_text`" in error.message
    assert len(error.message) <= 520
    assert not error.retryable


async def test_auth_failure_never_echoes_key(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        echoed = request.headers["SIGNOZ-API-KEY"]
        return httpx.Response(401, json=error_body("unauthenticated", f"bad key {echoed}"))

    error = await raises(settings, handler)
    assert error.code == SigNozErrorCode.AUTH
    assert error.http_status == 401
    assert "[redacted]" in error.message


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("slow"), SigNozErrorCode.TIMEOUT),
        (httpx.ConnectError("refused http://signoz.test"), SigNozErrorCode.UNAVAILABLE),
    ],
)
async def test_transport_errors_are_retryable(settings, exc, code):
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    error = await raises(settings, handler)
    assert error.code == code
    assert error.retryable
    assert "signoz.test" not in error.message


async def test_server_errors_are_retryable(settings):
    error = await raises(settings, lambda _: httpx.Response(503, text="upstream down"))
    assert error.code == SigNozErrorCode.UNAVAILABLE and error.retryable


async def test_html_app_page_is_malformed(settings):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<!doctype html><html></html>")

    error = await raises(settings, handler)
    assert error.code == SigNozErrorCode.MALFORMED_RESPONSE


async def test_redirects_are_not_followed(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(302, headers={"location": "http://elsewhere.test/steal"})

    error = await raises(settings, handler)
    assert error.code == SigNozErrorCode.MALFORMED_RESPONSE
    assert len(calls) == 1


async def test_oversized_response_is_aborted(settings):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "pad": "x" * 60_000})

    error = await raises(settings, handler)
    assert error.code == SigNozErrorCode.RESPONSE_TOO_LARGE


async def test_200_error_envelope_is_invalid_query(settings):
    error = await raises(
        settings, lambda _: httpx.Response(200, json=error_body("invalid_input", "bad step"))
    )
    assert error.code == SigNozErrorCode.INVALID_QUERY


async def test_disallowed_requests_never_leave_process(settings):
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("request should not be sent")

    async with mock_client(handler) as client:
        for method, path in [("POST", "/api/v1/rules"), ("DELETE", "/api/v5/query_range")]:
            with pytest.raises(SigNozError) as exc:
                await request_json(settings, method, path, {}, client)
            assert exc.value.code == SigNozErrorCode.CONFIG


async def test_check_health_requests_live_check(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/v1/health"
        assert "live" in request.url.params
        # Exact body SigNoz v0.145.0 returns from getHealth.
        return httpx.Response(200, json={"status": "ok"})

    async with mock_client(handler) as client:
        assert await check_health(settings=settings, client=client) is True


async def test_check_health_false_for_unexpected_status(settings):
    async with mock_client(lambda _: httpx.Response(200, json={"status": "degraded"})) as client:
        assert await check_health(settings=settings, client=client) is False


async def test_check_health_clickhouse_down_is_unavailable(settings):
    # Legacy query-service error shape: `error` is a string, not an object.
    def handler(_: httpx.Request) -> httpx.Response:
        body = {"status": "error", "errorType": "unavailable", "error": "clickhouse: dial tcp"}
        return httpx.Response(503, json=body)

    async with mock_client(handler) as client:
        with pytest.raises(SigNozError) as exc:
            await check_health(settings=settings, client=client)
    assert exc.value.code == SigNozErrorCode.UNAVAILABLE and exc.value.retryable


def test_missing_settings_name_variables_only(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("SIGNOZ_BASE_URL", raising=False)
    monkeypatch.setenv("SIGNOZ_API_KEY", "secret-value-123")
    with pytest.raises(SigNozError) as exc:
        load_signoz_settings()
    assert exc.value.code == SigNozErrorCode.CONFIG
    assert "SIGNOZ_BASE_URL" in exc.value.message
    assert "secret-value-123" not in exc.value.message


# --- observed_at and provenance ---------------------------------------------------------

WINDOW = {"start": "2026-10-14T14:00:00Z", "end": "2026-10-14T15:00:00Z"}


def fixture_handler(name: str) -> Handler:
    return lambda _: httpx.Response(200, json=load_fixture(name))


@pytest.mark.parametrize(
    ("operation", "request_model", "scope", "fixture"),
    [
        (
            query_metrics,
            lambda tr: MetricQueryRequest(time_range=tr, metric="cpu_utilization"),
            HOST_SCOPE,
            "metrics_formula.json",
        ),
        (log_volume, lambda tr: LogVolumeRequest(time_range=tr), HOST_SCOPE, "log_volume.json"),
        (
            trace_stats,
            lambda tr: TraceStatsRequest(time_range=tr),
            SERVICE_SCOPE,
            "trace_stats_scalar.json",
        ),
    ],
)
async def test_aggregated_results_have_no_observed_at(
    settings, time_range, operation, request_model, scope, fixture
):
    async with mock_client(fixture_handler(fixture)) as client:
        result = await operation(
            request_model(time_range), scope=scope, settings=settings, client=client
        )
    assert result.observed_at is None
    assert result.request["time_range"] == WINDOW
    assert result.source_reference.endswith("@1791986400000-1791990000000")


async def test_record_results_use_returned_timestamps(settings, time_range):
    async with mock_client(fixture_handler("logs_raw.json")) as client:
        logs = await search_logs(
            LogSearchRequest(time_range=time_range),
            scope=HOST_SCOPE,
            settings=settings,
            client=client,
        )
    async with mock_client(fixture_handler("spans_raw.json")) as client:
        spans = await search_spans(
            SpanSearchRequest(time_range=time_range),
            scope=SERVICE_SCOPE,
            settings=settings,
            client=client,
        )
    assert logs.observed_at == datetime(2026, 10, 14, 15, 9, 12, 283261, tzinfo=UTC)
    assert spans.observed_at == datetime(2026, 10, 14, 7, 18, 15, 100000, tzinfo=UTC)


async def test_get_trace_provenance_keeps_query_window(settings, time_range):
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    async with mock_client(fixture_handler("spans_raw.json")) as client:
        result = await get_trace(
            TraceRequest(trace_id=trace_id, time_range=time_range),
            scope=SERVICE_SCOPE,
            settings=settings,
            client=client,
        )
    assert result.source_reference == f"signoz:trace:{trace_id}@1791986400000-1791990000000"
    assert result.request["time_range"] == WINDOW
    assert result.observed_at == datetime(2026, 10, 14, 7, 18, 15, 67000, tzinfo=UTC)


# --- client lifecycle -------------------------------------------------------------------


@pytest.fixture
def owned_clients(monkeypatch):
    """Captures clients request_json creates itself, routed through a mock transport."""
    created: list[httpx.AsyncClient] = []
    handlers: list[Handler] = []
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        client = real(*args, transport=httpx.MockTransport(handlers[0]), **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return created, handlers


@pytest.mark.parametrize("status", [200, 503])
async def test_internally_created_client_is_closed(settings, owned_clients, status):
    created, handlers = owned_clients
    handlers.append(lambda _: httpx.Response(status, json={"status": "ok"}))
    try:
        await request_json(settings, "GET", "/api/v1/health?live")
    except SigNozError:
        assert status == 503
    (client,) = created
    assert client.is_closed


async def test_internally_created_client_is_closed_on_transport_error(settings, owned_clients):
    created, handlers = owned_clients

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    handlers.append(handler)
    with pytest.raises(SigNozError):
        await request_json(settings, "GET", "/api/v1/health?live")
    assert created[0].is_closed


async def test_caller_client_stays_open_after_success_and_error(settings):
    responses = iter(
        [
            httpx.Response(200, json={"status": "ok"}),
            httpx.Response(401, json={}),
            httpx.Response(200, json={"status": "ok"}),
        ]
    )
    async with mock_client(lambda _: next(responses)) as client:
        assert await check_health(settings=settings, client=client)
        with pytest.raises(SigNozError):
            await check_health(settings=settings, client=client)
        assert not client.is_closed
        assert await check_health(settings=settings, client=client)
    assert client.is_closed  # closed by the caller's own context manager


async def test_get_trace_observed_at_is_earliest_returned_span_not_trace_start(
    settings, time_range
):
    # Root span missing: the earliest returned span is not the trace's real start.
    payload = load_fixture("spans_raw.json")
    rows = payload["data"]["data"]["results"][0]["rows"]
    payload["data"]["data"]["results"][0]["rows"] = rows[1:]
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

    async with mock_client(lambda _: httpx.Response(200, json=payload)) as client:
        result = await get_trace(
            TraceRequest(trace_id=trace_id, time_range=time_range),
            scope=SERVICE_SCOPE,
            settings=settings,
            client=client,
        )
    assert result.data.completeness == "incomplete"
    assert not result.data.root_found
    assert result.observed_at == result.data.spans[0].timestamp
    assert result.observed_at == datetime(2026, 10, 14, 7, 18, 15, 100000, tzinfo=UTC)
    assert "trace duration unknown" in result.summary
