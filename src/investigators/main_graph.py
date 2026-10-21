"""Main Investigator graph (D014): one turn of an investigation.

    start → assess ─answer/clarify→ compose → validate ─ok→ persist
              │ investigate          ↑         │ repair
              ↓                      └─────────┘
          dispatch → run_round → assess
    (assess or compose failing → fail)

Main reasons over investigation memory in PostgreSQL only. It never calls tools and never
holds an assignment token: run_round hands assignments to run_local_investigator, which
mints its own. Deterministic nodes own every state change through the turn services;
persist writes answer, report revision, snapshot and idle in one idempotent transaction.
An answer that is still unusable after its repair is replaced by a deterministic fallback
(validated findings only) or, when there is nothing validated to list, fails the turn;
a follow-up's report revision that is still wrong is refused, so the current one stands.
Thread: turn:{turn_id}.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import httpx2
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime
from openai import LengthFinishReasonError

from src.db.session import SessionLocal
from src.investigators import context, policy, prompts, records
from src.investigators.local_graph import AssignmentNotRunning, run_local_investigator
from src.investigators.main_validation import (
    ANSWER_BLOCKING,
    Memory,
    build_memory,
    conclusion_as_of,
    fallback_answer,
    merge_report,
    report_diff,
    substantive,
    validate_answer,
    validate_verdict,
)
from src.investigators.schemas import AnswerDraft, Verdict
from src.investigators.structured import audit_line, structured_call, tokens_of
from src.investigators.validation import NOTE_PREFIX, problem_codes
from src.investigators.windows import Window, covers, requested_window
from src.mcp_server import McpSettings
from src.models import InvestigationTurn, TurnDecision, TurnKind, TurnStatus
from src.services import turns
from src.services.progress import append_progress_event

logger = logging.getLogger(__name__)

DECISIONS = {
    "answer": TurnDecision.REUSED_EVIDENCE,
    "clarify": TurnDecision.CLARIFICATION,
}


@dataclass
class MainContext:
    """Never checkpointed. Main's own nodes use only main_model; the rest is passed through
    to the Local Investigators."""

    main_model: BaseChatModel
    local_model: BaseChatModel
    checkpointer: BaseCheckpointSaver
    mcp_settings: McpSettings | None = None
    mcp_transport: httpx2.AsyncBaseTransport | None = None


class MainState(TypedDict, total=False):
    investigation_id: str
    turn_id: str
    question: str
    initial: bool
    seq: int
    # The time window the question states (ISO strings), if any.
    window: dict[str, str] | None
    deadline: float
    rounds: int
    verdict: dict[str, Any] | None
    assignment_ids: list[str]
    # Raw model draft awaiting validation, and the problems that sent it back.
    draft: dict[str, Any] | None
    repair_problems: list[str]
    repairs: int
    first_problems: list[str]
    # Validated answer and report, ready to persist.
    answer: dict[str, Any] | None
    model_calls: int
    tokens: int
    problems: list[str]
    validation_audit: list[str]
    limited: bool
    stop_reason: str | None
    # Structure of compose outputs that were unparseable or had no prose, for debugging
    # (see _compose_diagnostics); never their text beyond a short head, never reasoning.
    compose_diagnostics: list[dict[str, Any]]


def _budget_left(state: MainState) -> int:
    if state["tokens"] >= policy.MAIN_MAX_TOKENS:
        return 0
    return max(policy.MAIN_MAX_MODEL_CALLS - state["model_calls"], 0)


async def _emit(state: MainState, event_type: str, payload: dict[str, Any]) -> None:
    """Best effort, like the Local Investigator's: never fails the turn."""
    try:
        async with SessionLocal() as s:
            append_progress_event(
                s, uuid.UUID(state["investigation_id"]), event_type, payload,
                turn_id=uuid.UUID(state["turn_id"]),
            )  # fmt: skip
            await s.commit()
    except Exception:
        logger.warning("could not record progress event %s", event_type, exc_info=True)


async def _memory(investigation_id: str) -> dict[str, Any]:
    async with SessionLocal() as s:
        return await records.investigation_memory(s, uuid.UUID(investigation_id))


async def _conversation(state: MainState) -> dict[str, Any]:
    async with SessionLocal() as s:
        return await records.conversation(s, uuid.UUID(state["investigation_id"]), state["seq"])


