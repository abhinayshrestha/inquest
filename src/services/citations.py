"""Resolves a short ref from an answer (ev_/fd_/hy_ + 8 hex) to a record of one
investigation, for citation inspection in the UI.

The boundary is the same one answer validation uses: a ref only ever resolves within
its own investigation, and a prefix shared by two records resolves to nothing rather
than a guess. Only recorded summaries and metadata are returned; evidence data_json
(the tool output) is never exposed beyond its time window.
"""

import re
import uuid
from typing import Any

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.investigators.refs import ref
from src.investigators.windows import parse_range
from src.models import Environment, Evidence, Finding, FindingEvidence, Hypothesis, ToolCall
from src.schemas.environment import EnvironmentSummary
from src.schemas.investigation import (
    CitedEvidenceLink,
    EvidenceCitation,
    FindingCitation,
    HypothesisCitation,
)

REF_PATTERN = r"^(ev|fd|hy)_[0-9a-f]{8}$"
MAX_FINDING_EVIDENCE = 50
_REF = re.compile(REF_PATTERN)
# Repair bookkeeping appended to findings for the investigators; not for readers.
_AUDIT_LINE = re.compile(r"^\[Inquest\] Validation audit:.*$\n?", re.MULTILINE)


async def _unique(session: AsyncSession, model: Any, investigation_id: uuid.UUID, prefix: str):
    rows = list(
        await session.scalars(
            select(model)
            .where(
                model.investigation_id == investigation_id,
                cast(model.id, String).like(f"{prefix}%"),
            )
            .limit(2)
        )
    )
    return rows[0] if len(rows) == 1 else None


async def _environment(session: AsyncSession, environment_id: uuid.UUID) -> EnvironmentSummary:
    environment = await session.get(Environment, environment_id)
    return EnvironmentSummary.model_validate(environment)


async def _evidence(session: AsyncSession, e: Evidence) -> EvidenceCitation:
    tool_name = (
        await session.scalar(select(ToolCall.tool_name).where(ToolCall.id == e.tool_call_id))
        if e.tool_call_id
        else None
    )
    window = parse_range(e.data_json.get("time_range"))
    return EvidenceCitation(
        ref=ref("evidence", e.id),
        evidence_type=e.evidence_type.value,
        title=e.title,
        summary=e.summary,
        source_provider=e.source_provider,
        tool_name=tool_name,
        entity_type=e.entity_type,
        entity_identifier=e.entity_identifier,
        observed_at=e.observed_at,
        collected_at=e.collected_at,
        window_start=window[0] if window else None,
        window_end=window[1] if window else None,
        redaction_applied=e.redaction_applied,
        environment=await _environment(session, e.environment_id),
    )


async def _finding(session: AsyncSession, f: Finding) -> FindingCitation:
    links = (
        await session.execute(
            select(FindingEvidence.evidence_id, FindingEvidence.stance, Evidence.title)
            .join(Evidence, Evidence.id == FindingEvidence.evidence_id)
            .where(FindingEvidence.finding_id == f.id)
            .order_by(FindingEvidence.created_at)
            .limit(MAX_FINDING_EVIDENCE)
        )
    ).all()
    return FindingCitation(
        ref=ref("finding", f.id),
        title=f.title,
        summary=_AUDIT_LINE.sub("", f.summary).strip(),
        status=f.status.value,
        confidence=f.confidence,
        created_at=f.created_at,
        evidence=[
            CitedEvidenceLink(ref=ref("evidence", eid), stance=stance.value, title=title)
            for eid, stance, title in links
        ],
        environment=await _environment(session, f.environment_id),
    )


async def resolve_citation(
    session: AsyncSession, investigation_id: uuid.UUID, citation_ref: str
) -> EvidenceCitation | FindingCitation | HypothesisCitation | None:
    if not _REF.match(citation_ref):
        return None
    kind, prefix = citation_ref.split("_", 1)
    if kind == "ev":
        evidence = await _unique(session, Evidence, investigation_id, prefix)
        return await _evidence(session, evidence) if evidence else None
    if kind == "fd":
        finding = await _unique(session, Finding, investigation_id, prefix)
        return await _finding(session, finding) if finding else None
    hypothesis = await _unique(session, Hypothesis, investigation_id, prefix)
    if hypothesis is None:
        return None
    return HypothesisCitation(
        ref=ref("hypothesis", hypothesis.id),
        statement=hypothesis.statement,
        status=hypothesis.status.value,
        confidence=hypothesis.confidence,
        created_at=hypothesis.created_at,
        environment=await _environment(session, hypothesis.environment_id),
    )
