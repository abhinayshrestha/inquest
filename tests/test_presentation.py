"""Response blocks (D023): deterministic projections from validated answers and evidence."""

import math
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.investigators.refs import ref
from src.models import (
    Evidence,
    EvidenceStance,
    EvidenceType,
    Finding,
    FindingEvidence,
    FindingStatus,
    InvestigationAssignment,
    InvestigationTurn,
    TurnDecision,
    TurnStatus,
)
from src.schemas.presentation import Presentation
from src.services import presentation
from tests.records import add_assignment
from tests.tenancy import Member

WINDOW = {"start": "2026-10-21T17:16:00Z", "end": "2026-10-21T17:46:00Z"}


def _trace_stats(groups: list[tuple[str, int, int, float]]) -> dict[str, Any]:
    return {
        "time_range": WINDOW,
        "data": {
            "group_by": "operation",
            "has_more": False,
            "groups": [
                {
                    "group": name,
                    "count": count,
                    "error_count": errors,
                    "error_rate": errors / count,
                    "p50_ms": p95 / 2,
                    "p95_ms": p95,
                    "p99_ms": p95 * 1.1,
                }
                for name, count, errors, p95 in groups
            ],  # fmt: skip
        },
    }


def _metric(name: str, unit: str, points: list[tuple[str, float]]) -> dict[str, Any]:
    values = [v for _, v in points]
    return {
        "time_range": WINDOW,
        "data": {
            "metric": name,
            "step_seconds": 60,
            "total_series": 1,
            "series": [
                {
                    "labels": {"host.name": "vm"},
                    "unit": unit,
                    "min": min(values),
                    "max": max(values),
                    "mean": sum(values) / len(values),
                    "median": values[0],
                    "last": values[-1],
                    "sum": sum(values),
                    "max_at": points[0][0],
                    "points": [list(p) for p in points],
                }
            ],  # fmt: skip
        },
    }


SLOW = [
    ("GET /slow", 31, 0, 1527.1),
    ("request handler - /slow", 62, 0, 1535.3),
    ("GET /", 4, 0, 3.2),
]
CPU = [("2026-10-21T17:20:00Z", 1.2), ("2026-10-21T17:21:00Z", 5.8), ("2026-10-21T17:23:00Z", 1.1)]


async def _evidence(
    session: AsyncSession,
    assignment: InvestigationAssignment,
    data_json: dict,
    kind=EvidenceType.TRACE,
) -> Evidence:
    evidence = Evidence(
        assignment=assignment, evidence_type=kind, source_provider="signoz",
        title=f"{kind.value} evidence", summary="recorded summary", data_json=data_json,
    )  # fmt: skip
    session.add(evidence)
    await session.flush()
    return evidence


async def _answered(
    session: AsyncSession,
    assignment: InvestigationAssignment,
    question: str,
    answer_md: str,
    cites: list[Evidence] = (),
) -> InvestigationTurn:
    turn = await session.get(InvestigationTurn, assignment.turn_id)
    now = datetime.now(UTC)
    turn.question = question
    turn.status = TurnStatus.COMPLETED
    turn.decision = TurnDecision.INVESTIGATED
    turn.started_at = turn.completed_at = now
    turn.answer_json = {
        "answer_md": answer_md,
        "refs": [
            {"id": str(e.id), "ref": ref("evidence", e.id), "kind": "evidence"} for e in cites
        ],
    }
    await session.flush()
    return turn


async def _setup(session: AsyncSession, tenant: Member | None = None) -> InvestigationAssignment:
    assignment = await add_assignment(session, tenant=tenant)
    await session.flush()
    return assignment


def _types(p: Presentation) -> list[str]:
    return [f"{b.type}:{getattr(b, 'kind', '')}".rstrip(":") for b in p.blocks]


