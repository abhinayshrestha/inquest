"""Main Investigator turns end to end: the real Main and Local graphs, in-process MCP app,
Tool Executor and PostgreSQL checkpoints on the test database. Models are scripted and
SigNoz is mocked. Everything created is deleted afterwards."""

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from sqlalchemy import delete, select

from src.db.session import SessionLocal
from src.investigators import policy
from src.investigators.checkpoint import thread_id as assignment_thread_id
from src.investigators.runner import (
    RunnerBusy,
    cancel_investigation_task,
    recover,
    recovery_plan,
    run_turn,
    start_turn_task,
    turn_thread_id,
)
from src.investigators.schemas import (
    AnswerDraft,
    AssignmentRequest,
    Conclusion,
    EvidenceLink,
    FindingDraft,
    HypothesisDraft,
    HypothesisPlan,
    HypothesisUpdate,
    ReportConclusion,
    ReportDraft,
    Verdict,
)
from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentType,
    Evidence,
    Finding,
    FindingEvidence,
    Hypothesis,
    HypothesisEvidence,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    ProgressEvent,
    ToolCall,
    TurnDecision,
    TurnStatus,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations
from src.services import turns as turn_services
from tests.investigators.conftest import (
    HYPOTHESIS_REF,
    MCP_SETTINGS,
    ScriptedModel,
    refs_in,
    tool_call,
    window,
)

FINDING_REF = re.compile(r"\bfd_[0-9a-f]{8}\b")
PLAN = HypothesisPlan(
    hypotheses=[
        HypothesisDraft(statement="One operation dominates latency", test_plan="trace_stats"),
        HypothesisDraft(statement="Host CPU is saturated", test_plan="query_metrics cpu"),
    ]
)


@dataclass
class MainWorld:
    investigation_id: uuid.UUID
    turn_id: uuid.UUID
    environment_id: uuid.UUID
    environment: str
    service: str


@pytest.fixture
async def main_world(checkpointer) -> AsyncIterator[MainWorld]:
    async with built_main_world(checkpointer) as world:
        yield world


@asynccontextmanager
async def built_main_world(
    checkpointer, environment_fields: dict[str, Any] | None = None
) -> AsyncIterator[MainWorld]:
    """A committed investigation with turn 1; ownerless unless `environment_fields` place
    it in an organization (the investigation follows its environment)."""
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as s:
        environment = Environment(
            name=f"env-{suffix}",
            type=EnvironmentType.LOCAL,
            metadata_json={"signoz": {"hosts": [f"host-{suffix}"], "services": [f"svc-{suffix}"]}},
            **(environment_fields or {}),
        )
        s.add(environment)
        await s.commit()
        investigation = await investigations.create_investigation(
            s,
            InvestigationCreate(
                environment_id=environment.id, title="t", question="Why is checkout slow?"
            ),
        )
        turn_id = await s.scalar(
            select(InvestigationTurn.id).where(
                InvestigationTurn.investigation_id == investigation.id
            )
        )
        world = MainWorld(
            investigation.id, turn_id, environment.id, environment.name, f"svc-{suffix}"
        )
    yield world

    async with SessionLocal() as s:
        assignment_ids = list(
            await s.scalars(
                select(InvestigationAssignment.id).where(
                    InvestigationAssignment.investigation_id == world.investigation_id
                )
            )
        )
        turn_ids = list(
            await s.scalars(
                select(InvestigationTurn.id).where(
                    InvestigationTurn.investigation_id == world.investigation_id
                )
            )
        )
    for a in assignment_ids:
        await checkpointer.adelete_thread(assignment_thread_id(a))
    for t in turn_ids:
        await checkpointer.adelete_thread(turn_thread_id(t))
    async with SessionLocal() as s, s.begin():
        i = world.investigation_id
        for model in (FindingEvidence, HypothesisEvidence, ProgressEvent, InvestigationReport):
            await s.execute(delete(model).where(model.investigation_id == i))
        await s.execute(delete(Finding).where(Finding.investigation_id == i))
        await s.execute(delete(Hypothesis).where(Hypothesis.investigation_id == i))
        await s.execute(delete(Evidence).where(Evidence.investigation_id == i))
        await s.execute(delete(ToolCall).where(ToolCall.assignment_id.in_(assignment_ids)))
        await s.execute(
            delete(InvestigationAssignment).where(InvestigationAssignment.investigation_id == i)
        )
        await s.execute(delete(InvestigationTurn).where(InvestigationTurn.investigation_id == i))
        await s.execute(
            delete(InvestigationEnvironment).where(InvestigationEnvironment.investigation_id == i)
        )
        await s.execute(delete(Investigation).where(Investigation.id == i))
        await s.execute(delete(Environment).where(Environment.id == world.environment_id))


