import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from src.db.session import SessionLocal, engine
from src.models import (
    AssignmentStatus,
    EnvironmentStatus,
    Evidence,
    EvidenceType,
    InvestigationStatus,
    ToolCall,
    ToolCallStatus,
)
from src.tools import Outcome, policy, sweep_stale_tool_calls
from src.tools import executor as executor_module
from tests.signoz.conftest import TEST_API_KEY, load_fixture
from tests.tools.conftest import (
    EMPTY,
    World,
    expression,
    replies,
    trace_stats_args,
    window,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(policy, "RETRY_BACKOFF_SECONDS", 0)


# --- success -----------------------------------------------------------------------------


async def test_success_persists_tool_call_and_evidence(world: World):
    handler, seen = replies("trace_stats_scalar.json")
    args = trace_stats_args(service=world.service)

    result = await world.run("signoz.trace_stats", args, handler, key="lg-call-1")

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.outcome is Outcome.OBSERVATIONS
    assert result.data and result.data["groups"][0]["group"] == "GET /api/orders"
    assert result.error is None and not result.replayed
    # Scope comes first and is the environment's own service, then the narrowing.
    assert expression(seen[0]) == (
        f"(service.name IN ['{world.service}']) AND service.name = '{world.service}'"
    )

    (call,) = await world.tool_calls()
    assert call.id == result.tool_call_id
    assert call.status is ToolCallStatus.SUCCEEDED
    assert call.idempotency_key == "lg-call-1"
    assert call.started_at is not None and call.completed_at >= call.started_at
    assert call.duration_ms is not None and call.attempt_number == 1
    assert call.input_json["service"] == world.service and call.input_json["where"] == []
    meta = call.output_metadata_json
    assert meta["outcome"] == "observations"
    assert meta["scope"] == {
        "identity": "service",
        "values": [world.service],
        "deployment_environment": None,
    }
    assert meta["time_range"] == call.input_json["time_range"]
    assert meta["source_reference"].startswith("signoz:traces:")
    assert "groups" not in json.dumps(meta)  # telemetry stays out of the ToolCall
    assert "GET /api/orders" in call.result_summary

    (evidence,) = await world.evidence()
    assert result.evidence_ids == [evidence.id]
    assert meta["evidence_ids"] == [str(evidence.id)]
    assert evidence.tool_call_id == call.id
    assert (evidence.investigation_id, evidence.environment_id) == (
        world.investigation_id,
        world.environment_id,
    )
    assert evidence.evidence_type is EvidenceType.TRACE
    assert evidence.source_provider == "signoz"
    assert evidence.source_reference == meta["source_reference"]
    assert (evidence.entity_type, evidence.entity_identifier) == ("service", world.service)
    assert evidence.data_json["scope"]["values"] == [world.service]
    assert evidence.data_json["data"] == result.data
    assert world.service in evidence.title


async def test_no_database_connection_is_held_during_the_signoz_call(world: World):
    checked_out: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        checked_out.append(engine.pool.checkedout())
        return httpx.Response(200, json=load_fixture("trace_stats_scalar.json"))

    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    assert checked_out == [0]


async def test_host_metrics_query_only_authorized_hosts(world: World):
    handler, seen = replies("metrics_formula.json")
    result = await world.run(
        "signoz.query_metrics", {"time_range": window(), "metric": "cpu_utilization"}, handler
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    body = json.loads(seen[0].content)
    for query in body["compositeQuery"]["queries"]:
        if "filter" in query["spec"]:
            assert query["spec"]["filter"]["expression"].startswith(
                f"(host.name IN ['{world.host}'])"
            )
            assert "service.name" not in query["spec"]["filter"]["expression"]


async def test_get_trace_is_scoped_and_warns(world: World):
    handler, seen = replies("spans_raw.json")
    result = await world.run(
        "signoz.get_trace", {"trace_id": TRACE_ID, "time_range": window()}, handler
    )
    assert (
        expression(seen[0]) == f"(service.name IN ['{world.service}']) AND trace_id = '{TRACE_ID}'"
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert any("outside the environment scope" in w for w in result.warnings)
    (evidence,) = await world.evidence()
    assert (evidence.entity_type, evidence.entity_identifier) == ("trace", TRACE_ID)


# --- empty results -----------------------------------------------------------------------


async def test_empty_result_records_outcome_without_evidence(world: World):
    handler, seen = replies(EMPTY, "trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.outcome is Outcome.EMPTY
    assert result.telemetry_available == "yes"
    assert result.evidence_ids == []
    assert "not evidence that the system is healthy" in result.summary
    assert await world.evidence() == []
    # The probe keeps the identity scope but drops the operation grouping.
    probe = json.loads(seen[1].content)["compositeQuery"]["queries"][0]["spec"]
    assert probe["groupBy"][0]["name"] == "service.name"
    assert probe["filter"]["expression"] == f"(service.name IN ['{world.service}'])"

    (call,) = await world.tool_calls()
    meta = call.output_metadata_json
    assert (meta["outcome"], meta["telemetry_available"]) == ("empty", "yes")
    assert meta["coverage_probe"]["operation"] == "signoz.trace_stats"
    assert meta["coverage_probe"]["count"] > 0
    assert meta["time_range"] and meta["scope"]["identity"] == "service"
    assert meta["evidence_ids"] == []
    assert "not evidence that the system is healthy" in call.result_summary


async def test_empty_result_without_any_telemetry(world: World):
    handler, _ = replies(EMPTY, EMPTY)
    result = await world.run(
        "signoz.search_logs",
        {"time_range": window(), "where": [{"key": "severity_text", "op": "eq", "value": "ERROR"}]},
        handler,
    )
    assert (result.outcome, result.telemetry_available) == (Outcome.EMPTY, "no")
    assert "Telemetry for this scope in the window: no." in result.summary


async def test_probe_failure_never_fails_the_primary_query(world: World):
    handler, seen = replies(EMPTY, 503)
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.telemetry_available == "unknown"
    assert len(seen) == 2  # the probe is not retried
    (call,) = await world.tool_calls()
    assert call.output_metadata_json["coverage_probe"]["error_code"] == "signoz_unavailable"
    assert call.error_code is None


async def test_probe_timeout_is_unknown(world: World, monkeypatch):
    monkeypatch.setattr(policy, "EXECUTION_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(policy, "MIN_PROBE_SECONDS", 0.1)
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.sleep(5)
        return httpx.Response(200, json=EMPTY)

    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.telemetry_available == "unknown"
    (call,) = await world.tool_calls()
    assert call.output_metadata_json["coverage_probe"]["error_code"] == "executor.timeout"
    assert call.duration_ms < 2_000


async def test_probe_is_skipped_without_enough_time(world: World, monkeypatch):
    monkeypatch.setattr(policy, "MIN_PROBE_SECONDS", 100)
    handler, seen = replies(EMPTY)
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.telemetry_available == "unknown"
    assert len(seen) == 1
    (call,) = await world.tool_calls()
    assert call.output_metadata_json["coverage_probe"]["skipped"] == "insufficient_time"


async def test_bare_query_is_its_own_coverage_probe(world: World):
    handler, seen = replies(EMPTY)
    result = await world.run(
        "signoz.query_metrics", {"time_range": window(), "metric": "cpu_utilization"}, handler
    )
    assert result.telemetry_available == "no"
    assert len(seen) == 1


async def test_trace_not_found(world: World):
    handler, _ = replies(EMPTY, EMPTY)
    result = await world.run(
        "signoz.get_trace", {"trace_id": TRACE_ID, "time_range": window()}, handler
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.outcome is Outcome.NOT_FOUND
    assert await world.evidence() == []


# --- rejections --------------------------------------------------------------------------


def _never(_: httpx.Request) -> httpx.Response:
    raise AssertionError("SigNoz must not be called")


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        ("shell.exec", {"cmd": "id"}, "policy.unknown_tool"),
        ("signoz.raw_sql", {"sql": "SELECT 1"}, "policy.unknown_tool"),
        (
            "signoz.trace_stats",
            trace_stats_args(filter="service.name = 'other'"),
            "policy.invalid_arguments",
        ),
        (
            "signoz.trace_stats",
            trace_stats_args(environment_id="00000000-0000-0000-0000-000000000000"),
            "policy.invalid_arguments",
        ),
        (
            "signoz.trace_stats",
            trace_stats_args(where=[{"key": "service.name", "op": "eq", "value": "other"}]),
            "policy.invalid_arguments",
        ),
        ("signoz.trace_stats", "not-an-object", "policy.invalid_arguments"),
        ("signoz.trace_stats", trace_stats_args(service="other-svc"), "policy.out_of_scope"),
        (
            "signoz.query_metrics",
            {"time_range": window(), "metric": "cpu_utilization", "service": "x"},
            "policy.identity_mismatch",
        ),
        (
            "signoz.query_metrics",
            {"time_range": window(hours_ago=24 * 8), "metric": "cpu_utilization"},
            "policy.lookback_exceeded",
        ),
    ],
)
async def test_rejected_requests_are_persisted_without_querying(
    world: World, tool, arguments, code
):
    result = await world.run(tool, arguments, _never, key="k1")

    assert result.status is ToolCallStatus.REJECTED
    assert result.error and result.error.code == code
    (call,) = await world.tool_calls()
    assert call.id == result.tool_call_id
    assert (call.status, call.error_code) == (ToolCallStatus.REJECTED, code)
    assert call.started_at is None and call.completed_at is not None
    # Rejected rows never hold an idempotency key, so a corrected retry is judged afresh.
    assert call.idempotency_key is None
    assert await world.evidence() == []


async def test_invalid_argument_rows_are_sanitized(world: World):
    result = await world.run(
        "signoz.trace_stats",
        {"time_range": "password=hunter2", "notes": "x" * 5_000, "secret": TEST_API_KEY},
        _never,
    )
    assert result.error.code == "policy.invalid_arguments"
    (call,) = await world.tool_calls()
    stored = json.dumps(call.input_json) + (call.error_message or "")
    assert "hunter2" not in stored and TEST_API_KEY not in stored
    assert len(call.input_json["notes"]) <= 200


@pytest.mark.parametrize(
    ("world_kwargs", "code"),
    [
        ({"signoz": None}, "policy.scope_missing"),
        ({"signoz": {"hosts": ["only-a-host"]}}, "policy.scope_missing_identity"),
        ({"signoz": {"hosts": "not-a-list"}}, "policy.scope_invalid"),
        ({"assignment_status": AssignmentStatus.PENDING}, "policy.assignment_not_running"),
        ({"assignment_status": AssignmentStatus.CANCELLED}, "policy.assignment_not_running"),
        (
            {"investigation_status": InvestigationStatus.CORRELATING},
            "policy.assignment_not_running",
        ),
        ({"env_status": EnvironmentStatus.DISABLED}, "policy.environment_not_allowed"),
    ],
)
async def test_environment_and_assignment_state_rejections(make_world, world_kwargs, code):
    world = await make_world(**world_kwargs)
    result = await world.run("signoz.trace_stats", trace_stats_args(), _never)
    assert result.status is ToolCallStatus.REJECTED
    assert result.error.code == code
    (call,) = await world.tool_calls()
    assert call.error_code == code


async def test_context_must_match_the_assignment(make_world):
    world, other = await make_world(), await make_world()
    for overrides in (
        {"environment_id": other.environment_id},
        {"investigation_id": other.investigation_id},
    ):
        result = await world.run(
            "signoz.trace_stats", trace_stats_args(), _never, context=world.context(**overrides)
        )
        assert result.error.code == "policy.context_mismatch"
    assert [c.error_code for c in await world.tool_calls()] == ["policy.context_mismatch"] * 2
    assert await other.tool_calls() == []


async def test_unknown_assignment_is_refused_without_a_row(world: World):
    import uuid

    context = world.context(assignment_id=uuid.uuid4())
    result = await world.run("signoz.trace_stats", trace_stats_args(), _never, context=context)
    assert result.tool_call_id is None
    assert result.error.code == "policy.unknown_assignment"
    assert await world.tool_calls() == []


async def test_identity_shared_with_another_active_environment_is_ambiguous(make_world):
    world = await make_world()
    other = await make_world(signoz={"hosts": [], "services": [world.service]})
    result = await world.run("signoz.trace_stats", trace_stats_args(), _never)
    assert result.error.code == "policy.scope_ambiguous"

    # Retiring the other environment removes the ambiguity.
    async with SessionLocal() as s, s.begin():
        from src.models import Environment

        (await s.get(Environment, other.environment_id)).status = EnvironmentStatus.DISABLED
    handler, _ = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED


async def test_deployment_environment_tells_shared_services_apart(make_world):
    world = await make_world(
        signoz={"services": ["shared-svc"], "deployment_environment": "staging"}
    )
    await make_world(signoz={"services": ["shared-svc"], "deployment_environment": "prod"})
    handler, seen = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    assert expression(seen[0]) == (
        "(service.name IN ['shared-svc'] AND deployment.environment = 'staging')"
    )


async def test_injection_attempt_stays_a_literal(world: World):
    handler, seen = replies("trace_stats_scalar.json")
    payload = "x') OR (service.name IN ['other-svc'"
    await world.run(
        "signoz.trace_stats",
        trace_stats_args(where=[{"key": "http.route", "op": "eq", "value": payload}]),
        handler,
    )
    assert expression(seen[0]) == (
        f"(service.name IN ['{world.service}']) AND http.route = 'x\\') OR (service.name IN "
        "[\\'other-svc\\''"
    )


# --- budgets -----------------------------------------------------------------------------


async def test_rejections_count_toward_the_rejection_budget(world: World, monkeypatch):
    monkeypatch.setattr(policy, "MAX_REJECTIONS_PER_ASSIGNMENT", 2)
    for _ in range(2):
        await world.run("nope", {}, _never)
    handler, _ = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)

    assert result.error.code == "policy.rejection_budget_exhausted"
    calls = await world.tool_calls()
    assert [c.status for c in calls] == [ToolCallStatus.REJECTED] * 3


async def test_call_budget(world: World, monkeypatch):
    monkeypatch.setattr(policy, "MAX_TOOL_CALLS_PER_ASSIGNMENT", 1)
    handler, _ = replies("trace_stats_scalar.json")
    first = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    second = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert first.status is ToolCallStatus.SUCCEEDED
    assert second.error.code == "policy.call_budget_exhausted"


async def test_stale_running_rows_are_swept_at_admission(world: World, monkeypatch):
    monkeypatch.setattr(policy, "MAX_CONCURRENT_PER_ASSIGNMENT", 1)
    async with SessionLocal() as s, s.begin():
        s.add(
            ToolCall(
                assignment_id=world.assignment_id,
                tool_name="signoz.trace_stats",
                tool_category="telemetry",
                status=ToolCallStatus.RUNNING,
                started_at=func.now() - timedelta(hours=1),
            )
        )
    handler, _ = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    stale = (await world.tool_calls())[0]
    assert (stale.status, stale.error_code) == (ToolCallStatus.TIMED_OUT, "executor.abandoned")


# --- failures, timeouts, retries ---------------------------------------------------------


async def test_executor_deadline(world: World, monkeypatch):
    monkeypatch.setattr(policy, "EXECUTION_DEADLINE_SECONDS", 0.2)

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=EMPTY)

    result = await world.run("signoz.trace_stats", trace_stats_args(), slow)
    assert result.status is ToolCallStatus.TIMED_OUT
    assert result.error.code == "executor.timeout" and result.error.retryable
    assert result.data is None
    (call,) = await world.tool_calls()
    assert call.status is ToolCallStatus.TIMED_OUT and 150 <= call.duration_ms < 2_000


async def test_signoz_timeout_is_not_retried(world: World):
    handler, seen = replies(httpx.ReadTimeout("slow"))
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.TIMED_OUT
    assert result.error.code == "signoz_timeout"
    assert len(seen) == 1


async def test_unavailable_is_retried_once(world: World):
    handler, seen = replies(503, "trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.SUCCEEDED
    (call,) = await world.tool_calls()
    assert call.attempt_number == 2
    attempts = call.output_metadata_json["attempts"]
    assert [a.get("error_code") for a in attempts] == ["signoz_unavailable", None]
    assert len(seen) == 2


async def test_unavailable_twice_fails(world: World):
    handler, seen = replies(503, 503, 503)
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.FAILED
    assert result.error.code == "signoz_unavailable" and result.error.retryable
    assert len(seen) == 2
    (call,) = await world.tool_calls()
    assert call.attempt_number == 2
    assert call.output_metadata_json["error_retryable"] is True


async def test_invalid_query_is_not_retried(world: World):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"status": "error", "error": {"code": "invalid_input", "message": "bad key"}}
        )

    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.FAILED
    assert result.error.code == "signoz_invalid_query" and not result.error.retryable
    assert "bad key" in result.error.message


async def test_credentials_never_reach_rows_or_results(world: World):
    def echo(request: httpx.Request) -> httpx.Response:
        key = request.headers["SIGNOZ-API-KEY"]
        return httpx.Response(401, json={"status": "error", "error": {"message": f"bad {key}"}})

    def boom(_: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"exploded with {TEST_API_KEY}")

    payload = load_fixture("trace_stats_scalar.json")
    payload["data"]["data"]["results"][0]["data"][0][0] = f"GET /leak/{TEST_API_KEY}"

    results = [
        await world.run("signoz.trace_stats", trace_stats_args(), echo),
        await world.run("signoz.trace_stats", trace_stats_args(), boom),
        await world.run("signoz.trace_stats", trace_stats_args(), replies(payload)[0]),
    ]
    assert results[0].error.code == "signoz_auth"
    assert results[1].error.code == "executor.internal"
    assert results[1].error.message == "internal error (RuntimeError)"
    assert results[2].status is ToolCallStatus.SUCCEEDED
    for result in results:
        assert TEST_API_KEY not in result.model_dump_json()

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(func.row_to_json(ToolCall.__table__.table_valued())).where(
                    ToolCall.assignment_id == world.assignment_id
                )
            )
        ).scalars()
        evidence_rows = (
            await s.execute(
                select(func.row_to_json(Evidence.__table__.table_valued())).where(
                    Evidence.assignment_id == world.assignment_id
                )
            )
        ).scalars()
        dumped = json.dumps([*rows, *evidence_rows])
    assert TEST_API_KEY not in dumped
    assert "[redacted]" in dumped


