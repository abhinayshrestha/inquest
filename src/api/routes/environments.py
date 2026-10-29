import uuid

from fastapi import APIRouter, HTTPException, Query, status

from src.api.deps import OrganizationQuery, SessionDep
from src.auth.deps import CurrentUser, WritingUser
from src.models import Environment
from src.schemas.environment import (
    EnvironmentCreateRequest,
    EnvironmentDetail,
    EnvironmentListItem,
    EnvironmentRead,
    TelemetryCoverage,
)
from src.services import access, environments, limits

router = APIRouter(prefix="/environments", tags=["environments"])


def _not_found(what: str = "environment") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


@router.get("", response_model=list[EnvironmentListItem])
async def list_environments(
    session: SessionDep,
    identity: CurrentUser,
    organization_id: OrganizationQuery,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[EnvironmentListItem]:
    """The organization's environments: names, types, statuses and investigation counts;
    no configuration (metadata_json)."""
    try:
        await access.require_member(session, organization_id, identity.user)
    except access.NoAccess as exc:
        raise _not_found("organization") from exc
    return await environments.list_environments(
        session, organization_id=organization_id, limit=limit
    )


async def _environment(
    session: SessionDep, identity: CurrentUser, environment_id: uuid.UUID
) -> Environment:
    try:
        return await access.environment_for(session, identity.user, environment_id)
    except access.NoAccess as exc:
        raise _not_found() from exc


@router.get("/{environment_id}", response_model=EnvironmentDetail)
async def get_environment(
    environment_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> EnvironmentDetail:
    """Telemetry scope (identity names), activity counts and whether providers are
    configured for this environment; credentials and URLs are never returned."""
    environment = await _environment(session, identity, environment_id)
    return await environments.environment_detail(session, environment)


@router.get("/{environment_id}/telemetry", response_model=TelemetryCoverage)
async def get_telemetry_coverage(
    environment_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> TelemetryCoverage:
    """Telemetry availability as last recorded by this environment's investigations, per
    signal and identity. Historical observations with timestamps, not a live check."""
    await _environment(session, identity, environment_id)
    return await environments.telemetry_coverage(session, environment_id)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=EnvironmentRead)
async def create_environment(
    data: EnvironmentCreateRequest, session: SessionDep, identity: WritingUser
) -> Environment:
    """Creates an environment in one of the caller's organizations, owned by the caller.
    It starts configuration-required: it cannot run investigations until it has its own
    credentials (Phase 7C); process-wide development credentials are never shared."""
    try:
        await access.require_member(session, data.organization_id, identity.user)
    except access.NoAccess as exc:
        raise _not_found("organization") from exc
    await limits.hit(
        session, "environment_create", user=identity.user.id, organization=data.organization_id
    )
    try:
        return await environments.create_environment(
            session, data, organization_id=data.organization_id, owner_user_id=identity.user.id
        )
    except access.NoAccess as exc:
        raise _not_found("organization") from exc
    except environments.EnvironmentNameTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="environment name already exists"
        ) from exc
