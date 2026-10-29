"""Save-time validation: one cheap authenticated read per provider, through the
destination guard, before anything is stored (D034). Failures are a code with a fixed
message; provider response text is never returned, since it could echo the key or
internal details. Callers pass `client` only in tests (mock transports)."""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx

from src.credentials.network import (
    DestinationNotAllowed,
    Resolver,
    guarded_client,
    parse_destination,
    vet_destination,
)
from src.credentials.settings import CredentialConfig
from src.integrations.signoz import builders
from src.integrations.signoz.client import QUERY_RANGE_PATH, request_json
from src.integrations.signoz.errors import SigNozError, SigNozErrorCode
from src.integrations.signoz.schemas import TimeRange
from src.integrations.signoz.settings import SigNozSettings
from src.schemas.credentials import NebiusCredentialsInput, SigNozCredentialsInput

TIMEOUT = httpx.Timeout(15.0, connect=5.0)
MAX_RESPONSE_BYTES = 2_000_000

MESSAGES = {
    "destination_not_allowed": "this endpoint is not allowed by the server's destination policy",
    "auth_failed": "the provider rejected these credentials",
    "model_not_found": "a model id is not available to this key",
    "unreachable": "the provider could not be reached or is unavailable",
    "provider_error": "the provider returned an unexpected response",
    # Telemetry isolation (D037). Fixed text: never which organization, URL, key or name.
    "connection_not_isolated": "this connection cannot be accepted as dedicated to this "
    "organization; use your own SigNoz, or a provider-scoped workspace from the operator",
    "dedication_unverified": "an operator has not verified this connection as dedicated to "
    "this organization; ask an operator to verify it, or use a provider-scoped workspace",
    "tenant_keys_missing": "no ingestion keys of this workspace are assigned to this "
    "organization; an operator must assign them first",
    "scope_not_enforced": "this key can read telemetry beyond this organization's ingestion "
    "keys; use a key whose role is restricted to them",
}


class CredentialValidationFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message = MESSAGES[code]


@asynccontextmanager
async def _client(
    client: httpx.AsyncClient | None,
    origins: frozenset[str],
    *,
    allowlisted_only: bool,
    resolver: Resolver | None,
) -> AsyncIterator[httpx.AsyncClient]:
    if client is not None:
        yield client
        return
    async with guarded_client(
        origins, allowlisted_only=allowlisted_only, timeout=TIMEOUT, resolver=resolver
    ) as owned:
        yield owned


async def _vetted(
    base_url: str,
    origins: frozenset[str],
    *,
    allowlisted_only: bool,
    resolver: Resolver | None,
) -> None:
    try:
        await vet_destination(
            parse_destination(base_url),
            origins,
            allowlisted_only=allowlisted_only,
            resolver=resolver,
        )
    except DestinationNotAllowed:
        raise CredentialValidationFailed("destination_not_allowed") from None
    except OSError:
        raise CredentialValidationFailed("unreachable") from None


async def _get_models(http: httpx.AsyncClient, data: NebiusCredentialsInput) -> set[str]:
    url = data.base_url.rstrip("/") + "/models"
    headers = {
        "Authorization": f"Bearer {data.api_key.get_secret_value()}",
        "Accept": "application/json",
    }
    async with http.stream(
        "GET", url, headers=headers, timeout=TIMEOUT, follow_redirects=False
    ) as response:
        if response.status_code in (401, 403):
            raise CredentialValidationFailed("auth_failed")
        if response.status_code == 429 or response.status_code >= 500:
            raise CredentialValidationFailed("unreachable")
        if response.status_code != 200:
            raise CredentialValidationFailed("provider_error")
        raw = b""
        async for chunk in response.aiter_bytes():
            raw += chunk
            if len(raw) > MAX_RESPONSE_BYTES:
                raise CredentialValidationFailed("provider_error")
    try:
        entries = json.loads(raw)["data"]
        return {e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)}
    except (ValueError, KeyError, TypeError):
        raise CredentialValidationFailed("provider_error") from None


async def validate_nebius(
    data: NebiusCredentialsInput,
    config: CredentialConfig,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: Resolver | None = None,
) -> None:
    """The key lists the catalog and both models are in it. Nebius endpoints must be on
    the operator allowlist (https only)."""
    await _vetted(data.base_url, config.nebius_origins, allowlisted_only=True, resolver=resolver)
    try:
        async with _client(
            client, config.nebius_origins, allowlisted_only=True, resolver=resolver
        ) as http:
            models = await _get_models(http, data)
    except DestinationNotAllowed:
        raise CredentialValidationFailed("destination_not_allowed") from None
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        raise CredentialValidationFailed("destination_not_allowed") from None
    except httpx.TransportError:
        raise CredentialValidationFailed("unreachable") from None
    if not {data.main_model, data.local_model} <= models:
        raise CredentialValidationFailed("model_not_found")


