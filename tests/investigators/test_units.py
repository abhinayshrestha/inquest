import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import text

from src.db.session import engine
from src.investigators import policy
from src.investigators.checkpoint import SCHEMA
from src.investigators.context import model_view, render_result
from src.investigators.local_graph import _budget_stop, normalize_reply
from src.investigators.refs import ref, ref_index, resolve
from src.investigators.schemas import (
    Conclusion,
    EvidenceLink,
    FindingDraft,
    HypothesisDraft,
    HypothesisPlan,
    HypothesisUpdate,
)
from src.investigators.validation import Known, validate_conclusion, validate_plan
from src.models import ToolCallStatus
from src.tools.schemas import Outcome, ToolError, ToolExecutionResult

EV, EV2, HY = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def known(links: dict | None = None) -> Known:
    return Known(
        evidence=ref_index("evidence", [EV, EV2]),
        hypotheses=ref_index("hypothesis", [HY]),
        links=links or {},
    )


def link(evidence_id: uuid.UUID, stance: str = "supports") -> EvidenceLink:
    return EvidenceLink(evidence_ref=ref("evidence", evidence_id), stance=stance)  # type: ignore[arg-type]


def finding(
    status: str = "confirmed", evidence: list[EvidenceLink] | None = None, **kw
) -> FindingDraft:
    return FindingDraft(
        title="t",
        summary="s",
        status=status,
        evidence=evidence or [],
        **kw,  # type: ignore[arg-type]
    )


def conclusion(updates=(), findings=None) -> Conclusion:
    return Conclusion(
        verdict="sufficient",
        gaps=[" ", "logs"],
        hypothesis_updates=list(updates),
        findings=findings if findings is not None else [finding(evidence=[link(EV)])],
    )


# --- refs ----------------------------------------------------------------------------------


def test_refs_resolve_only_within_the_index():
    index = ref_index("evidence", [EV])
    assert resolve("evidence", ref("evidence", EV), index) == EV
    assert resolve("evidence", f"[{ref('evidence', EV).upper()}]", index) == EV
    assert resolve("evidence", "ev_00000000", index) is None
    # Right digits, wrong type tag.
    assert resolve("hypothesis", ref("evidence", EV).replace("ev_", "hy_"), index) is None
    assert resolve("evidence", str(EV), index) is None


def test_colliding_refs_are_ambiguous_not_guessed():
    a = uuid.UUID("12345678" + "0" * 24)
    b = uuid.UUID("12345678" + "f" * 24)
    assert resolve("evidence", "ev_12345678", ref_index("evidence", [a, b])) is None


# --- validation ----------------------------------------------------------------------------


def test_valid_conclusion_resolves_refs_to_ids():
    clean, problems = validate_conclusion(
        conclusion(
            [HypothesisUpdate(hypothesis_ref=ref("hypothesis", HY), status="supported",
                              confidence=0.6, evidence=[link(EV)])]
        ),
        known(),
    )  # fmt: skip
    assert problems == []
    assert clean["gaps"] == ["logs"]
    (update,) = clean["hypothesis_updates"]
    assert update == {
        "hypothesis_id": str(HY),
        "status": "supported",
        "confidence": 0.6,
        "evidence": [{"evidence_id": str(EV), "stance": "supports"}],
    }
    assert clean["findings"][0]["status"] == "confirmed"


def test_invented_evidence_is_dropped_and_unsupported_finding_downgraded():
    clean, problems = validate_conclusion(
        conclusion(findings=[finding(evidence=[EvidenceLink(evidence_ref="ev_deadbeef",
                                                             stance="supports")],
                                     confidence=0.9)]),
        known(),
    )  # fmt: skip
    assert any("ev_deadbeef" in p for p in problems)
    (f,) = clean["findings"]
    assert (f["status"], f["confidence"], f["evidence"]) == ("inconclusive", None, [])


def test_neutral_or_contradicting_links_do_not_support_a_finding():
    clean, problems = validate_conclusion(
        conclusion(
            findings=[finding("preliminary", [link(EV, "neutral"), link(EV2, "contradicts")])]
        ),
        known(),
    )
    assert problems and clean["findings"][0]["status"] == "inconclusive"
    # The links are kept: contradicting evidence is never hidden.
    assert len(clean["findings"][0]["evidence"]) == 2


