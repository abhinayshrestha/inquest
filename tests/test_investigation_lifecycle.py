import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentType,
    InvestigationStatus,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations


async def _pending(session: AsyncSession):
    environment = Environment(name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL)
    session.add(environment)
    await session.flush()
    return await investigations.create_investigation(
        session,
        InvestigationCreate(environment_id=environment.id, title="t", question="why?"),
    )


async def test_start_moves_investigation_and_current_assignments_forward(session: AsyncSession):
    investigation = await _pending(session)

    started = await investigations.start_investigation(session, investigation.id)

    assert started.status is InvestigationStatus.INVESTIGATING
    assert started.started_at is not None
    (assignment,) = started.assignments
    assert assignment.status is AssignmentStatus.RUNNING
    assert assignment.started_at == started.started_at


async def test_start_is_refused_unless_pending(session: AsyncSession):
    investigation = await _pending(session)
    await investigations.start_investigation(session, investigation.id)

    with pytest.raises(investigations.InvalidTransition, match="investigating"):
        await investigations.start_investigation(session, investigation.id)


async def test_start_refuses_assignments_that_already_moved(session: AsyncSession):
    investigation = await _pending(session)
    investigation.assignments[0].status = AssignmentStatus.CANCELLED
    await session.flush()

    with pytest.raises(investigations.InvalidTransition, match="cancelled"):
        await investigations.start_investigation(session, investigation.id)
    assert investigation.status is InvestigationStatus.PENDING


async def test_start_unknown_investigation(session: AsyncSession):
    with pytest.raises(investigations.InvestigationNotFound):
        await investigations.start_investigation(session, uuid.uuid4())
