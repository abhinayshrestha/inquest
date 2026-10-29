import uuid
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Selects which of the caller's organizations to read; the route checks membership.
OrganizationQuery = Annotated[uuid.UUID, Query(description="One of the caller's organizations")]
