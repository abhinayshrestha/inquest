import secrets
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from src.api.deps import SessionDep
from src.auth import oidc
from src.auth.deps import LOGIN_COOKIE, Identity, WritingUser, optional_identity, session_cookie
from src.auth.sessions import (
    LOGIN_STATE_MINUTES,
    create_login_state,
    create_session,
    revoke_session,
    take_login_state,
    upsert_user,
)
from src.auth.settings import AuthNotConfigured, AuthSettings, load_auth_settings
from src.schemas.identity import SessionRead, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])
HttpDep = Annotated[httpx.AsyncClient, Depends(oidc.http_client)]


def _settings() -> AuthSettings:
    try:
        return load_auth_settings()
    except AuthNotConfigured as exc:
        # No fallback identity: sign-in is simply unavailable.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication is not configured on this server",
        ) from exc


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _set_cookie(response: Response, settings: AuthSettings, name: str, value: str, seconds: int):
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        # Lax: sent on the provider's top-level redirect back, not on cross-site subrequests.
        samesite="lax",
    )


def _failed(settings: AuthSettings, code: str) -> RedirectResponse:
    response = RedirectResponse(f"{settings.origin}/?auth_error={quote(code)}", status_code=303)
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return response


@router.get("/session", response_model=SessionRead)
async def get_session(
    response: Response, identity: Annotated[Identity | None, Depends(optional_identity)]
) -> SessionRead:
    """Who the browser is signed in as, and its CSRF token. Always 200."""
    _no_store(response)
    try:
        load_auth_settings()
    except AuthNotConfigured:
        return SessionRead(configured=False, authenticated=False)
    if identity is None:
        return SessionRead(configured=True, authenticated=False)
    return SessionRead(
        configured=True,
        authenticated=True,
        user=UserRead.model_validate(identity.user),
        csrf_token=identity.session.csrf_token,
        expires_at=identity.session.expires_at,
    )


@router.get("/login")
async def login(
    session: SessionDep,
    http: HttpDep,
    return_to: Annotated[str | None, Query(max_length=500)] = None,
) -> RedirectResponse:
    """Starts sign-in at the provider (Authorization Code + PKCE + nonce)."""
    settings = _settings()
    try:
        metadata = await oidc.discover(settings, http)
    except oidc.OIDCError as exc:
        raise HTTPException(
            status_code=503, detail=f"sign-in provider unavailable ({exc.code})"
        ) from exc
    verifier, challenge = oidc.new_pkce()
    nonce = secrets.token_urlsafe(32)
    # Binds this login to this browser: the callback must come back with the same cookie.
    browser = secrets.token_urlsafe(32)
    state = await create_login_state(
        session, browser_token=browser, verifier=verifier, nonce=nonce, return_to=return_to or "/"
    )
    url = oidc.authorization_url(metadata, settings, state=state, nonce=nonce, challenge=challenge)
    response = RedirectResponse(url, status_code=303)
    _no_store(response)
    _set_cookie(response, settings, LOGIN_COOKIE, browser, LOGIN_STATE_MINUTES * 60)
    return response


@router.get("/callback")
async def callback(
    request: Request,
    session: SessionDep,
    http: HttpDep,
    code: Annotated[str | None, Query(max_length=2000)] = None,
    state: Annotated[str | None, Query(max_length=200)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> RedirectResponse:
    """The provider's redirect. Errors end at the app with a short code, never the
    provider's description (which may echo request data)."""
    settings = _settings()
    if error or not code or not state:
        return _failed(settings, "provider_error" if error else "missing_code")
    pending = await take_login_state(
        session, state=state, browser_token=request.cookies.get(LOGIN_COOKIE)
    )
    if pending is None:
        return _failed(settings, "invalid_state")
    try:
        metadata = await oidc.discover(settings, http)
        id_token = await oidc.exchange_code(
            metadata, settings, http, code=code, verifier=pending.code_verifier
        )
        identity = await oidc.verify_id_token(
            id_token, metadata, settings, http, nonce=pending.nonce
        )
    except oidc.OIDCError as exc:
        return _failed(settings, exc.code)
    user = await upsert_user(session, identity)
    if user.disabled_at is not None:
        await session.commit()
        return _failed(settings, "account_disabled")
    token, auth = await create_session(session, user, settings)
    response = RedirectResponse(f"{settings.origin}{pending.return_to}", status_code=303)
    _no_store(response)
    response.delete_cookie(LOGIN_COOKIE, path="/")
    _set_cookie(
        response,
        settings,
        session_cookie(settings),
        token,
        int((auth.expires_at - auth.created_at).total_seconds()),
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(session: SessionDep, identity: WritingUser) -> Response:
    """Ends this browser's Inquest session. The provider's own sign-in session (single
    sign-on) is not ended here."""
    settings = _settings()
    await revoke_session(session, identity.session.id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        session_cookie(settings), path="/", secure=settings.cookie_secure, httponly=True
    )
    return response
