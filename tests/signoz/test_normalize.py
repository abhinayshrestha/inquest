import copy
import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from src.integrations.signoz import normalize
from src.integrations.signoz.errors import SigNozError, SigNozErrorCode
from src.integrations.signoz.schemas import (
    MAX_DATA_BYTES,
    MAX_LOG_BODY_CHARS,
    MAX_POINTS_PER_SERIES,
    MAX_SERIES,
    MAX_TRACE_SPANS,
    TraceCompleteness,
)
from tests.signoz.conftest import load_fixture


def ts_series(n_series: int, n_points: int) -> dict:
    return {
        "status": "success",
        "data": {
            "type": "time_series",
            "data": {
                "results": [
                    {
                        "queryName": "A",
                        "aggregations": [
                            {
                                "index": 0,
                                "series": [
                                    {
                                        "labels": [{"key": {"name": "device"}, "value": f"d{s}"}],
                                        "values": [
                                            {
                                                "timestamp": 1791986400000 + i * 60_000,
                                                "value": s + i,
                                            }
                                            for i in range(n_points)
                                        ],
                                    }
                                    for s in range(n_series)
                                ],
                            }
                        ],
                    }
                ]
            },
            "meta": {},
        },
    }


def test_unwrap_reads_stats_and_warnings():
    payload = load_fixture("metrics_formula.json")
    payload["data"]["warning"] = {"message": "partial data", "warnings": [{"message": "w2"}]}
    results, stats, warnings = normalize.unwrap(payload)
    assert results[0]["queryName"] == "F"
    assert (stats.rows_scanned, stats.bytes_scanned, stats.signoz_duration_ms) == (
        28800,
        1843200,
        41,
    )
    assert warnings == ["partial data", "w2"]


@pytest.mark.parametrize(
    "payload", [{"status": "ok"}, {"status": "success", "data": []}, {"status": "success"}]
)
def test_unwrap_rejects_unexpected_envelopes(payload):
    with pytest.raises(SigNozError) as exc:
        normalize.unwrap(payload)
    assert exc.value.code == SigNozErrorCode.MALFORMED_RESPONSE


def test_unwrap_tolerates_null_results():
    payload = {"status": "success", "data": {"type": "raw", "data": {"results": None}}}
    assert normalize.unwrap(payload)[0] == []


def test_metric_data_computes_series_stats():
    results, _, _ = normalize.unwrap(load_fixture("metrics_formula.json"))
    data, truncated = normalize.metric_data(results, "cpu_utilization", "percent", 60, "F")
    (series,) = data.series
    assert not truncated
    assert series.labels == {"host.name": "abbyvm-QEMU-Virtual-Machine"}
    assert (series.min, series.max, series.last, series.mean) == (3.5, 97.25, 96.0, 50.3125)
    assert series.max_at == datetime(2026, 10, 14, 14, 2, tzinfo=UTC)
    assert series.unit == "percent"


def test_metric_data_caps_series_and_downsamples_keeping_peaks():
    payload = ts_series(MAX_SERIES + 5, 240)
    payload["data"]["data"]["results"][0]["aggregations"][0]["series"][0]["values"][7]["value"] = (
        10_000
    )
    results, _, _ = normalize.unwrap(payload)
    data, truncated = normalize.metric_data(results, "m", None, 60, "A")
    assert truncated
    assert data.total_series == MAX_SERIES + 5
    assert len(data.series) == MAX_SERIES
    assert data.series[0].labels == {"device": "d0"}  # the spike ranks first
    assert len(data.series[0].points) <= MAX_POINTS_PER_SERIES
    assert max(v for _, v in data.series[0].points) == 10_000
    assert len(data.model_dump_json()) <= MAX_DATA_BYTES


