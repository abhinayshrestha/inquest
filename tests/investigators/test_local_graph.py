import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from src.db.session import SessionLocal
from src.investigators import context as context_module
from src.investigators import local_graph
from src.investigators.checkpoint import thread_id
from src.investigators.mcp_tools import McpCallError
from src.investigators.records import finding_id, hypothesis_id
from src.investigators.refs import ref
from src.investigators.schemas import (
    Conclusion,
    EvidenceLink,
    FindingDraft,
    HypothesisDraft,
    HypothesisPlan,
    HypothesisUpdate,
)
from src.models import (
    AssignmentStatus,
    Finding,
    FindingEvidence,
    FindingStatus,
    Hypothesis,
    HypothesisEvidence,
    HypothesisStatus,
    InvestigationAssignment,
    ToolCallStatus,
)
from tests.investigators.conftest import (
    HYPOTHESIS_REF,
    ScriptedModel,
    refs_in,
    tool_call,
    window,
)
from tests.tools.conftest import EMPTY, World

pytestmark = pytest.mark.usefixtures("investigator_world")

PLAN = HypothesisPlan(
    hypotheses=[
        HypothesisDraft(
            statement="Checkout latency comes from one slow operation",
            test_plan="trace_stats grouped by operation",
        ),
        HypothesisDraft(statement="Host CPU is saturated", test_plan="query_metrics cpu"),
    ]
)


def trace_stats(call_id: str = "call_0", **extra: Any) -> Any:
    return lambda _: tool_call(
        "signoz_trace_stats", {"time_range": window(), "group_by": "operation", **extra}, call_id
    )


def h_ref(world: World, index: int) -> str:
    return ref("hypothesis", hypothesis_id(world.assignment_id, index))


def conclusion(world: World, *, status: str = "confirmed", evidence: str | None = None) -> Any:
    """Concludes from whatever evidence ref the conclude prompt listed."""

    def build(messages: list[Any]) -> Conclusion:
        ev = evidence or refs_in(messages)[0]
        return Conclusion(
            verdict="sufficient",
            gaps=[],
            hypothesis_updates=[
                HypothesisUpdate(
                    hypothesis_ref=h_ref(world, 0),
                    status="supported",
                    confidence=0.7,
                    evidence=[EvidenceLink(evidence_ref=ev, stance="supports")],
                ),
                # Genuinely untested: active, no links.
                HypothesisUpdate(hypothesis_ref=h_ref(world, 1), status="active", evidence=[]),
            ],
            findings=[
                FindingDraft(
                    title="One operation dominates checkout latency",
                    summary="trace_stats shows the slow operation.",
                    status=status,
                    confidence=0.8,
                    hypothesis_ref=h_ref(world, 0),
                    hypothesis_relation="supports",
                    evidence=[EvidenceLink(evidence_ref=ev, stance="supports")],
                )
            ],
        )

    return build


def inconclusive(messages: list[Any]) -> Conclusion:
    """Every listed hypothesis left active: genuine uncertainty, nothing concluded."""
    return Conclusion(
        verdict="sufficient",
        gaps=["no usable telemetry"],
        hypothesis_updates=[
            HypothesisUpdate(hypothesis_ref=h, status="active", evidence=[])
            for h in refs_in(messages, HYPOTHESIS_REF)
        ],
        findings=[
            FindingDraft(
                title="Cause not determined",
                summary="The available telemetry could not answer the objective.",
                status="inconclusive",
                evidence=[],
            )
        ],
    )


async def rows(world: World, model: type) -> list[Any]:
    async with SessionLocal() as s:
        return list(
            await s.scalars(select(model).where(model.investigation_id == world.investigation_id))
        )


async def assignment_status(world: World) -> AssignmentStatus:
    async with SessionLocal() as s:
        return (await s.get(InvestigationAssignment, world.assignment_id)).status  # type: ignore[union-attr]


