"""Test helper: an assignment with the turn and environment scope the schema requires."""

import uuid
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentType,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationTurn,
    TriggerType,
    TurnKind,
    TurnStatus,
)


async def add_assignment(
    session: AsyncSession,
    *,
    investigation: Investigation | None = None,
    environment: Environment | None = None,
    **fields: Any,
) -> InvestigationAssignment:
    """Reuses the investigation's turn 1 and an existing scope row when there are any;
    otherwise creates them. Adds to the session without flushing."""
    investigation = investigation or Investigation(
        title="API latency spike",
        question="Why did /checkout p99 latency spike?",
        trigger_type=TriggerType.MANUAL,
    )
    environment = environment or Environment(
        name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL
    )
    turn, scoped = None, False
    if inspect(investigation).persistent:
        turn = await session.scalar(
            select(InvestigationTurn).where(
                InvestigationTurn.investigation_id == investigation.id,
                InvestigationTurn.seq == 1,
            )
        )
        if inspect(environment).persistent:
            scoped = (
                await session.get(InvestigationEnvironment, (investigation.id, environment.id))
                is not None
            )
    if turn is None:
        running = fields.get("status") == AssignmentStatus.RUNNING
        turn = InvestigationTurn(
            investigation=investigation,
            seq=1,
            kind=TurnKind.INITIAL,
            question=investigation.question,
            status=TurnStatus.RUNNING if running else TurnStatus.PENDING,
        )
    if not scoped:
        session.add(InvestigationEnvironment(investigation=investigation, environment=environment))
    assignment = InvestigationAssignment(
        investigation=investigation,
        environment=environment,
        turn=turn,
        round=fields.pop("round", 1),
        objective=fields.pop("objective", "Investigate inquest-demo /checkout latency"),
        **fields,
    )
    session.add(assignment)
    return assignment
