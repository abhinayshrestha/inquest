import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    Environment,
    EnvironmentStatus,
    Investigation,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    TurnStatus,
)
from src.schemas.investigation import OverviewCounts
from src.services.investigations import latest_turns


async def _grouped(session: AsyncSession, query) -> dict:
    return dict((await session.execute(query)).all())


async def overview_counts(session: AsyncSession, organization_id: uuid.UUID) -> OverviewCounts:
    """Grouped counts over one organization's stored records; every bucket is present,
    zero when empty."""
    in_org = Investigation.organization_id == organization_id
    investigations = await _grouped(
        session,
        select(Investigation.status, func.count()).where(in_org).group_by(Investigation.status),
    )
    turns = await _grouped(
        session,
        select(InvestigationTurn.status, func.count())
        .join(Investigation, Investigation.id == InvestigationTurn.investigation_id)
        .where(in_org)
        .group_by(InvestigationTurn.status),
    )
    latest = latest_turns()
    by_latest = await _grouped(
        session,
        select(latest.c.status, func.count())
        .join(Investigation, Investigation.id == latest.c.investigation_id)
        .where(in_org)
        .group_by(latest.c.status),
    )
    environments = await _grouped(
        session,
        select(Environment.status, func.count())
        .where(Environment.organization_id == organization_id)
        .group_by(Environment.status),
    )
    reports_total, with_reports = (
        await session.execute(
            select(func.count(), func.count(func.distinct(InvestigationReport.investigation_id)))
            .join(Investigation, Investigation.id == InvestigationReport.investigation_id)
            .where(in_org)
        )
    ).one()
    return OverviewCounts(
        investigations_total=sum(investigations.values()),
        investigations_by_status={s: investigations.get(s, 0) for s in InvestigationStatus},
        latest_turn_by_status={s: by_latest.get(s, 0) for s in TurnStatus},
        turns_total=sum(turns.values()),
        turns_by_status={s: turns.get(s, 0) for s in TurnStatus},
        environments_total=sum(environments.values()),
        environments_active=environments.get(EnvironmentStatus.ACTIVE, 0),
        reports_total=reports_total,
        investigations_with_reports=with_reports,
    )