async def test_full_run_persists_evidence_backed_findings(run, world: World, signoz, checkpointer):
    seen = signoz("trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[trace_stats(), "One operation dominates the latency."],
        structured=[PLAN, conclusion(world)],
    )

    result = await run(world, model)

    assert (result.status, result.stop_reason) == (AssignmentStatus.COMPLETED, None)
    assert (result.model_calls, result.tool_calls, result.tokens) == (4, 1, 480)
    assert result.problems == []

    # The tool went through MCP and the executor, keyed by the framework tool_call_id.
    (call,) = await world.tool_calls()
    assert (call.tool_name, call.status, call.idempotency_key) == (
        "signoz.trace_stats",
        ToolCallStatus.SUCCEEDED,
        "call_0",
    )
    assert len(seen) == 1
    (evidence,) = await world.evidence()
    ev = ref("evidence", evidence.id)

    # The model saw the six MCP tools and a compact result naming the evidence ref.
    assert {t["function"]["name"] for t in model.tools} >= {"signoz_trace_stats"}
    tool_message = model.agent_prompts[1][-1]
    assert f"evidence: {ev}" in tool_message.content

    hypotheses = {h.id: h for h in await rows(world, Hypothesis)}
    assert set(hypotheses) == {hypothesis_id(world.assignment_id, i) for i in (0, 1)}
    first = hypotheses[hypothesis_id(world.assignment_id, 0)]
    assert (first.status, first.confidence) == (HypothesisStatus.SUPPORTED, 0.7)
    assert hypotheses[hypothesis_id(world.assignment_id, 1)].status == HypothesisStatus.ACTIVE
    (link,) = await rows(world, HypothesisEvidence)
    assert (link.hypothesis_id, link.evidence_id) == (first.id, evidence.id)
    assert link.stance == "supports"

    (finding,) = await rows(world, Finding)
    assert (finding.id, finding.status, finding.hypothesis_id) == (
        finding_id(world.assignment_id, 0),
        FindingStatus.CONFIRMED,
        first.id,
    )
    (finding_link,) = await rows(world, FindingEvidence)
    assert finding_link.evidence_id == evidence.id

    # Nothing token-like ever reaches the checkpoint.
    config = {"configurable": {"thread_id": thread_id(world.assignment_id)}}
    dumped = json.dumps([c.checkpoint async for c in checkpointer.alist(config)], default=str)
    assert "Bearer" not in dumped and "eyJ" not in dumped