@pytest.mark.parametrize(
    ("status", "stance", "accepted"),
    [
        ("supported", "supports", True),
        ("supported", "contradicts", False),
        ("rejected", "contradicts", True),
        ("rejected", "neutral", False),
        ("weakened", "supports", False),
        ("active", "neutral", True),
    ],
)
def test_hypothesis_status_needs_matching_evidence(status, stance, accepted):
    update = HypothesisUpdate(
        hypothesis_ref=ref("hypothesis", HY), status=status, evidence=[link(EV, stance)]
    )
    clean, problems = validate_conclusion(conclusion([update]), known())
    (u,) = clean["hypothesis_updates"]
    assert (u["status"] == status) is accepted and (not problems) is accepted
    assert u["evidence"] == [{"evidence_id": str(EV), "stance": stance}]


def test_existing_links_count_and_cannot_be_contradicted():
    existing = {(HY, EV): "contradicts"}
    update = HypothesisUpdate(hypothesis_ref=ref("hypothesis", HY), status="rejected", evidence=[])
    clean, problems = validate_conclusion(conclusion([update]), known(existing))
    assert problems == [] and clean["hypothesis_updates"][0]["status"] == "rejected"

    flip = HypothesisUpdate(
        hypothesis_ref=ref("hypothesis", HY), status="active", evidence=[link(EV, "supports")]
    )
    clean, problems = validate_conclusion(conclusion([flip]), known(existing))
    assert any("already linked" in p for p in problems)
    assert clean["hypothesis_updates"][0]["evidence"] == []


def test_unknown_hypotheses_and_self_contradicting_links_are_rejected():
    clean, problems = validate_conclusion(
        conclusion(
            [HypothesisUpdate(hypothesis_ref="hy_00000000", status="active", evidence=[])],
            [finding(evidence=[link(EV), link(EV, "contradicts")], hypothesis_ref="hy_00000000")],
        ),
        known(),
    )
    assert clean["hypothesis_updates"] == []
    (f,) = clean["findings"]
    assert f["hypothesis_id"] is None and f["evidence"] == [] and f["status"] == "inconclusive"
    assert len(problems) >= 3


def test_at_least_one_finding_and_at_most_the_limit():
    _, problems = validate_conclusion(conclusion(findings=[]), known())
    assert any("at least one finding" in p for p in problems)
    clean, problems = validate_conclusion(
        conclusion(findings=[finding(evidence=[link(EV)])] * (policy.MAX_FINDINGS + 2)), known()
    )
    assert len(clean["findings"]) == policy.MAX_FINDINGS and problems


def test_plan_is_deduplicated_and_bounded():
    drafts = [HypothesisDraft(statement=f"cause {i % 6}", test_plan="q") for i in range(8)]
    drafts.append(HypothesisDraft(statement="  Cause   1 ", test_plan="q"))
    kept, problems = validate_plan(HypothesisPlan(hypotheses=drafts))
    assert len(kept) == policy.MAX_HYPOTHESES
    assert [k["statement"] for k in kept][:2] == ["cause 0", "cause 1"]
    assert problems
    assert validate_plan(HypothesisPlan(hypotheses=[]))[1] == [
        "[no_hypotheses] at least one hypothesis is required"
    ]


# --- tool-call ID hygiene ------------------------------------------------------------------


def calls(*ids: str | None) -> AIMessage:
    return AIMessage(
        content="\n\nchecking",
        tool_calls=[{"name": "t", "args": {}, "id": i, "type": "tool_call"} for i in ids],
        additional_kwargs={"tool_calls": [{"id": "provider-copy"}], "other": 1},
    )


def test_provider_ids_are_kept_when_valid_and_unused():
    reply = normalize_reply(calls("chatcmpl-tool-b29dc8b2fb4808eb"), [])
    assert [c["id"] for c in reply.tool_calls] == ["chatcmpl-tool-b29dc8b2fb4808eb"]
    assert reply.content == "checking"
    assert reply.additional_kwargs == {"other": 1}


def test_reused_malformed_and_missing_ids_are_replaced():
    history = [calls("call_0")]
    reply = normalize_reply(calls("call_0", "bad id!", None, "x" * 101, "dup", "dup"), history)
    ids = [c["id"] for c in reply.tool_calls]
    assert ids[4] == "dup"
    assert all(i.startswith("tc_") for i in ids[:4] + ids[5:])
    assert len(set(ids)) == len(ids)


# --- model-facing rendering ----------------------------------------------------------------


def result(**kw) -> ToolExecutionResult:
    base = {"tool_call_id": uuid.uuid4(), "tool_name": "signoz.trace_stats",
            "status": ToolCallStatus.SUCCEEDED}  # fmt: skip
    return ToolExecutionResult(**(base | kw))