async def test_latency_question_gets_totals_bar_chart_and_table(session: AsyncSession):
    a = await _setup(session)
    stats = await _evidence(session, a, _trace_stats(SLOW))
    turn = await _answered(session, a, "Which endpoint is the slowest?", "GET /slow.", [stats])

    p = await presentation.build_presentation(session, turn)

    assert _types(p) == ["markdown", "metric", "chart:bar", "table", "evidence"]
    metric, chart, table = p.blocks[1], p.blocks[2], p.blocks[3]
    assert [(i.label, i.value, i.unit) for i in metric.items] == [
        ("Spans", 97, "spans"), ("Error spans", 0, "spans"), ("Error rate", 0.0, "%"),
    ]  # fmt: skip
    assert chart.series[0].unit == "ms"
    assert [(pt.x, pt.values["p95"], pt.sample_count) for pt in chart.points] == [
        ("request handler - /slow", 1535.3, 62), ("GET /slow", 1527.1, 31), ("GET /", 3.2, 4),
    ]  # fmt: skip
    # Windows and refs come from the evidence; the 4-span group is flagged as a small sample.
    assert chart.window.start == datetime(2026, 10, 21, 17, 16, tzinfo=UTC)
    assert chart.evidence_refs == table.evidence_refs == [ref("evidence", stats.id)]
    assert "1 group(s) have fewer than 30 spans" in chart.note
    assert {c.key: c.unit for c in table.columns}["p95"] == "ms"


async def test_cpu_question_charts_only_the_cpu_series_with_stored_points(session: AsyncSession):
    a = await _setup(session)
    cpu = await _evidence(
        session, a, _metric("cpu_utilization", "percent", CPU), EvidenceType.METRIC
    )
    mem = await _evidence(
        session, a, _metric("memory_utilization", "percent", CPU), EvidenceType.METRIC
    )
    turn = await _answered(session, a, "Is CPU high?", "CPU peaked at 5.8 %.", [cpu, mem])

    p = await presentation.build_presentation(session, turn)

    assert _types(p) == ["markdown", "metric", "chart:line", "evidence"]
    chart = p.blocks[2]
    assert chart.title == "CPU utilization"
    # Exactly the recorded points: the missing 17:22 minute stays missing.
    assert [(pt.x, pt.values) for pt in chart.points] == [(x, {"s0": v}) for x, v in CPU]
    assert chart.series[0].unit == "percent"
    assert "gaps are not filled" in chart.note
    item = p.blocks[1].items[0]
    assert (item.stat, item.unit, item.sample_count, item.step_seconds) == (
        "mean",
        "percent",
        3,
        60,
    )


async def test_chart_request_charts_all_chartable_evidence_up_to_the_cap(session: AsyncSession):
    a = await _setup(session)
    cited = [await _evidence(session, a, _trace_stats(SLOW))] + [
        await _evidence(session, a, _metric(f"m{i}", "percent", CPU), EvidenceType.METRIC)
        for i in range(5)
    ]
    turn = await _answered(session, a, "Can you show me in charts?", "Here.", cited)

    p = await presentation.build_presentation(session, turn)

    charts = [b for b in p.blocks if b.type == "chart"]
    assert len([b for b in p.blocks if b.type in ("chart", "table", "metric")]) == 4
    assert len(charts) == 4


async def test_prose_answers_stay_markdown(session: AsyncSession):
    a = await _setup(session)
    await _evidence(session, a, _trace_stats(SLOW))  # in memory, but not cited
    turn = await _answered(session, a, "What should I check next?", "Profile the handler.")

    p = await presentation.build_presentation(session, turn)

    assert _types(p) == ["markdown"]


async def test_insufficient_data_produces_no_chart(session: AsyncSession):
    a = await _setup(session)
    one_point = await _evidence(
        session, a, _metric("cpu_utilization", "percent", CPU[:1]), EvidenceType.METRIC
    )
    one_group = await _evidence(session, a, _trace_stats(SLOW[:1]))
    no_window = await _evidence(session, a, {"data": _trace_stats(SLOW)["data"]})
    turn = await _answered(
        session, a, "Show CPU and latency in charts", "x", [one_point, one_group, no_window]
    )

    p = await presentation.build_presentation(session, turn)

    # One point is not a line and one group is not a comparison; evidence without a
    # window cannot carry a data block at all.
    assert not [b for b in p.blocks if b.type == "chart"]
    assert [b.type for b in p.blocks if b.type in ("metric", "table")] == [
        "metric",
        "metric",
        "table",
    ]