def _probe_query() -> dict:
    # The health endpoint is open access; only a query proves the key. A five-minute log
    # count is the cheapest query; its result is discarded.
    end = datetime.now(UTC)
    spec = {"name": "A", "signal": "logs", "aggregations": [{"expression": "count()"}]}
    return builders.envelope(
        TimeRange(start=end - timedelta(minutes=5), end=end),
        "scalar",
        [builders.builder_query(spec)],
    )


SIGNOZ_FAILURES = {
    SigNozErrorCode.AUTH: "auth_failed",
    SigNozErrorCode.TIMEOUT: "unreachable",
    SigNozErrorCode.UNAVAILABLE: "unreachable",
    SigNozErrorCode.CONFIG: "destination_not_allowed",
}


# Probes a key restricted to the organization's ingestion keys must fail (D037): the same
# count without the tenant filter, with an ingestion key id nobody was assigned, and the
# query languages a scoped role cannot run. Any success means the key reaches further.
def _raw_probe(kind: str, query: str) -> dict:
    end = datetime.now(UTC)
    return builders.envelope(
        TimeRange(start=end - timedelta(minutes=5), end=end),
        "scalar",
        [{"type": kind, "spec": {"name": "A", "query": query, "disabled": False}}],
    )


def _escape_probes(key_ids: tuple[str, ...], others: tuple[str, ...]) -> list[dict]:
    # Other organizations' keys on this workspace catch a role that also covers a known
    # tenant; a random id catches one that covers any key.
    foreign = ("inquest-probe-" + uuid.uuid4().hex, *others)
    assert not set(foreign) & set(key_ids)
    return [
        _probe_query(),
        *(builders.tenant_scoped(_probe_query(), (key,)) for key in foreign),
        _raw_probe("promql", "vector(1)"),
        _raw_probe("clickhouse_sql", "SELECT 1"),
    ]


async def _scope_enforced(
    settings: SigNozSettings,
    key_ids: tuple[str, ...],
    others: tuple[str, ...],
    http: httpx.AsyncClient,
) -> None:
    # The organization's own keys first: proves the key works at all (and maps 401 etc.).
    scoped = settings.model_copy(update={"tenant_key_ids": key_ids})
    await request_json(scoped, "POST", QUERY_RANGE_PATH, _probe_query(), http)
    for probe in _escape_probes(key_ids, others):
        try:
            await request_json(settings, "POST", QUERY_RANGE_PATH, probe, http)
        except SigNozError as exc:
            if exc.http_status == 403:
                continue  # denied, as it must be
            raise
        raise CredentialValidationFailed("scope_not_enforced")


async def validate_signoz(
    data: SigNozCredentialsInput,
    config: CredentialConfig,
    *,
    tenant_key_ids: tuple[str, ...] = (),
    foreign_key_ids: tuple[str, ...] = (),
    client: httpx.AsyncClient | None = None,
    resolver: Resolver | None = None,
) -> None:
    """The key runs a query. Public https endpoints, or exact allowlisted origins (those
    this organization may use: the caller passes a config narrowed to them). With
    `tenant_key_ids` (provider-scoped isolation), the key must also be confined to them:
    its own keys answer, everything else is denied with 403, including the keys assigned
    to other organizations on the workspace (`foreign_key_ids`, never shown to anyone)."""
    await _vetted(
        data.base_url, config.telemetry_origins, allowlisted_only=False, resolver=resolver
    )
    settings = SigNozSettings(base_url=data.base_url, api_key=data.api_key)
    try:
        async with _client(
            client, config.telemetry_origins, allowlisted_only=False, resolver=resolver
        ) as http:
            if tenant_key_ids:
                await _scope_enforced(settings, tenant_key_ids, foreign_key_ids, http)
            else:
                await request_json(settings, "POST", QUERY_RANGE_PATH, _probe_query(), http)
    except DestinationNotAllowed:
        raise CredentialValidationFailed("destination_not_allowed") from None
    except SigNozError as exc:
        raise CredentialValidationFailed(SIGNOZ_FAILURES.get(exc.code, "provider_error")) from None