def test_metric_data_skips_non_numeric_points_and_missing_query():
    payload = ts_series(1, 3)
    payload["data"]["data"]["results"][0]["aggregations"][0]["series"][0]["values"][1]["value"] = (
        None
    )
    results, _, _ = normalize.unwrap(payload)
    data, _ = normalize.metric_data(results, "m", None, 60, "A")
    assert len(data.series[0].points) == 2
    assert normalize.metric_data(results, "m", None, 60, "F")[0].series == []


def test_log_volume_reports_spike_against_median(time_range):
    results, _, _ = normalize.unwrap(load_fixture("log_volume.json"))
    data, truncated = normalize.log_volume_data(results, 60)
    assert not truncated
    assert data.total_count == 525
    syslog = data.series[0]
    assert syslog.labels == {"log.file.name": "syslog"}
    assert (syslog.max, syslog.median, syslog.sum) == (480, 11, 522)
    summary = normalize.summarize_log_volume(data, time_range)
    assert "peak 480/step at 2026-10-14T14:02:00Z (43.6364x median)" in summary


def test_log_data_extracts_fields_and_redacts(time_range):
    results, _, _ = normalize.unwrap(load_fixture("logs_raw.json"))
    data, truncated, redacted = normalize.log_data(results, limit=2, offset=0)

    assert redacted
    assert not truncated
    assert data.has_more and data.next_offset == 2
    first, second = data.records
    assert first.timestamp == datetime(2026, 10, 14, 15, 9, 12, 283261, tzinfo=UTC)
    assert (first.host, first.log_file, first.severity, first.service) == (
        "abbyvm-QEMU-Virtual-Machine",
        "syslog",
        None,
        None,
    )
    assert first.attributes == {"log.file.path": "/var/log/syslog", "os.type": "linux"}
    assert "hunter2" not in second.body
    assert "password=[REDACTED]" in second.body
    assert "hunter2" not in normalize.summarize_logs(data, time_range, redacted)


def test_log_data_clips_bodies_and_fits_evidence_limit():
    payload = load_fixture("logs_raw.json")
    row = payload["data"]["data"]["results"][0]["rows"][0]
    row["data"]["body"] = "x" * 5_000
    row["data"]["attributes_string"] = {f"k{i}": "v" * 1_000 for i in range(30)}
    payload["data"]["data"]["results"][0]["rows"] = [copy.deepcopy(row) for _ in range(101)]
    results, _, _ = normalize.unwrap(payload)

    data, truncated, _ = normalize.log_data(results, limit=100, offset=0)
    assert truncated and data.has_more
    assert len(data.records[0].body) == MAX_LOG_BODY_CHARS
    assert len(data.records[0].attributes) == 10
    assert len(data.model_dump_json()) <= MAX_DATA_BYTES
    assert data.next_offset == len(data.records)


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "aws key AKIAIOSFODNN7EXAMPLE used",
        "token=abc123def",
        '{"api_key": "sk-live-123456"}',
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redact_removes_credentials(text):
    redacted, changed = normalize.redact(text)
    assert changed
    for secret in ("eyJhbGciOiJIUzI1NiJ9", "AKIAIOSFODNN7EXAMPLE", "abc123def", "sk-live", "MIIE"):
        assert secret not in redacted


def test_redact_leaves_ordinary_auth_lines():
    line = "sshd[991]: Accepted password for abby from 10.0.0.5 port 22"
    assert normalize.redact(line) == (line, False)


def test_span_data_maps_intrinsic_fields(time_range):
    results, _, _ = normalize.unwrap(load_fixture("spans_raw.json"))
    data, truncated = normalize.span_data(results, limit=50, offset=0)
    assert not truncated and not data.has_more and data.next_offset is None
    root, child = data.spans
    assert root.duration_ms == 2140.5
    assert (root.has_error, root.http_status, root.parent_span_id, root.kind) == (
        True,
        500,
        None,
        "Server",
    )
    assert (child.http_status, child.parent_span_id) == (None, "00f067aa0ba902b7")
    assert "1 of the 2 returned spans have errors" in normalize.summarize_spans(data, time_range)


