"""Local Investigator graph (D014): one running assignment in one environment.

    brief → hypothesize → record_hypotheses → agent ⇄ tools → conclude → record_conclusion
                                                ↑______________| (need_more, bounded)

The model reasons and chooses queries; deterministic code here decides what runs, enforces
budgets, validates every structured answer against the database and persists the result.
Checkpoint state holds messages, counters and validated drafts only. The MCP client and
the model live in the run's context, which is never checkpointed, so no token can reach a
checkpoint. Thread: asg:{assignment_id}.
"""

import asyncio
import dataclasses
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict

import httpx2
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from mcp import Client

from src.db.session import SessionLocal
from src.investigators import context, lifecycle, policy, prompts, records
from src.investigators.checkpoint import thread_id
from src.investigators.llm import local_model
from src.investigators.mcp_tools import McpCallError, call_tool, connect, tool_specs
from src.investigators.refs import ref
from src.investigators.schemas import Conclusion, HypothesisPlan
from src.investigators.structured import audit_line, structured_call, tokens_of
from src.investigators.validation import (
    NOTE_PREFIX,
    TOPICS,
    is_known_unavailable,
    objective_topics,
    sanitize_note,
    speculative_terms,
    unavailable_key,
    validate_conclusion,
    validate_plan,
)
from src.mcp_server import McpSettings
from src.models import (
    AssignmentStatus,
    InvestigationAssignment,
    InvestigationStatus,
    can_use_process_credentials,
)
from src.models.evidence import MAX_SUMMARY_CHARS
from src.services.investigations import finish_assignment
from src.services.progress import append_progress_event
from src.tools.schemas import ToolExecutionResult

logger = logging.getLogger(__name__)

# Same rule the MCP server applies to idempotency keys.
TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
CLOSED_CODES = frozenset({"policy.assignment_not_running", "executor.assignment_closed"})
EXECUTOR_LIMIT_CODES = frozenset(
    {"policy.call_budget_exhausted", "policy.rejection_budget_exhausted"}
)
ASSIGNMENT_CLOSED = "assignment_closed"
# Stops that mean the investigator broke rather than ran out of budget.
FAILURE_STOPS = frozenset({"model_error", "mcp_unavailable"})
NOTE_CHARS = 2_000
ARGS_PREVIEW_CHARS = 200


@dataclass
class LocalContext:
    model: BaseChatModel
    mcp: Client
    tools: list[dict[str, Any]]


class LocalState(TypedDict, total=False):
    assignment_id: str
    # For progress events; absent in checkpoints written before they existed.
    investigation_id: str
    turn_id: str
    objective: str
    # Epoch seconds; assignment.started_at + WALL_CLOCK_SECONDS.
    deadline: float
    messages: Annotated[list[AnyMessage], add_messages]
    hypotheses: list[dict[str, str]]
    model_calls: int
    tool_calls: int
    tokens: int
    mcp_failures: int
    need_more: int
    tool_log: list[str]
    # Queries that produced no usable telemetry; attached to findings as caveats.
    coverage_gaps: list[str]
    # signal|scope keys proven absent (telemetry_available="no"), see unavailable_key().
    unavailable: list[str]
    # Follow-up questions not pursued because their telemetry is known unavailable.
    unresolved_gaps: list[str]
    # Compact, category-only record of what validation repaired or could not repair.
    validation_audit: list[str]
    note: str
    conclusion: dict[str, Any] | None
    problems: list[str]
    stop_reason: str | None


class AssignmentNotRunning(Exception):
    pass


# --- helpers -------------------------------------------------------------------------------


def _budget_stop(state: LocalState) -> str | None:
    if state.get("mcp_failures", 0) >= policy.MAX_MCP_FAILURES:
        return "mcp_unavailable"
    if state.get("model_calls", 0) >= policy.MAX_MODEL_CALLS - policy.RESERVED_CONCLUDE_CALLS:
        return "budget:model_calls"
    if state.get("tokens", 0) >= policy.MAX_TOKENS - policy.RESERVED_CONCLUDE_TOKENS:
        return "budget:tokens"
    if state.get("tool_calls", 0) >= policy.MAX_TOOL_CALLS:
        return "budget:tool_calls"
    if time.time() >= state["deadline"] - policy.CONCLUDE_HEADROOM_SECONDS:
        return "budget:wall_clock"
    return None


