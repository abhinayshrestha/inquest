from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text

from src.api.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc
    return {"status": "ok", "database": "ok"}