async def test_refs_from_another_investigation_are_never_projected(session: AsyncSession):
    mine = await _setup(session)
    other = await _setup(session)
    foreign = await _evidence(session, other, _trace_stats(SLOW))
    turn = await _answered(
        session,
        mine,
        "Which endpoint is slowest?",
        f"See {ref('evidence', foreign.id)}.",
        [foreign],
    )

    p = await presentation.build_presentation(session, turn)

    assert _types(p) == ["markdown"]


async def test_findings_bring_their_linked_evidence(session: AsyncSession):
    a = await _setup(session)
    stats = Evidence(
        assignment=a, evidence_type=EvidenceType.TRACE, source_provider="signoz", title="t",
        summary="s\n[Inquest] Validation audit: conclude: repaired x", data_json=_trace_stats(SLOW),
    )  # fmt: skip
    finding = Finding(assignment=a, title="slow", summary="slow", status=FindingStatus.CONFIRMED)
    link = FindingEvidence(finding=finding, evidence=stats, stance=EvidenceStance.SUPPORTS)
    session.add_all([stats, finding, link])
    await session.flush()
    turn = await _answered(
        session, a, "Which endpoint is slowest?", f"[{ref('finding', finding.id)}]"
    )

    p = await presentation.build_presentation(session, turn)

    assert "chart:bar" in _types(p)
    evidence_block = p.blocks[-1]
    assert evidence_block.items[0].summary == "s"


async def test_fenced_code_becomes_a_code_block(session: AsyncSession):
    a = await _setup(session)
    turn = await _answered(
        session,
        a,
        "How do I query it?",
        "Run:\n\n```bash\ncurl -s localhost:8080/slow\n```\n\nDone.",
    )

    p = await presentation.build_presentation(session, turn)

    assert _types(p) == ["markdown", "code", "markdown"]
    assert (p.blocks[1].language, p.blocks[1].code) == ("bash", "curl -s localhost:8080/slow")


# --- persistence, API and boundaries -------------------------------------------------------


