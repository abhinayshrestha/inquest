"""Turns, report revisions, progress events and the idle lifecycle (Phase 2). Runs in the
rolled-back `session`; constraint checks that are deferred to commit are forced immediate."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentType,
    ImmutableRecordError,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationStatus,
    InvestigationTurn,
    ProgressEvent,
    TurnDecision,
    TurnKind,
    TurnStatus,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations, progress, turns
from src.services.investigations import InvalidTransition


async def _environment(session: AsyncSession) -> Environment:
    environment = Environment(name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL)
    session.add(environment)
    await session.flush()
    return environment


async def _created(session: AsyncSession) -> Investigation:
    environment = await _environment(session)
    return await investigations.create_investigation(
        session,
        InvestigationCreate(environment_id=environment.id, title="t", question="Why slow?"),
    )


async def _turn(session: AsyncSession, investigation_id: uuid.UUID, seq: int) -> InvestigationTurn:
    turn = await session.scalar(
        select(InvestigationTurn).where(
            InvestigationTurn.investigation_id == investigation_id, InvestigationTurn.seq == seq
        )
    )
    assert turn is not None
    return turn


async def _events(session: AsyncSession, investigation_id: uuid.UUID) -> list[str]:
    return [e.type for e in await progress.list_progress_events(session, investigation_id)]


async def _idle(session: AsyncSession) -> Investigation:
    """Created, started, its assignment finished and turn 1 answered."""
    investigation = await _created(session)
    await investigations.start_investigation(session, investigation.id)
    (assignment,) = await session.scalars(
        select(InvestigationAssignment).where(
            InvestigationAssignment.investigation_id == investigation.id
        )
    )
    await investigations.finish_assignment(session, assignment.id, AssignmentStatus.COMPLETED)
    turn = await _turn(session, investigation.id, 1)
    await turns.finish_turn(
        session,
        turn.id,
        TurnStatus.COMPLETED,
        decision=TurnDecision.INVESTIGATED,
        answer_json={"answer_md": "It is /slow."},
    )
    await session.refresh(investigation)
    return investigation


# --- creation and start --------------------------------------------------------------------


async def test_creation_records_turn_one_scope_and_links_the_assignment(session: AsyncSession):
    investigation = await _created(session)

    turn = await _turn(session, investigation.id, 1)
    assert (turn.kind, turn.status, turn.question) == (
        TurnKind.INITIAL,
        TurnStatus.PENDING,
        "Why slow?",
    )
    (assignment,) = await session.scalars(
        select(InvestigationAssignment).where(
            InvestigationAssignment.investigation_id == investigation.id
        )
    )
    assert assignment.turn_id == turn.id and assignment.objective == "Why slow?"
    scope = await session.get(
        InvestigationEnvironment, (investigation.id, assignment.environment_id)
    )
    assert scope is not None
    assert await _events(session, investigation.id) == ["turn.created"]


async def test_start_runs_turn_one_and_announces_it(session: AsyncSession):
    investigation = await _created(session)

    await investigations.start_investigation(session, investigation.id)

    assert (await _turn(session, investigation.id, 1)).status == TurnStatus.RUNNING
    assert await _events(session, investigation.id) == [
        "turn.created",
        "investigation.status_changed",
        "turn.started",
        "assignment.started",
    ]


# --- finishing turns -----------------------------------------------------------------------


async def test_finished_turn_leaves_the_investigation_idle_and_open(session: AsyncSession):
    investigation = await _idle(session)

    assert investigation.status == InvestigationStatus.IDLE
    assert investigation.completed_at is None  # idle is not closed
    turn = await _turn(session, investigation.id, 1)
    assert (turn.status, turn.decision) == (TurnStatus.COMPLETED, TurnDecision.INVESTIGATED)
    assert turn.answer_json == {"answer_md": "It is /slow."} and turn.completed_at is not None
    events = await _events(session, investigation.id)
    assert events[-3:] == ["assignment.finished", "investigation.status_changed", "turn.completed"]


async def test_turn_cannot_complete_while_its_assignments_run(session: AsyncSession):
    investigation = await _created(session)
    await investigations.start_investigation(session, investigation.id)
    turn = await _turn(session, investigation.id, 1)

    with pytest.raises(InvalidTransition, match="1 active assignments"):
        await turns.finish_turn(
            session, turn.id, TurnStatus.COMPLETED, decision=TurnDecision.INVESTIGATED
        )


async def test_cancelling_a_turn_cancels_its_assignments(session: AsyncSession):
    investigation = await _created(session)
    await investigations.start_investigation(session, investigation.id)
    turn = await _turn(session, investigation.id, 1)

    await turns.finish_turn(session, turn.id, TurnStatus.CANCELLED)

    statuses = await session.scalars(
        select(InvestigationAssignment.status).where(
            InvestigationAssignment.investigation_id == investigation.id
        )
    )
    assert set(statuses) == {AssignmentStatus.CANCELLED}
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.IDLE


@pytest.mark.parametrize(
    ("status", "kwargs", "message"),
    [
        (TurnStatus.COMPLETED, {}, "needs a decision"),
        (TurnStatus.FAILED, {"answer_json": {"a": 1}}, "only a completed turn has an answer"),
        (TurnStatus.RUNNING, {}, "does not finish a turn"),
    ],
)
async def test_finish_turn_arguments_are_checked(session: AsyncSession, status, kwargs, message):
    investigation = await _created(session)
    turn = await _turn(session, investigation.id, 1)
    with pytest.raises(InvalidTransition, match=message):
        await turns.finish_turn(session, turn.id, status, **kwargs)


# --- follow-ups ----------------------------------------------------------------------------


async def test_follow_up_turns_are_numbered_and_run_through_correlating(session: AsyncSession):
    investigation = await _idle(session)

    turn = await turns.create_follow_up_turn(session, investigation.id, "Is CPU involved?")
    assert (turn.seq, turn.kind, turn.status) == (2, TurnKind.FOLLOW_UP, TurnStatus.PENDING)

    await turns.start_turn(session, turn.id)
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.CORRELATING

    await turns.finish_turn(
        session, turn.id, TurnStatus.COMPLETED, decision=TurnDecision.REUSED_EVIDENCE
    )
    await session.refresh(investigation)
    assert investigation.status == InvestigationStatus.IDLE
    third = await turns.create_follow_up_turn(session, investigation.id, "When did it start?")
    assert third.seq == 3
    assert [t.seq for t in await turns.list_turns(session, investigation.id)] == [1, 2, 3]


async def test_second_follow_up_is_refused_while_one_is_active(session: AsyncSession):
    investigation = await _idle(session)
    await turns.create_follow_up_turn(session, investigation.id, "first")

    with pytest.raises(turns.TurnConflict):
        await turns.create_follow_up_turn(session, investigation.id, "second")


async def test_follow_up_needs_an_idle_investigation(session: AsyncSession):
    investigation = await _created(session)
    await investigations.start_investigation(session, investigation.id)
    turn = await _turn(session, investigation.id, 1)
    await turns.finish_turn(session, turn.id, TurnStatus.CANCELLED)
    closed = await investigations.lock_investigation(session, investigation.id)
    investigations.transition_investigation(
        session, closed, InvestigationStatus.COMPLETED, closed.started_at
    )
    await session.commit()

    with pytest.raises(InvalidTransition, match="idle investigation, not completed"):
        await turns.create_follow_up_turn(session, investigation.id, "more?")


async def test_follow_up_on_a_pending_investigation_is_refused(session: AsyncSession):
    investigation = await _created(session)
    # Turn 1 is still pending: that is the active turn.
    with pytest.raises(turns.TurnConflict):
        await turns.create_follow_up_turn(session, investigation.id, "q")


# --- lifecycle table -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "to", "allowed"),
    [
        (InvestigationStatus.PENDING, InvestigationStatus.INVESTIGATING, True),
        (InvestigationStatus.INVESTIGATING, InvestigationStatus.IDLE, True),
        (InvestigationStatus.IDLE, InvestigationStatus.CORRELATING, True),
        (InvestigationStatus.IDLE, InvestigationStatus.COMPLETED, True),
        (InvestigationStatus.IDLE, InvestigationStatus.FAILED, False),
        (InvestigationStatus.COMPLETED, InvestigationStatus.INVESTIGATING, False),
        (InvestigationStatus.CANCELLED, InvestigationStatus.IDLE, False),
        (InvestigationStatus.PENDING, InvestigationStatus.COMPLETED, False),
    ],
)
async def test_investigation_transitions(session: AsyncSession, start, to, allowed):
    investigation = await _created(session)
    investigation.status = start
    if allowed:
        investigations.transition_investigation(
            session, investigation, to, investigation.created_at
        )
        assert investigation.status == to
        assert (investigation.completed_at is not None) == (to in investigations.TERMINAL)
    else:
        with pytest.raises(InvalidTransition):
            investigations.transition_investigation(
                session, investigation, to, investigation.created_at
            )


# --- reports -------------------------------------------------------------------------------


async def test_report_revisions_are_versioned_and_mirrored(session: AsyncSession):
    investigation = await _created(session)
    await investigations.start_investigation(session, investigation.id)
    turn_1 = await _turn(session, investigation.id, 1)

    v1 = await turns.record_report_revision(session, turn_1.id, {"summary": "v1"})
    assert v1.version == 1
    with pytest.raises(turns.ReportConflict):
        await turns.record_report_revision(session, turn_1.id, {"summary": "again"})

    await session.execute(
        text("UPDATE investigation_assignments SET status = 'completed' WHERE turn_id = :t"),
        {"t": turn_1.id},
    )
    await turns.finish_turn(
        session, turn_1.id, TurnStatus.COMPLETED, decision=TurnDecision.INVESTIGATED
    )
    with pytest.raises(InvalidTransition, match="only a running turn"):
        await turns.record_report_revision(session, turn_1.id, {"summary": "late"})

    turn_2 = await turns.create_follow_up_turn(session, investigation.id, "more?")
    await turns.start_turn(session, turn_2.id)
    v2 = await turns.record_report_revision(session, turn_2.id, {"summary": "v2"})

    assert v2.version == 2
    assert [r.version for r in await turns.list_reports(session, investigation.id)] == [1, 2]
    assert (await turns.latest_report(session, investigation.id)).id == v2.id
    await session.refresh(investigation)
    assert investigation.final_result_json == {"summary": "v2"}
    assert "report.revised" in await _events(session, investigation.id)


async def test_report_versions_are_immutable(session: AsyncSession):
    investigation = await _created(session)
    await investigations.start_investigation(session, investigation.id)
    report = await turns.record_report_revision(
        session, (await _turn(session, investigation.id, 1)).id, {"summary": "v1"}
    )
    report.report_json = {"summary": "rewritten"}
    with pytest.raises(ImmutableRecordError):
        await session.flush()


# --- progress events -----------------------------------------------------------------------


async def test_events_replay_after_a_cursor(session: AsyncSession):
    investigation = await _idle(session)
    events = await progress.list_progress_events(session, investigation.id)
    later = await progress.list_progress_events(session, investigation.id, after_id=events[2].id)
    assert [e.id for e in later] == [e.id for e in events[3:]]
    assert [e.id for e in events] == sorted(e.id for e in events)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"nested": {"deeper": {"x": 1}}}, "one object level"),
        ({"blob": b"bytes"}, "scalars"),
        ({"rows": list(range(51))}, "at most 50"),
        ({f"k{i}": i for i in range(33)}, "at most 32 keys"),
        ({f"k{i}": "x" * 400 for i in range(30)}, "exceeds 8192 bytes"),
    ],
)
def test_payloads_that_could_carry_bulk_data_are_rejected(payload, message):
    with pytest.raises(progress.InvalidEvent, match=message):
        progress.sanitize_payload(payload)


def test_payload_strings_and_ids_are_normalized():
    run_id = uuid.uuid4()
    clean = progress.sanitize_payload({"id": run_id, "note": "x" * 600, "ok": True})
    assert clean["id"] == str(run_id) and len(clean["note"]) == 500 and clean["ok"] is True


async def test_unknown_event_types_and_updates_are_refused(session: AsyncSession):
    investigation = await _created(session)
    with pytest.raises(progress.InvalidEvent, match="unknown event type"):
        progress.append_progress_event(session, investigation.id, "turn.exploded")
    event = progress.append_progress_event(session, investigation.id, "turn.decision", {"a": 1})
    await session.flush()
    event.payload_json = {"a": 2}
    with pytest.raises(ImmutableRecordError):
        await session.flush()


# --- database constraints ------------------------------------------------------------------


async def test_database_allows_one_active_turn(session: AsyncSession):
    investigation = await _created(session)
    with pytest.raises(IntegrityError, match="uq_investigation_turns_one_active"):
        await session.execute(
            text(
                "INSERT INTO investigation_turns (id, investigation_id, seq, kind, question) "
                "VALUES (gen_random_uuid(), :i, 2, 'follow_up', 'q')"
            ),
            {"i": investigation.id},
        )


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ("2, 'initial', 'completed', NULL", "ck_investigation_turns_initial_is_first"),
        ("2, 'follow_up', 'running', '{\"a\": 1}'", "ck_investigation_turns_answer_when_done"),
    ],
)
async def test_turn_checks(session: AsyncSession, values, constraint):
    investigation = await _idle(session)
    with pytest.raises(IntegrityError, match=constraint):
        await session.execute(
            text(
                "INSERT INTO investigation_turns "
                "(id, investigation_id, seq, kind, status, answer_json, question) "
                f"VALUES (gen_random_uuid(), :i, {values}, 'q')"
            ),
            {"i": investigation.id},
        )


async def test_assignment_environment_must_be_in_scope(session: AsyncSession):
    investigation = await _created(session)
    outsider = await _environment(session)
    turn = await _turn(session, investigation.id, 1)
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    session.add(
        InvestigationAssignment(
            investigation_id=investigation.id,
            environment_id=outsider.id,
            turn_id=turn.id,
            round=2,
            objective="x",
        )
    )
    with pytest.raises(IntegrityError, match="fk_investigation_assignments_scope"):
        await session.flush()


async def test_records_cannot_point_at_another_investigations_turn(session: AsyncSession):
    mine, theirs = await _created(session), await _created(session)
    their_turn = await _turn(session, theirs.id, 1)
    with pytest.raises(IntegrityError, match="fk_progress_events_turn_id_investigation_turns"):
        await session.execute(
            text(
                "INSERT INTO progress_events (investigation_id, turn_id, type) "
                "VALUES (:i, :t, 'turn.started')"
            ),
            {"i": mine.id, "t": their_turn.id},
        )


async def test_event_type_format_is_checked(session: AsyncSession):
    investigation = await _created(session)
    with pytest.raises(IntegrityError, match="ck_progress_events_type_format"):
        await session.execute(
            text("INSERT INTO progress_events (investigation_id, type) VALUES (:i, 'Bad Type')"),
            {"i": investigation.id},
        )


# --- API compatibility ---------------------------------------------------------------------


async def test_api_create_keeps_its_shape_and_adds_turns(client: AsyncClient, session, tenant):
    environment = (
        await client.post(
            "/environments",
            json={
                "organization_id": str(tenant.organization_id),
                "name": "ubuntu-vm",
                "type": "local",
            },
        )
    ).json()
    created = await client.post(
        "/investigations",
        json={"environment_id": environment["id"], "title": "t", "question": "Why slow?"},
    )
    assert created.status_code == 201
    body = created.json()
    (assignment,) = body["assignments"]
    (turn,) = body["turns"]
    assert assignment["objective"] == "Why slow?" and assignment["status"] == "pending"
    assert assignment["turn_id"] == turn["id"]
    assert (turn["seq"], turn["kind"], turn["status"]) == (1, "initial", "pending")

    fetched = (await client.get(f"/investigations/{body['id']}")).json()
    assert fetched["turns"] == body["turns"]
    assert await session.scalar(select(func.count()).select_from(ProgressEvent)) == 1