async def _is_open(assignment_id: uuid.UUID) -> bool:
    async with SessionLocal() as s:
        loaded = await records.load_assignment(s, assignment_id)
    return (
        loaded is not None
        and loaded[0].status == AssignmentStatus.RUNNING
        and loaded[1].status == InvestigationStatus.INVESTIGATING
    )


def normalize_reply(reply: AIMessage, history: list[AnyMessage]) -> AIMessage:
    """Gives every tool call an ID that is well-formed and unused in this thread, before the
    message is checkpointed: the ID is the executor's idempotency key, and a provider that
    reused one (e.g. `call_0` each turn) would otherwise replay an old result."""
    seen = {
        c.get("id")
        for m in history
        if isinstance(m, AIMessage)
        for c in [*m.tool_calls, *m.invalid_tool_calls]
    }

    def fresh(call_id: str | None) -> str:
        if not call_id or not TOOL_CALL_ID.match(call_id) or call_id in seen:
            call_id = f"tc_{uuid.uuid4().hex}"
        seen.add(call_id)
        return call_id

    content = reply.content.strip() if isinstance(reply.content, str) else reply.content
    return reply.model_copy(
        update={
            "content": content,
            "tool_calls": [{**c, "id": fresh(c.get("id"))} for c in reply.tool_calls],
            "invalid_tool_calls": [
                {**c, "id": fresh(c.get("id"))} for c in reply.invalid_tool_calls
            ],
            # The serializer prefers tool_calls; drop the provider copy so they cannot diverge.
            "additional_kwargs": {
                k: v for k, v in reply.additional_kwargs.items() if k != "tool_calls"
            },
        }
    )


def _annotate(conclusion: dict[str, Any], line: str) -> None:
    for f in conclusion["findings"]:
        note = f"\n{NOTE_PREFIX}{line}"
        f["summary"] = f["summary"][: MAX_SUMMARY_CHARS - len(note)] + note


def _fallback_conclusion(state: LocalState, reason: str) -> dict[str, Any]:
    queries = len(state.get("tool_log", []))
    gaps = state.get("coverage_gaps", [])[: policy.MAX_GAPS_NOTED]
    caveat = (
        f"\n\n{NOTE_PREFIX}Unverified (no usable telemetry): {'; '.join(gaps)}." if gaps else ""
    )
    return {
        "verdict": "sufficient",
        "gaps": [],
        "hypothesis_updates": [],
        "findings": [
            {
                "title": "Inconclusive: no supported conclusion was reached",
                "summary": (
                    f"The Local Investigator stopped ({reason}) before reaching a supported "
                    f"conclusion for: {state.get('objective', '')[:500]}. {queries} queries "
                    "were made; their results are in this assignment's tool calls and evidence."
                    + caveat
                ),
                "status": "inconclusive",
                "confidence": None,
                "hypothesis_id": None,
                "evidence": [],
            }
        ],
    }


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else "(none)"


async def _emit(state: LocalState, events: list[tuple[str, dict[str, Any]]]) -> None:
    """Progress events for this assignment. Best effort: a failure to record activity is
    logged and never fails the investigation."""
    if not events:
        return
    assignment_id = uuid.UUID(state["assignment_id"])
    try:
        async with SessionLocal() as s:
            if state.get("investigation_id") and state.get("turn_id"):
                investigation_id = uuid.UUID(state["investigation_id"])
                turn_id = uuid.UUID(state["turn_id"])
            else:
                assignment = await s.get(InvestigationAssignment, assignment_id)
                assert assignment is not None
                investigation_id, turn_id = assignment.investigation_id, assignment.turn_id
            for event_type, payload in events:
                append_progress_event(
                    s, investigation_id, event_type, payload,
                    turn_id=turn_id, assignment_id=assignment_id,
                )  # fmt: skip
            await s.commit()
    except Exception:
        logger.warning("could not record progress events for %s", assignment_id)


# --- nodes ---------------------------------------------------------------------------------


