"""Phase 3 turn services: dispatch ownership of the pre-created assignment, rounds, the
atomic and idempotent end of a turn, failures and legacy reconciliation."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentType,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    TurnDecision,
    TurnStatus,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations, progress, turns
from src.services.investigations import InvalidTransition


async def _env(session: AsyncSession) -> Environment:
    environment = Environment(name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL)
    session.add(environment)
    await session.flush()
    return environment


async def _running_turn(session: AsyncSession) -> tuple[Investigation, InvestigationTurn]:
    """Created via the API service (round-1 assignment pre-created), turn 1 started the
    Main way: start_turn, not start_investigation."""
    environment = await _env(session)
    investigation = await investigations.create_investigation(
        session, InvestigationCreate(environment_id=environment.id, title="t", question="Why?")
    )
    turn = await session.scalar(
        select(InvestigationTurn).where(InvestigationTurn.investigation_id == investigation.id)
    )
    await turns.start_turn(session, turn.id)
    return investigation, turn


async def _assignments(session: AsyncSession, investigation_id) -> list[InvestigationAssignment]:
    return list(
        await session.scalars(
            select(InvestigationAssignment)
            .where(InvestigationAssignment.investigation_id == investigation_id)
            .order_by(InvestigationAssignment.round, InvestigationAssignment.created_at)
        )
    )


async def _scope(session: AsyncSession, investigation: Investigation, environment: Environment):
    session.add(
        InvestigationEnvironment(investigation_id=investigation.id, environment_id=environment.id)
    )
    await session.flush()


# --- dispatch ------------------------------------------------------------------------------


async def test_first_dispatch_adopts_the_pre_created_assignment(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)

    ids = await turns.dispatch_round(
        session, turn.id, [(pre.environment_id, "Find the slowest operation in traces")]
    )

    (assignment,) = await _assignments(session, investigation.id)  # no duplicate
    assert ids == [pre.id] == [assignment.id]
    assert (assignment.round, assignment.status) == (1, AssignmentStatus.RUNNING)
    assert assignment.objective == "Find the slowest operation in traces"
    await session.refresh(investigation)
    assert (investigation.status, investigation.current_round) == (
        InvestigationStatus.INVESTIGATING,
        1,
    )
    types = [e.type for e in await progress.list_progress_events(session, investigation.id)]
    assert types[-2:] == ["round.started", "assignment.started"]


async def test_replayed_dispatch_returns_the_running_round(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    first = await turns.dispatch_round(session, turn.id, [(pre.environment_id, "a")])

    again = await turns.dispatch_round(session, turn.id, [(pre.environment_id, "b")])

    assert again == first
    assert len(await _assignments(session, investigation.id)) == 1


async def test_later_rounds_add_assignments_and_new_scoped_environments(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    await turns.dispatch_round(session, turn.id, [(pre.environment_id, "round 1")])
    await investigations.finish_assignment(session, pre.id, AssignmentStatus.COMPLETED)
    await turns.end_round(session, turn.id)
    second_env = await _env(session)
    await _scope(session, investigation, second_env)

    ids = await turns.dispatch_round(
        session, turn.id, [(pre.environment_id, "round 2 a"), (second_env.id, "round 2 b")]
    )

    rows = await _assignments(session, investigation.id)
    assert [(a.round, a.status) for a in rows] == [
        (1, AssignmentStatus.COMPLETED),
        (2, AssignmentStatus.RUNNING),
        (2, AssignmentStatus.RUNNING),
    ]
    assert set(ids) == {a.id for a in rows[1:]}
    await session.refresh(investigation)
    assert investigation.current_round == 2


async def test_pending_assignments_main_did_not_ask_for_are_cancelled(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    other = await _env(session)
    await _scope(session, investigation, other)

    ids = await turns.dispatch_round(session, turn.id, [(other.id, "only the other one")])

    rows = {a.id: a for a in await _assignments(session, investigation.id)}
    assert rows[pre.id].status == AssignmentStatus.CANCELLED
    (new_id,) = ids
    assert (rows[new_id].round, rows[new_id].environment_id) == (1, other.id)


async def test_dispatch_refuses_out_of_scope_environments_and_idle_turns(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    outsider = await _env(session)
    with pytest.raises(InvalidTransition, match="scope"):
        await turns.dispatch_round(session, turn.id, [(outsider.id, "x")])
    await turns.finish_turn(session, turn.id, TurnStatus.CANCELLED)
    with pytest.raises(InvalidTransition, match="only a running turn"):
        await turns.dispatch_round(session, turn.id, [(outsider.id, "x")])


async def test_end_round_fails_leftovers_and_is_idempotent(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    await turns.dispatch_round(session, turn.id, [(pre.environment_id, "a")])

    await turns.end_round(session, turn.id)
    await turns.end_round(session, turn.id)

    (assignment,) = await _assignments(session, investigation.id)
    assert assignment.status == AssignmentStatus.FAILED
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.CORRELATING
    types = [e.type for e in await progress.list_progress_events(session, investigation.id)]
    assert types.count("round.finished") == 1


# --- end of a turn -------------------------------------------------------------------------


async def test_complete_turn_writes_everything_once(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    await session.delete(pre)  # no work: answer from (empty) memory
    await session.flush()
    report = {"outcome": "inconclusive", "summary": "s"}

    done = await turns.complete_turn(
        session, turn.id, decision=TurnDecision.REUSED_EVIDENCE,
        answer_json={"answer_md": "a"}, report_json=report,
    )  # fmt: skip
    replay = await turns.complete_turn(
        session, turn.id, decision=TurnDecision.INVESTIGATED,
        answer_json={"answer_md": "different"}, report_json={"outcome": "x"},
    )  # fmt: skip

    assert replay.id == done.id
    assert done.status == TurnStatus.COMPLETED
    assert done.answer_json == {"answer_md": "a", "report_version": 1}
    assert done.decision == TurnDecision.REUSED_EVIDENCE
    assert await session.scalar(select(func.count()).select_from(InvestigationReport)) == 1
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.IDLE
    assert investigation.final_result_json == report


async def test_complete_turn_is_atomic(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    await turns.dispatch_round(session, turn.id, [(pre.environment_id, "a")])

    # Still-running assignment: the finish step refuses, so the revision must not land.
    with pytest.raises(InvalidTransition, match="active assignments"):
        await turns.complete_turn(
            session, turn.id, decision=TurnDecision.INVESTIGATED,
            answer_json={"answer_md": "a"}, report_json={"outcome": "inconclusive"},
        )  # fmt: skip
    await session.rollback()

    assert await session.scalar(select(func.count()).select_from(InvestigationReport)) == 0
    await session.refresh(turn)
    assert turn.status == TurnStatus.RUNNING


async def test_complete_turn_without_a_report(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    await session.delete(pre)
    await session.flush()
    done = await turns.complete_turn(
        session, turn.id, decision=TurnDecision.CLARIFICATION, answer_json={"answer_md": "?"}
    )
    assert done.answer_json["report_version"] is None
    await session.refresh(investigation)
    assert investigation.final_result_json is None


async def test_fail_turn_closes_work_and_keeps_history(session: AsyncSession):
    investigation, turn = await _running_turn(session)
    (pre,) = await _assignments(session, investigation.id)
    await turns.dispatch_round(session, turn.id, [(pre.environment_id, "a")])

    assert await turns.fail_turn(session, turn.id, "model_error") is True
    assert await turns.fail_turn(session, turn.id, "again") is False

    await session.refresh(turn)
    assert (turn.status, turn.error_code) == (TurnStatus.FAILED, "model_error")
    (assignment,) = await _assignments(session, investigation.id)
    assert assignment.status == AssignmentStatus.FAILED
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.IDLE
    # Retry is a new turn, and allowed.
    retry = await turns.create_follow_up_turn(session, investigation.id, "Why? (retry)")
    assert retry.seq == 2


async def test_failing_a_pending_turn_cancels_it(session: AsyncSession):
    environment = await _env(session)
    investigation = await investigations.create_investigation(
        session, InvestigationCreate(environment_id=environment.id, title="t", question="q")
    )
    turn = await session.scalar(
        select(InvestigationTurn).where(InvestigationTurn.investigation_id == investigation.id)
    )
    await turns.fail_turn(session, turn.id, "turn_timeout")
    await session.refresh(turn)
    assert (turn.status, turn.error_code) == (TurnStatus.CANCELLED, "turn_timeout")


async def test_legacy_running_turn_is_reconciled_not_resumed(session: AsyncSession):
    environment = await _env(session)
    investigation = await investigations.create_investigation(
        session, InvestigationCreate(environment_id=environment.id, title="t", question="q")
    )
    await investigations.start_investigation(session, investigation.id)  # legacy path
    turn = await session.scalar(
        select(InvestigationTurn).where(InvestigationTurn.investigation_id == investigation.id)
    )

    assert await turns.reconcile_legacy_turn(session, turn.id) is True
    assert await turns.reconcile_legacy_turn(session, turn.id) is False

    await session.refresh(turn)
    assert (turn.status, turn.error_code) == (TurnStatus.FAILED, turns.LEGACY_ERROR)
    (assignment,) = await _assignments(session, investigation.id)
    assert assignment.status == AssignmentStatus.TIMED_OUT
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.IDLE