async def test_presentation_is_stored_and_reused_without_rebuilding(
    client: AsyncClient, session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    def no_model(*_args, **_kwargs):
        raise AssertionError("presentation must never call a model")

    monkeypatch.setattr("src.investigators.llm.chat_model", no_model)
    a = await _setup(session, tenant)
    stats = await _evidence(session, a, _trace_stats(SLOW))
    turn = await _answered(session, a, "Which endpoint is slowest?", "GET /slow.", [stats])
    url = f"/investigations/{a.investigation_id}/turns/{turn.id}/presentation"

    first = await client.get(url)
    assert first.status_code == 200
    stored = await session.scalar(
        select(InvestigationTurn.presentation_json).where(InvestigationTurn.id == turn.id)
    )
    assert stored == first.json()

    calls = []
    original = presentation.build_presentation
    monkeypatch.setattr(
        presentation, "build_presentation", lambda *a, **k: calls.append(1) or original(*a, **k)
    )
    again = await client.get(url)
    assert again.json() == first.json()  # same generated_at: served from storage
    assert calls == []


async def test_invalid_stored_presentation_is_rebuilt(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    a = await _setup(session, tenant)
    stats = await _evidence(session, a, _trace_stats(SLOW))
    turn = await _answered(session, a, "Which endpoint is slowest?", "x", [stats])
    tampered = {
        "version": 1, "turn_id": str(turn.id), "generated_at": "2026-10-21T17:00:00Z",
        "blocks": [{"type": "html", "html": "<script>alert(1)</script>"}],
    }  # fmt: skip
    await session.execute(
        update(InvestigationTurn)
        .where(InvestigationTurn.id == turn.id)
        .values(presentation_json=tampered)
    )

    body = (
        await client.get(f"/investigations/{a.investigation_id}/turns/{turn.id}/presentation")
    ).json()

    assert "script" not in str(body)
    assert body["blocks"][0] == {"type": "markdown", "markdown": "x"}


async def test_presentation_api_boundaries(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    a = await _setup(session, tenant)
    other = await _setup(session, tenant)
    pending = await session.get(InvestigationTurn, a.turn_id)

    assert (
        await client.get(f"/investigations/{a.investigation_id}/turns/{pending.id}/presentation")
    ).status_code == 409
    # A turn is only reachable through its own investigation.
    assert (
        await client.get(
            f"/investigations/{other.investigation_id}/turns/{pending.id}/presentation"
        )
    ).status_code == 404
    assert (
        await client.get(f"/investigations/{a.investigation_id}/turns/{uuid.uuid4()}/presentation")
    ).status_code == 404


async def test_database_keeps_presentations_on_completed_turns_only(session: AsyncSession):
    a = await _setup(session)
    with pytest.raises(IntegrityError, match="presentation_when_done"):
        await session.execute(
            update(InvestigationTurn)
            .where(InvestigationTurn.id == a.turn_id)
            .values(presentation_json={"version": 1})
        )


# --- schema hardening ----------------------------------------------------------------------

BASE = {"version": 1, "turn_id": str(uuid.uuid4()), "generated_at": "2026-10-21T17:00:00Z"}
WIN = {"start": "2026-10-21T17:16:00Z", "end": "2026-10-21T17:46:00Z"}
CHART = {
    "type": "chart", "kind": "bar", "title": "t", "x_label": "x",
    "series": [{"key": "p95", "label": "p95", "unit": "ms"}],
    "points": [{"x": "a", "values": {"p95": 1}}, {"x": "b", "values": {"p95": 2}}],
    "evidence_refs": ["ev_0123abcd"], "window": WIN,
}  # fmt: skip


@pytest.mark.parametrize(
    "block",
    [
        {"type": "html", "html": "<b>x</b>"},
        {"type": "markdown", "markdown": "x", "onclick": "alert(1)"},
        CHART | {"evidence_refs": []},
        CHART | {"evidence_refs": ["fd_0123abcd"]},
        CHART | {"points": [{"x": "a", "values": {"p95": math.nan}}, {"x": "b", "values": {}}]},
        CHART | {"points": [{"x": "a", "values": {"other": 1}}, {"x": "b", "values": {}}]},
        CHART | {"points": [{"x": "a", "values": {"p95": 1}}]},
        CHART | {"kind": "line"},  # category labels are not timestamps
        CHART | {"window": {"start": "2026-10-21T18:00:00Z", "end": "2026-10-21T17:00:00Z"}},
        CHART | {"window": {"start": "2026-10-21T17:00:00", "end": "2026-10-21T18:00:00"}},
        {
            "type": "table",
            "title": "t",
            "columns": [{"key": "n", "label": "n", "kind": "number"}],
            "rows": [{"n": "not a number"}],
            "evidence_refs": ["ev_0123abcd"],
            "window": WIN,
        },
        {
            "type": "table",
            "title": "t",
            "columns": [{"key": "n", "label": "n", "kind": "number"}],
            "rows": [{"__proto__": 1}],
            "evidence_refs": ["ev_0123abcd"],
            "window": WIN,
        },
        {"type": "code", "language": "js; alert(1)", "code": "x"},
    ],
)
def test_malicious_or_invalid_blocks_are_rejected(block: dict):
    with pytest.raises(ValidationError):
        Presentation.model_validate(BASE | {"blocks": [block]})


def test_valid_blocks_are_accepted():
    p = Presentation.model_validate(
        BASE | {"blocks": [{"type": "markdown", "markdown": "x"}, CHART]}
    )
    assert [b.type for b in p.blocks] == ["markdown", "chart"]