@pytest.fixture
def go(mcp_app, checkpointer):  # noqa: F811
    async def run(
        world: MainWorld,
        main: ScriptedModel,
        local: ScriptedModel | None = None,
        turn_id: uuid.UUID | None = None,
    ):
        return await run_turn(
            turn_id or world.turn_id,
            checkpointer,
            main=main,  # type: ignore[arg-type]
            local=local or ScriptedModel(),  # type: ignore[arg-type]
            mcp_settings=MCP_SETTINGS,
            mcp_transport=httpx2.ASGITransport(app=mcp_app),
        )

    return run


# --- scripted model output -----------------------------------------------------------------


def investigate(world: MainWorld, objective: str = "Find the slowest operation in traces"):
    return Verdict(
        decision="investigate",
        rationale="nothing known yet",
        gaps=["latency by operation"],
        assignments=[AssignmentRequest(environment=world.environment, objective=objective)],
    )


ANSWER = Verdict(decision="answer", rationale="memory suffices", gaps=[], assignments=[])


def local_script(service: str) -> ScriptedModel:
    """One Local round: trace_stats, a note, a conclusion citing what it found."""

    def conclude(messages: list[Any]) -> Conclusion:
        ev = refs_in(messages)[0]
        hy = refs_in(messages, HYPOTHESIS_REF)
        return Conclusion(
            verdict="sufficient",
            gaps=[],
            hypothesis_updates=[
                HypothesisUpdate(hypothesis_ref=hy[0], status="supported",
                                 evidence=[EvidenceLink(evidence_ref=ev, stance="supports")]),
                *[HypothesisUpdate(hypothesis_ref=h, status="active", evidence=[])
                  for h in hy[1:]],
            ],
            findings=[
                FindingDraft(title="One operation dominates checkout latency",
                             summary="trace_stats shows the slow operation.", status="confirmed",
                             hypothesis_ref=hy[0], hypothesis_relation="supports",
                             evidence=[EvidenceLink(evidence_ref=ev, stance="supports")])
            ],
        )  # fmt: skip

    return ScriptedModel(
        replies=[
            lambda _: tool_call(
                "signoz_trace_stats",
                {"time_range": window(), "group_by": "operation", "service": service},
            ),
            "One operation dominates.",
        ],
        structured=[PLAN, conclude],
    )


# Inconclusive: the conclusion is an observation, and naming a cause needs a causal one.
def compose(outcome: str = "inconclusive", status: str = "confirmed", invented=False):
    def build(messages: list[Any]) -> AnswerDraft:
        evidence = refs_in(messages)
        ev = "ev_deadbeef" if invented else (evidence[0] if evidence else None)
        fd = refs_in(messages, FINDING_REF)
        cite = fd[0] if fd else ev or ""
        return AnswerDraft(
            answer_md=f"One operation dominates checkout latency {cite}.",
            report=ReportDraft(
                outcome=outcome,
                summary="Checkout latency comes from one operation.",
                conclusions=[
                    ReportConclusion(statement="One operation dominates checkout latency",
                                     status=status, confidence="high",
                                     supporting=[ev] if ev else [], contradicting=[])
                ],
                timeline=[],
                gaps=[],
                next_checks=[],
            ),
        )  # fmt: skip

    return build


async def turn_row(world: MainWorld) -> InvestigationTurn:
    async with SessionLocal() as s:
        return await s.get(InvestigationTurn, world.turn_id)  # type: ignore[return-value]


