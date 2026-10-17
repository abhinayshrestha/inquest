import asyncio
import json
from typing import Any

import httpx
import pytest

from src.models import AssignmentStatus, ToolCallStatus
from src.models.evidence import MAX_DATA_JSON_BYTES
from src.tools import policy
from tests.mcp_server.conftest import connect, token_for
from tests.signoz.conftest import load_fixture
from tests.tools.conftest import EMPTY, World, trace_stats_args

META_KEY = "inquest/tool_call_id"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(policy, "RETRY_BACKOFF_SECONDS", 0)


async def call(
    app: Any,
    world: World,
    name: str = "signoz_trace_stats",
    arguments: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    async with connect(app, token_for(world), **(headers or {})) as client:
        return await client.call_tool(
            name,
            trace_stats_args() if arguments is None else arguments,
            meta={META_KEY: key} if key is not None else None,
        )


async def test_valid_call_runs_through_the_executor_and_returns_evidence(
    mcp_app, world: World, signoz
):
    seen = signoz("trace_stats_scalar.json")
    result = await call(mcp_app, world)

    assert result.is_error is False
    body = result.structured_content
    assert body["status"] == "succeeded" and body["outcome"] == "observations"
    assert body["tool_name"] == "signoz.trace_stats"
    assert body["error"] is None and len(body["evidence_ids"]) == 1
    assert json.loads(result.content[0].text) == body

    (row,) = await world.tool_calls()
    assert (str(row.id), row.tool_name, row.status) == (
        body["tool_call_id"],
        "signoz.trace_stats",
        ToolCallStatus.SUCCEEDED,
    )
    (evidence,) = await world.evidence()
    assert [str(evidence.id)] == body["evidence_ids"]
    assert evidence.investigation_id == world.investigation_id
    # Scope came from the token's environment, not from anything the client sent.
    assert (
        world.service
        in json.loads(seen[0].content)["compositeQuery"]["queries"][0]["spec"]["filter"][
            "expression"
        ]
    )


async def test_empty_result_is_not_an_error_and_creates_no_evidence(mcp_app, world: World, signoz):
    signoz(EMPTY, "trace_stats_scalar.json")
    result = await call(mcp_app, world)

    assert result.is_error is False
    body = result.structured_content
    assert (body["outcome"], body["telemetry_available"]) == ("empty", "yes")
    assert body["evidence_ids"] == [] and body["data"] is not None
    assert await world.evidence() == []


async def test_invalid_arguments_are_rejected_audited_and_counted(mcp_app, world: World, signoz):
    seen = signoz("trace_stats_scalar.json")
    result = await call(mcp_app, world, arguments={"group_by": "nonsense"})

    assert result.is_error is True
    body = result.structured_content
    assert body["status"] == "rejected"
    assert body["error"]["code"] == "policy.invalid_arguments"
    (row,) = await world.tool_calls()
    assert row.status is ToolCallStatus.REJECTED and str(row.id) == body["tool_call_id"]
    assert seen == []


async def test_unknown_tool_is_rejected_by_the_executor(mcp_app, world: World, signoz):
    signoz("trace_stats_scalar.json")
    result = await call(mcp_app, world, "signoz_raw_query", {"query": "SELECT 1"})

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "policy.unknown_tool"
    (row,) = await world.tool_calls()
    assert row.status is ToolCallStatus.REJECTED


async def test_closed_assignment_runs_nothing(mcp_app, world: World, signoz):
    seen = signoz("trace_stats_scalar.json")
    await world.set_status(assignment=AssignmentStatus.COMPLETED)
    result = await call(mcp_app, world)

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "policy.assignment_not_running"
    assert seen == []


async def test_failed_signoz_call_is_an_error_result(mcp_app, world: World, signoz):
    signoz(400)
    result = await call(mcp_app, world)

    assert result.is_error is True
    body = result.structured_content
    assert body["status"] == "failed" and body["data"] is None
    assert body["tool_call_id"] is not None and body["error"]["code"]


async def test_result_stays_within_the_evidence_bound(mcp_app, world: World, signoz):
    signoz("logs_raw.json")
    result = await call(
        mcp_app,
        world,
        "signoz_search_logs",
        {"time_range": trace_stats_args()["time_range"], "identity": "host", "host": world.host},
    )

    assert result.is_error is False, result.structured_content
    assert len(json.dumps(result.structured_content["data"])) <= MAX_DATA_JSON_BYTES


# --- idempotency -------------------------------------------------------------------------


async def test_replayed_meta_key_runs_once(mcp_app, world: World, signoz):
    seen = signoz("trace_stats_scalar.json")
    args = trace_stats_args()
    first = await call(mcp_app, world, arguments=args, key="chatcmpl-tool-1")
    again = await call(mcp_app, world, arguments=args, key="chatcmpl-tool-1")

    assert first.structured_content["replayed"] is False
    assert again.structured_content["replayed"] is True
    for field in ("tool_call_id", "evidence_ids", "data"):
        assert again.structured_content[field] == first.structured_content[field]
    assert len(seen) == 1
    (row,) = await world.tool_calls()
    assert row.idempotency_key == "chatcmpl-tool-1"


async def test_header_key_is_accepted_for_adapters_that_cannot_set_meta(
    mcp_app, world: World, signoz
):
    seen = signoz("trace_stats_scalar.json")
    args = trace_stats_args()
    headers = {"Inquest-Tool-Call-Id": "lc-call-7"}
    await call(mcp_app, world, arguments=args, headers=headers)
    again = await call(mcp_app, world, arguments=args, headers=headers)

    assert again.structured_content["replayed"] is True
    assert len(seen) == 1


async def test_same_key_with_different_arguments_conflicts(mcp_app, world: World, signoz):
    signoz("trace_stats_scalar.json")
    await call(mcp_app, world, arguments=trace_stats_args(), key="k-1")
    other = await call(mcp_app, world, arguments=trace_stats_args(group_by="service"), key="k-1")

    assert other.is_error is True
    assert other.structured_content["error"]["code"] == "policy.idempotency_conflict"


async def test_without_a_key_every_call_executes(mcp_app, world: World, signoz):
    # JSON-RPC request ids repeat across connections and are never used as keys.
    seen = signoz("trace_stats_scalar.json")
    args = trace_stats_args()
    first = await call(mcp_app, world, arguments=args)
    second = await call(mcp_app, world, arguments=args)

    assert first.structured_content["tool_call_id"] != second.structured_content["tool_call_id"]
    assert len(seen) == 2
    assert [c.idempotency_key for c in await world.tool_calls()] == [None, None]


@pytest.mark.parametrize(
    ("key", "headers"),
    [
        ("has spaces", None),
        ("x" * 101, None),
        ("meta-key", {"Inquest-Tool-Call-Id": "header-key"}),
    ],
    ids=["bad-chars", "too-long", "meta-header-disagree"],
)
async def test_unusable_key_runs_nothing(mcp_app, world: World, signoz, key, headers):
    seen = signoz("trace_stats_scalar.json")
    result = await call(mcp_app, world, key=key, headers=headers)

    assert result.is_error is True
    body = result.structured_content
    assert body["error"]["code"] == "mcp.invalid_idempotency_key"
    assert body["tool_call_id"] is None
    assert seen == [] and await world.tool_calls() == []


# --- cancellation ------------------------------------------------------------------------


async def test_client_cancellation_is_recorded(mcp_app, world: World, signoz):
    arrived: list[httpx.Request] = []

    async def stalled(request: httpx.Request) -> httpx.Response:
        arrived.append(request)
        await asyncio.sleep(30)
        return httpx.Response(200, json=load_fixture("trace_stats_scalar.json"))

    signoz(handler=stalled)
    task = asyncio.create_task(call(mcp_app, world))
    async with asyncio.timeout(5):
        while not arrived:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Polls rows the server-side task finalizes after the client is gone.
    async with asyncio.timeout(5):
        while (rows := await world.tool_calls()) and rows[0].status is ToolCallStatus.RUNNING:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
    (row,) = rows
    assert (row.status, row.error_code) == (ToolCallStatus.CANCELLED, "executor.cancelled")
    assert await world.evidence() == []
