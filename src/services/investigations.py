import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    AssignmentStatus,
    Environment,
    Investigation,
    InvestigationAssignment,
    InvestigationStatus,
    TriggerType,
)
from src.schemas.investigation import InvestigationCreate

INITIAL_ROUND = 1


class EnvironmentNotFound(Exception):
    pass


class InvestigationNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


async def create_investigation(session: AsyncSession, data: InvestigationCreate) -> Investigation:
    environment = await session.get(Environment, data.environment_id)
    if environment is None:
        raise EnvironmentNotFound(data.environment_id)

    investigation = Investigation(
        title=data.title,
        question=data.question,
        severity=data.severity,
        status=InvestigationStatus.PENDING,
        trigger_type=TriggerType.MANUAL,
    )
    investigation.assignments.append(
        InvestigationAssignment(
            environment=environment,
            round=INITIAL_ROUND,
            objective=data.question,
            status=AssignmentStatus.PENDING,
        )
    )
    session.add(investigation)
    # One commit flushes both inserts in a single transaction: no investigation without
    # its initial assignment.
    await session.commit()
    return investigation


async def get_investigation(
    session: AsyncSession, investigation_id: uuid.UUID
) -> Investigation | None:
    return await session.scalar(
        select(Investigation)
        .where(Investigation.id == investigation_id)
        .options(
            selectinload(Investigation.assignments).selectinload(
                InvestigationAssignment.environment
            )
        )
    )


async def start_investigation(session: AsyncSession, investigation_id: uuid.UUID) -> Investigation:
    """pending -> investigating, and the current round's pending assignments -> running.

    The minimal transition the future orchestrator will own; anything else is refused."""
    investigation = await session.scalar(
        select(Investigation)
        .where(Investigation.id == investigation_id)
        .options(selectinload(Investigation.assignments))
        # Serializes concurrent starts: the second sees investigating and is refused.
        .with_for_update()
    )
    if investigation is None:
        raise InvestigationNotFound(investigation_id)
    if investigation.status != InvestigationStatus.PENDING:
        raise InvalidTransition(f"investigation is {investigation.status}, not pending")

    current = [a for a in investigation.assignments if a.round == investigation.current_round]
    for assignment in current:
        if assignment.status != AssignmentStatus.PENDING:
            raise InvalidTransition(f"assignment {assignment.id} is {assignment.status}")

    now = datetime.now(UTC)
    investigation.status = InvestigationStatus.INVESTIGATING
    investigation.started_at = now
    for assignment in current:
        assignment.status = AssignmentStatus.RUNNING
        assignment.started_at = now
    await session.commit()
    return investigation


FINAL_ASSIGNMENT_STATUSES = frozenset(
    {
        AssignmentStatus.COMPLETED,
        AssignmentStatus.FAILED,
        AssignmentStatus.TIMED_OUT,
        AssignmentStatus.CANCELLED,
    }
)


async def finish_assignment(
    session: AsyncSession, assignment_id: uuid.UUID, status: AssignmentStatus
) -> bool:
    """running -> a final status. False if the assignment was no longer running (someone
    else, e.g. a cancel, already closed it); that outcome is left alone."""
    if status not in FINAL_ASSIGNMENT_STATUSES:
        raise InvalidTransition(f"{status} is not a final assignment status")
    result = await session.execute(
        update(InvestigationAssignment)
        .where(
            InvestigationAssignment.id == assignment_id,
            InvestigationAssignment.status == AssignmentStatus.RUNNING,
        )
        .values(status=status, completed_at=datetime.now(UTC))
    )
    await session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]