def test_trace_data_builds_trace_summary():
    results, _, _ = normalize.unwrap(load_fixture("spans_raw.json"))
    data, truncated = normalize.trace_data(results, "4bf92f3577b34da6a3ce929d0e0e4736")
    assert not truncated
    assert (data.span_count, data.root_service, data.error_spans) == (2, "inquest-demo", 1)
    assert data.duration_ms == 2140.5


def test_trace_stats_maps_columns_by_aggregation_index(time_range):
    results, _, _ = normalize.unwrap(load_fixture("trace_stats_scalar.json"))
    data, truncated = normalize.trace_stats_data(results, "operation")
    assert not truncated and not data.has_more
    orders, health = data.groups
    assert orders.group == "GET /api/orders"
    assert (orders.count, orders.error_count, orders.error_rate) == (412, 37, 0.0898)
    assert (orders.p50_ms, orders.p95_ms, orders.p99_ms) == (42.1, 830.0, 2140.5)
    assert health.p99_ms == 3.0
    assert "[GET /api/orders] 412 spans, 37 errors (8.98%)" in normalize.summarize_trace_stats(
        data, time_range
    )


def test_unparseable_timestamp_is_malformed():
    payload = load_fixture("spans_raw.json")
    payload["data"]["data"]["results"][0]["rows"][0]["timestamp"] = "yesterday"
    results, _, _ = normalize.unwrap(payload)
    with pytest.raises(SigNozError) as exc:
        normalize.span_data(results, limit=50, offset=0)
    assert exc.value.code == SigNozErrorCode.MALFORMED_RESPONSE


# --- downsampling -----------------------------------------------------------------------

BASE = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)


def points(values: list[float], gaps: list[int] | None = None) -> list[tuple[datetime, float]]:
    offsets = [0]
    for gap in gaps or [60] * (len(values) - 1):
        offsets.append(offsets[-1] + gap)
    return [(BASE + timedelta(seconds=o), v) for o, v in zip(offsets, values, strict=True)]


def assert_well_formed(original, sampled):
    assert len(sampled) <= MAX_POINTS_PER_SERIES
    assert sampled[0] == original[0] and sampled[-1] == original[-1]
    times = [t for t, _ in sampled]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert set(sampled) <= set(original)


def test_downsample_keeps_spike():
    values = [50.0] * 500
    values[250] = 999.0
    original = points(values)
    sampled = normalize.downsample(original)
    assert_well_formed(original, sampled)
    assert original[250] in sampled


def test_downsample_keeps_drop():
    values = [80.0] * 500
    values[300] = 0.0
    original = points(values)
    sampled = normalize.downsample(original)
    assert_well_formed(original, sampled)
    assert original[300] in sampled


def test_downsample_keeps_spike_and_drop_in_same_bucket():
    values = [50.0] * 500
    values[101], values[103] = 100.0, 0.0
    original = points(values)
    sampled = normalize.downsample(original)
    assert_well_formed(original, sampled)
    assert sampled.index(original[101]) < sampled.index(original[103])


def test_downsample_constant_series():
    original = points([7.0] * 500)
    sampled = normalize.downsample(original)
    assert_well_formed(original, sampled)
    assert {v for _, v in sampled} == {7.0}


def test_downsample_irregular_timestamps_keep_global_extremes():
    values = [float((i * 37) % 101) for i in range(400)]
    values[17], values[389] = -5.0, 500.0
    gaps = [1 + (i * 13) % 300 for i in range(399)]
    original = points(values, gaps)
    sampled = normalize.downsample(original)
    assert_well_formed(original, sampled)
    assert min(v for _, v in sampled) == -5.0
    assert max(v for _, v in sampled) == 500.0


def test_downsample_leaves_short_series_untouched():
    original = points([1.0, 3.0, 2.0])
    assert normalize.downsample(original) == original