async def rows(world: MainWorld, model: type) -> list[Any]:
    async with SessionLocal() as s:
        return list(
            await s.scalars(select(model).where(model.investigation_id == world.investigation_id))
        )


async def event_types(world: MainWorld) -> list[str]:
    return [e.type for e in sorted(await rows(world, ProgressEvent), key=lambda e: e.id)]


# --- the first complete investigation ------------------------------------------------------


async def test_initial_turn_end_to_end(go, main_world: MainWorld, signoz):
    seen = signoz("trace_stats_scalar.json")
    main = ScriptedModel(structured=[investigate(main_world), ANSWER, compose()])

    result = await go(main_world, main, local_script(main_world.service))

    assert (result.status, result.outcome, result.report_version) == (
        TurnStatus.COMPLETED, "inconclusive", 1,
    )  # fmt: skip
    assert (result.rounds, result.model_calls, result.problems) == (1, 3, [])
    turn = await turn_row(main_world)
    assert turn.decision == TurnDecision.INVESTIGATED
    assert turn.answer_json["report_version"] == 1 and FINDING_REF.search(
        turn.answer_json["answer_md"]
    )
    # Main took over the pre-created assignment: one assignment, Main's objective.
    (assignment,) = await rows(main_world, InvestigationAssignment)
    assert (assignment.round, assignment.status) == (1, AssignmentStatus.COMPLETED)
    assert assignment.objective == "Find the slowest operation in traces"
    assert len(seen) == 1 and len(await rows(main_world, Evidence)) == 1
    (report,) = await rows(main_world, InvestigationReport)
    async with SessionLocal() as s:
        investigation = await s.get(Investigation, main_world.investigation_id)
    assert investigation.status == InvestigationStatus.IDLE
    assert investigation.final_result_json == report.report_json
    assert report.report_json["conclusions"][0]["status"] == "confirmed"
    events = await event_types(main_world)
    for expected in (
        "turn.started",
        "round.started",
        "assignment.started",
        "assignment.finished",
        "round.finished",
        "report.revised",
        "turn.completed",
    ):
        assert expected in events  # fmt: skip
    # Main never touched tools or credentials.
    assert main.tools == []
    prompts = json.dumps([[str(m.content) for m in p] for p in main.structured_prompts])
    assert "Bearer" not in prompts and "signoz_" not in prompts and "eyJ" not in prompts


async def test_round_budget_forces_a_limited_answer(go, main_world, signoz, tight):
    tight(MAIN_MAX_ROUNDS=1)
    signoz("trace_stats_scalar.json")
    # Second verdict wants another round; the budget turns it into an answer.
    main = ScriptedModel(
        structured=[investigate(main_world), investigate(main_world, "more"), compose()]
    )

    result = await go(main_world, main, local_script(main_world.service))

    turn = await turn_row(main_world)
    assert result.status == TurnStatus.COMPLETED and result.rounds == 1
    assert turn.answer_json["limited"] is True
    assert "budget is exhausted" in main.structured_prompts[2][-1].content
    assert len(await rows(main_world, InvestigationAssignment)) == 1


async def test_unsupported_claims_are_repaired_then_downgraded(go, main_world, signoz):
    signoz("trace_stats_scalar.json")
    bad = compose(invented=True)
    main = ScriptedModel(structured=[investigate(main_world), ANSWER, bad, bad])

    result = await go(main_world, main, local_script(main_world.service))

    assert result.status == TurnStatus.COMPLETED
    assert "Your previous answer had these problems" in main.structured_prompts[3][-1].content
    (report,) = await rows(main_world, InvestigationReport)
    assert report.report_json["outcome"] == "inconclusive"
    assert report.report_json["conclusions"][0]["status"] == "unverified"
    assert "ev_deadbeef" not in json.dumps(report.report_json)
    assert any("unresolved" in a and "unsupported_confirmed" in a
               for a in result.validation_audit)  # fmt: skip


