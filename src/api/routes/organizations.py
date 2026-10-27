import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status

from src.api.deps import SessionDep
from src.auth.deps import CurrentUser, WritingUser
from src.auth.settings import load_auth_settings
from src.models import OrganizationInvitation, User
from src.schemas.identity import (
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    InvitationRead,
    MemberRead,
    MembershipRead,
    MeRead,
    OrganizationCreate,
    OrganizationRead,
    RoleUpdate,
    UserRead,
)
from src.services import organizations as orgs

router = APIRouter(tags=["organizations"])


def _http(exc: Exception) -> HTTPException:
    # Outsiders get 404 for an organization, never 403: its existence is not disclosed.
    if isinstance(exc, orgs.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if isinstance(exc, orgs.Forbidden):
        return HTTPException(status.HTTP_403_FORBIDDEN, "your role does not allow this")
    if isinstance(exc, orgs.InvitationUnusable):
        return HTTPException(status.HTTP_410_GONE, "this invitation is no longer valid")
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


ERRORS = (orgs.NotFound, orgs.Forbidden, orgs.Conflict, orgs.InvitationUnusable)


def _invitation(invitation: OrganizationInvitation, inviter: User | None) -> InvitationRead:
    return InvitationRead(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        status=orgs.invitation_status(invitation, datetime.now(UTC)),  # type: ignore[arg-type]
        invited_by=(inviter.display_name or inviter.email) if inviter else None,
        created_at=invitation.created_at,
        expires_at=invitation.expires_at,
    )


@router.get("/me", response_model=MeRead)
async def me(session: SessionDep, identity: CurrentUser) -> MeRead:
    memberships = await orgs.my_memberships(session, identity.user)
    return MeRead(
        user=UserRead.model_validate(identity.user),
        memberships=[
            MembershipRead(organization_id=o.id, name=o.name, slug=o.slug, role=r)
            for o, r in memberships
        ],
    )


@router.post("/organizations", status_code=201, response_model=OrganizationRead)
async def create_organization(
    data: OrganizationCreate, session: SessionDep, identity: WritingUser
) -> OrganizationRead:
    try:
        o = await orgs.create_organization(session, identity.user, data.name)
    except ERRORS as exc:
        raise _http(exc) from exc
    return OrganizationRead(
        id=o.id, name=o.name, slug=o.slug, created_at=o.created_at, role="owner", member_count=1
    )


@router.get("/organizations/{organization_id}", response_model=OrganizationRead)
async def get_organization(
    organization_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> OrganizationRead:
    try:
        o, role, count = await orgs.get_organization(session, organization_id, identity.user)
    except ERRORS as exc:
        raise _http(exc) from exc
    return OrganizationRead(
        id=o.id, name=o.name, slug=o.slug, created_at=o.created_at, role=role, member_count=count
    )


@router.get("/organizations/{organization_id}/members", response_model=list[MemberRead])
async def list_members(
    organization_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> list[MemberRead]:
    try:
        rows = await orgs.list_members(session, organization_id, identity.user)
    except ERRORS as exc:
        raise _http(exc) from exc
    return [
        MemberRead(
            user_id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=m.role,
            joined_at=m.created_at,
        )  # fmt: skip
        for m, u in rows
    ]


@router.patch("/organizations/{organization_id}/members/{user_id}", response_model=MemberRead)
async def change_role(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    data: RoleUpdate,
    session: SessionDep,
    identity: WritingUser,
) -> MemberRead:
    try:
        m = await orgs.change_role(session, organization_id, identity.user, user_id, data.role)
    except ERRORS as exc:
        raise _http(exc) from exc
    u = await session.get(User, m.user_id)
    return MemberRead(
        user_id=u.id, email=u.email, display_name=u.display_name, role=m.role,
        joined_at=m.created_at,
    )  # fmt: skip


@router.delete("/organizations/{organization_id}/members/{user_id}", status_code=204)
async def remove_member(
    organization_id: uuid.UUID, user_id: uuid.UUID, session: SessionDep, identity: WritingUser
) -> Response:
    try:
        await orgs.remove_member(session, organization_id, identity.user, user_id)
    except ERRORS as exc:
        raise _http(exc) from exc
    return Response(status_code=204)


@router.get("/organizations/{organization_id}/invitations", response_model=list[InvitationRead])
async def list_invitations(
    organization_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> list[InvitationRead]:
    try:
        rows = await orgs.list_invitations(session, organization_id, identity.user)
    except ERRORS as exc:
        raise _http(exc) from exc
    return [_invitation(i, u) for i, u in rows]


@router.post(
    "/organizations/{organization_id}/invitations",
    status_code=201,
    response_model=InvitationCreated,
)
async def create_invitation(
    organization_id: uuid.UUID, data: InvitationCreate, session: SessionDep, identity: WritingUser
) -> InvitationCreated:
    try:
        invitation, token = await orgs.create_invitation(
            session, organization_id, identity.user, str(data.email), data.role
        )
    except ERRORS as exc:
        raise _http(exc) from exc
    origin = load_auth_settings().origin
    return InvitationCreated(
        **_invitation(invitation, identity.user).model_dump(),
        accept_url=f"{origin}/invitations/accept#token={token}",
    )


@router.delete("/organizations/{organization_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    organization_id: uuid.UUID,
    invitation_id: uuid.UUID,
    session: SessionDep,
    identity: WritingUser,
) -> Response:
    try:
        await orgs.revoke_invitation(session, organization_id, identity.user, invitation_id)
    except ERRORS as exc:
        raise _http(exc) from exc
    return Response(status_code=204)


@router.post("/invitations/accept", response_model=MembershipRead)
async def accept_invitation(
    data: InvitationAccept, session: SessionDep, identity: WritingUser
) -> MembershipRead:
    try:
        o = await orgs.accept_invitation(session, identity.user, data.token)
    except ERRORS as exc:
        raise _http(exc) from exc
    role = await orgs.membership_role(session, o.id, identity.user)
    return MembershipRead(organization_id=o.id, name=o.name, slug=o.slug, role=role)
