"""Phase 4: follow-up turns on an idle investigation, end to end (real Main and Local
graphs, in-process MCP app, Tool Executor, PostgreSQL checkpoints; scripted models,
mocked SigNoz). Turn 1 is run first in every test, then the follow-up."""

import asyncio
import uuid
from typing import Any

import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from src.db.session import SessionLocal
from src.investigators.runner import cancel_investigation_task, start_turn_task
from src.investigators.schemas import AnswerDraft, ReportConclusion, ReportDraft, Verdict
from src.models import (
    AssignmentStatus,
    Evidence,
    Investigation,
    InvestigationAssignment,
    InvestigationReport,
    InvestigationTurn,
    ToolCall,
    TurnDecision,
    TurnStatus,
)
from src.services import turns as turn_services
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel, refs_in
from tests.investigators.test_main_graph import (  # noqa: F401
    ANSWER,
    FINDING_REF,
    Crash,
    MainWorld,
    compose,
    go,
    investigate,
    local_script,
    main_world,
    rows,
)
from tests.mcp_server.conftest import signoz  # noqa: F401

CONCLUSION = "One operation dominates checkout latency"


@pytest.fixture(autouse=True)
def mocked_signoz(signoz):  # noqa: F811
    # Every test runs turn 1 against mocked SigNoz; tests that count requests call
    # signoz() again themselves.
    signoz("trace_stats_scalar.json")


async def first_turn(go, world: MainWorld) -> None:  # noqa: F811
    result = await go(
        world,
        ScriptedModel(structured=[investigate(world), ANSWER, compose()]),
        local_script(world.service),
    )
    assert result.status == TurnStatus.COMPLETED and result.report_version == 1


async def ask(world: MainWorld, question: str) -> uuid.UUID:
    async with SessionLocal() as s:
        return (await turn_services.create_follow_up_turn(s, world.investigation_id, question)).id


async def turn(turn_id: uuid.UUID) -> InvestigationTurn:
    async with SessionLocal() as s:
        return await s.get(InvestigationTurn, turn_id)  # type: ignore[return-value]


async def tool_calls(world: MainWorld) -> int:
    async with SessionLocal() as s:
        ids = select(InvestigationAssignment.id).where(
            InvestigationAssignment.investigation_id == world.investigation_id
        )
        return len(
            list(await s.scalars(select(ToolCall.id).where(ToolCall.assignment_id.in_(ids))))
        )


async def reports(world: MainWorld) -> list[InvestigationReport]:
    return sorted(await rows(world, InvestigationReport), key=lambda r: r.version)


def follow_up_answer(*, report: Any = None, invented: bool = False):
    """Answers from memory, citing a finding from the prompt; report optional."""

    def build(messages: list[Any]) -> AnswerDraft:
        cite = "fd_deadbeef" if invented else refs_in(messages, FINDING_REF)[0]
        return AnswerDraft(
            answer_md=f"Yes: one operation dominates the latency {cite}.",
            report=report(messages) if callable(report) else report,
        )

    return build


def revised_report(status: str = "probable", outcome: str = "inconclusive"):
    def build(messages: list[Any]) -> ReportDraft:
        ev = refs_in(messages)
        return ReportDraft(
            outcome=outcome,
            summary="Revised after the follow-up round.",
            conclusions=[
                ReportConclusion(
                    statement=CONCLUSION,
                    status=status,
                    confidence="medium",
                    supporting=[ev[0]],
                    contradicting=[],
                )
            ],  # fmt: skip
            timeline=[],
            gaps=[],
            next_checks=[],
            changes=[],
        )

    return build


# --- answered from memory ------------------------------------------------------------------


async def test_follow_up_answered_entirely_from_existing_evidence(go, main_world):  # noqa: F811
    await first_turn(go, main_world)
    before_calls, (v1,) = await tool_calls(main_world), await reports(main_world)
    turn_2 = await ask(main_world, "Which operation dominates the latency?")

    main = ScriptedModel(structured=[ANSWER, follow_up_answer()])
    result = await go(main_world, main, ScriptedModel(), turn_id=turn_2)

    assert result.status == TurnStatus.COMPLETED and result.rounds == 0
    t2 = await turn(turn_2)
    assert t2.decision == TurnDecision.REUSED_EVIDENCE
    assert await tool_calls(main_world) == before_calls  # zero new MCP ToolCalls
    assert len(await rows(main_world, InvestigationAssignment)) == 1
    assert [r.version for r in await reports(main_world)] == [1]
    assert (t2.answer_json["report_version"], t2.answer_json["current_report_version"]) == (
        None,
        1,
    )
    assert FINDING_REF.search(t2.answer_json["answer_md"])
    # Main saw the earlier turn and the current report.
    prompt = main.structured_prompts[0][-1].content
    assert "Turn 1 Q: Why is checkout slow?" in prompt and "Version 1, outcome" in prompt
    # Turn 1 and its report are untouched.
    assert (await reports(main_world))[0].report_json == v1.report_json
    assert (await turn(main_world.turn_id)).status == TurnStatus.COMPLETED