async def test_missing_signoz_settings_fail_the_call(world: World, monkeypatch):
    from src.integrations.signoz import SigNozError, SigNozErrorCode

    def broken():
        raise SigNozError(SigNozErrorCode.CONFIG, "invalid or missing settings: SIGNOZ_API_KEY")

    monkeypatch.setattr(executor_module, "load_signoz_settings", broken)
    from src.tools import execute_tool

    result = await execute_tool("signoz.trace_stats", trace_stats_args(), world.context())
    assert result.status is ToolCallStatus.FAILED
    assert result.error.code == "signoz_config"


async def test_output_too_large_is_withheld(world: World, monkeypatch):
    monkeypatch.setattr(executor_module, "MAX_DATA_JSON_BYTES", 100)
    handler, _ = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)
    assert result.status is ToolCallStatus.FAILED
    assert result.error.code == "executor.output_too_large"
    assert result.data is None and result.evidence_ids == []
    assert await world.evidence() == []


# --- database failures -------------------------------------------------------------------


async def test_admission_database_failure_runs_nothing(world: World):
    def unavailable():
        raise OperationalError("connect", {}, ConnectionRefusedError())

    result = await world.run(
        "signoz.trace_stats", trace_stats_args(), _never, session_factory=unavailable
    )
    assert result.tool_call_id is None
    assert result.error.code == "executor.persistence_unavailable" and result.error.retryable