async def brief(state: LocalState) -> dict[str, Any]:
    assignment_id = uuid.UUID(state["assignment_id"])
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        loaded = await records.load_assignment(s, assignment_id)
        if loaded is None or loaded[0].status != AssignmentStatus.RUNNING:
            return {"stop_reason": ASSIGNMENT_CLOSED}
        assignment, investigation, environment = loaded
        catalog = await records.evidence_catalog(s, assignment, policy.BRIEF_EVIDENCE_ITEMS)
        prior_hypotheses = await records.hypotheses(s, assignment, own=False)
        prior_findings = await records.prior_findings(s, assignment)
    started = assignment.started_at or now
    text = context.brief_text(
        objective=assignment.objective,
        question=investigation.question,
        environment={
            "name": environment.name,
            "type": environment.type.value,
            "scope": records.environment_scope(environment),
        },
        evidence=catalog,
        prior_hypotheses=prior_hypotheses,
        prior_findings=prior_findings,
        now=now,
    )
    return {
        "investigation_id": str(assignment.investigation_id),
        "turn_id": str(assignment.turn_id),
        "objective": assignment.objective,
        "deadline": (started + timedelta(seconds=policy.WALL_CLOCK_SECONDS)).timestamp(),
        "messages": [SystemMessage(prompts.LOCAL_SYSTEM), HumanMessage(text)],
        "hypotheses": [],
        "model_calls": 0,
        "tool_calls": 0,
        "tokens": 0,
        "mcp_failures": 0,
        "need_more": 0,
        "tool_log": [],
        "coverage_gaps": [],
        "unavailable": [],
        "unresolved_gaps": [],
        "validation_audit": [],
        "note": "",
        "conclusion": None,
        "problems": [],
        "stop_reason": None,
    }


async def hypothesize(state: LocalState, runtime: Runtime[LocalContext]) -> dict[str, Any]:
    if stop := _budget_stop(state):
        return {"stop_reason": stop}
    calls_left = policy.MAX_MODEL_CALLS - policy.RESERVED_CONCLUDE_CALLS - state["model_calls"]
    # Names in hypotheses must come from what the model was shown.
    grounding = "\n".join(str(m.content) for m in state["messages"])
    try:
        drafts, problems, calls, tokens, first = await structured_call(
            runtime.context.model,
            HypothesisPlan,
            [*state["messages"], HumanMessage(prompts.HYPOTHESIZE)],
            lambda plan: validate_plan(plan, grounding),
            calls_left,
        )
    except Exception as exc:
        # Investigate without hypotheses rather than fail; the agent still has the objective.
        logger.warning("hypothesize model call failed: %s", type(exc).__name__)
        problems = [f"[model_error] hypothesize failed: {type(exc).__name__}"]
        drafts, calls, tokens, first = [], 1, 0, problems
    audit = audit_line("hypotheses", first, problems)
    return {
        "hypotheses": drafts or [],
        "validation_audit": state["validation_audit"] + ([audit] if audit else []),
        "problems": state["problems"] + problems,
        "model_calls": state["model_calls"] + calls,
        "tokens": state["tokens"] + tokens,
    }


async def record_hypotheses(state: LocalState) -> dict[str, Any]:
    drafts = state.get("hypotheses") or []
    async with SessionLocal() as s, s.begin():
        assignment = await s.get(InvestigationAssignment, uuid.UUID(state["assignment_id"]))
        assert assignment is not None
        ids = await records.record_hypotheses(s, assignment, drafts)
    await _emit(
        state,
        [
            ("hypothesis.recorded", {"ref": ref("hypothesis", hid), "statement": d["statement"]})
            for hid, d in zip(ids, drafts, strict=True)
        ],
    )
    lines = [
        f"{ref('hypothesis', hid)}: {d['statement']} (test: {d['test_plan']})"
        for hid, d in zip(ids, drafts, strict=True)
    ]
    text = _bullets(lines) if lines else "(none recorded; investigate the objective directly)"
    return {"messages": [HumanMessage(prompts.INVESTIGATE.format(hypotheses=text))]}


async def agent(state: LocalState, runtime: Runtime[LocalContext]) -> dict[str, Any]:
    if not await _is_open(uuid.UUID(state["assignment_id"])):
        return {"stop_reason": ASSIGNMENT_CLOSED}
    if stop := _budget_stop(state):
        if stop.startswith("budget:") and state.get("stop_reason") is None:
            await _emit(state, [("budget.exhausted", {"scope": "local", "reason": stop})])
        return {"stop_reason": stop}
    model = runtime.context.model.bind_tools(runtime.context.tools)
    try:
        reply = await model.ainvoke(context.model_view(state["messages"]))
    except Exception as exc:
        logger.warning("agent model call failed: %s", type(exc).__name__)
        return {"stop_reason": "model_error", "model_calls": state["model_calls"] + 1}
    reply = normalize_reply(reply, state["messages"])
    update: dict[str, Any] = {
        "messages": [reply],
        "model_calls": state["model_calls"] + 1,
        "tokens": state["tokens"] + tokens_of(reply),
    }
    if not (reply.tool_calls or reply.invalid_tool_calls):
        update["note"] = str(reply.content)[:NOTE_CHARS]
    return update


