import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from src.api.deps import OrganizationQuery, SessionDep
from src.api.sse import cursor_from, event_stream
from src.auth.deps import CurrentUser, Identity, WritingUser
from src.db.session import SessionLocal
from src.investigators import runtime
from src.investigators.runner import RunnerBusy
from src.models import (
    Investigation,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    TurnStatus,
)
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
from src.services import access, citations, investigations, presentation, turns

router = APIRouter(prefix="/investigations", tags=["investigations"])


def _not_found(what: str = "investigation") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


async def _investigation(
    session: SessionDep, identity: Identity, investigation_id: uuid.UUID
) -> Investigation:
    """404 unless the caller is a current member of the investigation's organization."""
    try:
        return await access.investigation_for(session, identity.user, investigation_id)
    except access.NoAccess as exc:
        raise _not_found() from exc


async def _runnable(session: SessionDep, investigation_id: uuid.UUID) -> None:
    # Tenant environments never run on the backend's process-wide credentials unless an
    # operator granted them; their own credentials arrive in Phase 7C (D033).
    if not await access.investigation_runnable(session, investigation_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="configuration required: this environment has no credentials to run "
            "investigations with",
        )


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
async def create_investigation(
    data: InvestigationCreate, session: SessionDep, identity: WritingUser
) -> Investigation:
    """Created in the environment's organization, which the caller must belong to."""
    try:
        await access.environment_for(session, identity.user, data.environment_id)
        return await investigations.create_investigation(session, data, created_by=identity.user.id)
    except (access.NoAccess, investigations.EnvironmentNotFound) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="environment not found"
        ) from exc


@router.get("", response_model=InvestigationPage)
async def list_investigations(
    session: SessionDep,
    identity: CurrentUser,
    organization_id: OrganizationQuery,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    environment_id: Annotated[uuid.UUID | None, Query(description="Only this environment")] = None,
    status: Annotated[list[InvestigationStatus], Query(max_length=7)] = [],  # noqa: B006
    latest_turn_status: Annotated[list[TurnStatus], Query(max_length=5)] = [],  # noqa: B006
) -> InvestigationPage:
    """Investigation history, newest first, one bounded page at a time; optionally only
    some investigation statuses or latest-turn statuses (repeat the parameter)."""
    try:
        await access.require_member(session, organization_id, identity.user)
    except access.NoAccess as exc:
        raise _not_found("organization") from exc
    data = await investigations.list_investigations(
        session,
        organization_id=organization_id,
        limit=limit,
        offset=offset,
        environment_id=environment_id,
        statuses=status,
        latest_turn_statuses=latest_turn_status,
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
                turn_count=data.turn_counts.get(i.id, 0),
                latest_report_version=data.latest_reports.get(i.id),
                latest_turn_status=data.latest_turn.get(i.id, (None, None))[0],
                latest_turn_completed_at=data.latest_turn.get(i.id, (None, None))[1],
            )
            for i in data.page
        ],
        next_offset=offset + limit if data.has_more else None,
    )


@router.get("/{investigation_id}", response_model=InvestigationRead)
async def get_investigation(
    investigation_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> Investigation:
    await _investigation(session, identity, investigation_id)
    investigation = await investigations.get_investigation(session, investigation_id)
    if investigation is None:
        raise _not_found()
    return investigation


@router.post(
    "/{investigation_id}/start",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TurnAccepted,
)
async def start_investigation(
    investigation_id: uuid.UUID, session: SessionDep, identity: WritingUser
) -> TurnAccepted:
    """Runs the initial question (turn 1) with the Main Investigator."""
    await _investigation(session, identity, investigation_id)
    await _runnable(session, investigation_id)
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
    investigation_id: uuid.UUID, data: FollowUpCreate, session: SessionDep, identity: WritingUser
) -> TurnAccepted:
    """A follow-up question on an idle investigation, accepted and run as a new turn."""
    await _investigation(session, identity, investigation_id)
    await _runnable(session, investigation_id)
    _runtime()
    try:
        turn = await turns.create_follow_up_turn(
            session,
            investigation_id,
            data.question,
            request_run=True,
            created_by=identity.user.id,
        )
        runtime.submit(investigation_id, turn.id)
    except investigations.InvestigationNotFound as exc:
        raise _not_found() from exc
    except (turns.TurnConflict, investigations.InvalidTransition, RunnerBusy) as exc:
        raise _conflict(exc) from exc
    return _accepted(turn)


@router.post("/{investigation_id}/cancel", response_model=TurnCancelled)
async def cancel_turn(
    investigation_id: uuid.UUID, session: SessionDep, identity: WritingUser
) -> TurnCancelled:
    """Cancels the active turn. Earlier turns, evidence and report versions stay."""
    await _investigation(session, identity, investigation_id)
    _runtime()
    turn_id = await runtime.cancel(investigation_id)
    if turn_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no active turn")
    return TurnCancelled(investigation_id=investigation_id, turn_id=turn_id)


@router.get("/{investigation_id}/reports/{version}", response_model=ReportRead)
async def get_report(
    investigation_id: uuid.UUID, version: int, session: SessionDep, identity: CurrentUser
) -> InvestigationReport:
    await _investigation(session, identity, investigation_id)
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
    identity: CurrentUser,
    ref: str = Path(pattern=citations.REF_PATTERN, description="ev_/fd_/hy_ + 8 hex digits"),
):
    """The evidence, finding or hypothesis a ref in this investigation's answers points
    to: recorded summary and metadata only, never raw tool output."""
    await _investigation(session, identity, investigation_id)
    citation = await citations.resolve_citation(session, investigation_id, ref)
    if citation is None:
        raise _not_found("citation")
    return citation


@router.get("/{investigation_id}/turns/{turn_id}/presentation", response_model=Presentation)
async def get_presentation(
    investigation_id: uuid.UUID, turn_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> Presentation:
    """Response blocks for a completed turn's answer (D023): built deterministically from
    the validated answer and the evidence it cites, stored with the turn on first
    request, and served from storage afterwards. Never calls a model."""
    await _investigation(session, identity, investigation_id)
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
    identity: CurrentUser,
    after: int | None = Query(default=None, ge=0, description="Replay events after this id"),
    follow: bool = Query(default=True, description="Keep streaming new events"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Progress as Server-Sent Events: replays persisted events after Last-Event-ID (or
    ?after=), then follows new ones. follow=false returns the replay and closes. The
    stream re-checks the session and membership while open and ends with an
    access_revoked event once either is gone."""
    await _investigation(session, identity, investigation_id)
    auth_session_id = identity.session.id

    async def allowed() -> bool:
        async with SessionLocal() as s:
            return await access.still_allowed(s, auth_session_id, investigation_id)

    return StreamingResponse(
        event_stream(
            investigation_id,
            after=cursor_from(last_event_id, after),
            follow=follow,
            is_disconnected=request.is_disconnected,
            still_allowed=allowed,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