async def test_unchanged_report_is_not_a_new_version(go, main_world):  # noqa: F811
    await first_turn(go, main_world)
    turn_2 = await ask(main_world, "Summarize the cause again.")
    same = revised_report(status="confirmed", outcome="inconclusive")

    await go(main_world, ScriptedModel(structured=[ANSWER, follow_up_answer(report=same)]),
             ScriptedModel(), turn_id=turn_2)  # fmt: skip

    assert [r.version for r in await reports(main_world)] == [1]
    t2 = await turn(turn_2)
    assert t2.answer_json["report_diff"]["material"] is False
    assert t2.answer_json["report_version"] is None


# --- new diagnostic work -------------------------------------------------------------------


async def test_follow_up_needing_new_queries_dispatches_a_new_round(go, main_world, signoz):  # noqa: F811
    seen = signoz("trace_stats_scalar.json")
    await first_turn(go, main_world)
    before_calls = await tool_calls(main_world)
    (v1,) = await reports(main_world)
    turn_2 = await ask(main_world, "Are there errors in the last 30 minutes?")

    main = ScriptedModel(
        structured=[investigate(main_world, "Check the latest traces"), ANSWER,
                    follow_up_answer(report=revised_report())],
    )  # fmt: skip
    result = await go(main_world, main, local_script(main_world.service), turn_id=turn_2)

    assert result.status == TurnStatus.COMPLETED and result.rounds == 1
    assert (await turn(turn_2)).decision == TurnDecision.INVESTIGATED
    assignments = sorted(await rows(main_world, InvestigationAssignment), key=lambda a: a.round)
    assert [(a.round, a.turn_id, a.status) for a in assignments] == [
        (1, main_world.turn_id, AssignmentStatus.COMPLETED),
        (2, turn_2, AssignmentStatus.COMPLETED),
    ]
    assert "Time window (UTC): last 30 minutes" in assignments[1].objective
    assert await tool_calls(main_world) == before_calls + 1 and len(seen) == 2
    assert len(await rows(main_world, Evidence)) == 2
    # The earlier evidence was in the new Local brief, so it could be reused.
    # Report: v2 with the revised conclusion; v1 unchanged and still accessible.
    v1_after, v2 = await reports(main_world)
    assert v1_after.report_json == v1.report_json
    assert v2.turn_id == turn_2 and v2.report_json["diff"]["status_changed"] == [
        ["one-operation-dominates-checkout-latency", "confirmed", "probable"]
    ]
    async with SessionLocal() as s:
        investigation = await s.get(Investigation, main_world.investigation_id)
    assert investigation.final_result_json == v2.report_json
    t2 = await turn(turn_2)
    assert (t2.answer_json["report_version"], t2.answer_json["current_report_version"]) == (2, 1)


async def test_stale_evidence_forces_new_queries_for_a_now_question(go, main_world, signoz):  # noqa: F811
    signoz("trace_stats_scalar.json")
    await first_turn(go, main_world)
    # Age turn 1's evidence: its window ended two hours ago.
    async with SessionLocal() as s, s.begin():
        await s.execute(
            text(
                "UPDATE evidence SET data_json = jsonb_set(data_json, '{time_range}', "
                "jsonb_build_object('start', to_char(now() - interval '150 minutes', "
                "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), 'end', to_char(now() - interval '120 "
                'minutes\', \'YYYY-MM-DD"T"HH24:MI:SS"Z"\'))) WHERE investigation_id = :i'
            ),
            {"i": main_world.investigation_id},
        )
    turn_2 = await ask(main_world, "Is it still slow right now?")
    # Main tries to answer from the old evidence twice (first try and its repair).
    main = ScriptedModel(structured=[ANSWER, ANSWER, ANSWER, follow_up_answer()])

    result = await go(main_world, main, local_script(main_world.service), turn_id=turn_2)

    assert result.rounds == 1 and any("stale_memory" in p for p in result.problems)
    assessed = main.structured_prompts[0][-1].content
    assert "Time window the question asks about: now" in assessed
    assert "Evidence covering that window: no" in assessed
    (_, new) = sorted(await rows(main_world, InvestigationAssignment), key=lambda a: a.round)
    assert new.turn_id == turn_2 and "Time window (UTC): now" in new.objective


# --- validation, conflicts, recovery -------------------------------------------------------


async def test_follow_up_citations_are_validated(go, main_world):  # noqa: F811
    await first_turn(go, main_world)
    turn_2 = await ask(main_world, "Which operation?")
    bad = follow_up_answer(invented=True)

    result = await go(main_world, ScriptedModel(structured=[ANSWER, bad, bad]), ScriptedModel(),
                      turn_id=turn_2)  # fmt: skip

    t2 = await turn(turn_2)
    assert result.status == TurnStatus.COMPLETED
    assert "fd_deadbeef" not in t2.answer_json["answer_md"]
    assert any("unknown_ref" in p for p in result.problems)
    assert any("uncited_answer" in p for p in result.problems)


