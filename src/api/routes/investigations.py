import uuid

from fastapi import APIRouter, HTTPException, status

from src.api.deps import SessionDep
from src.models import Investigation
from src.schemas.investigation import InvestigationCreate, InvestigationRead
from src.services import investigations

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvestigationRead)
async def create_investigation(data: InvestigationCreate, session: SessionDep) -> Investigation:
    try:
        return await investigations.create_investigation(session, data)
    except investigations.EnvironmentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="environment not found"
        ) from exc


@router.get("/{investigation_id}", response_model=InvestigationRead)
async def get_investigation(investigation_id: uuid.UUID, session: SessionDep) -> Investigation:
    investigation = await investigations.get_investigation(session, investigation_id)
    if investigation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="investigation not found")
    return investigation