async def test_finalize_failure_withholds_data_and_sweep_recovers(world: World, monkeypatch):
    async def fail(*_: Any, **__: Any):
        raise OperationalError("commit", {}, ConnectionResetError())

    monkeypatch.setattr(executor_module, "_finalize_in", fail)
    handler, _ = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)

    assert result.error.code == "executor.persistence_failed"
    assert result.data is None and result.evidence_ids == []
    (call,) = await world.tool_calls()
    assert call.status is ToolCallStatus.RUNNING

    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(ToolCall)
            .where(ToolCall.id == call.id)
            .values(started_at=func.now() - timedelta(hours=1))
        )
        assert await sweep_stale_tool_calls(s) >= 1
    (call,) = await world.tool_calls()
    assert (call.status, call.error_code) == (ToolCallStatus.TIMED_OUT, "executor.abandoned")


async def test_constraint_violation_records_failure_without_evidence(world: World, monkeypatch):
    real = executor_module.build_evidence

    def oversized(**kwargs: Any) -> Evidence:
        evidence = real(**kwargs)
        evidence.summary = "x" * 5_000  # violates ck_evidence_summary_length
        return evidence

    monkeypatch.setattr(executor_module, "build_evidence", oversized)
    handler, _ = replies("trace_stats_scalar.json")
    result = await world.run("signoz.trace_stats", trace_stats_args(), handler)

    assert result.status is ToolCallStatus.FAILED
    assert result.error.code == "executor.persistence_rejected"
    (call,) = await world.tool_calls()
    assert (call.status, call.error_code) == (
        ToolCallStatus.FAILED,
        "executor.persistence_rejected",
    )
    assert await world.evidence() == []


