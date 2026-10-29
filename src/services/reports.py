import uuid
from typing import get_args

from sqlalchemy import Integer, String, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    Investigation,
    InvestigationEnvironment,
    InvestigationReport,
    InvestigationTurn,
)
from src.schemas.environment import EnvironmentSummary
from src.schemas.investigation import ReportListItem, ReportOutcome

OUTCOMES = frozenset(get_args(ReportOutcome))


def _like(text: str) -> str:
    # The search term is data, not a pattern: escape LIKE wildcards.
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_reports(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    limit: int,
    offset: int,
    outcome: str | None = None,
    environment_id: uuid.UUID | None = None,
    latest_only: bool = False,
    title: str | None = None,
) -> tuple[list[ReportListItem], bool]:
    """Report versions across investigations, newest first. Only metadata is read from
    report_json (outcome, number of conclusions); bodies are fetched per version."""
    latest = (
        select(
            InvestigationReport.investigation_id,
            func.max(InvestigationReport.version).label("version"),
        )
        .join(Investigation, Investigation.id == InvestigationReport.investigation_id)
        .where(Investigation.organization_id == organization_id)
        .group_by(InvestigationReport.investigation_id)
        .subquery()
    )
    outcome_value = InvestigationReport.report_json["outcome"].astext
    raw = InvestigationReport.report_json["conclusions"]
    # A malformed stored report must not break the listing: count only real arrays.
    conclusions = case((func.jsonb_typeof(raw) == "array", func.jsonb_array_length(raw)), else_=0)
    query = (
        select(
            InvestigationReport,
            Investigation,
            InvestigationTurn.seq,
            latest.c.version,
            cast(outcome_value, String),
            cast(conclusions, Integer),
        )
        .join(Investigation, Investigation.id == InvestigationReport.investigation_id)
        .join(InvestigationTurn, InvestigationTurn.id == InvestigationReport.turn_id)
        .join(latest, latest.c.investigation_id == InvestigationReport.investigation_id)
        .where(Investigation.organization_id == organization_id)
        .options(
            selectinload(Investigation.environments).selectinload(
                InvestigationEnvironment.environment
            )
        )
    )
    if latest_only:
        query = query.where(InvestigationReport.version == latest.c.version)
    if outcome is not None:
        query = query.where(outcome_value == outcome)
    if environment_id is not None:
        query = query.where(
            select(InvestigationEnvironment)
            .where(
                InvestigationEnvironment.investigation_id == Investigation.id,
                InvestigationEnvironment.environment_id == environment_id,
            )
            .exists()
        )
    if title:
        query = query.where(Investigation.title.ilike(_like(title), escape="\\"))
    rows = (
        await session.execute(
            query.order_by(InvestigationReport.created_at.desc(), InvestigationReport.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
    ).all()
    items = [
        ReportListItem(
            id=report.id,
            investigation_id=investigation.id,
            investigation_title=investigation.title,
            investigation_status=investigation.status,
            environments=sorted(
                (
                    EnvironmentSummary.model_validate(e.environment)
                    for e in investigation.environments
                ),
                key=lambda e: e.name,
            ),
            version=report.version,
            is_latest=report.version == latest_version,
            turn_id=report.turn_id,
            turn_seq=seq,
            created_at=report.created_at,
            outcome=outcome_text if outcome_text in OUTCOMES else None,
            conclusion_count=count,
        )
        for report, investigation, seq, latest_version, outcome_text, count in rows[:limit]
    ]
    return items, len(rows) > limit