def _args_preview(args: Any) -> str:
    text = json.dumps(args, separators=(",", ":"), default=str)
    return text if len(text) <= ARGS_PREVIEW_CHARS else text[:ARGS_PREVIEW_CHARS] + "…"


async def tools(state: LocalState, runtime: Runtime[LocalContext]) -> dict[str, Any]:
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    used, failures, stop = state["tool_calls"], state["mcp_failures"], None
    gaps = list(state.get("coverage_gaps", []))
    unavailable = list(state.get("unavailable", []))
    admitted: list[dict[str, Any]] = []
    for call in last.tool_calls:
        if used < policy.MAX_TOOL_CALLS:
            used += 1
            admitted.append(call)
    semaphore = asyncio.Semaphore(policy.MAX_PARALLEL_TOOL_CALLS)
    await _emit(
        state,
        [
            ("tool_call.started", {"tool": c["name"], "call_id": c["id"],
                                   "arguments": _args_preview(c["args"])})
            for c in admitted
        ],
    )  # fmt: skip

    async def run(call: dict[str, Any]) -> ToolExecutionResult | Exception:
        async with semaphore:
            try:
                async with asyncio.timeout(policy.MCP_CALL_TIMEOUT_SECONDS):
                    return await call_tool(
                        runtime.context.mcp, call["name"], call["args"], call["id"]
                    )
            except (McpCallError, TimeoutError) as exc:
                return exc

    outcomes = dict(
        zip(
            [c["id"] for c in admitted],
            await asyncio.gather(*(run(c) for c in admitted)),
            strict=True,
        )
    )
    now = datetime.now(UTC)
    messages: list[ToolMessage] = []
    log: list[str] = []
    for call in last.tool_calls:
        outcome = outcomes.get(call["id"])
        where = f"{call['name']}({_args_preview(call['args'])})"
        if outcome is None:
            content = "error: tool-call budget exhausted; no query was made. Summarize now."
            digest, is_error = "not run: tool-call budget exhausted", True
        elif isinstance(outcome, Exception):
            failures += 1
            content = (
                f"error: the tool service could not be reached ({type(outcome).__name__}); "
                "no result. It may work if retried once; otherwise summarize what you have."
            )
            digest, is_error = f"transport failure ({type(outcome).__name__})", True
            gap = f"{call['name']}: tool service unreachable"
            if gap not in gaps:
                gaps.append(gap)
        else:
            failures = 0
            content, digest = context.render_result(outcome, call["args"], now)
            gap = context.coverage_gap(outcome, call["args"])
            if gap and gap not in gaps:
                gaps.append(gap)
            if outcome.telemetry_available == "no" and isinstance(call["args"], dict):
                key = unavailable_key(outcome.tool_name, call["args"])
                if key and key not in unavailable:
                    unavailable.append(key)
            is_error = outcome.status != "succeeded"
            code = outcome.error.code if outcome.error else None
            if code in CLOSED_CODES:
                stop = ASSIGNMENT_CLOSED
            elif code in EXECUTOR_LIMIT_CODES:
                stop = stop or "budget:executor"
        log.append(f"{where} → {digest}")
        messages.append(
            ToolMessage(
                content,
                tool_call_id=call["id"],
                name=call["name"],
                status="error" if is_error else "success",
                artifact={"digest": digest},
            )
        )
    for call in last.invalid_tool_calls:
        content = (
            f"error: the arguments for {call.get('name')} were not valid JSON; no query was "
            "made. Send the call again with valid arguments."
        )
        log.append(f"{call.get('name')}(invalid arguments) → not run")
        messages.append(
            ToolMessage(
                content,
                tool_call_id=call["id"],
                status="error",
                artifact={"digest": "not run: invalid arguments"},
            )
        )
    await _emit(
        state,
        [
            ("tool_call.finished", {
                "tool": call["name"],
                "call_id": call["id"],
                "status": str(outcome.status),
                "outcome": str(outcome.outcome) if outcome.outcome else None,
                "telemetry_available": outcome.telemetry_available,
                "evidence": [ref("evidence", e) for e in outcome.evidence_ids],
                "error_code": outcome.error.code if outcome.error else None,
                "replayed": outcome.replayed,
            } if isinstance(outcome, ToolExecutionResult) else {
                "tool": call["name"], "call_id": call["id"], "status": "transport_error",
            })
            for call in admitted
            for outcome in [outcomes[call["id"]]]
        ],
    )  # fmt: skip
    update: dict[str, Any] = {
        "messages": messages,
        "tool_calls": used,
        "mcp_failures": failures,
        "tool_log": state["tool_log"] + log,
        "coverage_gaps": gaps,
        "unavailable": unavailable,
    }
    if stop:
        update["stop_reason"] = stop
    return update