def test_series_stats_use_full_data_not_sample():
    values = [80.0 + (i % 7) for i in range(500)]
    values[300] = 0.0
    full = points(values)
    stats = normalize.series_stats({}, "percent", full)
    assert stats.min == 0.0
    assert stats.mean == round(statistics.fmean(values), 4)
    assert stats.median == statistics.median(values)
    assert stats.sum == round(math.fsum(values), 4)
    assert len(stats.points) <= MAX_POINTS_PER_SERIES


# --- sampled log and span pages ---------------------------------------------------------


def log_rows(n: int) -> dict:
    payload = load_fixture("logs_raw.json")
    row = payload["data"]["data"]["results"][0]["rows"][2]
    payload["data"]["data"]["results"][0]["rows"] = [copy.deepcopy(row) for _ in range(n)]
    return payload


def test_log_page_with_more_matches_states_unknown_total(time_range):
    results, _, _ = normalize.unwrap(log_rows(21))
    data, _, _ = normalize.log_data(results, limit=20, offset=40)
    assert (data.offset, data.has_more, data.next_offset, len(data.records)) == (40, True, 60, 20)
    summary = normalize.summarize_logs(data, time_range, redacted=False)
    assert "returned records 41–60 of an unknown total" in summary
    assert "next_offset=60" in summary
    assert "log_volume" in summary


def test_last_log_page_says_no_further_matches(time_range):
    results, _, _ = normalize.unwrap(log_rows(3))
    data, _, _ = normalize.log_data(results, limit=20, offset=20)
    assert not data.has_more and data.next_offset is None
    assert "returned records 21–23; no further matches" in normalize.summarize_logs(
        data, time_range, redacted=False
    )


def test_log_page_past_end_is_not_reported_as_no_matches(time_range):
    results, _, _ = normalize.unwrap(log_rows(0))
    data, _, _ = normalize.log_data(results, limit=20, offset=40)
    summary = normalize.summarize_logs(data, time_range, redacted=False)
    assert "no records; no further matches" in summary
    assert "no matching records" not in summary


def test_size_cut_log_page_continues_from_last_returned_record():
    payload = log_rows(50)
    for row in payload["data"]["data"]["results"][0]["rows"]:
        row["data"]["body"] = "y" * 5_000
        row["data"]["attributes_string"] = {f"k{i}": "v" * 1_000 for i in range(30)}
    results, _, _ = normalize.unwrap(payload)
    data, truncated, _ = normalize.log_data(results, limit=50, offset=100)
    assert truncated and data.has_more
    assert len(data.records) < 50
    assert data.next_offset == 100 + len(data.records)


def test_span_page_reports_sample_count_not_error_rate(time_range):
    payload = load_fixture("spans_raw.json")
    rows = payload["data"]["data"]["results"][0]["rows"]
    payload["data"]["data"]["results"][0]["rows"] = rows + [copy.deepcopy(rows[1])]
    results, _, _ = normalize.unwrap(payload)
    data, _ = normalize.span_data(results, limit=2, offset=50)
    assert (data.offset, data.has_more, data.next_offset) == (50, True, 52)
    summary = normalize.summarize_spans(data, time_range)
    assert "returned spans 51–52 of an unknown total" in summary
    assert "1 of the 2 returned spans have errors" in summary
    assert "not an error rate" in summary
    assert "%" not in summary


# --- trace completeness -----------------------------------------------------------------

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def span_row(span_id: str, parent: str, start_ms: int, duration_ms: float, **extra) -> dict:
    timestamp = (BASE + timedelta(milliseconds=start_ms)).isoformat().replace("+00:00", "Z")
    return {
        "timestamp": timestamp,
        "data": {
            "trace_id": TRACE_ID,
            "span_id": span_id,
            "parent_span_id": parent,
            "name": extra.get("name", "op"),
            "duration_nano": int(duration_ms * 1_000_000),
            "has_error": extra.get("error", False),
            "service.name": "inquest-demo",
        },
    }