def args(ended_minutes_ago: int = 0) -> dict:
    end = datetime.now(UTC) - timedelta(minutes=ended_minutes_ago)
    return {"time_range": {"start": (end - timedelta(minutes=30)).isoformat(),
                           "end": end.isoformat()}}  # fmt: skip


def test_observations_show_refs_and_clipped_data(monkeypatch):
    monkeypatch.setattr(policy, "TOOL_RESULT_CHARS", 50)
    content, digest = render_result(
        result(tool_name="signoz.search_logs", outcome=Outcome.OBSERVATIONS,
               summary="2 records", evidence_ids=[EV], telemetry_available="yes",
               data={"rows": ["x" * 200]}),
        args(),
    )  # fmt: skip
    assert f"evidence: {ref('evidence', EV)}" in content
    assert "(clipped; full data is in the evidence)" in content
    assert "NOT evidence" not in content and "staleness" not in content
    assert ref("evidence", EV) in digest and "2 records" in digest


def test_aggregate_results_show_the_summary_not_the_data():
    content, _ = render_result(
        result(outcome=Outcome.OBSERVATIONS, summary="p99 4.2 s", evidence_ids=[EV],
               data={"groups": [{"group": "GET /", "count": 5}]}),
        args(),
    )  # fmt: skip
    assert "summary: p99 4.2 s" in content and "data:" not in content


@pytest.mark.parametrize(
    ("telemetry", "note"),
    [("yes", "did arrive"), ("no", "coverage gap, not health"), ("unknown", "unknown")],
)
def test_empty_is_never_presented_as_health(telemetry, note):
    content, _ = render_result(
        result(outcome=Outcome.EMPTY, telemetry_available=telemetry, data={"groups": []}),
        args(),
    )
    assert "NOT evidence of healthy operation" in content and note in content
    assert "data:" not in content


def test_old_windows_are_flagged_as_the_past():
    content, _ = render_result(result(outcome=Outcome.OBSERVATIONS), args(ended_minutes_ago=90))
    assert "staleness: window ended 90 min ago" in content


def test_errors_and_replays_are_explained():
    content, digest = render_result(
        result(status=ToolCallStatus.REJECTED,
               error=ToolError(code="policy.invalid_arguments", message="bad", retryable=False)),
        {},
    )  # fmt: skip
    assert "policy.invalid_arguments: bad (change the request)" in content
    assert "error=policy.invalid_arguments" in digest
    content, _ = render_result(result(outcome=Outcome.OBSERVATIONS, replayed=True), {})
    assert "nothing was re-queried" in content


def test_older_tool_results_are_compacted_in_the_model_view(monkeypatch):
    monkeypatch.setattr(policy, "FULL_TOOL_RESULTS_KEPT", 1)
    messages = [HumanMessage("go")] + [
        ToolMessage(f"full {i}", tool_call_id=str(i), artifact={"digest": f"d{i}"})
        for i in range(3)
    ]
    view = model_view(messages)
    assert [m.content for m in view[1:]] == ["[earlier result] d0", "[earlier result] d1", "full 2"]
    # The state itself is untouched.
    assert messages[1].content == "full 0"


# --- budgets -------------------------------------------------------------------------------


def state(**kw) -> dict:
    return {"deadline": time.time() + 1_000, **kw}


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({}, None),
        (
            {"model_calls": policy.MAX_MODEL_CALLS - policy.RESERVED_CONCLUDE_CALLS},
            "budget:model_calls",
        ),
        ({"tokens": policy.MAX_TOKENS - policy.RESERVED_CONCLUDE_TOKENS}, "budget:tokens"),
        ({"tool_calls": policy.MAX_TOOL_CALLS}, "budget:tool_calls"),
        ({"mcp_failures": policy.MAX_MCP_FAILURES}, "mcp_unavailable"),
        ({"deadline": time.time() + policy.CONCLUDE_HEADROOM_SECONDS - 1}, "budget:wall_clock"),
    ],
)
def test_budget_stops(kw, reason):
    assert _budget_stop(state(**kw)) == reason  # type: ignore[arg-type]


# --- checkpoint storage --------------------------------------------------------------------


async def test_checkpoint_tables_live_outside_the_alembic_schema(checkpointer):
    async with engine.connect() as conn:
        schemas = set(
            await conn.scalars(
                text("SELECT table_schema FROM information_schema.tables "
                     "WHERE table_name = 'checkpoints'")
            )
        )  # fmt: skip
    assert schemas == {SCHEMA}
