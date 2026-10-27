from fastapi import APIRouter, HTTPException, status

from src.api.deps import OrganizationQuery, SessionDep
from src.auth.deps import CurrentUser
from src.schemas.investigation import OverviewCounts
from src.services import access
from src.services.overview import overview_counts

router = APIRouter(tags=["overview"])


@router.get("/overview", response_model=OverviewCounts)
async def get_overview(
    session: SessionDep,
    identity: CurrentUser,
    organization_id: OrganizationQuery,
) -> OverviewCounts:
    """Counts of the organization's stored investigations, turns, environments and
    reports, grouped by status. No health, alert or uptime figures: Inquest does not
    measure those."""
    try:
        await access.require_member(session, organization_id, identity.user)
    except access.NoAccess as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="organization not found"
        ) from exc
    return await overview_counts(session, organization_id)