def trace_results(rows: list[dict]) -> list[dict]:
    return [{"queryName": "A", "rows": rows}]


def test_consistent_trace_uses_root_duration():
    rows = [span_row("root", "", 0, 900), span_row("a", "root", 10, 300, error=True)]
    data, truncated = normalize.trace_data(trace_results(rows), TRACE_ID)
    assert not truncated
    assert data.completeness == TraceCompleteness.CONSISTENT
    assert (data.root_found, data.missing_parent_spans, data.incomplete_reasons) == (True, 0, [])
    assert (data.duration_ms, data.observed_window_ms) == (900.0, 900.0)
    summary = normalize.summarize_trace(data)
    assert "root span duration 900.0ms" in summary
    assert "cannot be ruled out" in summary


def test_trace_without_root_has_unknown_duration():
    rows = [span_row("a", "root", 10, 300), span_row("b", "a", 400, 2000)]
    data, _ = normalize.trace_data(trace_results(rows), TRACE_ID)
    assert data.completeness == TraceCompleteness.INCOMPLETE
    assert data.incomplete_reasons == ["root_span_missing", "parent_spans_missing"]
    assert (data.duration_ms, data.observed_window_ms) == (None, 2390.0)
    assert data.missing_parent_spans == 1
    summary = normalize.summarize_trace(data)
    assert "INCOMPLETE" in summary
    assert "trace duration unknown, at least 2390.0ms observed" in summary


def test_trace_with_orphaned_spans_is_incomplete_even_with_root():
    rows = [
        span_row("root", "", 0, 900),
        span_row("a", "gone1", 10, 100),
        span_row("b", "gone2", 20, 100),
        span_row("c", "gone2", 30, 100),
    ]
    data, _ = normalize.trace_data(trace_results(rows), TRACE_ID)
    assert data.completeness == TraceCompleteness.INCOMPLETE
    assert data.incomplete_reasons == ["parent_spans_missing"]
    assert data.missing_parent_spans == 2
    assert data.duration_ms == 900.0


def test_trace_hitting_span_limit_is_incomplete():
    rows = [span_row("root", "", 0, 5000)]
    rows += [span_row(f"s{i}", "root", i, 1) for i in range(MAX_TRACE_SPANS)]
    data, truncated = normalize.trace_data(trace_results(rows), TRACE_ID)
    assert truncated
    assert data.span_count == MAX_TRACE_SPANS
    assert data.completeness == TraceCompleteness.INCOMPLETE
    assert "span_limit_reached" in data.incomplete_reasons
    assert data.duration_ms == 5000.0


def assert_completeness_invariant(data):
    assert (data.completeness == TraceCompleteness.CONSISTENT) == (not data.incomplete_reasons)
    assert data.spans_omitted == data.span_count - len(data.spans)


def test_size_cap_omits_spans_without_marking_trace_incomplete():
    # "late" starts last, so the size cap drops it first, yet it parents a kept span
    # (clock skew). Judging on returned spans alone would invent a missing parent.
    rows = [span_row("root", "", 0, 20_000), span_row("skewed", "late", 5, 1)]
    rows += [span_row(f"s{i}", "root", 10 + i, 1, name="n" * 400) for i in range(197)]
    rows.append(span_row("late", "root", 10_000, 1))
    data, truncated = normalize.trace_data(trace_results(rows), TRACE_ID)

    assert truncated
    assert data.span_count == MAX_TRACE_SPANS
    assert data.spans_omitted > 0
    assert "late" not in {s.span_id for s in data.spans}
    assert "skewed" in {s.span_id for s in data.spans}
    assert data.completeness == TraceCompleteness.CONSISTENT
    assert data.incomplete_reasons == []
    assert data.missing_parent_spans == 0
    assert_completeness_invariant(data)
    assert len(data.model_dump_json()) <= MAX_DATA_BYTES
    summary = normalize.summarize_trace(data)
    assert f"{data.spans_omitted} omitted from this result for size" in summary
    assert "No missing root or parent spans detected" in summary


