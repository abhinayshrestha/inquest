from fastapi import APIRouter, HTTPException, Query, status

from src.api.deps import SessionDep
from src.models import Environment
from src.schemas.environment import EnvironmentCreate, EnvironmentRead, EnvironmentSummary
from src.services import environments

router = APIRouter(prefix="/environments", tags=["environments"])


@router.get("", response_model=list[EnvironmentSummary])
async def list_environments(
    session: SessionDep, limit: int = Query(default=100, ge=1, le=200)
) -> list[Environment]:
    """Names, types and statuses only; configuration (metadata_json) is not listed."""
    return await environments.list_environments(session, limit=limit)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=EnvironmentRead)
async def create_environment(data: EnvironmentCreate, session: SessionDep) -> Environment:
    try:
        return await environments.create_environment(session, data)
    except environments.EnvironmentNameTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="environment name already exists"
        ) from exc
