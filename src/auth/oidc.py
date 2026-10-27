"""OpenID Connect client for one configured provider (D030).

Authorization Code flow with PKCE (S256) and a nonce, run by the backend: the browser
only ever sees the provider's login page and an opaque session cookie, never tokens. The
ID token is verified here (signature from the provider's JWKS, issuer, audience,
expiry, nonce) and then discarded; access and refresh tokens are not kept, because
Inquest calls no provider API on the user's behalf.

This is the only provider-specific module. It returns verified claims; turning them into
users, sessions and organization access happens elsewhere.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from src.auth.settings import LOCAL_HOSTS, AuthSettings

# Asymmetric algorithms only: "none" and HMAC (signed with the client secret, which the
# provider and every client share) are never accepted for ID tokens.
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384"]
CLOCK_SKEW_SECONDS = 60
METADATA_TTL_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 10.0


class OIDCError(Exception):
    """A sign-in that must not succeed. `code` is safe to show; details are not."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    name: str | None


_metadata: dict[str, tuple[float, ProviderMetadata]] = {}
_jwks: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_caches() -> None:
    _metadata.clear()
    _jwks.clear()


def _secure_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)


async def discover(settings: AuthSettings, http: httpx.AsyncClient) -> ProviderMetadata:
    cached = _metadata.get(settings.issuer)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    url = settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = await http.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        doc = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OIDCError("provider_unavailable", type(exc).__name__) from None
    # The document must describe the configured issuer exactly (OIDC Discovery 4.3).
    if doc.get("issuer") != settings.issuer:
        raise OIDCError("issuer_mismatch")
    endpoints = {k: doc.get(k) for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")}
    if not all(_secure_url(v) for v in endpoints.values()):
        raise OIDCError("insecure_provider_endpoint")
    if "S256" not in doc.get("code_challenge_methods_supported", ["S256"]):
        raise OIDCError("pkce_unsupported")
    end_session = doc.get("end_session_endpoint")
    metadata = ProviderMetadata(
        issuer=doc["issuer"],
        end_session_endpoint=end_session if _secure_url(end_session) else None,
        **endpoints,  # type: ignore[arg-type]
    )
    _metadata[settings.issuer] = (time.monotonic() + METADATA_TTL_SECONDS, metadata)
    return metadata


def new_pkce() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge), RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(
    metadata: ProviderMetadata, settings: AuthSettings, *, state: str, nonce: str, challenge: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": settings.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in metadata.authorization_endpoint else "?"
    return f"{metadata.authorization_endpoint}{separator}{query}"


async def exchange_code(
    metadata: ProviderMetadata,
    settings: AuthSettings,
    http: httpx.AsyncClient,
    *,
    code: str,
    verifier: str,
) -> str:
    """The ID token from the token endpoint (client_secret_basic, RFC 6749 2.3.1)."""
    try:
        response = await http.post(
            metadata.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.redirect_uri,
                "code_verifier": verifier,
            },
            auth=(settings.client_id, settings.client_secret.get_secret_value()),
            headers={"Accept": "application/json"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise OIDCError("provider_unavailable", type(exc).__name__) from None
    if response.status_code != 200:
        # The provider's error body may echo request data; only the status is kept.
        raise OIDCError("token_exchange_failed", str(response.status_code))
    try:
        id_token = response.json().get("id_token")
    except ValueError:
        id_token = None
    if not isinstance(id_token, str):
        raise OIDCError("no_id_token")
    return id_token


async def _signing_key(
    metadata: ProviderMetadata, http: httpx.AsyncClient, kid: str | None, alg: str
) -> jwt.PyJWK:
    async def fetch() -> dict[str, Any]:
        try:
            response = await http.get(metadata.jwks_uri, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            jwks = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError("provider_unavailable", type(exc).__name__) from None
        _jwks[metadata.jwks_uri] = (time.monotonic() + METADATA_TTL_SECONDS, jwks)
        return jwks

    cached = _jwks.get(metadata.jwks_uri)
    jwks = cached[1] if cached and cached[0] > time.monotonic() else await fetch()
    for attempt in range(2):
        keys = [
            k
            for k in jwks.get("keys", [])
            if isinstance(k, dict)
            and k.get("use", "sig") == "sig"
            and (kid is None or k.get("kid") == kid)
        ]
        # Exactly one candidate: without a kid, several keys would be a guess.
        if len(keys) == 1:
            try:
                return jwt.PyJWK(keys[0], algorithm=alg)
            except jwt.PyJWTError:
                raise OIDCError("invalid_signing_key") from None
        if attempt == 0:
            # Unknown kid: the provider may have rotated keys since we cached them.
            jwks = await fetch()
    raise OIDCError("unknown_signing_key")


async def verify_id_token(
    id_token: str,
    metadata: ProviderMetadata,
    settings: AuthSettings,
    http: httpx.AsyncClient,
    *,
    nonce: str,
) -> VerifiedIdentity:
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise OIDCError("malformed_id_token") from None
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise OIDCError("unsupported_algorithm")
    key = await _signing_key(metadata, http, header.get("kid"), alg)
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=[alg],
            audience=settings.client_id,
            issuer=metadata.issuer,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError("invalid_id_token", type(exc).__name__) from None
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("nonce_mismatch")
    audience = claims["aud"]
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != settings.client_id:
        raise OIDCError("invalid_authorized_party")
    subject = claims["sub"]
    if not isinstance(subject, str) or not 0 < len(subject) <= 255:
        raise OIDCError("invalid_subject")
    email = claims.get("email")
    email = email.strip().lower() if isinstance(email, str) and "@" in email else None
    name = claims.get("name") or claims.get("preferred_username")
    return VerifiedIdentity(
        issuer=metadata.issuer,
        subject=subject,
        email=email[:320] if email else None,
        email_verified=claims.get("email_verified") is True and email is not None,
        name=name[:200] if isinstance(name, str) else None,
    )


async def http_client():
    """FastAPI dependency: the HTTP client used to reach the provider (tests replace it)."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        yield client