async def test_invented_refs_and_unsupported_conclusions_never_persist(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    invented = conclusion(world, evidence="ev_deadbeef")
    model = ScriptedModel(
        replies=[trace_stats(), "done"],
        # First answer cites invented evidence; the repair repeats the mistake.
        structured=[PLAN, invented, invented],
    )

    result = await run(world, model)

    assert result.status == AssignmentStatus.COMPLETED
    assert any("ev_deadbeef" in p and "does not exist" in p for p in result.problems)
    # The repair prompt told the model what was wrong.
    assert "does not exist" in model.structured_prompts[2][-1].content
    (finding,) = await rows(world, Finding)
    assert finding.status == FindingStatus.INCONCLUSIVE and finding.confidence is None
    assert await rows(world, FindingEvidence) == []
    assert await rows(world, HypothesisEvidence) == []
    # Status change without evidence is refused; the hypothesis stays active.
    assert {h.status for h in await rows(world, Hypothesis)} == {HypothesisStatus.ACTIVE}


async def test_repair_fixes_a_conclusion(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[trace_stats(), "done"],
        structured=[PLAN, None, conclusion(world)],  # None = output that is not valid JSON
    )

    result = await run(world, model)

    # The repair prompt named the problem; once fixed, nothing is left unresolved.
    assert "did not match the JSON schema" in model.structured_prompts[2][-1].content
    assert result.problems == []
    (finding,) = await rows(world, Finding)
    assert finding.status == FindingStatus.CONFIRMED


async def test_empty_result_is_shown_as_not_evidence_of_health(run, world: World, signoz):
    signoz(EMPTY, "trace_stats_scalar.json")  # query empty, coverage probe has data
    unsupported = Conclusion(
        verdict="sufficient",
        gaps=[],
        hypothesis_updates=[],
        findings=[
            FindingDraft(title="Checkout is healthy", summary="Nothing matched.",
                         status="confirmed", evidence=[])
        ],
    )  # fmt: skip
    model = ScriptedModel(
        replies=[trace_stats(), "nothing matched"],
        structured=[PLAN, unsupported, unsupported],
    )

    await run(world, model)

    content = model.agent_prompts[1][-1].content
    assert "outcome: empty" in content and "NOT evidence of healthy operation" in content
    assert "telemetry for this scope did arrive" in content
    assert await world.evidence() == []
    (finding,) = await rows(world, Finding)
    assert finding.status == FindingStatus.INCONCLUSIVE


async def test_stale_window_is_flagged(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    old = lambda _: tool_call(  # noqa: E731
        "signoz_trace_stats",
        {"time_range": window(ended_minutes_ago=180), "group_by": "operation"},
    )
    model = ScriptedModel(replies=[old, "done"], structured=[PLAN, conclusion(world)])

    await run(world, model)

    assert "describes the past, not the current state" in model.agent_prompts[1][-1].content


async def test_tool_budget_stops_the_loop_and_still_concludes(run, world: World, signoz, tight):
    tight(MAX_TOOL_CALLS=2)
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[trace_stats("a"), trace_stats("b")],
        structured=[PLAN, conclusion(world)],
    )

    result = await run(world, model)

    assert (result.stop_reason, result.tool_calls) == ("budget:tool_calls", 2)
    assert result.status == AssignmentStatus.COMPLETED
    assert len(await world.tool_calls()) == 2
    assert len(await rows(world, Finding)) == 1


async def test_model_call_budget(run, world: World, signoz, tight):
    tight(MAX_MODEL_CALLS=4)  # 2 reserved for conclude: hypothesize + 1 agent step
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(replies=[trace_stats()], structured=[PLAN, conclusion(world)])

    result = await run(world, model)

    assert result.stop_reason == "budget:model_calls"
    assert result.model_calls == 3 and result.status == AssignmentStatus.COMPLETED


async def test_token_budget(run, world: World, signoz, tight):
    tight(MAX_TOKENS=1_000, RESERVED_CONCLUDE_TOKENS=800)
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(replies=[trace_stats()], structured=[PLAN, conclusion(world)])

    result = await run(world, model)

    assert result.stop_reason == "budget:tokens"


async def test_reused_provider_tool_call_ids_are_replaced(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[trace_stats("call_0"), trace_stats("call_0", service=world.service), "done"],
        structured=[PLAN, conclusion(world)],
    )

    await run(world, model)

    keys = [c.idempotency_key for c in await world.tool_calls()]
    assert keys[0] == "call_0" and keys[1].startswith("tc_") and len(set(keys)) == 2


async def test_soft_wall_clock_yields_to_conclude(run, world: World, signoz):
    async with SessionLocal() as s, s.begin():
        assignment = await s.get(InvestigationAssignment, world.assignment_id)
        assignment.started_at = datetime.now(UTC) - timedelta(seconds=250)  # type: ignore[union-attr]
    # Inside the headroom from the start: no hypothesize, no tools, straight to conclude.
    model = ScriptedModel(structured=[inconclusive])

    result = await run(world, model)

    assert result.stop_reason == "budget:wall_clock" and result.tool_calls == 0
    assert result.status == AssignmentStatus.COMPLETED


async def test_hard_wall_clock_times_out(run, world: World):
    async with SessionLocal() as s, s.begin():
        assignment = await s.get(InvestigationAssignment, world.assignment_id)
        assignment.started_at = datetime.now(UTC) - timedelta(seconds=400)  # type: ignore[union-attr]

    result = await run(world, ScriptedModel())

    assert result.status == AssignmentStatus.TIMED_OUT


async def test_model_failure_records_an_inconclusive_finding_and_fails(run, world: World):
    model = ScriptedModel(
        replies=[RuntimeError("provider down")],
        structured=[PLAN, RuntimeError("provider down")],
    )

    result = await run(world, model)

    assert (result.status, result.stop_reason) == (AssignmentStatus.FAILED, "model_error")
    (finding,) = await rows(world, Finding)
    assert finding.status == FindingStatus.INCONCLUSIVE
    assert "model_error" in finding.summary


async def test_mcp_failures_stop_the_investigator(run, world: World, monkeypatch):
    async def unreachable(*_: Any) -> Any:
        raise McpCallError("ConnectError")

    monkeypatch.setattr(local_graph, "call_tool", unreachable)
    model = ScriptedModel(
        replies=[trace_stats("a"), trace_stats("b"), trace_stats("c")],
        structured=[PLAN, inconclusive],
    )

    result = await run(world, model)

    assert (result.status, result.stop_reason) == (AssignmentStatus.FAILED, "mcp_unavailable")
    assert "could not be reached" in model.agent_prompts[1][-1].content


async def test_cancelled_assignment_stops_without_concluding(run, world: World, signoz):
    signoz("trace_stats_scalar.json")

    async def cancel_then_call(_: Any) -> Any:
        await world.set_status(assignment=AssignmentStatus.CANCELLED)
        return tool_call("signoz_trace_stats", {"time_range": window(), "group_by": "operation"})

    model = ScriptedModel(replies=[cancel_then_call], structured=[PLAN])

    result = await run(world, model)

    assert (result.status, result.stop_reason) == (AssignmentStatus.CANCELLED, "assignment_closed")
    (call,) = await world.tool_calls()
    assert call.status == ToolCallStatus.REJECTED
    assert await rows(world, Finding) == []


class Crash(BaseException):
    """Simulates the process dying: not an Exception, so nothing in the graph absorbs it."""


async def test_crash_in_tools_node_resumes_and_replays_the_stored_result(
    run, world: World, signoz, monkeypatch
):
    seen = signoz("trace_stats_scalar.json")
    real_render = context_module.render_result
    crashed = False

    def render_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise Crash
        return real_render(*args, **kwargs)

    monkeypatch.setattr(context_module, "render_result", render_once)
    first = ScriptedModel(replies=[trace_stats()], structured=[PLAN])
    # The MCP client's task group wraps the crash in a group.
    with pytest.raises(BaseExceptionGroup) as crash:
        await run(world, first)
    assert crash.group_contains(Crash)
    assert await assignment_status(world) == AssignmentStatus.RUNNING

    second = ScriptedModel(replies=["done"], structured=[conclusion(world)])
    result = await run(world, second)

    assert result.resumed and result.status == AssignmentStatus.COMPLETED
    # The tools node ran again with the checkpointed tool_call_id: the executor returned the
    # stored result, so SigNoz was queried once and there is one ToolCall.
    assert len(seen) == 1 and len(await world.tool_calls()) == 1
    assert "stored result of an earlier identical call" in second.agent_prompts[0][-1].content
    # Hypotheses were persisted once despite two runs.
    assert len(await rows(world, Hypothesis)) == 2


async def test_finished_thread_is_not_run_again(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    await run(
        world,
        ScriptedModel(replies=[trace_stats(), "done"], structured=[PLAN, conclusion(world)]),
    )

    again = await run(world, ScriptedModel())  # would assert if the model were called

    assert again.resumed and again.status == AssignmentStatus.COMPLETED
    assert len(await rows(world, Finding)) == 1


async def test_assignment_not_running_is_refused(run, world: World):
    await world.set_status(assignment=AssignmentStatus.COMPLETED)

    with pytest.raises(local_graph.AssignmentNotRunning):
        await run(world, ScriptedModel())


async def test_unknown_assignment_is_refused(run, world: World):
    missing = dataclasses.replace(world, assignment_id=uuid.uuid4())

    with pytest.raises(local_graph.AssignmentNotRunning):
        await run(missing, ScriptedModel())


# --- regressions from the first live run ---------------------------------------------------


async def test_need_more_draft_is_not_repaired_and_its_problems_are_discarded(
    run, world: World, signoz
):
    signoz("trace_stats_scalar.json")
    draft = Conclusion(
        verdict="need_more",
        gaps=["error logs for the service"],
        hypothesis_updates=[],
        findings=[
            FindingDraft(title="x", summary="y", status="confirmed",
                         evidence=[EvidenceLink(evidence_ref="ev_deadbeef", stance="supports")])
        ],
    )  # fmt: skip
    model = ScriptedModel(
        replies=[trace_stats(), "first note", trace_stats("call_1", service=world.service), "done"],
        structured=[PLAN, draft, conclusion(world)],
    )

    result = await run(world, model)

    # PLAN, the draft (no repair), the final conclusion: three structured calls.
    assert len(model.structured_prompts) == 3
    assert "gaps were identified" in model.agent_prompts[2][-1].content
    assert result.problems == [] and result.status == AssignmentStatus.COMPLETED


async def test_coverage_gaps_reach_the_prompt_and_the_finding(run, world: World, signoz):
    signoz(EMPTY, EMPTY)  # empty service logs, empty probe: no coverage
    logs = lambda _: tool_call(  # noqa: E731
        "signoz_log_volume",
        {"time_range": window(), "identity": "service", "severity": ["ERROR"]},
    )
    model = ScriptedModel(replies=[logs, "no logs"], structured=[PLAN, inconclusive])

    await run(world, model)

    gap = "log_volume(identity=service, severity=['ERROR']): no telemetry arrived for this scope"
    conclude_prompt = model.structured_prompts[1][-1].content
    assert "Coverage gaps (no usable telemetry" in conclude_prompt and gap in conclude_prompt
    (finding,) = await rows(world, Finding)
    assert f"[Inquest] Unverified (no usable telemetry): {gap}" in finding.summary


async def test_model_gets_compact_tool_schemas(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(replies=["nothing to query"], structured=[PLAN, inconclusive])

    await run(world, model)

    text = json.dumps([t["function"]["parameters"] for t in model.tools])
    assert '"title"' not in text and '{"type": "null"}' not in text


async def test_invented_endpoint_in_a_hypothesis_triggers_repair(run, world: World):
    invented = HypothesisPlan(
        hypotheses=[HypothesisDraft(statement="/heavy-compute is slow", test_plan="trace_stats")]
    )
    model = ScriptedModel(replies=["done"], structured=[invented, PLAN, inconclusive])

    await run(world, model)

    assert "describe it generically" in model.structured_prompts[1][-1].content
    assert len(await rows(world, Hypothesis)) == 2


async def test_fallback_finding_also_carries_coverage_gaps(run, world: World, signoz):
    signoz(EMPTY, EMPTY)
    logs = lambda _: tool_call(  # noqa: E731
        "signoz_log_volume", {"time_range": window(), "identity": "service"}
    )
    model = ScriptedModel(replies=[logs, "done"], structured=[PLAN, RuntimeError("down")])

    result = await run(world, model)

    assert result.status == AssignmentStatus.FAILED
    (finding,) = await rows(world, Finding)
    assert "[Inquest] Unverified (no usable telemetry): log_volume(identity=service)" in (
        finding.summary
    )


# --- final hardening pass ------------------------------------------------------------------


def addressed(messages: list[Any], *, verdict: str = "sufficient", gaps=()) -> Conclusion:
    c = inconclusive(messages)
    return c.model_copy(update={"verdict": verdict, "gaps": list(gaps)})


def service_logs(call_id: str = "call_0") -> Any:
    return lambda _: tool_call(
        "signoz_log_volume",
        {"time_range": window(), "identity": "service", "severity": ["ERROR"]},
        call_id,
    )


async def test_need_more_for_known_unavailable_telemetry_does_not_loop(run, world: World, signoz):
    signoz(EMPTY, EMPTY)  # service logs: empty, probe empty → coverage "no"
    model = ScriptedModel(
        replies=[service_logs(), "no service logs"],
        # Asks again for the very logs just shown absent.
        structured=[PLAN, lambda m: addressed(m, verdict="need_more",
                                              gaps=["error logs for the service"])],
    )  # fmt: skip

    result = await run(world, model)

    assert result.tool_calls == 1 and len(model.agent_prompts) == 2
    assert not any("gaps were identified" in str(p[-1].content) for p in model.agent_prompts)
    (finding,) = await rows(world, Finding)
    assert "Unresolved (telemetry known unavailable): error logs for the service" in (
        finding.summary
    )


async def test_need_more_still_loops_for_pursuable_gaps(run, world: World, signoz):
    signoz(EMPTY, EMPTY, "trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[service_logs(), "note", trace_stats("call_1"), "done"],
        structured=[
            PLAN,
            lambda m: addressed(m, verdict="need_more",
                                gaps=["error logs for the service", "span latency by operation"]),
            addressed,
        ],
    )  # fmt: skip

    await run(world, model)

    need_more = model.agent_prompts[2][-1].content
    assert "span latency by operation" in need_more
    assert "error logs for the service" not in need_more
    (finding,) = await rows(world, Finding)
    assert "Unresolved (telemetry known unavailable): error logs for the service" in (
        finding.summary
    )


async def test_agent_note_is_sanitized_before_conclude(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    note = "Errors: none 【ev_log_volume_empty】. Host resources are healthy."
    model = ScriptedModel(replies=[trace_stats(), note], structured=[PLAN, addressed])

    result = await run(world, model)

    prompt = model.structured_prompts[1][-1].content
    assert "ev_log_volume_empty" not in prompt and "(no such ref)" in prompt
    assert "[UNSUPPORTED: no cited evidence observed this] Host resources are healthy" in prompt
    assert result.validation_audit == [
        "agent note: 1 fabricated refs removed, 1 unsupported claims flagged"
    ]
    (finding,) = await rows(world, Finding)
    assert "[Inquest] Validation audit: agent note: 1 fabricated refs removed" in finding.summary


async def test_repairs_leave_a_compact_audit_without_model_text(run, world: World, signoz):
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[trace_stats(), "done"],
        structured=[PLAN, conclusion(world, evidence="ev_deadbeef"), conclusion(world)],
    )

    result = await run(world, model)

    assert result.problems == []
    assert result.validation_audit == [
        "conclusion: repaired finding_unsupported, status_unsupported, unknown_evidence"
    ]
    (finding,) = await rows(world, Finding)
    audit = finding.summary.split("Validation audit: ")[1]
    assert "ev_deadbeef" not in audit and "unknown_evidence" in audit


async def test_cpu_and_memory_in_the_objective_are_always_answered(run, world: World):
    async with SessionLocal() as s, s.begin():
        assignment = await s.get(InvestigationAssignment, world.assignment_id)
        assignment.objective = "Is host CPU or memory utilization slowing the API?"  # type: ignore[union-attr]
    # The model ignores both topics, twice.
    model = ScriptedModel(replies=["done"], structured=[PLAN, addressed, addressed])

    result = await run(world, model)

    titles = {f.title: f for f in await rows(world, Finding)}
    for topic in ("Host CPU utilization: not assessed", "Host memory utilization: not assessed"):
        assert titles[topic].status == FindingStatus.INCONCLUSIVE
        assert "Not observed" in titles[topic].summary
    assert any("topic_unanswered" in p for p in result.problems)
    assert result.validation_audit == ["conclusion: unresolved topic_unanswered"]


async def test_need_more_chasing_an_unobserved_runtime_does_not_loop(run, world: World, signoz):
    # Phase 4 live run: a gap for `jvm_gc_pause_ms` on a Node.js service cost an extra
    # agent loop and conclude call.
    signoz("trace_stats_scalar.json")
    model = ScriptedModel(
        replies=[trace_stats(), "done"],
        structured=[PLAN, lambda m: addressed(m, verdict="need_more",
                                              gaps=["query jvm_gc_pause_ms for GC pauses"])],
    )  # fmt: skip

    result = await run(world, model)

    assert result.status == AssignmentStatus.COMPLETED
    assert len(model.agent_prompts) == 2 and result.tool_calls == 1
    assert not any("gaps were identified" in str(p[-1].content) for p in model.agent_prompts)
