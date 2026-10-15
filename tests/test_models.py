import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    Investigation,
    InvestigationAssignment,
    InvestigationStatus,
    TriggerType,
)


async def _seed(session: AsyncSession) -> tuple[Environment, Investigation]:
    environment = Environment(name="ubuntu-vm", type=EnvironmentType.LOCAL)
    investigation = Investigation(
        title="API latency spike",
        question="Why did /checkout p99 latency spike?",
        trigger_type=TriggerType.MANUAL,
    )
    investigation.assignments.append(
        InvestigationAssignment(
            environment=environment, round=1, objective="Check host CPU and memory pressure"
        )
    )
    session.add_all([environment, investigation])
    await session.flush()
    return environment, investigation


async def test_assignment_links_environment_and_investigation(session: AsyncSession) -> None:
    environment, investigation = await _seed(session)
    session.expunge_all()

    loaded = await session.scalar(
        select(InvestigationAssignment).options(
            selectinload(InvestigationAssignment.environment),
            selectinload(InvestigationAssignment.investigation),
        )
    )

    assert loaded is not None
    assert loaded.environment.id == environment.id
    assert loaded.investigation.id == investigation.id
    assert loaded.status is AssignmentStatus.PENDING
    assert loaded.started_at is None
    assert loaded.created_at.tzinfo is not None
    assert loaded.environment.status is EnvironmentStatus.ACTIVE
    assert loaded.environment.metadata_json == {}
    assert loaded.investigation.status is InvestigationStatus.PENDING
    assert loaded.investigation.current_round == 1
    assert loaded.investigation.severity is None
    assert loaded.investigation.started_at is None


async def test_enum_stored_as_lowercase_value(session: AsyncSession) -> None:
    _, investigation = await _seed(session)
    await session.execute(
        text(
            "UPDATE investigation_assignments SET status = 'timed_out' WHERE investigation_id = :id"
        ),
        {"id": investigation.id},
    )
    session.expunge_all()

    status = await session.scalar(select(InvestigationAssignment.status))

    assert status is AssignmentStatus.TIMED_OUT


async def test_deleting_investigation_cascades_to_assignments(session: AsyncSession) -> None:
    _, investigation = await _seed(session)

    await session.delete(investigation)
    await session.flush()

    count = await session.scalar(select(func.count()).select_from(InvestigationAssignment))
    assert count == 0


async def test_deleting_environment_with_assignments_is_restricted(session: AsyncSession) -> None:
    environment, _ = await _seed(session)

    await session.delete(environment)
    with pytest.raises(IntegrityError, match="fk_investigation_assignments_environment_id"):
        await session.flush()


async def test_invalid_enum_value_rejected_by_database(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_environments_environmenttype"):
        await session.execute(
            text("INSERT INTO environments (id, name, type) VALUES (gen_random_uuid(), 'x', 'gcp')")
        )


async def test_round_must_be_positive(session: AsyncSession) -> None:
    environment, investigation = await _seed(session)
    session.add(
        InvestigationAssignment(
            investigation_id=investigation.id,
            environment_id=environment.id,
            round=0,
            objective="invalid",
        )
    )

    with pytest.raises(IntegrityError, match="ck_investigation_assignments_round_positive"):
        await session.flush()
