"""Turns (the conversation) and report revisions of an investigation (D013).

Every operation locks the investigation row first, so turn creation, turn lifecycle and
report versioning of one investigation are serialized. The partial unique index on
active turns and the unique report version are the database backstops. A refusal leaves
the transaction (and its lock) to the caller to roll back, like the other services.
"""

import re
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
    InvestigationEnvironment,
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


class InvalidAnswer(ValueError):
    """A completed turn's answer has no content beyond markdown headings."""


HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}(?:\s.*)?$")


def answer_has_content(answer_md: str) -> bool:
    """Text beyond markdown headings. The last guard against persisting an empty or
    heading-only answer as a completed turn (live Phase 5 run, turn 2); the investigators
    apply stricter checks (citations, length) before getting here."""
    return any(line.strip() and not HEADING_LINE.match(line) for line in answer_md.splitlines())


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
    session: AsyncSession, investigation_id: uuid.UUID, question: str, *, request_run: bool = False
) -> InvestigationTurn:
    """A new question for an idle investigation. Refused while another turn is active or
    once the investigation is closed. `request_run` also accepts it for execution in the
    same transaction (see request_turn_run)."""
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
        run_requested_at=datetime.now(UTC) if request_run else None,
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
        if request_run:
            append_progress_event(session, investigation_id, "turn.queued", turn_id=turn.id)
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


async def request_turn_run(session: AsyncSession, turn_id: uuid.UUID) -> InvestigationTurn:
    """Accepts a pending turn for execution, exactly once. The mark is what lets recovery
    start a turn that was accepted but never started (process died in between), while
    never starting a turn that was only created."""
    turn, _ = await _locked_turn(session, turn_id)
    if turn.status != TurnStatus.PENDING:
        raise TurnConflict(f"turn {turn_id} is {turn.status}, not pending")
    if turn.run_requested_at is not None:
        raise TurnConflict(f"turn {turn_id} was already accepted for execution")
    turn.run_requested_at = datetime.now(UTC)
    append_progress_event(session, turn.investigation_id, "turn.queued", turn_id=turn.id)
    await session.commit()
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


async def _close_active_assignments(
    session: AsyncSession,
    turn: InvestigationTurn,
    status: AssignmentStatus,
    now: datetime,
) -> list[uuid.UUID]:
    closed = await session.execute(
        update(InvestigationAssignment)
        .where(
            InvestigationAssignment.turn_id == turn.id,
            InvestigationAssignment.status.in_(ACTIVE_ASSIGNMENTS),
        )
        .values(status=status, completed_at=now)
        .returning(InvestigationAssignment.id)
    )
    ids = [assignment_id for (assignment_id,) in closed]
    for assignment_id in ids:
        append_progress_event(
            session,
            turn.investigation_id,
            "assignment.finished",
            {"status": status.value},
            turn_id=turn.id,
            assignment_id=assignment_id,
        )
    return ids


async def _finish_in(
    session: AsyncSession,
    turn: InvestigationTurn,
    investigation: Investigation,
    status: TurnStatus,
    *,
    decision: TurnDecision | None,
    answer_json: dict[str, Any] | None,
    error_code: str | None,
    now: datetime,
) -> None:
    """finish_turn without the commit, for callers that combine it with other writes."""
    if status == TurnStatus.CANCELLED:
        await _close_active_assignments(session, turn, AssignmentStatus.CANCELLED, now)
    elif status == TurnStatus.FAILED:
        # A failed turn closes its work; the evidence it produced stays (retry = new turn).
        await _close_active_assignments(session, turn, AssignmentStatus.FAILED, now)
    else:
        still_active = await session.scalar(
            select(func.count())
            .select_from(InvestigationAssignment)
            .where(
                InvestigationAssignment.turn_id == turn.id,
                InvestigationAssignment.status.in_(ACTIVE_ASSIGNMENTS),
            )
        )
        if still_active:
            raise InvalidTransition(f"turn {turn.id} still has {still_active} active assignments")

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


