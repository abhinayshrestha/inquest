"""Sign-in, sessions, logout, expiry, CSRF/Origin protection and the local-only legacy
workspace (Phase 7A). Uses the fake OIDC provider in tests/auth_fixtures.py."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import oidc
from src.auth.settings import load_auth_settings
from src.db.session import get_session
from src.main import app
from src.models import AuthLoginState, AuthSession, User
from tests.auth_fixtures import CLIENT_ID, CLIENT_SECRET, ISSUER, PUBLIC_URL, FakeProvider

COOKIE = "__Host-inquest_session"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeProvider]:
    fake = FakeProvider()
    for key, value in {
        "ISSUER": ISSUER, "CLIENT_ID": CLIENT_ID, "CLIENT_SECRET": CLIENT_SECRET,
        "PUBLIC_URL": PUBLIC_URL,
    }.items():  # fmt: skip
        monkeypatch.setenv(f"INQUEST_AUTH_{key}", value)
    load_auth_settings.cache_clear()
    oidc.clear_caches()

    async def http():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as c:
            yield c

    app.dependency_overrides[oidc.http_client] = http
    yield fake
    app.dependency_overrides.pop(oidc.http_client, None)
    load_auth_settings.cache_clear()
    oidc.clear_caches()


@pytest.fixture
async def browser(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A browser on the app's origin over https (so Secure cookies are kept and sent)."""
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


async def sign_in(browser: AsyncClient, provider: FakeProvider, return_to: str = "/", **claims):
    """The whole browser flow; returns the final callback response."""
    start = await browser.get("/auth/login", params={"return_to": return_to})
    assert start.status_code == 303
    code = provider.authorize(start.headers["location"], **claims)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await browser.get("/auth/callback", params={"code": code, "state": state})


def set_cookies(response: httpx.Response) -> dict[str, SimpleCookie]:
    jar = {}
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie()
        cookie.load(header)
        for name, morsel in cookie.items():
            jar[name] = morsel
    return jar


# --- sign-in -------------------------------------------------------------------------------


async def test_login_starts_pkce_flow_bound_to_the_browser(
    provider: FakeProvider, browser: AsyncClient, session: AsyncSession
):
    response = await browser.get("/auth/login", params={"return_to": "/organizations"})

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ISSUER}/authorize"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC_URL}/api/auth/callback"
    assert query["code_challenge_method"] == "S256"
    assert "openid" in query["scope"].split()
    assert len(query["state"]) >= 32 and len(query["nonce"]) >= 32
    login = set_cookies(response)["inquest_login"]
    assert login["httponly"] and login["secure"] and login["samesite"].lower() == "lax"
    # Only hashes are stored: the state value itself is not in the database.
    stored = await session.scalar(select(AuthLoginState.state_hash))
    assert stored and query["state"] not in stored and len(stored) == 64


async def test_callback_creates_a_session_and_returns_to_the_app(
    provider: FakeProvider, browser: AsyncClient, session: AsyncSession
):
    response = await sign_in(browser, provider, return_to="/organizations")

    assert response.status_code == 303
    assert response.headers["location"] == f"{PUBLIC_URL}/organizations"
    cookie = set_cookies(response)[COOKIE]
    assert cookie["httponly"] and cookie["secure"] and cookie["path"] == "/"
    assert cookie["samesite"].lower() == "lax"
    assert set_cookies(response)["inquest_login"].value == ""  # pre-login cookie cleared

    me = (await browser.get("/auth/session")).json()
    assert me["authenticated"] is True
    assert me["user"]["email"] == "ada@example.com" and me["user"]["email_verified"] is True
    assert len(me["csrf_token"]) >= 32
    user = await session.scalar(select(User).where(User.subject == "user-1"))
    assert (user.issuer, user.display_name) == (ISSUER, "Ada")
    stored = await session.scalar(select(AuthSession.token_hash))
    assert stored != cookie.value and len(stored) == 64
    # The login state was single use.
    assert await session.scalar(select(AuthLoginState.id)) is None


async def test_open_redirects_are_neutralized(provider: FakeProvider, browser: AsyncClient):
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example", "javascript:x"):
        response = await sign_in(browser, provider, return_to=target)
        assert response.headers["location"] == f"{PUBLIC_URL}/", target


async def test_a_state_can_be_used_once_and_only_by_its_browser(
    provider: FakeProvider, browser: AsyncClient, session: AsyncSession
):
    start = await browser.get("/auth/login")
    code = provider.authorize(start.headers["location"])
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    # Login CSRF: the callback link opened in a browser that did not start this login.
    app.dependency_overrides[get_session] = lambda: session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as other:
        stolen = await other.get("/auth/callback", params={"code": code, "state": state})
    assert stolen.headers["location"] == f"{PUBLIC_URL}/?auth_error=invalid_state"
    assert COOKIE not in set_cookies(stolen)
    # The state was consumed by the attempt; the rightful browser cannot replay it either.
    replay = await browser.get("/auth/callback", params={"code": code, "state": state})
    assert replay.headers["location"].endswith("auth_error=invalid_state")
    assert provider.token_requests == 0


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ({"token_overrides": {"aud": "someone-else"}}, "invalid_id_token"),
        ({"token_overrides": {"iss": "https://evil.test"}}, "invalid_id_token"),
        ({"token_overrides": {"exp": 1_000_000}}, "invalid_id_token"),
        ({"token_overrides": {"nonce": "replayed"}}, "nonce_mismatch"),
        (
            {"sign_with": rsa.generate_private_key(public_exponent=65537, key_size=2048)},
            "invalid_id_token",
        ),
        ({"header_overrides": {"kid": "unknown"}}, "unknown_signing_key"),
        (
            {"header_overrides": {"alg": "HS256"}, "sign_with": CLIENT_SECRET},
            "unsupported_algorithm",
        ),
    ],
)
async def test_untrustworthy_id_tokens_are_refused(
    provider: FakeProvider, browser: AsyncClient, tamper: dict, error: str
):
    for attribute, value in tamper.items():
        setattr(provider, attribute, value)
    response = await sign_in(browser, provider)
    assert response.headers["location"] == f"{PUBLIC_URL}/?auth_error={error}"
    assert COOKIE not in set_cookies(response)
    assert (await browser.get("/auth/session")).json()["authenticated"] is False