async def test_assess_failure_fails_the_turn_and_keeps_it_retryable(go, main_world):
    main = ScriptedModel(structured=[RuntimeError("provider down")])

    result = await go(main_world, main)

    assert (result.status, result.error_code) == (TurnStatus.FAILED, "model_error")
    async with SessionLocal() as s:
        investigation = await s.get(Investigation, main_world.investigation_id)
        assert investigation.status == InvestigationStatus.IDLE
        retry = await turn_services.create_follow_up_turn(s, main_world.investigation_id, "retry")
    assert retry.seq == 2


async def test_local_failure_is_isolated_and_main_still_answers(go, main_world):
    failing_local = ScriptedModel(
        replies=[RuntimeError("down")], structured=[PLAN, RuntimeError("down")]
    )
    main = ScriptedModel(
        structured=[investigate(main_world), ANSWER, compose("inconclusive", "unverified")]
    )

    result = await go(main_world, main, failing_local)

    assert result.status == TurnStatus.COMPLETED and result.outcome == "inconclusive"
    (assignment,) = await rows(main_world, InvestigationAssignment)
    assert assignment.status == AssignmentStatus.FAILED
    (finding,) = await rows(main_world, Finding)
    assert finding.status.value == "inconclusive"


# --- restart recovery and replay -----------------------------------------------------------


class Crash(BaseException):
    """The process dying: not an Exception, so nothing in the graph absorbs it."""


async def test_crash_during_compose_resumes_without_rerunning_the_round(go, main_world, signoz):
    seen = signoz("trace_stats_scalar.json")
    first = ScriptedModel(structured=[investigate(main_world), ANSWER, Crash()])
    with pytest.raises(Crash):
        await go(main_world, first, local_script(main_world.service))
    assert (await turn_row(main_world)).status == TurnStatus.RUNNING

    # The Local model is unscripted: re-running the round would fail the test.
    result = await go(main_world, ScriptedModel(structured=[compose()]), ScriptedModel())

    assert result.resumed and result.status == TurnStatus.COMPLETED
    assert len(seen) == 1 and len(await rows(main_world, InvestigationAssignment)) == 1


async def test_persist_replay_after_commit_is_idempotent(go, main_world, signoz, monkeypatch):
    signoz("trace_stats_scalar.json")
    real = turn_services.complete_turn
    crashed = False

    async def commit_then_crash(*args: Any, **kwargs: Any) -> Any:
        nonlocal crashed
        done = await real(*args, **kwargs)
        if not crashed:
            crashed = True
            raise Crash  # after the commit, before the checkpoint
        return done

    monkeypatch.setattr(turn_services, "complete_turn", commit_then_crash)
    with pytest.raises(Crash):
        await go(main_world, ScriptedModel(structured=[investigate(main_world), ANSWER, compose()]),
                 local_script(main_world.service))  # fmt: skip

    result = await go(main_world, ScriptedModel(), ScriptedModel())

    assert result.status == TurnStatus.COMPLETED and result.report_version == 1
    assert len(await rows(main_world, InvestigationReport)) == 1
    assert (await event_types(main_world)).count("turn.completed") == 1


async def test_finished_turn_is_a_no_op(go, main_world, signoz):
    signoz("trace_stats_scalar.json")
    await go(main_world, ScriptedModel(structured=[investigate(main_world), ANSWER, compose()]),
             local_script(main_world.service))  # fmt: skip
    again = await go(main_world, ScriptedModel(), ScriptedModel())
    assert again.status == TurnStatus.COMPLETED and not again.resumed


async def test_legacy_running_turn_is_reconciled_not_resumed(go, main_world, checkpointer):
    async with SessionLocal() as s:
        await investigations.start_investigation(s, main_world.investigation_id)  # legacy path
    plan = await recovery_plan(checkpointer)
    assert {"turn_id": main_world.turn_id, "action": "reconcile"}.items() <= next(
        p for p in plan if p["turn_id"] == main_world.turn_id
    ).items()

    result = await go(main_world, ScriptedModel(), ScriptedModel())  # no model may be called

    assert result.reconciled and result.status == TurnStatus.FAILED
    assert result.error_code == turn_services.LEGACY_ERROR
    (assignment,) = await rows(main_world, InvestigationAssignment)
    assert assignment.status == AssignmentStatus.TIMED_OUT