async def test_concurrent_follow_ups_are_refused(go, main_world, mcp_app, checkpointer):  # noqa: F811
    await first_turn(go, main_world)
    turn_2 = await ask(main_world, "first follow-up")
    with pytest.raises(turn_services.TurnConflict):
        await ask(main_world, "second follow-up while the first is pending")

    entered = asyncio.Event()

    async def blocked(_: Any) -> Verdict:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = start_turn_task(
        main_world.investigation_id, turn_2, checkpointer,
        main=ScriptedModel(structured=[blocked]), local=ScriptedModel(),
        mcp_settings=MCP_SETTINGS, mcp_transport=httpx2.ASGITransport(app=mcp_app),
    )  # fmt: skip
    await entered.wait()
    with pytest.raises(turn_services.TurnConflict):
        await ask(main_world, "another one while the first is running")
    await cancel_investigation_task(main_world.investigation_id)
    assert task.cancelled()


async def test_follow_up_resumes_after_a_crash(go, main_world, signoz):  # noqa: F811
    seen = signoz("trace_stats_scalar.json")
    await first_turn(go, main_world)
    turn_2 = await ask(main_world, "Are there errors in the last 30 minutes?")
    with pytest.raises(Crash):
        await go(main_world,
                 ScriptedModel(structured=[investigate(main_world, "traces"), ANSWER, Crash()]),
                 local_script(main_world.service), turn_id=turn_2)  # fmt: skip
    assert (await turn(turn_2)).status == TurnStatus.RUNNING

    result = await go(main_world, ScriptedModel(structured=[follow_up_answer()]), ScriptedModel(),
                      turn_id=turn_2)  # fmt: skip

    assert result.resumed and result.status == TurnStatus.COMPLETED
    assert len(seen) == 2 and len(await rows(main_world, InvestigationAssignment)) == 2
    assert [r.version for r in await reports(main_world)] == [1]


async def test_failed_follow_up_keeps_history_and_can_be_retried(go, main_world):  # noqa: F811
    await first_turn(go, main_world)
    evidence = len(await rows(main_world, Evidence))
    (v1,) = await reports(main_world)
    turn_2 = await ask(main_world, "Which operation?")

    result = await go(main_world, ScriptedModel(structured=[RuntimeError("down")]),
                      ScriptedModel(), turn_id=turn_2)  # fmt: skip

    assert (result.status, result.error_code) == (TurnStatus.FAILED, "model_error")
    assert (await turn(main_world.turn_id)).status == TurnStatus.COMPLETED
    assert len(await rows(main_world, Evidence)) == evidence
    async with SessionLocal() as s:
        investigation = await s.get(Investigation, main_world.investigation_id)
    assert investigation.final_result_json == v1.report_json
    retry = await ask(main_world, "Which operation? (retry)")
    assert (await turn(retry)).seq == 3


async def test_cancelled_follow_up_keeps_history(main_world, go, mcp_app, checkpointer):  # noqa: F811
    await first_turn(go, main_world)
    turn_2 = await ask(main_world, "Which operation?")
    entered = asyncio.Event()

    async def blocked(_: Any) -> Verdict:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    start_turn_task(
        main_world.investigation_id, turn_2, checkpointer,
        main=ScriptedModel(structured=[blocked]), local=ScriptedModel(),
        mcp_settings=MCP_SETTINGS, mcp_transport=httpx2.ASGITransport(app=mcp_app),
    )  # fmt: skip
    await entered.wait()
    await cancel_investigation_task(main_world.investigation_id)

    assert (await turn(turn_2)).status == TurnStatus.CANCELLED
    assert (await turn(main_world.turn_id)).status == TurnStatus.COMPLETED
    assert [r.version for r in await reports(main_world)] == [1]


# --- API -----------------------------------------------------------------------------------


async def test_api_returns_turns_and_report_references(go, main_world, signoz, client: AsyncClient):  # noqa: F811
    signoz("trace_stats_scalar.json")
    await first_turn(go, main_world)
    turn_2 = await ask(main_world, "Are there errors in the last 30 minutes?")
    await go(main_world,
             ScriptedModel(structured=[investigate(main_world, "traces"), ANSWER,
                                       follow_up_answer(report=revised_report())]),
             local_script(main_world.service), turn_id=turn_2)  # fmt: skip

    body = (await client.get(f"/investigations/{main_world.investigation_id}")).json()

    assert [(t["seq"], t["status"]) for t in body["turns"]] == [(1, "completed"), (2, "completed")]
    assert body["turns"][1]["answer_json"]["report_version"] == 2
    assert [(r["version"], r["turn_id"]) for r in body["reports"]] == [
        (1, str(main_world.turn_id)),
        (2, str(turn_2)),
    ]
    assert body["status"] == "idle"