async def test_unsigned_tokens_are_refused(provider: FakeProvider, browser: AsyncClient):
    start = await browser.get("/auth/login")
    code = provider.authorize(start.headers["location"])
    nonce = provider.codes[code][0]
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "nonce": nonce, "iat": 1}
    # alg=none: a token anyone can mint, with exactly the claims the app expects.
    unsigned = jwt.encode(claims | {"exp": 9_999_999_999}, key=None, algorithm="none")
    original = provider.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"id_token": unsigned})
        return original(request)

    provider.handler = handler  # type: ignore[method-assign]
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    response = await browser.get("/auth/callback", params={"code": code, "state": state})
    assert response.headers["location"].endswith("auth_error=unsupported_algorithm")


async def test_provider_errors_are_reported_without_details(
    provider: FakeProvider, browser: AsyncClient
):
    response = await browser.get(
        "/auth/callback",
        params={"error": "access_denied", "error_description": "<script>x</script>"},
    )
    assert response.headers["location"] == f"{PUBLIC_URL}/?auth_error=provider_error"


# --- session lifecycle ---------------------------------------------------------------------


async def test_logout_needs_csrf_and_ends_the_session(provider: FakeProvider, browser: AsyncClient):
    await sign_in(browser, provider)
    csrf = (await browser.get("/auth/session")).json()["csrf_token"]

    assert (await browser.post("/auth/logout")).status_code == 403
    assert (
        await browser.post("/auth/logout", headers={"X-CSRF-Token": "x" * 43})
    ).status_code == 403
    response = await browser.post("/auth/logout", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 204
    assert (await browser.get("/auth/session")).json()["authenticated"] is False
    assert (await browser.get("/me")).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
        {"last_seen_at": datetime.now(UTC) - timedelta(hours=3)},
        {"revoked_at": datetime.now(UTC)},
    ],
)
async def test_expired_idle_or_revoked_sessions_stop_working(
    provider: FakeProvider, browser: AsyncClient, session: AsyncSession, change: dict
):
    await sign_in(browser, provider)
    assert (await browser.get("/me")).status_code == 200
    values = change | (
        {"created_at": datetime.now(UTC) - timedelta(days=2)} if "expires_at" in change else {}
    )
    await session.execute(update(AuthSession).values(**values))

    assert (await browser.get("/me")).status_code == 401


async def test_disabled_users_lose_access_and_cannot_sign_in(
    provider: FakeProvider, browser: AsyncClient, session: AsyncSession
):
    await sign_in(browser, provider)
    await session.execute(update(User).values(disabled_at=datetime.now(UTC)))
    assert (await browser.get("/me")).status_code == 401
    response = await sign_in(browser, provider)
    assert response.headers["location"].endswith("auth_error=account_disabled")


async def test_forged_session_cookies_are_rejected(provider: FakeProvider, browser: AsyncClient):
    browser.cookies.set(COOKIE, "forged-token-value", domain="test")
    assert (await browser.get("/me")).status_code == 401


# --- unauthenticated, unconfigured, cross-site ---------------------------------------------


async def test_without_a_provider_everything_protected_fails_closed(
    browser: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("INQUEST_AUTH_ISSUER", "")
    load_auth_settings.cache_clear()
    try:
        assert (await browser.get("/auth/session")).json() == {
            "configured": False, "authenticated": False, "user": None, "csrf_token": None,
            "expires_at": None,
        }  # fmt: skip
        assert (await browser.get("/auth/login")).status_code == 503
        assert (await browser.get("/me")).status_code == 401
        assert (await browser.post("/organizations", json={"name": "x"})).status_code == 401
    finally:
        load_auth_settings.cache_clear()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/me"),
        ("POST", "/organizations"),
        ("GET", f"/organizations/{uuid.uuid4()}"),
        ("GET", f"/organizations/{uuid.uuid4()}/members"),
        ("GET", f"/organizations/{uuid.uuid4()}/invitations"),
        ("POST", f"/organizations/{uuid.uuid4()}/invitations"),
        ("DELETE", f"/organizations/{uuid.uuid4()}/members/{uuid.uuid4()}"),
        ("POST", "/invitations/accept"),
        ("POST", "/auth/logout"),
    ],
)
async def test_unauthenticated_requests_are_rejected(
    provider: FakeProvider, browser: AsyncClient, method: str, path: str
):
    response = await browser.request(method, path, json={})
    assert response.status_code == 401


async def test_cross_site_writes_are_refused_even_with_a_valid_token(
    provider: FakeProvider, browser: AsyncClient
):
    await sign_in(browser, provider)
    csrf = (await browser.get("/auth/session")).json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    evil = await browser.post(
        "/organizations", json={"name": "x"}, headers=headers | {"Origin": "https://evil.example"}
    )
    fetch_meta = await browser.post(
        "/organizations", json={"name": "x"}, headers=headers | {"Sec-Fetch-Site": "cross-site"}
    )
    good = await browser.post(
        "/organizations", json={"name": "Acme"}, headers=headers | {"Origin": PUBLIC_URL}
    )

    assert (evil.status_code, fetch_meta.status_code, good.status_code) == (403, 403, 201)


# --- legacy workspace stays local ----------------------------------------------------------