async def test_recover_reconciles_legacy_turns(main_world, checkpointer):
    async with SessionLocal() as s:
        await investigations.start_investigation(s, main_world.investigation_id)
    plan = await recover(checkpointer)
    mine = next(p for p in plan if p["turn_id"] == main_world.turn_id)
    assert mine["action"] == "reconcile" and mine["done"] is True
    assert (await turn_row(main_world)).error_code == turn_services.LEGACY_ERROR


async def test_turn_wall_clock(go, main_world, tight):
    tight(TURN_WALL_CLOCK_SECONDS=0)
    result = await go(main_world, ScriptedModel(), ScriptedModel())
    assert (result.status, result.error_code) == (TurnStatus.CANCELLED, "turn_timeout")


# --- concurrency and cancellation ----------------------------------------------------------


async def test_a_second_runner_for_the_same_investigation_is_refused(go, main_world):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(_: Any) -> Verdict:
        entered.set()
        await release.wait()
        raise RuntimeError("stop here")

    first = asyncio.create_task(go(main_world, ScriptedModel(structured=[slow])))
    await entered.wait()
    with pytest.raises(RunnerBusy):
        await go(main_world, ScriptedModel())
    release.set()
    result = await first
    assert result.status == TurnStatus.FAILED  # the first runner finished normally


async def test_cancelling_a_running_turn(main_world, mcp_app, checkpointer):
    entered = asyncio.Event()

    async def forever(_: Any) -> Verdict:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = start_turn_task(
        main_world.investigation_id,
        main_world.turn_id,
        checkpointer,
        main=ScriptedModel(structured=[forever]),
        local=ScriptedModel(),
        mcp_settings=MCP_SETTINGS,
        mcp_transport=httpx2.ASGITransport(app=mcp_app),
    )
    await entered.wait()
    with pytest.raises(RunnerBusy):
        start_turn_task(main_world.investigation_id, main_world.turn_id, checkpointer)

    assert await cancel_investigation_task(main_world.investigation_id) is True
    assert task.cancelled()
    turn = await turn_row(main_world)
    assert turn.status == TurnStatus.CANCELLED
    (assignment,) = await rows(main_world, InvestigationAssignment)
    assert assignment.status == AssignmentStatus.CANCELLED


def test_main_budget_constants_are_sane():
    assert policy.MAIN_MAX_ROUNDS >= 1
    assert policy.TURN_WALL_CLOCK_SECONDS > policy.WALL_CLOCK_SECONDS * policy.MAIN_MAX_ROUNDS


async def test_compose_hitting_the_token_limit_is_repaired_not_failed(go, main_world, signoz):
    from openai import LengthFinishReasonError

    signoz("trace_stats_scalar.json")
    too_long = LengthFinishReasonError.__new__(LengthFinishReasonError)
    main = ScriptedModel(structured=[investigate(main_world), ANSWER, too_long, compose()])

    result = await go(main_world, main, local_script(main_world.service))

    assert result.status == TurnStatus.COMPLETED and result.report_version == 1
    assert "exceeded the output limit" in main.structured_prompts[3][-1].content


def test_main_model_gets_a_larger_output_limit():
    from src.investigators.llm import NebiusSettings, chat_model

    settings = NebiusSettings(api_key="k", main_model="m", local_model="l")
    assert chat_model("m", settings).max_tokens == settings.max_output_tokens
    assert chat_model("m", settings, max_tokens=settings.main_max_output_tokens).max_tokens == (
        16_384
    )


async def test_structured_output_methods(go, main_world, signoz):
    signoz("trace_stats_scalar.json")
    main = ScriptedModel(structured=[investigate(main_world), ANSWER, compose()])
    local = local_script(main_world.service)

    await go(main_world, main, local)

    # Compose avoids json_schema guided decoding (whitespace runaway on long reports).
    assert set(main.methods) == {("Verdict", "json_schema"), ("AnswerDraft", "function_calling")}
    assert {m for _, m in local.methods} == {"json_schema"}  # Local unchanged