def test_size_cap_on_incomplete_trace_keeps_only_trace_reasons():
    rows = [span_row(f"s{i}", "root", i, 1, name="n" * 400) for i in range(MAX_TRACE_SPANS)]
    data, truncated = normalize.trace_data(trace_results(rows), TRACE_ID)

    assert truncated and data.spans_omitted > 0
    assert data.completeness == TraceCompleteness.INCOMPLETE
    assert data.incomplete_reasons == ["root_span_missing", "parent_spans_missing"]
    assert data.missing_parent_spans == 1
    assert_completeness_invariant(data)
    summary = normalize.summarize_trace(data)
    assert "INCOMPLETE (root_span_missing, parent_spans_missing)" in summary
    assert "omitted from this result for size" in summary


def test_missing_trace_is_not_found():
    data, truncated = normalize.trace_data(trace_results([]), TRACE_ID)
    assert not truncated
    assert data.completeness == TraceCompleteness.NOT_FOUND
    assert data.duration_ms is None
    assert "not found" in normalize.summarize_trace(data)


# --- trace-stats summaries keep what matters first -----------------------------------------

LIVE_RUN_3_GROUPS = [
    # Query order from live run 3: the only failing operation came after the quiet ones.
    ("GET /slow", 1, 0, 1514.5),
    ("request handler - /slow", 2, 0, 1513.0),
    ("middleware - patched", 12, 0, 687.8),
    ("GET /", 1, 0, 16.4),
    ("request handler - /", 2, 0, 15.6),
    ("middleware - <anonymous>", 6, 0, 0.4),
    ("request handler - /error", 8, 0, 0.3),
    ("GET /error", 4, 4, 14.6),
]


def trace_stats(groups, has_more: bool = False):
    from src.integrations.signoz.schemas import TraceGroupStats, TraceStatsData

    return TraceStatsData(
        group_by="operation",
        has_more=has_more,
        groups=[
            TraceGroupStats(group=name, count=n, error_count=e, error_rate=round(e / n, 4),
                            p50_ms=p95 / 2, p95_ms=p95, p99_ms=p95 * 1.01)
            for name, n, e, p95 in groups
        ],
    )  # fmt: skip


def test_errors_and_slowest_come_first_whatever_the_query_order(time_range):
    summary = normalize.summarize_trace_stats(trace_stats(LIVE_RUN_3_GROUPS), time_range)

    totals, rest = summary.split(": ", 1)[1].split(". ", 1)
    assert totals == "8 groups, 36 spans, 4 errors (11.1111%)"
    assert rest.startswith("Errors: [GET /error] 4 spans, 4 errors (100.0%), p95 14.6ms.")
    assert "Slowest p95: [GET /slow] 1 span, p95 1514ms" in summary
    # Everything important fits in the 400 characters models are shown.
    assert summary.index("[GET /error]") < 200 and summary.index("Slowest p95") < 400


def test_listing_is_bounded_and_says_what_was_left_out(time_range):
    many = [(f"op-{i}", 100 + i, 0, 10.0) for i in range(20)] + [("op-bad", 5, 1, 9.0)]
    summary = normalize.summarize_trace_stats(trace_stats(many, has_more=True), time_range)

    assert summary.count("[op-") == normalize.TRACE_LISTED_GROUPS
    assert "+11 more groups not listed." in summary
    assert "More groups exist beyond this result." in summary
    assert summary.index("[op-bad]") < summary.index("Slowest")


def test_no_errors_is_said_explicitly(time_range):
    summary = normalize.summarize_trace_stats(trace_stats([("GET /", 50, 0, 20.0)]), time_range)
    assert "No span errors." in summary and "1 groups, 50 spans, 0 errors (0.0%)" in summary
