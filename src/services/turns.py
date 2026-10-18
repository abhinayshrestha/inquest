"""Turns (the conversation) and report revisions of an investigation (D013).

Every operation locks the investigation row first, so turn creation, turn lifecycle and
report versioning of one investigation are serialized. The partial unique index on
active turns and the unique report version are the database backstops. A refusal leaves
the transaction (and its lock) to the caller to roll back, like the other services.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    ACTIVE_TURN_STATUSES,
    AssignmentStatus,
    Investigation,
    InvestigationAssignment,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    TurnDecision,
    TurnKind,
    TurnStatus,
)
from src.services.investigations import (
    TERMINAL,
    InvalidTransition,
    lock_investigation,
    transition_investigation,
)
from src.services.progress import append_progress_event

T = TurnStatus
TURN_TRANSITIONS: dict[TurnStatus, frozenset[TurnStatus]] = {
    T.PENDING: frozenset({T.RUNNING, T.CANCELLED}),
    T.RUNNING: frozenset({T.COMPLETED, T.FAILED, T.CANCELLED}),
    T.COMPLETED: frozenset(),
    T.FAILED: frozenset(),
    T.CANCELLED: frozenset(),
}
FINISHED = frozenset({T.COMPLETED, T.FAILED, T.CANCELLED})
ACTIVE_ASSIGNMENTS = (AssignmentStatus.PENDING, AssignmentStatus.RUNNING)


class TurnNotFound(Exception):
    pass


class TurnConflict(Exception):
    """Another turn of the investigation is pending or running."""


class ReportConflict(Exception):
    """The turn already recorded its revision."""


async def _locked_turn(
    session: AsyncSession, turn_id: uuid.UUID
) -> tuple[InvestigationTurn, Investigation]:
    """Investigation row first, then the turn: the same lock order everywhere."""
    investigation_id = await session.scalar(
        select(InvestigationTurn.investigation_id).where(InvestigationTurn.id == turn_id)
    )
    if investigation_id is None:
        raise TurnNotFound(turn_id)
    investigation = await lock_investigation(session, investigation_id)
    turn = await session.scalar(
        select(InvestigationTurn).where(InvestigationTurn.id == turn_id).with_for_update()
    )
    assert turn is not None
    return turn, investigation


def _move(turn: InvestigationTurn, to: TurnStatus) -> None:
    if to not in TURN_TRANSITIONS[turn.status]:
        raise InvalidTransition(f"turn cannot go from {turn.status} to {to}")
    turn.status = to


async def create_follow_up_turn(
    session: AsyncSession, investigation_id: uuid.UUID, question: str
) -> InvestigationTurn:
    """A new question for an idle investigation. Refused while another turn is active or
    once the investigation is closed."""
    investigation = await lock_investigation(session, investigation_id)
    active = await session.scalar(
        select(func.count())
        .select_from(InvestigationTurn)
        .where(
            InvestigationTurn.investigation_id == investigation_id,
            InvestigationTurn.status.in_(ACTIVE_TURN_STATUSES),
        )
    )
    if active:
        raise TurnConflict(f"investigation {investigation_id} already has an active turn")
    if investigation.status != InvestigationStatus.IDLE:
        raise InvalidTransition(
            f"follow-ups need an idle investigation, not {investigation.status}"
        )

    seq = (
        await session.scalar(
            select(func.coalesce(func.max(InvestigationTurn.seq), 0)).where(
                InvestigationTurn.investigation_id == investigation_id
            )
        )
    ) + 1
    turn = InvestigationTurn(
        investigation_id=investigation_id,
        seq=seq,
        kind=TurnKind.FOLLOW_UP,
        question=question,
        status=TurnStatus.PENDING,
    )
    session.add(turn)
    try:
        await session.flush()
        append_progress_event(
            session,
            investigation_id,
            "turn.created",
            {"seq": seq, "kind": TurnKind.FOLLOW_UP.value},
            turn_id=turn.id,
        )
        await session.commit()
    except IntegrityError as exc:
        # Backstop for writers that skipped the lock: the one-active-turn index.
        await session.rollback()
        if "uq_investigation_turns_one_active" in str(exc.orig):
            raise TurnConflict(
                f"investigation {investigation_id} already has an active turn"
            ) from None
        raise
    return turn


async def start_turn(session: AsyncSession, turn_id: uuid.UUID) -> InvestigationTurn:
    """pending -> running; the investigation moves to correlating (Main reasoning)."""
    turn, investigation = await _locked_turn(session, turn_id)
    now = datetime.now(UTC)
    _move(turn, TurnStatus.RUNNING)
    turn.started_at = now
    transition_investigation(session, investigation, InvestigationStatus.CORRELATING, now)
    append_progress_event(session, investigation.id, "turn.started", turn_id=turn.id)
    await session.commit()
    return turn


async def finish_turn(
    session: AsyncSession,
    turn_id: uuid.UUID,
    status: TurnStatus,
    *,
    decision: TurnDecision | None = None,
    answer_json: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> InvestigationTurn:
    """Ends a turn and returns the investigation to idle, ready for follow-ups.

    completed needs a decision and refuses while any of the turn's assignments is still
    active; cancelled also cancels them; failed records an error code."""
    if status not in FINISHED:
        raise InvalidTransition(f"{status} does not finish a turn")
    if status == TurnStatus.COMPLETED and decision is None:
        raise InvalidTransition("a completed turn needs a decision")
    if status != TurnStatus.COMPLETED and answer_json is not None:
        raise InvalidTransition("only a completed turn has an answer")

    turn, investigation = await _locked_turn(session, turn_id)
    now = datetime.now(UTC)
    active_filter = (
        InvestigationAssignment.turn_id == turn.id,
        InvestigationAssignment.status.in_(ACTIVE_ASSIGNMENTS),
    )
    if status == TurnStatus.CANCELLED:
        cancelled = await session.execute(
            update(InvestigationAssignment)
            .where(*active_filter)
            .values(status=AssignmentStatus.CANCELLED, completed_at=now)
            .returning(InvestigationAssignment.id)
        )
        for (assignment_id,) in cancelled:
            append_progress_event(
                session,
                investigation.id,
                "assignment.finished",
                {"status": AssignmentStatus.CANCELLED.value},
                turn_id=turn.id,
                assignment_id=assignment_id,
            )
    else:
        still_active = await session.scalar(
            select(func.count()).select_from(InvestigationAssignment).where(*active_filter)
        )
        if still_active:
            raise InvalidTransition(f"turn {turn_id} still has {still_active} active assignments")

    _move(turn, status)
    turn.completed_at = now
    turn.decision = decision
    turn.answer_json = answer_json
    turn.error_code = error_code
    if investigation.status != InvestigationStatus.IDLE and investigation.status not in TERMINAL:
        transition_investigation(session, investigation, InvestigationStatus.IDLE, now)
    append_progress_event(
        session,
        investigation.id,
        f"turn.{status.value}",
        {"decision": decision.value if decision else None, "error_code": error_code},
        turn_id=turn.id,
    )
    await session.commit()
    return turn


async def record_report_revision(
    session: AsyncSession, turn_id: uuid.UUID, report_json: dict[str, Any]
) -> InvestigationReport:
    """Next immutable report version, produced by a running turn. Also refreshes the
    investigation's final_result_json snapshot so it always mirrors the latest version."""
    turn, investigation = await _locked_turn(session, turn_id)
    if turn.status != TurnStatus.RUNNING:
        raise InvalidTransition(f"only a running turn records a report, not a {turn.status} one")
    if await session.scalar(
        select(InvestigationReport.id).where(InvestigationReport.turn_id == turn.id)
    ):
        raise ReportConflict(f"turn {turn_id} already recorded its report revision")
    version = (
        await session.scalar(
            select(func.coalesce(func.max(InvestigationReport.version), 0)).where(
                InvestigationReport.investigation_id == investigation.id
            )
        )
    ) + 1
    report = InvestigationReport(
        investigation_id=investigation.id,
        turn_id=turn.id,
        version=version,
        report_json=report_json,
    )
    session.add(report)
    investigation.final_result_json = report_json
    append_progress_event(
        session, investigation.id, "report.revised", {"version": version}, turn_id=turn.id
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if "uq_investigation_reports" in str(exc.orig):
            raise ReportConflict(f"report version conflict for turn {turn_id}") from None
        raise
    return report


async def list_turns(session: AsyncSession, investigation_id: uuid.UUID) -> list[InvestigationTurn]:
    rows = await session.scalars(
        select(InvestigationTurn)
        .where(InvestigationTurn.investigation_id == investigation_id)
        .order_by(InvestigationTurn.seq)
    )
    return list(rows)


async def list_reports(
    session: AsyncSession, investigation_id: uuid.UUID
) -> list[InvestigationReport]:
    rows = await session.scalars(
        select(InvestigationReport)
        .where(InvestigationReport.investigation_id == investigation_id)
        .order_by(InvestigationReport.version)
    )
    return list(rows)


async def latest_report(
    session: AsyncSession, investigation_id: uuid.UUID
) -> InvestigationReport | None:
    return await session.scalar(
        select(InvestigationReport)
        .where(InvestigationReport.investigation_id == investigation_id)
        .order_by(InvestigationReport.version.desc())
        .limit(1)
    )
