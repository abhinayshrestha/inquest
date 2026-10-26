import uuid

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.api.deps import SessionDep
from src.api.sse import cursor_from, event_stream
from src.investigators import runtime
from src.investigators.runner import RunnerBusy
from src.models import Investigation, InvestigationReport, InvestigationTurn
from src.schemas.environment import EnvironmentSummary
from src.schemas.investigation import (
    Citation,
    FollowUpCreate,
    InvestigationCreate,
    InvestigationListItem,
    InvestigationPage,
    InvestigationRead,
    ReportRead,
    TurnAccepted,
    TurnCancelled,
)
from src.schemas.presentation import Presentation
from src.services import citations, investigations, presentation, turns

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


@router.get("", response_model=InvestigationPage)
async def list_investigations(
    session: SessionDep,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
) -> InvestigationPage:
    """Investigation history, newest first, one bounded page at a time."""
    page, turn_counts, latest_reports, has_more = await investigations.list_investigations(
        session, limit=limit, offset=offset
    )
    return InvestigationPage(
        items=[
            InvestigationListItem(
                id=i.id,
                title=i.title,
                status=i.status,
                severity=i.severity,
                trigger_type=i.trigger_type,
                created_at=i.created_at,
                updated_at=i.updated_at,
                started_at=i.started_at,
                completed_at=i.completed_at,
                environments=sorted(
                    (EnvironmentSummary.model_validate(e.environment) for e in i.environments),
                    key=lambda e: e.name,
                ),
                turn_count=turn_counts.get(i.id, 0),
                latest_report_version=latest_reports.get(i.id),
            )
            for i in page
        ],
        next_offset=offset + limit if has_more else None,
    )


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


@router.get("/{investigation_id}/citations/{ref}", response_model=Citation)
async def get_citation(
    investigation_id: uuid.UUID,
    session: SessionDep,
    ref: str = Path(pattern=citations.REF_PATTERN, description="ev_/fd_/hy_ + 8 hex digits"),
):
    """The evidence, finding or hypothesis a ref in this investigation's answers points
    to: recorded summary and metadata only, never raw tool output."""
    if await session.get(Investigation, investigation_id) is None:
        raise _not_found()
    citation = await citations.resolve_citation(session, investigation_id, ref)
    if citation is None:
        raise _not_found("citation")
    return citation


@router.get("/{investigation_id}/turns/{turn_id}/presentation", response_model=Presentation)
async def get_presentation(
    investigation_id: uuid.UUID, turn_id: uuid.UUID, session: SessionDep
) -> Presentation:
    """Response blocks for a completed turn's answer (D023): built deterministically from
    the validated answer and the evidence it cites, stored with the turn on first
    request, and served from storage afterwards. Never calls a model."""
    try:
        return await presentation.turn_presentation(session, investigation_id, turn_id)
    except presentation.TurnNotFound as exc:
        raise _not_found("turn") from exc
    except presentation.NotPresentable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="turn has no validated answer"
        ) from exc


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
