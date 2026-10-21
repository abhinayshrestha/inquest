"""Deterministic reads and writes of investigation memory for the Local Investigator.

Hypotheses and findings get IDs derived from the assignment and their position
(uuid5), and every insert is ON CONFLICT DO NOTHING, so a graph node replayed after a
crash writes nothing twice. Evidence is never written here: only the Tool Executor
records observations.
"""

import uuid
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.investigators import policy
from src.investigators.refs import ref, ref_index
from src.investigators.validation import EvidenceFacts, Known
from src.investigators.windows import parse_range
from src.models import (
    Environment,
    Evidence,
    EvidenceStance,
    EvidenceType,
    Finding,
    FindingEvidence,
    FindingStatus,
    Hypothesis,
    HypothesisEvidence,
    HypothesisStatus,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationReport,
    InvestigationTurn,
)
from src.tools.scope import SCOPE_KEY


def hypothesis_id(assignment_id: uuid.UUID, index: int) -> uuid.UUID:
    return uuid.uuid5(assignment_id, f"hypothesis:{index}")


def finding_id(assignment_id: uuid.UUID, index: int) -> uuid.UUID:
    return uuid.uuid5(assignment_id, f"finding:{index}")


def _clip(text: str, limit: int = policy.EVIDENCE_SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def load_assignment(
    session: AsyncSession, assignment_id: uuid.UUID
) -> tuple[InvestigationAssignment, Investigation, Environment] | None:
    row = (
        await session.execute(
            select(InvestigationAssignment, Investigation, Environment)
            .join(Investigation, Investigation.id == InvestigationAssignment.investigation_id)
            .join(Environment, Environment.id == InvestigationAssignment.environment_id)
            .where(InvestigationAssignment.id == assignment_id)
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1], row[2])


def environment_scope(environment: Environment) -> dict[str, Any]:
    raw = environment.metadata_json.get(SCOPE_KEY) if environment.metadata_json else None
    return raw if isinstance(raw, dict) else {}


async def evidence_catalog(
    session: AsyncSession, assignment: InvestigationAssignment, limit: int
) -> list[dict[str, Any]]:
    """Evidence of the assignment's investigation and environment, this assignment's first,
    newest first. Summaries only; data_json stays in the database."""
    rows = await session.execute(
        select(
            Evidence.id,
            Evidence.assignment_id,
            Evidence.evidence_type,
            Evidence.title,
            Evidence.summary,
            Evidence.observed_at,
            Evidence.collected_at,
        )
        .where(
            Evidence.investigation_id == assignment.investigation_id,
            Evidence.environment_id == assignment.environment_id,
        )
        .order_by(
            case((Evidence.assignment_id == assignment.id, 0), else_=1),
            Evidence.collected_at.desc(),
        )
        .limit(limit)
    )
    return [
        {
            "ref": ref("evidence", r.id),
            "own": r.assignment_id == assignment.id,
            "type": r.evidence_type.value,
            "title": r.title,
            "summary": _clip(r.summary),
            "observed_at": r.observed_at,
            "collected_at": r.collected_at,
        }
        for r in rows
    ]


async def hypotheses(
    session: AsyncSession, assignment: InvestigationAssignment, *, own: bool
) -> list[dict[str, Any]]:
    """This assignment's hypotheses (own=True), or earlier ones in the same environment."""
    query = select(Hypothesis).where(
        Hypothesis.investigation_id == assignment.investigation_id,
        Hypothesis.environment_id == assignment.environment_id,
    )
    query = query.where(
        Hypothesis.assignment_id == assignment.id
        if own
        else Hypothesis.assignment_id != assignment.id
    )
    rows = await session.scalars(query.order_by(Hypothesis.created_at, Hypothesis.id))
    return [
        {
            "ref": ref("hypothesis", h.id),
            "statement": h.statement,
            "status": h.status.value,
            "confidence": h.confidence,
        }
        for h in rows
    ]


async def prior_findings(
    session: AsyncSession, assignment: InvestigationAssignment
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(Finding)
        .where(
            Finding.investigation_id == assignment.investigation_id,
            Finding.environment_id == assignment.environment_id,
            Finding.assignment_id != assignment.id,
        )
        .order_by(Finding.created_at)
    )
    return [{"title": f.title, "status": f.status.value, "summary": _clip(f.summary)} for f in rows]


# What an evidence row can show about time and entities, from the shape of its data alone
# (no payload is loaded for this). See validation.EvidenceFacts.granularity.
_DATA = Evidence.data_json["data"]
GRANULARITY = case(
    (_DATA.has_key("groups"), "aggregate"),
    (_DATA.has_key("series"), "series"),
    (_DATA.has_key("records"), "events"),
    (_DATA.has_key("spans"), "trace"),
    (_DATA.has_key("trace_id"), "trace"),
    else_="aggregate",
)


def _samples(data_json: dict[str, Any] | None) -> dict[str, int]:
    data = (data_json or {}).get("data") or {}
    if isinstance(data.get("groups"), list):
        return {str(g.get("group", "")): int(g.get("count", 0)) for g in data["groups"]}
    if isinstance(data.get("spans"), list) and "trace_id" not in data:
        return {"": len(data["spans"])}
    return {}


async def known_refs(session: AsyncSession, assignment: InvestigationAssignment) -> Known:
    evidence = (
        await session.execute(
            select(
                Evidence.id,
                Evidence.evidence_type,
                Evidence.title,
                Evidence.summary,
                # Only trace evidence needs its data (span counts); skip the rest.
                case((Evidence.evidence_type == EvidenceType.TRACE, Evidence.data_json)),
                GRANULARITY,
                Evidence.data_json["time_range"],
            ).where(
                Evidence.investigation_id == assignment.investigation_id,
                Evidence.environment_id == assignment.environment_id,
            )
        )
    ).all()
    own = (
        await session.execute(
            select(Hypothesis.id, Hypothesis.status, Hypothesis.statement).where(
                Hypothesis.assignment_id == assignment.id
            )
        )
    ).all()
    hypothesis_ids = [h for h, _, _ in own]
    links = await session.execute(
        select(
            HypothesisEvidence.hypothesis_id,
            HypothesisEvidence.evidence_id,
            HypothesisEvidence.stance,
        ).where(HypothesisEvidence.hypothesis_id.in_(hypothesis_ids))
    )
    return Known(
        evidence=ref_index("evidence", [e[0] for e in evidence]),
        hypotheses=ref_index("hypothesis", hypothesis_ids),
        links={(h, e): s.value for h, e, s in links},
        facts={
            e_id: EvidenceFacts(
                type=e_type.value,
                text=f"{title}\n{summary}",
                samples=_samples(data),
                granularity=granularity,
                window=parse_range(time_range),
            )
            for e_id, e_type, title, summary, data, granularity, time_range in evidence
        },
        hypothesis_status={h: s.value for h, s, _ in own},
        hypothesis_text={h: statement for h, _, statement in own},
    )


async def record_hypotheses(
    session: AsyncSession, assignment: InvestigationAssignment, drafts: list[dict[str, str]]
) -> list[uuid.UUID]:
    ids = [hypothesis_id(assignment.id, i) for i in range(len(drafts))]
    if drafts:
        await session.execute(
            insert(Hypothesis)
            .values(
                [
                    {
                        "id": hid,
                        "assignment_id": assignment.id,
                        "investigation_id": assignment.investigation_id,
                        "environment_id": assignment.environment_id,
                        "statement": d["statement"],
                    }
                    for hid, d in zip(ids, drafts, strict=True)
                ]
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
    return ids


async def record_conclusion(
    session: AsyncSession, assignment: InvestigationAssignment, conclusion: dict[str, Any]
) -> list[uuid.UUID]:
    """Writes a validated conclusion (validation.validate_conclusion output)."""
    scope = {
        "investigation_id": assignment.investigation_id,
        "environment_id": assignment.environment_id,
    }
    links: list[dict[str, Any]] = []
    for u in conclusion["hypothesis_updates"]:
        hid = uuid.UUID(u["hypothesis_id"])
        if u["status"] is not None:
            # Assignment filter: a model-supplied ID can never touch another assignment.
            await session.execute(
                update(Hypothesis)
                .where(Hypothesis.id == hid, Hypothesis.assignment_id == assignment.id)
                .values(status=HypothesisStatus(u["status"]), confidence=u["confidence"])
            )
        links += [
            {"hypothesis_id": hid, "evidence_id": uuid.UUID(x["evidence_id"]), **scope,
             "stance": EvidenceStance(x["stance"])}
            for x in u["evidence"]
        ]  # fmt: skip
    if links:
        await session.execute(insert(HypothesisEvidence).values(links).on_conflict_do_nothing())

    ids = [finding_id(assignment.id, i) for i in range(len(conclusion["findings"]))]
    finding_links: list[dict[str, Any]] = []
    for fid, f in zip(ids, conclusion["findings"], strict=True):
        await session.execute(
            insert(Finding)
            .values(
                id=fid,
                assignment_id=assignment.id,
                **scope,
                hypothesis_id=uuid.UUID(f["hypothesis_id"]) if f["hypothesis_id"] else None,
                title=f["title"],
                summary=f["summary"],
                confidence=f["confidence"],
                status=FindingStatus(f["status"]),
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        finding_links += [
            {"finding_id": fid, "evidence_id": uuid.UUID(x["evidence_id"]), **scope,
             "stance": EvidenceStance(x["stance"])}
            for x in f["evidence"]
        ]  # fmt: skip
    if finding_links:
        await session.execute(
            insert(FindingEvidence).values(finding_links).on_conflict_do_nothing()
        )
    return ids


async def investigation_memory(
    session: AsyncSession, investigation_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the Main Investigator may reason over, from PostgreSQL only: scope,
    assignment history, findings with their evidence links, hypotheses, evidence
    summaries. Raw telemetry (data_json) is read only for trace span counts."""
    environments = (
        (
            await session.execute(
                select(Environment)
                .join(
                    InvestigationEnvironment,
                    InvestigationEnvironment.environment_id == Environment.id,
                )
                .where(InvestigationEnvironment.investigation_id == investigation_id)
                .order_by(Environment.name)
            )
        )
        .scalars()
        .all()
    )
    names = {e.id: e.name for e in environments}
    assignments = (
        (
            await session.execute(
                select(InvestigationAssignment)
                .where(InvestigationAssignment.investigation_id == investigation_id)
                .order_by(InvestigationAssignment.round, InvestigationAssignment.created_at)
            )
        )
        .scalars()
        .all()
    )
    findings = (
        (
            await session.execute(
                select(Finding)
                .where(Finding.investigation_id == investigation_id)
                .order_by(Finding.created_at, Finding.id)
            )
        )
        .scalars()
        .all()
    )
    finding_links = (
        await session.execute(
            select(
                FindingEvidence.finding_id, FindingEvidence.evidence_id, FindingEvidence.stance
            ).where(FindingEvidence.investigation_id == investigation_id)
        )
    ).all()
    hypotheses = (
        (
            await session.execute(
                select(Hypothesis)
                .where(Hypothesis.investigation_id == investigation_id)
                .order_by(Hypothesis.created_at, Hypothesis.id)
            )
        )
        .scalars()
        .all()
    )
    evidence = (
        await session.execute(
            select(
                Evidence.id,
                Evidence.environment_id,
                Evidence.evidence_type,
                Evidence.title,
                Evidence.summary,
                Evidence.collected_at,
                case((Evidence.evidence_type == EvidenceType.TRACE, Evidence.data_json)),
                # Only the queried window, for freshness; never the observations.
                Evidence.data_json["time_range"],
                GRANULARITY,
            )
            .where(Evidence.investigation_id == investigation_id)
            .order_by(Evidence.collected_at.desc())
        )
    ).all()
    turn_of = {a.id: a.turn_id for a in assignments}
    links: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for finding_id, evidence_id, stance in finding_links:
        links.setdefault(finding_id, []).append((evidence_id, stance.value))
    return {
        "environments": [
            {"id": e.id, "name": e.name, "type": e.type.value, "scope": environment_scope(e)}
            for e in environments
        ],
        "assignments": [
            {
                "round": a.round,
                "environment": names.get(a.environment_id, "?"),
                "status": a.status.value,
                "objective": a.objective,
            }
            for a in assignments
        ],
        "findings": [
            {
                "id": f.id,
                "environment": names.get(f.environment_id, "?"),
                # The turn whose round produced it, for answers scoped to this turn.
                "turn_id": turn_of.get(f.assignment_id),
                "status": f.status.value,
                "title": f.title,
                "summary": f.summary,
                "evidence": links.get(f.id, []),
            }
            for f in findings
        ],
        "hypotheses": [
            {
                "id": h.id,
                "environment": names.get(h.environment_id, "?"),
                "status": h.status.value,
                "statement": h.statement,
            }
            for h in hypotheses
        ],
        "evidence": [
            {
                "id": e_id,
                "environment": names.get(env_id, "?"),
                "type": e_type.value,
                "title": title,
                "summary": summary,
                "collected_at": collected_at,
                "samples": _samples(data),
                "window": parse_range(time_range),
                "granularity": granularity,
            }
            for (
                e_id,
                env_id,
                e_type,
                title,
                summary,
                collected_at,
                data,
                time_range,
                granularity,
            ) in evidence
        ],
    }


async def conversation(
    session: AsyncSession, investigation_id: uuid.UUID, before_seq: int
) -> dict[str, Any]:
    """Earlier turns (question, outcome, answer) and the current report, for follow-ups.
    Conversation and report are context; evidence and findings remain the only support."""
    turns = (
        (
            await session.execute(
                select(InvestigationTurn)
                .where(
                    InvestigationTurn.investigation_id == investigation_id,
                    InvestigationTurn.seq < before_seq,
                )
                .order_by(InvestigationTurn.seq)
            )
        )
        .scalars()
        .all()
    )
    report = await session.scalar(
        select(InvestigationReport)
        .where(InvestigationReport.investigation_id == investigation_id)
        .order_by(InvestigationReport.version.desc())
        .limit(1)
    )
    return {
        "turns": [
            {
                "seq": t.seq,
                "question": t.question,
                "status": t.status.value,
                "decision": t.decision.value if t.decision else None,
                "error_code": t.error_code,
                "answer_md": (t.answer_json or {}).get("answer_md", ""),
            }
            for t in turns
        ],
        "report": (
            {"version": report.version, **report.report_json} if report is not None else None
        ),
    }