def _check_finish(
    status: TurnStatus, decision: TurnDecision | None, answer_json: dict[str, Any] | None
) -> None:
    if status not in FINISHED:
        raise InvalidTransition(f"{status} does not finish a turn")
    if status == TurnStatus.COMPLETED and decision is None:
        raise InvalidTransition("a completed turn needs a decision")
    if status != TurnStatus.COMPLETED and answer_json is not None:
        raise InvalidTransition("only a completed turn has an answer")


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
    active; cancelled cancels them and failed fails them; nothing is deleted."""
    _check_finish(status, decision, answer_json)
    turn, investigation = await _locked_turn(session, turn_id)
    await _finish_in(
        session,
        turn,
        investigation,
        status,
        decision=decision,
        answer_json=answer_json,
        error_code=error_code,
        now=datetime.now(UTC),
    )
    await session.commit()
    return turn


async def _record_revision_in(
    session: AsyncSession,
    turn: InvestigationTurn,
    investigation: Investigation,
    report_json: dict[str, Any],
) -> InvestigationReport:
    """record_report_revision without the commit."""
    if turn.status != TurnStatus.RUNNING:
        raise InvalidTransition(f"only a running turn records a report, not a {turn.status} one")
    if await session.scalar(
        select(InvestigationReport.id).where(InvestigationReport.turn_id == turn.id)
    ):
        raise ReportConflict(f"turn {turn.id} already recorded its report revision")
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
    return report


async def _commit_or_conflict(session: AsyncSession, turn_id: uuid.UUID) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if "uq_investigation_reports" in str(exc.orig):
            raise ReportConflict(f"report version conflict for turn {turn_id}") from None
        raise


async def record_report_revision(
    session: AsyncSession, turn_id: uuid.UUID, report_json: dict[str, Any]
) -> InvestigationReport:
    """Next immutable report version, produced by a running turn. Also refreshes the
    investigation's final_result_json snapshot so it always mirrors the latest version."""
    turn, investigation = await _locked_turn(session, turn_id)
    report = await _record_revision_in(session, turn, investigation, report_json)
    await _commit_or_conflict(session, turn_id)
    return report


async def complete_turn(
    session: AsyncSession,
    turn_id: uuid.UUID,
    *,
    decision: TurnDecision,
    answer_json: dict[str, Any],
    report_json: dict[str, Any] | None = None,
) -> InvestigationTurn:
    """The end of a turn as one transaction: optional report revision, its
    final_result_json snapshot, the answer (which records the report version it
    describes) and the move to idle. Either all of it is durable or none.

    Idempotent: replaying it for a turn that already completed returns that turn
    unchanged, so a crash between this commit and the caller's checkpoint is harmless.
    Refuses (InvalidAnswer, before taking any lock) an answer without content."""
    if not answer_has_content(str(answer_json.get("answer_md") or "")):
        raise InvalidAnswer(f"turn {turn_id}: the answer has no content beyond headings")
    turn, investigation = await _locked_turn(session, turn_id)
    if turn.status == TurnStatus.COMPLETED:
        # Nothing changed: commit only to release the lock (rollback would expire `turn`).
        await session.commit()
        return turn
    answer = dict(answer_json)
    if report_json is not None:
        report = await _record_revision_in(session, turn, investigation, report_json)
        await session.flush()
        answer["report_version"] = report.version
    else:
        answer["report_version"] = None
    await _finish_in(
        session,
        turn,
        investigation,
        TurnStatus.COMPLETED,
        decision=decision,
        answer_json=answer,
        error_code=None,
        now=datetime.now(UTC),
    )
    await _commit_or_conflict(session, turn_id)
    return turn


async def fail_turn(session: AsyncSession, turn_id: uuid.UUID, error_code: str) -> bool:
    """Fails a running or pending turn (closing its active assignments) and returns the
    investigation to idle (D016). False if the turn had already finished."""
    turn, investigation = await _locked_turn(session, turn_id)
    if turn.status in FINISHED:
        # Nothing changed: commit only to release the lock (rollback would expire `turn`).
        await session.commit()
        return False
    if turn.status == TurnStatus.PENDING:
        # A pending turn never ran; it cannot fail, only be cancelled.
        status = TurnStatus.CANCELLED
    else:
        status = TurnStatus.FAILED
    await _finish_in(
        session,
        turn,
        investigation,
        status,
        decision=None,
        answer_json=None,
        error_code=error_code[:100],
        now=datetime.now(UTC),
    )
    await session.commit()
    return True


