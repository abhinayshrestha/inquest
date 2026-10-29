"""Per-environment credentials (Phase 7C-1, D034). Members read statuses; only the
environment's owner sets, replaces or clears them. No response carries a secret, a URL
or any part of one.

Runs use them through src/credentials/resolver.py (7C-2): an environment with any
credential row runs only on its own credentials, both kinds required. Replacing one (PUT)
leaves running turns alone: their next model step or tool call resolves the new one, and a
failed validation keeps the old one. Clearing one cancels the environment's active turns
(7C-3).
"""

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Awaitable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status

from src.api.deps import SessionDep
from src.auth.deps import CurrentUser, Identity, WritingUser
from src.credentials import crypto, resolver, validation
from src.credentials.network import DestinationNotAllowed
from src.credentials.settings import (
    CredentialConfig,
    CredentialStorageUnavailable,
    load_credential_config,
    optional_credential_config,
)
from src.investigators import runtime
from src.models import CredentialKind, Environment, TelemetryIsolation
from src.schemas.credentials import CredentialStatus, NebiusCredentialsInput, SigNozCredentialsInput
from src.services import access, credentials, limits

logger = logging.getLogger(__name__)

# Whole validation, every provider request included: httpx timeouts apply per operation,
# so a slow endpoint could otherwise hold a validation (and its slot) much longer.
VALIDATION_SECONDS = 60.0

router = APIRouter(prefix="/environments", tags=["credentials"])


def provider_client() -> httpx.AsyncClient | None:
    """None: validation builds its own guarded client per provider. Tests override this
    with a mock transport, never a real provider."""
    return None


ProviderClient = Annotated[httpx.AsyncClient | None, Depends(provider_client)]


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="environment not found")


def _not_owner() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="only the environment owner can change its credentials",
    )


def _config() -> CredentialConfig:
    try:
        return load_credential_config()
    except CredentialStorageUnavailable as exc:
        # The message names settings only; the response does not even do that.
        logger.warning("credential storage unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="credential storage is not configured on this server",
        ) from None


async def _owned(session: SessionDep, identity: Identity, environment_id: uuid.UUID) -> Environment:
    try:
        return await credentials.owned_environment(session, identity.user, environment_id)
    except access.NoAccess as exc:
        raise _not_found() from exc
    except credentials.NotEnvironmentOwner as exc:
        raise _not_owner() from exc


def _refused(exc: validation.CredentialValidationFailed) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": exc.code, "message": exc.message},
    )


async def _validated(check: Awaitable[Any]) -> Any:
    try:
        async with asyncio.timeout(VALIDATION_SECONDS):
            return await check
    except validation.CredentialValidationFailed as exc:
        raise _refused(exc) from None
    except TimeoutError:
        raise _refused(validation.CredentialValidationFailed("unreachable")) from None


async def _counted(session: SessionDep, identity: Identity, environment: Environment) -> None:
    """Each save makes outbound provider requests: counted for the owner, the organization
    and the server once the caller is known to own the environment. Never per endpoint, so
    a refusal says nothing about who else uses one."""
    await limits.hit(
        session, "credential_validation", user=identity.user.id,
        organization=environment.organization_id, server=True,
    )  # fmt: skip


async def _store(
    session: SessionDep,
    identity: Identity,
    environment_id: uuid.UUID,
    kind: CredentialKind,
    config: CredentialConfig,
    secret: dict[str, str],
    public: dict[str, str],
    isolation: TelemetryIsolation | None = None,
) -> CredentialStatus:
    try:
        return await credentials.store_credential(
            session, environment_id, identity.user, kind, secret=secret, public=public,
            config=config, isolation=isolation,
        )  # fmt: skip
    except validation.CredentialValidationFailed as exc:
        raise _refused(exc) from None
    except access.NoAccess as exc:
        raise _not_found() from exc
    except credentials.NotEnvironmentOwner as exc:
        # Ownership or membership changed while the provider was being checked.
        raise _not_owner() from exc


@router.get("/{environment_id}/credentials", response_model=list[CredentialStatus])
async def get_credentials(
    environment_id: uuid.UUID, session: SessionDep, identity: CurrentUser
) -> list[CredentialStatus]:
    """Whether each provider credential is set, usable by this server, when and by whom.
    Visible to every member of the environment's organization; never the values."""
    try:
        await access.environment_for(session, identity.user, environment_id)
    except access.NoAccess as exc:
        raise _not_found() from exc
    return await credentials.credential_statuses(
        session, environment_id, optional_credential_config()
    )


