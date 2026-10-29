import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from src.api.deps import OrganizationQuery, SessionDep
from src.auth.deps import CurrentUser
from src.schemas.investigation import ReportListPage, ReportOutcome
from src.services import access, reports

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_model=ReportListPage)
async def list_reports(
    session: SessionDep,
    identity: CurrentUser,
    organization_id: OrganizationQuery,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    outcome: ReportOutcome | None = None,
    environment_id: uuid.UUID | None = None,
    latest_only: bool = False,
    q: Annotated[
        str | None, Query(max_length=100, description="Investigation title contains")
    ] = None,
) -> ReportListPage:
    """Report versions across investigations, newest first, one bounded page at a time.
    Metadata only; read a report body with GET /investigations/{id}/reports/{version}."""
    try:
        await access.require_member(session, organization_id, identity.user)
    except access.NoAccess as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
        ) from exc
    items, has_more = await reports.list_reports(
        session,
        organization_id=organization_id,
        limit=limit,
        offset=offset,
        outcome=outcome,
        environment_id=environment_id,
        latest_only=latest_only,
        title=q.strip() if q else None,
    )
    return ReportListPage(items=items, next_offset=offset + limit if has_more else None)