def _window(state: MainState) -> Window | None:
    w = state.get("window")
    if not w:
        return None
    return Window(datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]), w["label"])


# --- nodes ---------------------------------------------------------------------------------


async def start(state: MainState) -> dict[str, Any]:
    """pending → running (investigation → correlating). The runner never invokes this
    graph for a turn that is already running without a checkpoint."""
    turn_id = uuid.UUID(state["turn_id"])
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, turn_id)
        assert turn is not None
        if turn.status == TurnStatus.PENDING:
            turn = await turns.start_turn(s, turn_id)
        started = turn.started_at or datetime.now(UTC)
        investigation_id, question, kind, seq = (
            turn.investigation_id, turn.question, turn.kind, turn.seq,
        )  # fmt: skip
    # Fixed when the turn starts, so a resumed turn keeps the window it was asked about.
    window = requested_window(question, started)
    return {
        "investigation_id": str(investigation_id),
        "question": question,
        "initial": kind == TurnKind.INITIAL,
        "seq": seq,
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "label": window.label,
        }
        if window
        else None,  # fmt: skip
        "deadline": (started + timedelta(seconds=policy.TURN_WALL_CLOCK_SECONDS)).timestamp(),
        "rounds": 0,
        "verdict": None,
        "assignment_ids": [],
        "draft": None,
        "repair_problems": [],
        "repairs": 0,
        "first_problems": [],
        "answer": None,
        "model_calls": 0,
        "tokens": 0,
        "problems": [],
        "validation_audit": [],
        "limited": False,
        "stop_reason": None,
    }


async def assess(state: MainState, runtime: Runtime[MainContext]) -> dict[str, Any]:
    memory = await _memory(state["investigation_id"])
    known = build_memory(memory)
    conversation = await _conversation(state)
    window = _window(state)
    covered = window is None or covers(
        window, [e["window"] for e in memory["evidence"] if e["window"]]
    )
    if _budget_left(state) == 0:
        # No budget to reason: answer with what exists, flagged as limited.
        return {
            "verdict": {"decision": "answer", "assignments": [], "gaps": [], "limited": True},
            "limited": True,
        }
    prompt = prompts.ASSESS.format(
        question=state["question"],
        conversation=context.conversation_text(conversation),
        report=context.report_text(conversation["report"]),
        window=window.text() if window else "none stated",
        coverage="n/a" if window is None else ("yes" if covered else "no"),
        rounds=state["rounds"],
        max_rounds=policy.MAIN_MAX_ROUNDS,
        environments="\n".join(f"- {line}" for line in context.environment_lines(memory)),
        memory=context.memory_text(memory, policy.MEMORY_CHARS),
    )
    try:
        verdict, problems, calls, tokens, first = await structured_call(
            runtime.context.main_model,
            Verdict,
            [SystemMessage(prompts.MAIN_SYSTEM), HumanMessage(prompt)],
            lambda v: validate_verdict(
                v,
                known,
                question=state["question"],
                rounds=state["rounds"],
                window=window,
                covered=covered,
            ),
            _budget_left(state),
        )
    except Exception as exc:
        logger.warning("assess model call failed: %s", type(exc).__name__)
        return {"stop_reason": "model_error", "model_calls": state["model_calls"] + 1}
    update: dict[str, Any] = {
        "model_calls": state["model_calls"] + calls,
        "tokens": state["tokens"] + tokens,
        "problems": state["problems"] + problems,
    }
    if verdict is None:
        return update | {"stop_reason": "assess_unparseable"}
    if entry := audit_line(f"assess round {state['rounds']}", first, problems):
        update["validation_audit"] = state["validation_audit"] + [entry]
    # The plan, not the reasoning: decision, sizes, and the objectives Main assigned.
    await _emit(
        state,
        "turn.decision",
        {
            "decision": verdict["decision"],
            "round": state["rounds"],
            "gaps": len(verdict["gaps"]),
            "assignments": len(verdict["assignments"]),
            "objectives": [a["objective"][:200] for a in verdict["assignments"]],
            "limited": verdict["limited"],
        },
    )
    if verdict["limited"]:
        await _emit(state, "budget.exhausted", {"scope": "main", "reason": "rounds"})
    update["verdict"] = verdict
    update["limited"] = state["limited"] or verdict["limited"]
    return update


