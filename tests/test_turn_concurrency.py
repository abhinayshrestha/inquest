"""Concurrency of follow-up turns and report revisions. Commits for real: row locks and
unique indexes only interact across separate transactions. Rows are deleted afterwards."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from src.db.session import SessionLocal
from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentType,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationReport,
    InvestigationTurn,
    ProgressEvent,
    TurnDecision,
    TurnStatus,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations, turns


@pytest.fixture
async def idle_investigation() -> AsyncIterator[uuid.UUID]:
    async with SessionLocal() as s:
        environment = Environment(name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL)
        s.add(environment)
        await s.commit()
        environment_id = environment.id
        investigation = await investigations.create_investigation(
            s, InvestigationCreate(environment_id=environment_id, title="t", question="q")
        )
        investigation_id = investigation.id
        await investigations.start_investigation(s, investigation_id)
        assignment_id = await s.scalar(
            select(InvestigationAssignment.id).where(
                InvestigationAssignment.investigation_id == investigation_id
            )
        )
        await investigations.finish_assignment(s, assignment_id, AssignmentStatus.COMPLETED)
        turn_id = await s.scalar(
            select(InvestigationTurn.id).where(
                InvestigationTurn.investigation_id == investigation_id
            )
        )
        await turns.finish_turn(
            s, turn_id, TurnStatus.COMPLETED, decision=TurnDecision.INVESTIGATED
        )
    yield investigation_id
    async with SessionLocal() as s, s.begin():
        for model in (ProgressEvent, InvestigationReport, InvestigationAssignment):
            await s.execute(delete(model).where(model.investigation_id == investigation_id))
        await s.execute(
            delete(InvestigationTurn).where(InvestigationTurn.investigation_id == investigation_id)
        )
        await s.execute(
            delete(InvestigationEnvironment).where(
                InvestigationEnvironment.investigation_id == investigation_id
            )
        )
        await s.execute(delete(Investigation).where(Investigation.id == investigation_id))
        await s.execute(delete(Environment).where(Environment.id == environment_id))


async def _follow_up(investigation_id: uuid.UUID, question: str) -> object:
    async with SessionLocal() as s:
        try:
            return await turns.create_follow_up_turn(s, investigation_id, question)
        except turns.TurnConflict as exc:
            return exc


async def test_concurrent_follow_ups_create_exactly_one_turn(idle_investigation: uuid.UUID):
    results = await asyncio.gather(
        *(_follow_up(idle_investigation, f"question {i}") for i in range(5))
    )

    created = [r for r in results if isinstance(r, InvestigationTurn)]
    assert len(created) == 1 and created[0].seq == 2
    assert sum(isinstance(r, turns.TurnConflict) for r in results) == 4
    async with SessionLocal() as s:
        assert len(await turns.list_turns(s, idle_investigation)) == 2


async def test_index_refuses_a_second_active_turn_even_without_the_lock(
    idle_investigation: uuid.UUID,
):
    insert = text(
        "INSERT INTO investigation_turns (id, investigation_id, seq, kind, question) "
        "VALUES (gen_random_uuid(), :i, :seq, 'follow_up', 'q')"
    )
    async with SessionLocal() as first, SessionLocal() as second:
        await first.execute(insert, {"i": idle_investigation, "seq": 2})
        # The second insert waits on the first transaction's index entry...
        pending = asyncio.create_task(second.execute(insert, {"i": idle_investigation, "seq": 3}))
        await asyncio.sleep(0.2)
        assert not pending.done()
        await first.commit()
        # ...and fails once the first commits.
        with pytest.raises(IntegrityError, match="uq_investigation_turns_one_active"):
            await pending
        await second.rollback()


async def _revise(turn_id: uuid.UUID, n: int) -> object:
    async with SessionLocal() as s:
        try:
            return await turns.record_report_revision(s, turn_id, {"summary": f"v{n}"})
        except turns.ReportConflict as exc:
            return exc


async def test_concurrent_revisions_of_one_turn_record_one_version(idle_investigation: uuid.UUID):
    async with SessionLocal() as s:
        turn = await turns.create_follow_up_turn(s, idle_investigation, "more?")
        await turns.start_turn(s, turn.id)
        turn_id = turn.id

    results = await asyncio.gather(*(_revise(turn_id, n) for n in range(4)))

    reports = [r for r in results if isinstance(r, InvestigationReport)]
    assert len(reports) == 1 and reports[0].version == 1
    assert sum(isinstance(r, turns.ReportConflict) for r in results) == 3