async def conclude(state: LocalState, runtime: Runtime[LocalContext]) -> dict[str, Any]:
    async with SessionLocal() as s:
        assignment = await s.get(InvestigationAssignment, uuid.UUID(state["assignment_id"]))
        assert assignment is not None
        known = dataclasses.replace(
            await records.known_refs(s, assignment),
            context_text="\n".join([state["objective"], *state["tool_log"]]),
            gaps=state.get("coverage_gaps", []),
            unresolved=state.get("unresolved_gaps", []),
            topics=objective_topics(state["objective"]),
        )
        catalog = await records.evidence_catalog(s, assignment, policy.CATALOG_EVIDENCE_ITEMS)
        own_hypotheses = await records.hypotheses(s, assignment, own=True)
    # The note is the agent's own prose: fabricated refs go, unbacked claims are marked.
    note, note_changes = sanitize_note(state.get("note") or "", known)
    unavailable = state.get("unavailable", [])
    stop = state.get("stop_reason")
    may_continue = (
        stop is None and state["need_more"] < policy.MAX_NEED_MORE and _budget_stop(state) is None
    )
    prompt = prompts.CONCLUDE.format(
        objective=state["objective"],
        hypotheses=_bullets(context.hypothesis_lines(own_hypotheses)),
        evidence=_bullets(context.evidence_lines(catalog, datetime.now(UTC))),
        tool_log=_bullets(state["tool_log"]),
        gaps=_bullets(known.gaps),
        note=note or "(none)",
        stop_note=f"\nThe investigation stopped early ({stop}). Conclude with what exists.\n"
        if stop
        else "",
        verdict_rule=prompts.VERDICT_FREE if may_continue else prompts.VERDICT_FINAL,
        subjects=", ".join(TOPICS[t].label for t in known.topics) or "none beyond the objective",
    )
    calls_left = policy.MAX_MODEL_CALLS - state["model_calls"]
    update: dict[str, Any] = {}

    grounding = "\n".join([str(m.content) for m in state["messages"][:2]] + list(state["tool_log"]))

    def pursuable(gaps: list[str]) -> list[str]:
        # Not known to be unavailable, and not chasing a runtime nothing has shown.
        return [
            g
            for g in gaps
            if g.strip()
            and not is_known_unavailable(g, unavailable)
            and not speculative_terms(g, grounding)
        ]

    if state["tokens"] >= policy.MAX_TOKENS or calls_left <= 0:
        problems = ["[budget] model budget exhausted before concluding"]
        clean, calls, tokens, first = None, 0, 0, problems
    else:
        try:
            clean, problems, calls, tokens, first = await structured_call(
                runtime.context.model,
                Conclusion,
                [SystemMessage(prompts.LOCAL_SYSTEM), HumanMessage(prompt)],
                lambda c: validate_conclusion(c, known),
                calls_left,
                # Only a draft that will really loop is discarded, and so not repaired.
                skip_repair=lambda c: (
                    may_continue and c.verdict == "need_more" and bool(pursuable(c.gaps))
                ),
            )
        except Exception as exc:
            logger.warning("conclude model call failed: %s", type(exc).__name__)
            problems = [f"[model_error] conclude failed: {type(exc).__name__}"]
            clean, calls, tokens, first = None, 1, 0, problems
            if stop is None:
                update["stop_reason"] = stop = "model_error"
    update |= {
        "model_calls": state["model_calls"] + calls,
        "tokens": state["tokens"] + tokens,
    }
    if clean is None:
        clean = _fallback_conclusion(state, stop or "no valid conclusion")
    unresolved = list(state.get("unresolved_gaps", []))
    if may_continue and clean["verdict"] == "need_more":
        pending = pursuable(clean["gaps"])
        # Asking again for telemetry already shown absent would repeat the same empty
        # query; keep the question as an unresolved gap instead.
        unresolved += [g for g in clean["gaps"] if g.strip() and g not in pending]
        if pending:
            # The draft is discarded, so are its problems: only the final conclusion reports.
            update["need_more"] = state["need_more"] + 1
            update["unresolved_gaps"] = unresolved
            update["messages"] = [HumanMessage(prompts.NEED_MORE.format(gaps=_bullets(pending)))]
            return update
        if len(unresolved) > len(state.get("unresolved_gaps", [])):
            new = unresolved[len(state.get("unresolved_gaps", [])) :]
            _annotate(clean, "Unresolved (telemetry known unavailable): " + "; ".join(new) + ".")
    update["problems"] = state["problems"] + problems
    update["unresolved_gaps"] = unresolved
    if not clean["findings"]:
        clean["findings"] = _fallback_conclusion(state, "no finding produced")["findings"]

    audit = list(state["validation_audit"])
    if entry := audit_line("conclusion", first, problems):
        audit.append(entry)
    if any(note_changes.values()):
        audit.append(
            f"agent note: {note_changes['fabricated_ref']} fabricated refs removed, "
            f"{note_changes['unsupported_claim']} unsupported claims flagged"
        )
    if audit:
        _annotate(clean, "Validation audit: " + " | ".join(audit))
    update["validation_audit"] = audit
    update["conclusion"] = clean
    return update