async def dispatch(state: MainState) -> dict[str, Any]:
    requests = [
        (uuid.UUID(a["environment_id"]), a["objective"]) for a in state["verdict"]["assignments"]
    ]
    async with SessionLocal() as s:
        ids = await turns.dispatch_round(s, uuid.UUID(state["turn_id"]), requests)
    return {"assignment_ids": [str(i) for i in ids], "rounds": state["rounds"] + 1}


async def run_round(state: MainState, runtime: Runtime[MainContext]) -> dict[str, Any]:
    """Runs (or resumes) one Local Investigator per assignment, in parallel. Each one
    records its assignment's final status; failures are isolated to their assignment."""
    ctx = runtime.context

    async def one(assignment_id: str) -> None:
        try:
            await run_local_investigator(
                uuid.UUID(assignment_id),
                ctx.checkpointer,
                model=ctx.local_model,
                mcp_settings=ctx.mcp_settings,
                mcp_transport=ctx.mcp_transport,
            )
        except AssignmentNotRunning:
            pass  # Already closed (e.g. finished before a replay); nothing to run.
        except Exception:
            logger.exception("local investigator %s failed", assignment_id)

    await asyncio.gather(*(one(a) for a in state["assignment_ids"]))
    async with SessionLocal() as s:
        await turns.end_round(s, uuid.UUID(state["turn_id"]))
    return {"assignment_ids": []}


DIAGNOSTIC_HEAD_CHARS = 160


