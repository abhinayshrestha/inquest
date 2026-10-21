import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from src.models import (
    AssignmentStatus,
    Environment,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationStatus,
    InvestigationTurn,
    TriggerType,
    TurnKind,
    TurnStatus,
)
from src.schemas.investigation import InvestigationCreate
from src.services.progress import append_progress_event

INITIAL_ROUND = 1
INITIAL_TURN = 1

S = InvestigationStatus
# Explicit lifecycle (D016). investigating = assignments running; correlating = Main
# reasoning over memory; idle = no turn running, follow-ups accepted; the last three are
# terminal.
INVESTIGATION_TRANSITIONS: dict[InvestigationStatus, frozenset[InvestigationStatus]] = {
    S.PENDING: frozenset({S.INVESTIGATING, S.CORRELATING, S.IDLE, S.FAILED, S.CANCELLED}),
    S.INVESTIGATING: frozenset({S.CORRELATING, S.IDLE, S.FAILED, S.CANCELLED}),
    S.CORRELATING: frozenset({S.INVESTIGATING, S.IDLE, S.FAILED, S.CANCELLED}),
    S.IDLE: frozenset({S.CORRELATING, S.INVESTIGATING, S.COMPLETED, S.CANCELLED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}
TERMINAL = frozenset({S.COMPLETED, S.FAILED, S.CANCELLED})


class EnvironmentNotFound(Exception):
    pass


class InvestigationNotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


def transition_investigation(
    session: AsyncSession, investigation: Investigation, to: InvestigationStatus, now: datetime
) -> None:
    """Applies one allowed status change to a locked investigation and records it."""
    current = investigation.status
    if to not in INVESTIGATION_TRANSITIONS[current]:
        raise InvalidTransition(f"investigation cannot go from {current} to {to}")
    investigation.status = to
    if investigation.started_at is None and to not in TERMINAL:
        investigation.started_at = now
    # Only closing an investigation completes it; idle keeps it open for follow-ups.
    if to in TERMINAL:
        investigation.completed_at = now
    append_progress_event(
        session,
        investigation.id,
        "investigation.status_changed",
        {"from": current.value, "to": to.value},
    )


async def lock_investigation(session: AsyncSession, investigation_id: uuid.UUID) -> Investigation:
    """SELECT ... FOR UPDATE: every lifecycle change of one investigation (and its turns
    and reports) is serialized on this row."""
    investigation = await session.scalar(
        select(Investigation).where(Investigation.id == investigation_id).with_for_update()
    )
    if investigation is None:
        raise InvestigationNotFound(investigation_id)
    return investigation


async def create_investigation(session: AsyncSession, data: InvestigationCreate) -> Investigation:
    """Investigation, its environment scope, turn 1 (the question) and the round-1
    assignment for that turn, in one transaction."""
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
    investigation.environments.append(InvestigationEnvironment(environment=environment))
    turn = InvestigationTurn(
        seq=INITIAL_TURN,
        kind=TurnKind.INITIAL,
        question=data.question,
        status=TurnStatus.PENDING,
    )
    investigation.turns.append(turn)
    # Kept until the Main Investigator writes objectives (Phase 3): the initial question
    # is still the first assignment's objective, so existing clients see no change.
    investigation.assignments.append(
        InvestigationAssignment(
            environment=environment,
            turn=turn,
            round=INITIAL_ROUND,
            objective=data.question,
            status=AssignmentStatus.PENDING,
        )
    )
    session.add(investigation)
    await session.flush()
    append_progress_event(
        session,
        investigation.id,
        "turn.created",
        {"seq": INITIAL_TURN, "kind": TurnKind.INITIAL.value},
        turn_id=turn.id,
    )
    # One commit: no investigation without its scope, first turn and first assignment.
    await session.commit()
    set_committed_value(investigation, "reports", [])  # a new investigation has none
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
            ),
            selectinload(Investigation.turns),
            selectinload(Investigation.reports),
        )
    )


async def start_investigation(session: AsyncSession, investigation_id: uuid.UUID) -> Investigation:
    """pending -> investigating: turn 1 and the current round's pending assignments start.

    The minimal transition the future orchestrator will own; anything else is refused."""
    investigation = await session.scalar(
        select(Investigation)
        .where(Investigation.id == investigation_id)
        .options(selectinload(Investigation.assignments), selectinload(Investigation.turns))
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
    turn = next(t for t in investigation.turns if t.seq == INITIAL_TURN)
    if turn.status != TurnStatus.PENDING:
        raise InvalidTransition(f"turn {turn.id} is {turn.status}")

    now = datetime.now(UTC)
    transition_investigation(session, investigation, InvestigationStatus.INVESTIGATING, now)
    turn.status, turn.started_at = TurnStatus.RUNNING, now
    append_progress_event(session, investigation.id, "turn.started", turn_id=turn.id)
    for assignment in current:
        assignment.status = AssignmentStatus.RUNNING
        assignment.started_at = now
        append_progress_event(
            session,
            investigation.id,
            "assignment.started",
            {"round": assignment.round, "environment_id": assignment.environment_id},
            turn_id=assignment.turn_id,
            assignment_id=assignment.id,
        )
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
    row = (
        await session.execute(
            update(InvestigationAssignment)
            .where(
                InvestigationAssignment.id == assignment_id,
                InvestigationAssignment.status == AssignmentStatus.RUNNING,
            )
            .values(status=status, completed_at=datetime.now(UTC))
            .returning(InvestigationAssignment.investigation_id, InvestigationAssignment.turn_id)
        )
    ).one_or_none()
    if row is not None:
        append_progress_event(
            session,
            row.investigation_id,
            "assignment.finished",
            {"status": status.value},
            turn_id=row.turn_id,
            assignment_id=assignment_id,
        )
    await session.commit()
    return row is not None