async def record_conclusion(state: LocalState) -> dict[str, Any]:
    async with SessionLocal() as s, s.begin():
        assignment = await s.get(InvestigationAssignment, uuid.UUID(state["assignment_id"]))
        assert assignment is not None
        conclusion = state["conclusion"] or {}
        finding_ids = await records.record_conclusion(s, assignment, conclusion)
    await _emit(
        state,
        [
            ("hypothesis.updated", {"ref": ref("hypothesis", uuid.UUID(u["hypothesis_id"])),
                                    "status": u["status"]})
            for u in conclusion.get("hypothesis_updates", [])
            if u["status"] is not None
        ]
        + [
            ("finding.recorded", {"ref": ref("finding", fid), "status": f["status"],
                                  "title": f["title"]})
            for fid, f in zip(finding_ids, conclusion.get("findings", []), strict=True)
        ],
    )  # fmt: skip
    return {}


# --- routing -------------------------------------------------------------------------------


def _after_brief(state: LocalState) -> str:
    return END if state.get("stop_reason") == ASSIGNMENT_CLOSED else "hypothesize"


def _after_agent(state: LocalState) -> str:
    stop = state.get("stop_reason")
    if stop == ASSIGNMENT_CLOSED:
        return END
    last = state["messages"][-1]
    wants_tools = isinstance(last, AIMessage) and (last.tool_calls or last.invalid_tool_calls)
    if stop is None and wants_tools:
        return "tools"
    return "conclude"


def _after_tools(state: LocalState) -> str:
    return END if state.get("stop_reason") == ASSIGNMENT_CLOSED else "agent"


def _after_conclude(state: LocalState) -> str:
    return "record_conclusion" if state.get("conclusion") else "agent"


def build_graph() -> StateGraph:
    graph = StateGraph(LocalState, context_schema=LocalContext)
    graph.add_node("brief", brief)
    graph.add_node("hypothesize", hypothesize)
    graph.add_node("record_hypotheses", record_hypotheses)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_node("conclude", conclude)
    graph.add_node("record_conclusion", record_conclusion)
    graph.set_entry_point("brief")
    graph.add_conditional_edges("brief", _after_brief, ["hypothesize", END])
    graph.add_edge("hypothesize", "record_hypotheses")
    graph.add_edge("record_hypotheses", "agent")
    graph.add_conditional_edges("agent", _after_agent, ["tools", "conclude", END])
    graph.add_conditional_edges("tools", _after_tools, ["agent", END])
    graph.add_conditional_edges("conclude", _after_conclude, ["record_conclusion", "agent"])
    graph.add_edge("record_conclusion", END)
    return graph


# --- runner --------------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalRunResult:
    assignment_id: uuid.UUID
    # Assignment status after the run, as stored.
    status: AssignmentStatus
    stop_reason: str | None
    resumed: bool
    model_calls: int
    tool_calls: int
    tokens: int
    problems: list[str]
    validation_audit: list[str]