@router.put("/{environment_id}/credentials/nebius", response_model=CredentialStatus)
async def put_nebius_credentials(
    environment_id: uuid.UUID,
    data: NebiusCredentialsInput,
    session: SessionDep,
    identity: WritingUser,
    client: ProviderClient,
) -> CredentialStatus:
    """Sets or replaces the environment's Nebius Token Factory key and models, after the
    key has listed the catalog containing both models. On failure nothing changes."""
    config = _config()
    # Owner first: nobody else can make this server contact a provider.
    environment = await _owned(session, identity, environment_id)
    async with limits.validation_slot():
        # Commits, which also releases the connection while the provider is called.
        await _counted(session, identity, environment)
        await _validated(validation.validate_nebius(data, config, client=client))
    return await _store(
        session, identity, environment_id, CredentialKind.NEBIUS, config,
        secret={"api_key": data.api_key.get_secret_value(), "base_url": data.base_url},
        public={"main_model": data.main_model, "local_model": data.local_model},
    )  # fmt: skip


@router.put("/{environment_id}/credentials/signoz", response_model=CredentialStatus)
async def put_signoz_credentials(
    environment_id: uuid.UUID,
    data: SigNozCredentialsInput,
    session: SessionDep,
    identity: WritingUser,
    client: ProviderClient,
) -> CredentialStatus:
    """Sets or replaces the environment's SigNoz URL and key, after the key has run a
    query there. On failure nothing changes, here or for any other environment (D037):

    - dedicated: outside local development the operator must have verified the
      connection for this organization (dedication_unverified, D038);
      refused (connection_not_isolated) when another organization already reaches it,
      checked only after the key worked, so the answer reveals nothing to someone without
      working credentials for it;
    - provider_scoped: the operator must have assigned this organization ingestion keys of
      the workspace (tenant_keys_missing), and the key must be confined to them
      (scope_not_enforced) before anything is stored."""
    config = _config()
    environment = await _owned(session, identity, environment_id)
    # Allowlisted private origins only where the operator granted them to this organization.
    narrowed = dataclasses.replace(
        config,
        telemetry_origins=resolver.telemetry_origins_for(config, environment.organization_id),
    )
    isolation = TelemetryIsolation(data.isolation)
    keys: tuple[str, ...] = ()
    others: tuple[str, ...] = ()
    if isolation is TelemetryIsolation.PROVIDER_SCOPED:
        try:
            fingerprint = crypto.fingerprint(config, data.base_url)
        except DestinationNotAllowed:
            raise _refused(
                validation.CredentialValidationFailed("destination_not_allowed")
            ) from None
        keys = await _validated(credentials.tenant_key_ids(session, environment, fingerprint))
        others = await credentials.foreign_key_ids(session, environment, fingerprint)
    async with limits.validation_slot():
        await _counted(session, identity, environment)
        await _validated(
            validation.validate_signoz(
                data, narrowed, tenant_key_ids=keys, foreign_key_ids=others, client=client
            )
        )
    return await _store(
        session, identity, environment_id, CredentialKind.SIGNOZ, config,
        secret={"api_key": data.api_key.get_secret_value(), "base_url": data.base_url},
        public={}, isolation=isolation,
    )  # fmt: skip


@router.delete("/{environment_id}/credentials/{kind}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_credentials(
    environment_id: uuid.UUID, kind: CredentialKind, session: SessionDep, identity: WritingUser
) -> Response:
    """Clears one credential. Works without the key ring, so unusable credentials can
    always be removed. The environment's active turns are then cancelled: with one kind
    missing it cannot run, and no new model step or tool call can use the removed one."""
    try:
        revoked = await credentials.clear_credential(session, environment_id, identity.user, kind)
    except access.NoAccess as exc:
        raise _not_found() from exc
    except credentials.NotEnvironmentOwner as exc:
        raise _not_owner() from exc
    if revoked is not None:
        assert revoked.organization_id is not None
        await runtime.stop_environment_runs(
            revoked.id, "credentials_revoked", organization_id=revoked.organization_id,
            actor_user_id=identity.user.id,
        )  # fmt: skip
    return Response(status_code=status.HTTP_204_NO_CONTENT)
