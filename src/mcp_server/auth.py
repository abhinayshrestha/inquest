"""Assignment-bound bearer tokens for the MCP server.

The orchestrator mints one token per running assignment and hands it to that assignment's
investigator runtime (never to the model, never into graph state). The token is the only
source of investigation, assignment and environment IDs for MCP tool calls. Revocation is
the assignment leaving `running`: the executor re-checks that under a row lock on every
call, so an unexpired token for a closed assignment can run nothing.
"""

import logging
import time
import uuid
from typing import Any

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.models import InvestigationAssignment

logger = logging.getLogger(__name__)

ISSUER = "inquest"
AUDIENCE = "inquest-mcp"
# Granted only to Environment Investigators; the Main Investigator never gets a token (D009).
INVESTIGATE_SCOPE = "inquest:investigate"
ALGORITHM = "HS256"
MIN_SECRET_CHARS = 32
REQUIRED_CLAIMS = ["iss", "aud", "sub", "exp", "iat", "jti", "scope", "inv", "env"]


class McpSettings(BaseSettings):
    """Separate from core Settings so the app and DB tests start without a token secret;
    without one every MCP request is refused."""

    model_config = SettingsConfigDict(
        env_prefix="MCP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    token_secret: SecretStr | None = None
    token_ttl_seconds: int = Field(default=900, gt=0, le=3600)
    # Host header allowlist (DNS rebinding protection); "host:*" allows any port.
    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    # Where investigator runtimes reach this server: loopback into the same backend (D010).
    url: str = "http://127.0.0.1:8000/mcp/"


class TokenConfigError(Exception):
    pass


def _secret(settings: McpSettings) -> str:
    secret = settings.token_secret.get_secret_value() if settings.token_secret else ""
    if len(secret) < MIN_SECRET_CHARS:
        # Names the variable, never its value.
        raise TokenConfigError(f"MCP_TOKEN_SECRET must be at least {MIN_SECRET_CHARS} characters")
    return secret


def mint_assignment_token(
    assignment: InvestigationAssignment, settings: McpSettings, *, ttl_seconds: int | None = None
) -> str:
    """For the orchestrator, when it launches an investigator. Not reachable over HTTP."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(assignment.id),
        "inv": str(assignment.investigation_id),
        "env": str(assignment.environment_id),
        "scope": INVESTIGATE_SCOPE,
        "iat": now,
        "exp": now + (ttl_seconds or settings.token_ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _secret(settings), algorithm=ALGORITHM)


def _decode(token: str, secret: str) -> dict[str, Any] | None:
    try:
        # algorithms is pinned so a token cannot choose "none" or an asymmetric algorithm.
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": REQUIRED_CLAIMS},
        )
        binding = {
            "investigation_id": uuid.UUID(claims["inv"]),
            "assignment_id": uuid.UUID(claims["sub"]),
            "environment_id": uuid.UUID(claims["env"]),
        }
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
        return None
    return {**binding, "scope": claims["scope"], "exp": claims["exp"]}


class AssignmentTokenVerifier:
    """mcp TokenVerifier: turns a bearer token into the trusted assignment binding."""

    def __init__(self, settings: McpSettings):
        self.settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            secret = _secret(self.settings)
        except TokenConfigError as exc:
            logger.error("MCP request refused: %s", exc)
            return None
        claims = _decode(token, secret)
        if claims is None:
            # The token itself is never logged.
            logger.info("MCP request refused: invalid bearer token")
            return None
        scope = claims["scope"]
        return AccessToken(
            token=token,
            client_id="inquest-investigator",
            scopes=[scope] if isinstance(scope, str) else [],
            expires_at=int(claims["exp"]),
            subject=str(claims["assignment_id"]),
            claims={k: claims[k] for k in ("investigation_id", "assignment_id", "environment_id")},
        )
