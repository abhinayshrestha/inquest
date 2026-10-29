"""Who is calling, from the session cookie only. Identity never comes from request
parameters or bodies: user and organization ids sent by the browser are inputs to check,
not proof of anything."""

import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from src.api.deps import SessionDep
from src.auth.sessions import record_activity, resolve_session
from src.auth.settings import AuthNotConfigured, AuthSettings, load_auth_settings
from src.models import AuthSession, User

LOGIN_COOKIE = "inquest_login"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def session_cookie(settings: AuthSettings) -> str:
    # __Host- cookies are Secure, host-only and path=/: a subdomain cannot set or shadow them.
    return "__Host-inquest_session" if settings.cookie_secure else "inquest_session"


@dataclass(frozen=True)
class Identity:
    user: User
    session: AuthSession


def _unauthenticated(detail: str = "sign in required") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def optional_identity(request: Request, session: SessionDep) -> Identity | None:
    try:
        settings = load_auth_settings()
    except AuthNotConfigured:
        return None
    found = await resolve_session(session, request.cookies.get(session_cookie(settings)), settings)
    return Identity(user=found[1], session=found[0]) if found else None


async def require_user(
    identity: Annotated[Identity | None, Depends(optional_identity)],
) -> Identity:
    """401 without a valid session. With authentication unconfigured there are no sessions,
    so every protected route fails closed; there is no development identity."""
    if identity is None:
        raise _unauthenticated()
    return identity


async def require_csrf(
    request: Request, session: SessionDep, identity: Annotated[Identity, Depends(require_user)]
) -> Identity:
    """Writes need the session's CSRF token in X-CSRF-Token (the cookie alone is ambient and
    would be sent by any site's forged request). A write that passes is the user's own
    action, so it moves the idle deadline (D043); reads never do."""
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not secrets.compare_digest(sent, identity.session.csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")
        await record_activity(session, identity.session)
    return identity


CurrentUser = Annotated[Identity, Depends(require_user)]
WritingUser = Annotated[Identity, Depends(require_csrf)]