def _compose_diagnostics(out: dict[str, Any]) -> dict[str, Any]:
    """What an unusable compose output looked like: finish reason, token counts, the shape
    of each tool call (argument names and sizes, the answer's first characters), and the
    parse error's type. Reasoning and message content are counted, never kept; report and
    argument text (which can quote telemetry summaries) is never kept."""
    raw = out["raw"]
    usage = getattr(raw, "usage_metadata", None) or {}
    extra = getattr(raw, "additional_kwargs", {}) or {}
    calls = []
    for call in getattr(raw, "tool_calls", []) or []:
        args = call.get("args") or {}
        answer = args.get("answer_md")
        report = args.get("report")
        calls.append(
            {
                "name": call.get("name"),
                "arg_keys": sorted(args),
                "answer_md_chars": len(answer) if isinstance(answer, str) else None,
                "answer_md_lines": len(answer.splitlines()) if isinstance(answer, str) else None,
                "answer_md_head": answer[:DIAGNOSTIC_HEAD_CHARS]
                if isinstance(answer, str)
                else None,  # fmt: skip
                "report": type(report).__name__,
                "report_conclusions": len(report.get("conclusions") or [])
                if isinstance(report, dict)
                else None,  # fmt: skip
            }
        )
    error = out.get("parsing_error")
    return {
        "finish_reason": (getattr(raw, "response_metadata", {}) or {}).get("finish_reason"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning"),
        "content_chars": len(str(raw.content or "")),
        "reasoning_chars": len(str(extra.get("reasoning_content") or "")),
        "tool_calls": calls,
        "invalid_tool_calls": len(getattr(raw, "invalid_tool_calls", []) or []),
        "parsing_error": type(error).__name__ if error is not None else None,
    }


async def compose(state: MainState, runtime: Runtime[MainContext]) -> dict[str, Any]:
    memory = await _memory(state["investigation_id"])
    conversation = await _conversation(state)
    prompt = prompts.COMPOSE.format(
        question=state["question"],
        conversation=context.conversation_text(conversation),
        report=context.report_text(conversation["report"]),
        memory=context.memory_text(memory, policy.MEMORY_CHARS),
        limited_note=prompts.LIMITED_NOTE if state["limited"] else "",
        report_instruction=(
            prompts.REPORT_INITIAL if state["initial"] else prompts.REPORT_FOLLOW_UP
        ),
    )
    messages = [SystemMessage(prompts.MAIN_SYSTEM), HumanMessage(prompt)]
    if state["repair_problems"]:
        # The previous draft, when there was one (none after a length or schema failure),
        # then what was wrong with it.
        if state["draft"] is not None:
            draft_json = AnswerDraft.model_validate(state["draft"]).model_dump_json()
            messages.append(AIMessage(content=draft_json))
        messages.append(
            HumanMessage(
                prompts.MAIN_REPAIR.format(
                    problems="\n".join(f"- {p}" for p in state["repair_problems"])
                )
            )
        )
    if _budget_left(state) == 0:
        return {"stop_reason": "budget_exhausted", "draft": None}
    # A schema-only tool for structured output, not an MCP tool: Main still has none.
    runnable = runtime.context.main_model.with_structured_output(
        AnswerDraft, method=policy.MAIN_COMPOSE_METHOD, include_raw=True
    )
    try:
        out = await runnable.ainvoke(messages)
    except LengthFinishReasonError:
        # Not a provider failure: the answer outgrew the output limit. Ask once for a
        # shorter one through the normal repair path.
        logger.warning("compose output hit the token limit")
        return {
            "model_calls": state["model_calls"] + 1,
            "draft": None,
            "repair_problems": [
                "[length] the answer exceeded the output limit; be concise: a short answer, "
                "a summary under 1500 characters, at most 5 conclusions, short timeline"
            ],
        }
    except Exception as exc:
        logger.warning("compose model call failed: %s", type(exc).__name__)
        return {"stop_reason": "model_error", "model_calls": state["model_calls"] + 1}
    update: dict[str, Any] = {
        "model_calls": state["model_calls"] + 1,
        "tokens": state["tokens"] + tokens_of(out["raw"]),
    }
    if out["parsed"] is None or not substantive(out["parsed"].answer_md):
        diagnostics = _compose_diagnostics(out)
        logger.warning("compose output unusable: %s", json.dumps(diagnostics, default=str))
        update["compose_diagnostics"] = [*state.get("compose_diagnostics", []), diagnostics]
    if out["parsed"] is None:
        return update | {
            "draft": None,
            "repair_problems": [f"[schema] output did not match the JSON schema: "
                                f"{str(out['parsing_error'])[:300]}"],
        }  # fmt: skip
    return update | {"draft": out["parsed"].model_dump(), "repair_problems": []}


async def validate(state: MainState) -> dict[str, Any]:
    """Deterministic: checks the draft against memory. Sends it back once with the
    problems; after that, keeps the cleaned (dropped/downgraded) version, unless the answer
    itself is still unusable: then the fallback answer, or failure."""
    raw = await _memory(state["investigation_id"])
    memory = build_memory(raw)
    decision = (state["verdict"] or {}).get("decision", "answer")
    if state["draft"] is None:
        problems = state["repair_problems"]
        clean = None
    else:
        clean, problems = validate_answer(
            AnswerDraft.model_validate(state["draft"]),
            memory,
            report_required=state["initial"],
            clarify=decision == "clarify",
        )
    first = state["first_problems"] if state["repairs"] else problems
    if problems and state["repairs"] < policy.MAIN_MAX_REPAIRS and _budget_left(state):
        return {
            "repair_problems": problems,
            "repairs": state["repairs"] + 1,
            "first_problems": first,
        }
    audit = list(state["validation_audit"])
    if clean is not None and (entry := audit_line("answer", first, problems)):
        audit.append(entry)
    if clean is None or problem_codes(problems) & ANSWER_BLOCKING:
        fallback = None
        if decision != "clarify":
            current = (await _conversation(state))["report"]
            fallback = fallback_answer(
                raw,
                memory,
                turn_id=uuid.UUID(state["turn_id"]),
                unresolved=problems,
                initial=state["initial"],
                report_version=current["version"] if current else None,
            )
        if fallback is None:
            stop = "compose_unparseable" if clean is None else "answer_invalid"
            return {
                "stop_reason": state.get("stop_reason") or stop,
                "answer": None,
                "problems": state["problems"] + problems,
            }
        clean, fallback_problems = fallback
        audit.append(f"answer: fallback ({', '.join(sorted(problem_codes(problems)))})")
        problems = problems + fallback_problems
    return {
        "answer": clean,
        "problems": state["problems"] + problems,
        "validation_audit": audit,
        "repair_problems": [],
    }


def _usable(answer: dict[str, Any], memory: Memory, clarify: bool) -> bool:
    """The final persistence check, whatever produced the answer: prose, and refs unless
    it is a clarification or there is nothing to cite."""
    md = answer.get("answer_md") or ""
    return substantive(md) and (bool(answer.get("refs")) or clarify or memory.empty)


async def persist(state: MainState) -> dict[str, Any]:
    verdict = state["verdict"] or {}
    decision = DECISIONS.get(verdict.get("decision", "answer"), TurnDecision.REUSED_EVIDENCE)
    if state["rounds"] and decision == TurnDecision.REUSED_EVIDENCE:
        decision = TurnDecision.INVESTIGATED
    answer = state["answer"] or {}
    memory = build_memory(await _memory(state["investigation_id"]))
    if not _usable(answer, memory, clarify=decision == TurnDecision.CLARIFICATION):
        return await _fail_invalid(state)
    report = answer.get("report")
    current = (await _conversation(state))["report"]
    answer_md = answer.get("answer_md", "")
    diff = None
    window = _window(state)
    rejected: list[str] = []
    if report is not None and not state["initial"] and answer.get("report_problems"):
        # A revision still wrong after its repair would replace a valid report with a
        # downgraded one (live Phase 5, v2): the current version stands instead.
        rejected = sorted(problem_codes(answer["report_problems"]))
        report = None
        answer_md += (
            f"\n\n{NOTE_PREFIX}The proposed report revision was not accepted (unresolved: "
            f"{', '.join(rejected)}); "
            + (f"report v{current['version']} remains current." if current else "no report yet.")
        )
    if report is not None:
        as_of = (window.end if window else datetime.now(UTC)).isoformat(timespec="minutes")
        report = {
            **report,
            "conclusions": [
                c | {"as_of": conclusion_as_of(c, memory, as_of)} for c in report["conclusions"]
            ],
        }
        report = merge_report(report, current, as_of=as_of)
        diff = report_diff(report, current)
        # Only a material change is a new version; otherwise the current one stands.
        report = {**report, "diff": diff} if diff["material"] else None
    answer_json = {
        "answer_md": answer_md,
        "refs": answer.get("refs", []),
        "fallback": bool(answer.get("fallback")),
        "report_rejected": rejected,
        "rounds": state["rounds"],
        "limited": state["limited"],
        "window": window.text() if window else None,
        "current_report_version": current["version"] if current else None,
        "report_diff": diff,
        "validation_audit": state["validation_audit"],
        "unresolved_problems": [p[:300] for p in state["problems"][-20:]],
    }
    async with SessionLocal() as s:
        # Idempotent: a replay after the commit returns the completed turn untouched.
        try:
            await turns.complete_turn(
                s,
                uuid.UUID(state["turn_id"]),
                decision=decision,
                answer_json=answer_json,
                report_json=report,
            )
        except turns.InvalidAnswer:
            return await _fail_invalid(state)
    return {}


async def _fail_invalid(state: MainState) -> dict[str, Any]:
    """Nothing trustworthy to persist: the turn fails, its evidence, findings and the
    current report stay as they are."""
    logger.warning("turn %s: no usable answer at persistence; failing it", state["turn_id"])
    async with SessionLocal() as s:
        await turns.fail_turn(s, uuid.UUID(state["turn_id"]), "answer_invalid")
    return {"stop_reason": "answer_invalid"}


async def fail(state: MainState) -> dict[str, Any]:
    async with SessionLocal() as s:
        await turns.fail_turn(s, uuid.UUID(state["turn_id"]), state.get("stop_reason") or "failed")
    return {}


# --- routing -------------------------------------------------------------------------------


def _after_assess(state: MainState) -> str:
    if state.get("stop_reason"):
        return "fail"
    return "dispatch" if state["verdict"]["decision"] == "investigate" else "compose"


def _after_compose(state: MainState) -> str:
    return "fail" if state.get("stop_reason") else "validate"


def _after_validate(state: MainState) -> str:
    if state.get("answer"):
        return "persist"
    if state.get("stop_reason"):
        return "fail"
    return "compose"


def build_graph() -> StateGraph:
    graph = StateGraph(MainState, context_schema=MainContext)
    for name, node in (
        ("start", start),
        ("assess", assess),
        ("dispatch", dispatch),
        ("run_round", run_round),
        ("compose", compose),
        ("validate", validate),
        ("persist", persist),
        ("fail", fail),
    ):
        graph.add_node(name, node)
    graph.set_entry_point("start")
    graph.add_edge("start", "assess")
    graph.add_conditional_edges("assess", _after_assess, ["dispatch", "compose", "fail"])
    graph.add_edge("dispatch", "run_round")
    graph.add_edge("run_round", "assess")
    graph.add_conditional_edges("compose", _after_compose, ["validate", "fail"])
    graph.add_conditional_edges("validate", _after_validate, ["persist", "compose", "fail"])
    graph.add_edge("persist", END)
    graph.add_edge("fail", END)
    return graph