async def dispatch_round(
    session: AsyncSession,
    turn_id: uuid.UUID,
    requests: list[tuple[uuid.UUID, str]],
) -> list[uuid.UUID]:
    """Starts a round of assignments for a running turn: (environment_id, objective) each.

    - Takes ownership of assignments pre-created for the turn (create_investigation still
      makes the round-1 assignment): a pending assignment for a requested environment is
      adopted, its objective replaced by Main's, instead of inserting a duplicate.
      Pending ones for environments Main did not ask for are cancelled.
    - Idempotent: if the turn already has running assignments (a replay after the round
      was started), those are returned and nothing new is created.
    - Only environments in the investigation's scope; one assignment per environment."""
    turn, investigation = await _locked_turn(session, turn_id)
    if turn.status != TurnStatus.RUNNING:
        raise InvalidTransition(f"only a running turn dispatches, not a {turn.status} one")
    assignments = list(
        await session.scalars(
            select(InvestigationAssignment)
            .where(InvestigationAssignment.turn_id == turn.id)
            .with_for_update()
        )
    )
    running = [a.id for a in assignments if a.status == AssignmentStatus.RUNNING]
    if running:
        # Nothing changed: commit only to release the lock (rollback would expire `turn`).
        await session.commit()
        return running

    scope = set(
        await session.scalars(
            select(InvestigationEnvironment.environment_id).where(
                InvestigationEnvironment.investigation_id == investigation.id
            )
        )
    )
    wanted: dict[uuid.UUID, str] = {}
    for environment_id, objective in requests:
        if environment_id in scope and environment_id not in wanted and objective.strip():
            wanted[environment_id] = objective.strip()
    if not wanted:
        raise InvalidTransition("no requested environment is in the investigation's scope")

    pending = [a for a in assignments if a.status == AssignmentStatus.PENDING]
    if pending:
        round_ = pending[0].round
    else:
        any_assignment = await session.scalar(
            select(InvestigationAssignment.id)
            .where(InvestigationAssignment.investigation_id == investigation.id)
            .limit(1)
        )
        round_ = investigation.current_round + 1 if any_assignment else investigation.current_round
    now = datetime.now(UTC)
    started: list[InvestigationAssignment] = []
    adopted = {a.environment_id: a for a in pending}
    for environment_id, objective in wanted.items():
        assignment = adopted.pop(environment_id, None)
        if assignment is None:
            assignment = InvestigationAssignment(
                investigation_id=investigation.id,
                environment_id=environment_id,
                turn_id=turn.id,
                round=round_,
                objective=objective,
            )
            session.add(assignment)
        assignment.objective = objective
        assignment.status = AssignmentStatus.RUNNING
        assignment.started_at = now
        started.append(assignment)
    for leftover in adopted.values():
        leftover.status = AssignmentStatus.CANCELLED
        leftover.completed_at = now
        append_progress_event(
            session,
            investigation.id,
            "assignment.finished",
            {"status": AssignmentStatus.CANCELLED.value, "reason": "not dispatched"},
            turn_id=turn.id,
            assignment_id=leftover.id,
        )
    await session.flush()
    investigation.current_round = round_
    transition_investigation(session, investigation, InvestigationStatus.INVESTIGATING, now)
    append_progress_event(
        session,
        investigation.id,
        "round.started",
        {"round": round_, "assignments": len(started)},
        turn_id=turn.id,
    )
    for assignment in started:
        append_progress_event(
            session,
            investigation.id,
            "assignment.started",
            {"round": round_, "environment_id": assignment.environment_id,
             "objective": assignment.objective},
            turn_id=turn.id,
            assignment_id=assignment.id,
        )  # fmt: skip
    await session.commit()
    return [a.id for a in started]


async def end_round(session: AsyncSession, turn_id: uuid.UUID) -> None:
    """After the Local Investigators returned: anything still running is failed (its
    runner gave up without closing it) and the investigation goes back to correlating.
    Idempotent: a replay when the investigation is already correlating does nothing."""
    turn, investigation = await _locked_turn(session, turn_id)
    if investigation.status != InvestigationStatus.INVESTIGATING:
        # Nothing changed: commit only to release the lock (rollback would expire `turn`).
        await session.commit()
        return
    now = datetime.now(UTC)
    closed = await _close_active_assignments(session, turn, AssignmentStatus.FAILED, now)
    transition_investigation(session, investigation, InvestigationStatus.CORRELATING, now)
    append_progress_event(
        session,
        investigation.id,
        "round.finished",
        {"round": investigation.current_round, "force_closed": len(closed)},
        turn_id=turn.id,
    )
    await session.commit()


LEGACY_ERROR = "legacy_no_checkpoint"


async def reconcile_legacy_turn(session: AsyncSession, turn_id: uuid.UUID) -> bool:
    """A running turn with no orchestration checkpoint (backfilled, or started outside
    the Main runner) cannot be resumed: there is no workflow to continue. It is failed
    explicitly, its still-active assignments are timed out, and the investigation returns
    to idle so the user can ask again. Evidence and findings already gathered stay."""
    turn, investigation = await _locked_turn(session, turn_id)
    if turn.status != TurnStatus.RUNNING:
        # Nothing changed: commit only to release the lock (rollback would expire `turn`).
        await session.commit()
        return False
    now = datetime.now(UTC)
    await _close_active_assignments(session, turn, AssignmentStatus.TIMED_OUT, now)
    await _finish_in(
        session,
        turn,
        investigation,
        TurnStatus.FAILED,
        decision=None,
        answer_json=None,
        error_code=LEGACY_ERROR,
        now=now,
    )
    await session.commit()
    return True


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
