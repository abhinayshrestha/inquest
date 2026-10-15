from fastapi import APIRouter, HTTPException, status

from src.api.deps import SessionDep
from src.models import Environment
from src.schemas.environment import EnvironmentCreate, EnvironmentRead
from src.services import environments

router = APIRouter(prefix="/environments", tags=["environments"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=EnvironmentRead)
async def create_environment(data: EnvironmentCreate, session: SessionDep) -> Environment:
    try:
        return await environments.create_environment(session, data)
    except environments.EnvironmentNameTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="environment name already exists"
        ) from exc
