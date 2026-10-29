import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.models import OrganizationRole


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None
    email_verified: bool
    display_name: str | None


class SessionRead(BaseModel):
    """GET /auth/session. `configured` false means sign-in is unavailable on this server
    (no provider configured); nothing protected is reachable then."""

    configured: bool
    authenticated: bool
    user: UserRead | None = None
    # For the X-CSRF-Token header on writes; only ever sent to the session's own browser.
    csrf_token: str | None = None
    expires_at: datetime | None = None


class MembershipRead(BaseModel):
    organization_id: uuid.UUID
    name: str
    slug: str
    role: OrganizationRole


class MeRead(BaseModel):
    user: UserRead
    memberships: list[MembershipRead]


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class OrganizationRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    role: OrganizationRole
    member_count: int


class MemberRead(BaseModel):
    user_id: uuid.UUID
    email: str | None
    display_name: str | None
    role: OrganizationRole
    joined_at: datetime


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: OrganizationRole


class InvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=320)
    role: Literal["admin", "member"] = "member"


class InvitationRead(BaseModel):
    id: uuid.UUID
    email: str
    role: OrganizationRole
    status: Literal["pending", "accepted", "revoked", "expired"]
    invited_by: str | None
    created_at: datetime
    expires_at: datetime


class InvitationCreated(InvitationRead):
    # Shown once to the inviter to pass on; only its hash is stored. The token is in the
    # URL fragment, so it never reaches server or proxy logs.
    accept_url: str


class InvitationSignIn(BaseModel):
    """POST /auth/login: sign in from an invitation link (D042)."""

    model_config = ConfigDict(extra="forbid")

    invitation_token: str = Field(min_length=20, max_length=200)
    return_to: str | None = Field(default=None, max_length=500)


class SignInStart(BaseModel):
    # The provider's authorization URL; the browser navigates there.
    authorization_url: str


class InvitationAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)