def _final_status(values: dict[str, Any]) -> AssignmentStatus | None:
    stop = values.get("stop_reason")
    if stop == ASSIGNMENT_CLOSED:
        # Someone else closed it (cancel, timeout sweep); keep their outcome.
        return None
    if stop in FAILURE_STOPS or not values.get("conclusion"):
        return AssignmentStatus.FAILED
    return AssignmentStatus.COMPLETED


async def _finish(assignment_id: uuid.UUID, status: AssignmentStatus) -> None:
    async with SessionLocal() as s:
        await finish_assignment(s, assignment_id, status)


async def run_local_investigator(
    assignment_id: uuid.UUID,
    checkpointer: BaseCheckpointSaver,
    *,
    model: BaseChatModel | None = None,
    mcp_settings: McpSettings | None = None,
    mcp_transport: httpx2.AsyncBaseTransport | None = None,
) -> LocalRunResult:
    """Runs, or resumes from its checkpoint, the investigator for one running assignment,
    then records the assignment's final status. Re-running a finished thread is a no-op."""
    graph = build_graph().compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id(assignment_id)}}
    snapshot = await graph.aget_state(config)
    resumed = bool(snapshot.values)
    async with SessionLocal() as s:
        loaded = await records.load_assignment(s, assignment_id)
    if loaded is None:
        raise AssignmentNotRunning(f"assignment {assignment_id} does not exist")
    assignment, investigation, environment = loaded

    values: dict[str, Any] = dict(snapshot.values)
    if not (resumed and not snapshot.next):
        if assignment.status != AssignmentStatus.RUNNING:
            raise AssignmentNotRunning(f"assignment {assignment_id} is {assignment.status}")
        # Checked before any model call or MCP token: the token's tenant comes from these
        # rows, and process-wide credentials serve only environments allowed them (D033).
        refusal = (
            "scope_mismatch"
            if investigation.organization_id != environment.organization_id
            else None
            if can_use_process_credentials(environment)
            else "configuration_required"
        )
        if refusal is not None:
            await _finish(assignment_id, AssignmentStatus.FAILED)
            raise AssignmentNotRunning(f"assignment {assignment_id} refused: {refusal}")
        model = model or local_model()
        mcp_settings = mcp_settings or McpSettings()
        started = assignment.started_at or datetime.now(UTC)
        remaining = (
            started + timedelta(seconds=policy.WALL_CLOCK_SECONDS) - datetime.now(UTC)
        ).total_seconds()
        status: AssignmentStatus | None
        deadline = asyncio.timeout(max(remaining, 0))
        try:
            async with (
                deadline,
                connect(
                    assignment,
                    organization_id=investigation.organization_id,
                    url=mcp_settings.url,
                    settings=mcp_settings,
                    transport=mcp_transport,
                ) as client,
            ):
                run_context = LocalContext(model=model, mcp=client, tools=await tool_specs(client))
                await graph.ainvoke(
                    None if resumed else {"assignment_id": str(assignment_id)},
                    config,
                    context=run_context,
                    durability="sync",
                )
            status = _final_status(dict((await graph.aget_state(config)).values))
        except TimeoutError:
            if not deadline.expired():
                logger.exception("local investigator %s failed", assignment_id)
            status = AssignmentStatus.TIMED_OUT if deadline.expired() else AssignmentStatus.FAILED
        except asyncio.CancelledError:
            # Shutdown is not a cancel: leave the assignment running for recovery.
            if not lifecycle.shutting_down():
                await asyncio.shield(_finish(assignment_id, AssignmentStatus.CANCELLED))
            raise
        except Exception:
            # Exception text may carry provider or database details; log it, don't return it.
            logger.exception("local investigator %s failed", assignment_id)
            status = AssignmentStatus.FAILED
        if status is not None:
            await _finish(assignment_id, status)
        values = dict((await graph.aget_state(config)).values)

    async with SessionLocal() as s:
        final = await s.get(InvestigationAssignment, assignment_id)
    return LocalRunResult(
        assignment_id=assignment_id,
        status=final.status if final else assignment.status,
        stop_reason=values.get("stop_reason"),
        resumed=resumed,
        model_calls=values.get("model_calls", 0),
        tool_calls=values.get("tool_calls", 0),
        tokens=values.get("tokens", 0),
        problems=list(values.get("problems", [])),
        validation_audit=list(values.get("validation_audit", [])),
    )
