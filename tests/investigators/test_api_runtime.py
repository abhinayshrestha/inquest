"""Phase 5: API execution, SSE progress, cancellation and recovery, end to end through
the real FastAPI app, runtime, runner, Main and Local graphs, MCP app and executor.
Models are scripted and SigNoz is mocked; nothing here spends model budget."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import pytest
from sqlalchemy import select

from src.auth.settings import load_auth_settings
from src.db.session import SessionLocal
from src.investigators import lifecycle, runner, runtime
from src.investigators.checkpoint import thread_id as assignment_thread_id
from src.investigators.runner import investigation_lock, turn_thread_id
from src.investigators.schemas import Verdict
from src.models import (
    AssignmentStatus,
    Investigation,
    InvestigationAssignment,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    ProgressEvent,
    ToolCall,
    TurnStatus,
)
from src.services import investigations, progress, turns
from tests.credential_fixtures import allow_process_credentials
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel
from tests.investigators.test_main_graph import (  # noqa: F401
    ANSWER,
    Crash,
    MainWorld,
    built_main_world,
    compose,
    investigate,
    local_script,
    rows,
)
from tests.mcp_server.conftest import signoz  # noqa: F401
from tests.tenancy import Member, api_client, committed_members, configure_auth


@pytest.fixture(autouse=True)
def mocked_signoz(signoz):  # noqa: F811
    signoz("trace_stats_scalar.json")


@pytest.fixture
async def member(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Member]:
    configure_auth(monkeypatch)
    allow_process_credentials(monkeypatch)
    async with committed_members() as (signed_in,):
        yield signed_in
    load_auth_settings.cache_clear()


@pytest.fixture
async def main_world(checkpointer, member: Member) -> AsyncIterator[MainWorld]:
    # An organization's environment that an operator granted the development credentials.
    async with built_main_world(checkpointer, member.owns | {"process_credentials": True}) as w:
        yield w


@pytest.fixture
async def api(member: Member) -> AsyncIterator[httpx.AsyncClient]:
    # The real app and real sessions: the runtime's tasks must see committed rows.
    async with api_client(member) as c:
        yield c


@pytest.fixture
async def rt(checkpointer, mcp_app) -> AsyncIterator[runtime.Runtime]:
    started = runtime.Runtime(
        checkpointer,
        run_kwargs={"mcp_settings": MCP_SETTINGS,
                    "mcp_transport": httpx2.ASGITransport(app=mcp_app)},
    )  # fmt: skip
    runtime.start(started)
    yield started
    await runtime.stop()
    lifecycle.reset()


def script(rt_: runtime.Runtime, main: ScriptedModel, local: ScriptedModel | None = None) -> None:
    rt_.run_kwargs["main"] = main
    rt_.run_kwargs["local"] = local or ScriptedModel()


def happy(world: MainWorld) -> tuple[ScriptedModel, ScriptedModel]:
    return (
        ScriptedModel(structured=[investigate(world), ANSWER, compose()]),
        local_script(world.service),
    )


async def finished(world: MainWorld) -> Any:
    task = runner._tasks.get(world.investigation_id)
    if task is not None:
        return await asyncio.gather(task, return_exceptions=True)
    return None


async def turn_row(turn_id: uuid.UUID) -> InvestigationTurn:
    async with SessionLocal() as s:
        return await s.get(InvestigationTurn, turn_id)  # type: ignore[return-value]


def sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in fields:
            events.append({"id": int(fields["id"]), "event": fields["event"],
                           **json.loads(fields["data"])})  # fmt: skip
    return events


async def cleanup_turn_threads(checkpointer, world: MainWorld) -> None:
    async with SessionLocal() as s:
        for t in await s.scalars(
            select(InvestigationTurn.id).where(
                InvestigationTurn.investigation_id == world.investigation_id
            )
        ):
            await checkpointer.adelete_thread(turn_thread_id(t))
        for a in await s.scalars(
            select(InvestigationAssignment.id).where(
                InvestigationAssignment.investigation_id == world.investigation_id
            )
        ):
            await checkpointer.adelete_thread(assignment_thread_id(a))


# --- lifecycle -----------------------------------------------------------------------------


async def test_start_runs_turn_one_and_answers_through_existing_responses(api, rt, main_world):  # noqa: F811
    script(rt, *happy(main_world))

    response = await api.post(f"/investigations/{main_world.investigation_id}/start")

    assert response.status_code == 202
    body = response.json()
    assert (body["turn_id"], body["seq"], body["status"]) == (str(main_world.turn_id), 1, "queued")
    assert body["events_url"] == f"/investigations/{main_world.investigation_id}/events"
    await finished(main_world)
    investigation = (await api.get(f"/investigations/{main_world.investigation_id}")).json()
    assert investigation["status"] == "idle"
    (turn,) = investigation["turns"]
    assert turn["status"] == "completed" and turn["answer_json"]["report_version"] == 1
    assert [r["version"] for r in investigation["reports"]] == [1]
    report = (await api.get(f"/investigations/{main_world.investigation_id}/reports/1")).json()
    assert report["report_json"]["outcome"] == "inconclusive"
    assert (
        await api.get(f"/investigations/{main_world.investigation_id}/reports/9")
    ).status_code == 404
    # Already started: refused, nothing runs twice.
    assert (
        await api.post(f"/investigations/{main_world.investigation_id}/start")
    ).status_code == 409


async def test_runner_disabled_refuses_without_marking_the_turn(api, main_world):  # noqa: F811
    response = await api.post(f"/investigations/{main_world.investigation_id}/start")
    assert response.status_code == 503 and "INQUEST_RUNNER_ENABLED" in response.json()["detail"]
    assert (await turn_row(main_world.turn_id)).run_requested_at is None
    # Access is checked first: an investigation the caller cannot see is just not found.
    assert (await api.post(f"/investigations/{uuid.uuid4()}/start")).status_code == 404


async def test_concurrent_starts_accept_exactly_one(api, rt, main_world):  # noqa: F811
    script(rt, *happy(main_world))
    url = f"/investigations/{main_world.investigation_id}/start"

    first, second = await asyncio.gather(api.post(url), api.post(url))

    assert sorted([first.status_code, second.status_code]) == [202, 409]
    await finished(main_world)
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.COMPLETED
    assert len(await rows(main_world, InvestigationReport)) == 1


async def test_follow_up_through_the_api(api, rt, main_world):  # noqa: F811
    url = f"/investigations/{main_world.investigation_id}"
    assert (await api.post(f"{url}/turns", json={"question": "too early"})).status_code == 409
    script(rt, *happy(main_world))
    await api.post(f"{url}/start")
    await finished(main_world)

    def answer_from_memory(messages: list[Any]) -> Any:
        from tests.investigators.test_followups import follow_up_answer

        return follow_up_answer()(messages)

    script(rt, ScriptedModel(structured=[ANSWER, answer_from_memory]))
    response = await api.post(f"{url}/turns", json={"question": "Which operation?"})
    assert response.status_code == 202 and response.json()["seq"] == 2
    await finished(main_world)

    turns_ = (await api.get(url)).json()["turns"]
    assert [(t["seq"], t["status"], t["decision"]) for t in turns_] == [
        (1, "completed", "investigated"),
        (2, "completed", "reused_evidence"),
    ]
    assert (await api.post(f"{url}/turns", json={"question": ""})).status_code == 422


async def test_failure_is_recorded_and_streamed(api, rt, main_world):  # noqa: F811
    script(rt, ScriptedModel(structured=[RuntimeError("provider down")]))
    await api.post(f"/investigations/{main_world.investigation_id}/start")
    await finished(main_world)

    turn = await turn_row(main_world.turn_id)
    assert (turn.status, turn.error_code) == (TurnStatus.FAILED, "model_error")
    events = sse((await api.get(f"/investigations/{main_world.investigation_id}/events",
                                params={"follow": "false"})).text)  # fmt: skip
    assert events[-1]["event"] == "turn.failed"
    assert events[-1]["payload"]["error_code"] == "model_error"


# --- SSE -----------------------------------------------------------------------------------


async def test_sse_replays_in_order_and_resumes_after_last_event_id(api, rt, main_world):  # noqa: F811
    script(rt, *happy(main_world))
    await api.post(f"/investigations/{main_world.investigation_id}/start")
    await finished(main_world)
    url = f"/investigations/{main_world.investigation_id}/events"

    response = await api.get(url, params={"follow": "false"})

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("retry: ")
    events = sse(response.text)
    ids = [e["id"] for e in events]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    types = [e["event"] for e in events]
    for expected in ("turn.created", "turn.queued", "turn.started", "turn.decision",
                     "round.started", "assignment.started", "hypothesis.recorded",
                     "tool_call.started", "tool_call.finished", "hypothesis.updated",
                     "finding.recorded", "assignment.finished", "round.finished",
                     "report.revised", "turn.completed"):  # fmt: skip
        assert expected in types, expected
    order = [
        "turn.queued",
        "turn.started",
        "turn.decision",
        "tool_call.started",
        "tool_call.finished",
        "finding.recorded",
        "report.revised",
        "turn.completed",
    ]
    # First occurrences in lifecycle order (Main decides again after the round).
    firsts = [types.index(t) for t in order]
    assert firsts == sorted(firsts)
    assert types.count("turn.decision") == 2
    decision = next(e for e in events if e["event"] == "turn.decision")
    assert decision["payload"]["decision"] == "investigate" and decision["payload"]["objectives"]
    finished_call = next(e for e in events if e["event"] == "tool_call.finished")
    assert finished_call["payload"]["status"] == "succeeded"
    assert finished_call["payload"]["evidence"][0].startswith("ev_")

    # Reconnect: Last-Event-ID wins over ?after=.
    middle = ids[len(ids) // 2]
    resumed = sse((await api.get(url, params={"follow": "false", "after": 0},
                                 headers={"Last-Event-ID": str(middle)})).text)  # fmt: skip
    assert [e["id"] for e in resumed] == [i for i in ids if i > middle]
    assert sse((await api.get(url, params={"follow": "false", "after": ids[-1]})).text) == []
    assert (await api.get(f"/investigations/{uuid.uuid4()}/events")).status_code == 404


async def test_progress_events_carry_no_secrets_telemetry_or_reasoning(api, rt, main_world):  # noqa: F811
    script(rt, *happy(main_world))
    await api.post(f"/investigations/{main_world.investigation_id}/start")
    await finished(main_world)

    text = (await api.get(f"/investigations/{main_world.investigation_id}/events",
                          params={"follow": "false"})).text  # fmt: skip

    for forbidden in ("Bearer", "eyJ", "reasoning", '"groups"', '"records"', '"spans"',
                      "p50_ms", "api_key", "token"):  # fmt: skip
        assert forbidden not in text, forbidden
    for event in sse(text):
        assert len(json.dumps(event["payload"])) < 4_000


async def test_sse_follow_streams_new_events_and_stops_on_disconnect(main_world):  # noqa: F811
    from src.api.sse import event_stream

    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    stream = event_stream(main_world.investigation_id, after=0, follow=True,
                          is_disconnected=is_disconnected)  # fmt: skip
    assert (await anext(stream)).startswith("retry:")
    created = await anext(stream)
    assert "event: turn.created" in created

    async with SessionLocal() as s:
        progress.append_progress_event(
            s,
            main_world.investigation_id,
            "turn.decision",
            {"decision": "answer"},
            turn_id=main_world.turn_id,
        )
        await s.commit()
    nxt = await asyncio.wait_for(anext(stream), timeout=5)
    assert "event: turn.decision" in nxt

    disconnected = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=5)


# --- cancellation --------------------------------------------------------------------------


async def test_cancel_during_an_mcp_call_keeps_earlier_history(api, rt, main_world, signoz):  # noqa: F811
    url = f"/investigations/{main_world.investigation_id}"
    script(rt, *happy(main_world))
    await api.post(f"{url}/start")
    await finished(main_world)
    (v1,) = await rows(main_world, InvestigationReport)

    in_signoz, release = asyncio.Event(), asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        in_signoz.set()
        await release.wait()
        return httpx.Response(200, json={})

    signoz(handler=hanging)
    script(rt, ScriptedModel(structured=[investigate(main_world, "traces now"), ANSWER, compose()]),
           local_script(main_world.service))  # fmt: skip
    turn_2 = (await api.post(f"{url}/turns", json={"question": "Errors right now?"})).json()

    await asyncio.wait_for(in_signoz.wait(), timeout=10)
    response = await api.post(f"{url}/cancel")
    release.set()

    assert response.status_code == 200 and response.json()["turn_id"] == turn_2["turn_id"]
    await finished(main_world)
    t2 = await turn_row(uuid.UUID(turn_2["turn_id"]))
    assert t2.status == TurnStatus.CANCELLED
    assignments = sorted(await rows(main_world, InvestigationAssignment), key=lambda a: a.round)
    assert assignments[-1].status == AssignmentStatus.CANCELLED
    async with SessionLocal() as s:
        calls = list(
            await s.scalars(select(ToolCall).where(ToolCall.assignment_id == assignments[-1].id))
        )
        investigation = await s.get(Investigation, main_world.investigation_id)
    assert calls and all(c.status.value in ("cancelled", "rejected") for c in calls)
    # Earlier turn, evidence and report untouched; the investigation is open again.
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.COMPLETED
    (v1_after,) = await rows(main_world, InvestigationReport)
    assert v1_after.report_json == v1.report_json
    assert investigation.status == InvestigationStatus.IDLE
    assert (await api.post(f"{url}/cancel")).status_code == 409


async def test_cancel_of_a_queued_turn_without_a_task(api, rt, main_world):  # noqa: F811
    async with SessionLocal() as s:
        await turns.request_turn_run(s, main_world.turn_id)  # accepted, never started

    response = await api.post(f"/investigations/{main_world.investigation_id}/cancel")

    assert response.status_code == 200
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.CANCELLED
    plan = await runner.recovery_plan(rt.checkpointer)
    assert all(p["turn_id"] != main_world.turn_id for p in plan)  # not restarted later


# --- recovery and duplicate execution ------------------------------------------------------


async def test_recovery_starts_accepted_turns_only(rt, main_world):  # noqa: F811
    script(rt, *happy(main_world))
    plan = await runtime.recover()
    assert all(p["turn_id"] != main_world.turn_id for p in plan)  # created only: left alone
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.PENDING

    async with SessionLocal() as s:
        await turns.request_turn_run(s, main_world.turn_id)  # accepted, then "crash"
    plan = await runtime.recover()

    assert {"turn_id": main_world.turn_id, "action": "start"}.items() <= next(
        p for p in plan if p["turn_id"] == main_world.turn_id
    ).items()
    await finished(main_world)
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.COMPLETED


async def test_recovery_resumes_an_interrupted_turn_from_its_checkpoint(api, rt, main_world):  # noqa: F811
    script(rt, ScriptedModel(structured=[investigate(main_world), ANSWER, Crash()]),
           local_script(main_world.service))  # fmt: skip
    await api.post(f"/investigations/{main_world.investigation_id}/start")
    await finished(main_world)  # the process "died" in compose
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.RUNNING

    script(rt, ScriptedModel(structured=[compose()]))  # Local unscripted: no rerun allowed
    plan = await runtime.recover()

    assert next(p for p in plan if p["turn_id"] == main_world.turn_id)["action"] == "resume"
    await finished(main_world)
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.COMPLETED
    assert len(await rows(main_world, InvestigationAssignment)) == 1
    assert len(await rows(main_world, InvestigationReport)) == 1


async def test_recovery_never_resumes_a_legacy_turn(rt, main_world):  # noqa: F811
    async with SessionLocal() as s:
        await investigations.start_investigation(s, main_world.investigation_id)  # no checkpoint
    script(rt, ScriptedModel())  # any model call would fail the test

    plan = await runtime.recover()

    assert next(p for p in plan if p["turn_id"] == main_world.turn_id)["action"] == "reconcile"
    turn = await turn_row(main_world.turn_id)
    assert (turn.status, turn.error_code) == (TurnStatus.FAILED, turns.LEGACY_ERROR)
    assert runner._tasks.get(main_world.investigation_id) is None


async def test_shutdown_is_not_a_cancel_and_recovery_resumes(rt, main_world, checkpointer):  # noqa: F811
    entered = asyncio.Event()

    async def blocked(_: Any) -> Verdict:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    script(rt, ScriptedModel(structured=[blocked]))
    async with SessionLocal() as s:
        await turns.request_turn_run(s, main_world.turn_id)
    runtime.submit(main_world.investigation_id, main_world.turn_id)
    await entered.wait()

    await runtime.stop()  # backend shutting down mid-assess

    assert (await turn_row(main_world.turn_id)).status == TurnStatus.RUNNING
    (assignment,) = await rows(main_world, InvestigationAssignment)
    assert assignment.status == AssignmentStatus.PENDING

    lifecycle.reset()  # "restart"
    runtime.start(runtime.Runtime(checkpointer, run_kwargs=rt.run_kwargs))
    script(rt, *happy(main_world))
    plan = await runtime.recover()
    assert next(p for p in plan if p["turn_id"] == main_world.turn_id)["action"] == "resume"
    await finished(main_world)
    assert (await turn_row(main_world.turn_id)).status == TurnStatus.COMPLETED


async def test_another_worker_holding_the_lock_prevents_duplicate_execution(api, rt, main_world):  # noqa: F811
    script(rt, ScriptedModel(), ScriptedModel())  # must never be called
    async with investigation_lock(main_world.investigation_id):  # "another worker"
        response = await api.post(f"/investigations/{main_world.investigation_id}/start")
        assert response.status_code == 202
        (outcome,) = await finished(main_world)
        assert isinstance(outcome, runner.RunnerBusy)
        turn = await turn_row(main_world.turn_id)
        assert turn.status == TurnStatus.PENDING and turn.run_requested_at is not None
        # The accepted turn is still owed a run: the dispatcher retries it once the lease
        # is free (or recovery after a restart starts it), exactly once either way.
        script(rt, *happy(main_world))
    await runtime.recover()
    await finished(main_world)
    async with SessionLocal() as s:
        events = list(await s.scalars(select(ProgressEvent.type).where(
            ProgressEvent.investigation_id == main_world.investigation_id)))  # fmt: skip
    assert events.count("turn.started") == 1 and events.count("turn.completed") == 1


async def test_lifespan_is_off_by_default_and_opt_in(checkpointer):
    async with runtime.lifespan(runtime.RunnerSettings(enabled=False)):
        with pytest.raises(runtime.RunnerDisabled):
            runtime.current()
    async with runtime.lifespan(runtime.RunnerSettings(enabled=True, recover_on_startup=False)):
        assert runtime.current() is not None
    with pytest.raises(runtime.RunnerDisabled):
        runtime.current()
    lifecycle.reset()


@pytest.fixture(autouse=True)
async def _threads(checkpointer, main_world):  # noqa: F811
    yield
    await cleanup_turn_threads(checkpointer, main_world)