# --- idempotency -------------------------------------------------------------------------


async def test_replay_returns_stored_outcome_without_querying(world: World):
    handler, seen = replies("trace_stats_scalar.json")
    args = trace_stats_args()
    first = await world.run("signoz.trace_stats", args, handler, key="lg-1")
    second = await world.run("signoz.trace_stats", args, handler, key="lg-1")

    assert len(seen) == 1
    assert second.replayed and not first.replayed
    assert second.tool_call_id == first.tool_call_id
    assert second.evidence_ids == first.evidence_ids
    assert second.data == first.data and second.summary == first.summary
    assert len(await world.tool_calls()) == 1


async def test_replay_of_a_failed_call_returns_the_failure(world: World):
    handler, seen = replies(httpx.ReadTimeout("slow"))
    args = trace_stats_args()
    first = await world.run("signoz.trace_stats", args, handler, key="lg-2")
    second = await world.run("signoz.trace_stats", args, handler, key="lg-2")
    assert len(seen) == 1
    assert second.replayed and second.status is ToolCallStatus.TIMED_OUT
    assert second.error.code == first.error.code == "signoz_timeout"


async def test_idempotency_conflict(world: World):
    handler, seen = replies("trace_stats_scalar.json")
    first = await world.run("signoz.trace_stats", trace_stats_args(), handler, key="lg-3")
    conflict = await world.run(
        "signoz.trace_stats", trace_stats_args(group_by="service"), handler, key="lg-3"
    )
    assert conflict.error.code == "policy.idempotency_conflict"
    assert len(seen) == 1
    calls = await world.tool_calls()
    assert [(c.id == first.tool_call_id, c.idempotency_key) for c in calls] == [
        (True, "lg-3"),
        (False, None),
    ]


async def test_rejected_request_does_not_consume_its_key(world: World):
    await world.set_status(assignment=AssignmentStatus.PENDING)
    args = trace_stats_args()
    rejected = await world.run("signoz.trace_stats", args, _never, key="lg-4")
    assert rejected.error.code == "policy.assignment_not_running"

    await world.set_status(assignment=AssignmentStatus.RUNNING)
    handler, _ = replies("trace_stats_scalar.json")
    accepted = await world.run("signoz.trace_stats", args, handler, key="lg-4")
    assert accepted.status is ToolCallStatus.SUCCEEDED and not accepted.replayed
