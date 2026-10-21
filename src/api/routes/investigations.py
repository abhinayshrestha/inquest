import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.api.deps import SessionDep
from src.api.sse import cursor_from, event_stream
from src.investigators import runtime
from src.investigators.runner import RunnerBusy
from src.models import Investigation, InvestigationReport, InvestigationTurn
from src.schemas.investigation import (
    FollowUpCreate,
    InvestigationCreate,
    InvestigationRead,
    ReportRead,
    TurnAccepted,
    TurnCancelled,
)
from src.services import investigations, turns

router = APIRouter(prefix="/investigations", tags=["investigations"])


def _not_found(what: str = "investigation") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _runtime() -> runtime.Runtime:
    try:
        return runtime.current()
    except runtime.RunnerDisabled as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


def _accepted(turn: InvestigationTurn) -> TurnAccepted:
    return TurnAccepted(
        investigation_id=turn.investigation_id,
        turn_id=turn.id,
        seq=turn.seq,
        events_url=f"/investigations/{turn.investigation_id}/events",
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvestigationRead)
async def create_investigation(data: InvestigationCreate, session: SessionDep) -> Investigation:
    try:
        return await investigations.create_investigation(session, data)
    except investigations.EnvironmentNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="environment not found"
        ) from exc


@router.get("/{investigation_id}", response_model=InvestigationRead)
async def get_investigation(investigation_id: uuid.UUID, session: SessionDep) -> Investigation:
    investigation = await investigations.get_investigation(session, investigation_id)
    if investigation is None:
        raise _not_found()
    return investigation


@router.post(
    "/{investigation_id}/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TurnAccepted,
)
async def start_investigation(investigation_id: uuid.UUID, session: SessionDep) -> TurnAccepted:
    """Runs the initial question (turn 1) with the Main Investigator."""
    _runtime()
    turn = await session.scalar(
        select(InvestigationTurn).where(
            InvestigationTurn.investigation_id == investigation_id, InvestigationTurn.seq == 1
        )
    )
    if turn is None:
        raise _not_found()
    try:
        turn = await turns.request_turn_run(session, turn.id)
        runtime.submit(investigation_id, turn.id)
    except (turns.TurnConflict, RunnerBusy) as exc:
        raise _conflict(exc) from exc
    return _accepted(turn)


@router.post(
    "/{investigation_id}/turns",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TurnAccepted,
)
async def ask_follow_up(
    investigation_id: uuid.UUID, data: FollowUpCreate, session: SessionDep
) -> TurnAccepted:
    """A follow-up question on an idle investigation, accepted and run as a new turn."""
    _runtime()
    try:
        turn = await turns.create_follow_up_turn(
            session, investigation_id, data.question, request_run=True
        )
        runtime.submit(investigation_id, turn.id)
    except investigations.InvestigationNotFound as exc:
        raise _not_found() from exc
    except (turns.TurnConflict, investigations.InvalidTransition, RunnerBusy) as exc:
        raise _conflict(exc) from exc
    return _accepted(turn)


@router.post("/{investigation_id}/cancel", response_model=TurnCancelled)
async def cancel_turn(investigation_id: uuid.UUID, session: SessionDep) -> TurnCancelled:
    """Cancels the active turn. Earlier turns, evidence and report versions stay."""
    _runtime()
    if await session.get(Investigation, investigation_id) is None:
        raise _not_found()
    turn_id = await runtime.cancel(investigation_id)
    if turn_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no active turn")
    return TurnCancelled(investigation_id=investigation_id, turn_id=turn_id)


@router.get("/{investigation_id}/reports/{version}", response_model=ReportRead)
async def get_report(
    investigation_id: uuid.UUID, version: int, session: SessionDep
) -> InvestigationReport:
    report = await session.scalar(
        select(InvestigationReport).where(
            InvestigationReport.investigation_id == investigation_id,
            InvestigationReport.version == version,
        )
    )
    if report is None:
        raise _not_found("report version")
    return report


@router.get("/{investigation_id}/events")
async def stream_events(
    investigation_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    after: int | None = Query(default=None, ge=0, description="Replay events after this id"),
    follow: bool = Query(default=True, description="Keep streaming new events"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Progress as Server-Sent Events: replays persisted events after Last-Event-ID (or
    ?after=), then follows new ones. follow=false returns the replay and closes."""
    if await session.get(Investigation, investigation_id) is None:
        raise _not_found()
    return StreamingResponse(
        event_stream(
            investigation_id,
            after=cursor_from(last_event_id, after),
            follow=follow,
            is_disconnected=request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
