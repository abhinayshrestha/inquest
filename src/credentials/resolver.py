"""Which credentials a run may use (Phase 7C-2, D034): the single authority for the runner,
Main and Local Investigators, the Tool Executor, the investigation API and environment
detail.

- Ownerless legacy environments (D032) use the process-wide `.env` credentials: host-side
  tooling only, and only in a local development mode (LOCAL_APP_ENVS).
- An environment with any credential row of its own uses its own credentials exclusively:
  both kinds must be present, readable and inside the destination policy. A missing kind
  is never borrowed from `.env`, and an operator grant is ignored.
- Otherwise an operator grant (`process_credentials`) uses `.env`, only when the operator
  also allows it on this server (INQUEST_ALLOW_PROCESS_CREDENTIALS), and likewise only in
  a local development mode: production, staging or any other hosted mode refuses it.
- Anything else is refused with a code; nothing falls back.
- Telemetry isolation (D037): a SigNoz connection is `dedicated` (the organization's own
  SigNoz) or `provider_scoped` (a shared workspace restricted to operator-bound ingestion
  keys, which every query carries). Operator-allowlisted private origins are usable
  outside local development only by organizations granted them. Scope ambiguity is
  decided among one organization's environments only: another organization can never
  make an environment's identities ambiguous.
- Connection policy (D038), re-evaluated on every resolution, so saved, checkpointed and
  replayed work follows the current state: a dedicated connection is refused when another
  organization also reaches it (its credentials, assigned keys, verification or, for the
  process-wide connection, development grants), unless an operator verified it as this
  organization's; outside local development it is refused without that verification.
  Development grants may share the process-wide connection with each other, never with a
  tenant's own connection. One code for every isolation refusal, so the answer reveals nothing
  about other organizations.

Resolution decrypts in memory for the caller about to make the provider call. The result
is never stored, logged, checkpointed or returned by the API.
"""

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import ColumnElement, exists, func, literal, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.credentials import crypto
from src.credentials.network import DestinationNotAllowed, parse_destination
from src.credentials.settings import CredentialConfig, optional_credential_config
from src.integrations.signoz import SigNozError, load_signoz_settings
from src.models import (
    CredentialKind,
    Environment,
    EnvironmentCredential,
    Investigation,
    InvestigationEnvironment,
    SigNozDedicatedConnection,
    SigNozTenantKey,
    TelemetryIsolation,
)

# Fixed messages: they name the problem, never a value.
REFUSALS = {
    "configuration_required": "this environment has no credentials to run investigations with",
    "credentials_unavailable": "this environment's stored credentials cannot be read by this "
    "server",
    "destination_not_allowed": "a stored provider endpoint is outside this server's "
    "destination policy",
    "process_credentials_not_permitted": "process-wide development credentials are not "
    "permitted on this server",
    "multi_environment_unsupported": "investigations across several environments with their "
    "own credentials are not supported",
    "scope_mismatch": "the environment does not belong to the investigation's organization",
    "telemetry_isolation_unverified": "this environment's telemetry connection is not "
    "verified as isolated from other organizations; an operator must review it",
}

# Why a SigNoz connection fails the isolation policy, for operators only (startup log,
# scripts.bind_signoz_tenant review). Tenants see telemetry_isolation_unverified.
ISOLATION_PROBLEMS = {
    "tenant_keys_missing": "provider-scoped, but no ingestion keys are assigned to the "
    "organization",
    "dedication_unverified": "dedicated, but not verified by an operator (required outside "
    "local development)",
    "connection_shared": "another organization also uses or is assigned this connection",
}


# APP_ENV values that mean a developer's own machine. Allowlisted, so a mode added later
# (staging, a hosted demo) refuses process-wide credentials until it is listed here.
LOCAL_APP_ENVS = frozenset({"local", "test"})


def local_development() -> bool:
    return get_settings().app_env in LOCAL_APP_ENVS


class CredentialRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.message = REFUSALS[code]


class ProcessCredentialSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INQUEST_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Off unless the operator opts in: granted tenant environments then use the backend's
    # own Nebius key and SigNoz connection. Local development modes only.
    allow_process_credentials: bool = False


@dataclass(frozen=True)
class Credentials:
    source: Literal["environment", "process"]
    environment_id: uuid.UUID
    # Own credentials only: api_key, base_url, main_model, local_model.
    nebius: dict[str, str] = field(default_factory=dict, repr=False)
    # Own credentials only: api_key, base_url.
    signoz: dict[str, str] = field(default_factory=dict, repr=False)
    fingerprint: str | None = None
    # With telemetry_origins narrowed to those this organization may use.
    config: CredentialConfig | None = field(default=None, repr=False)
    organization_id: uuid.UUID | None = None
    isolation: TelemetryIsolation | None = None
    # Provider-scoped connections only: every query is restricted to these.
    tenant_key_ids: tuple[str, ...] = ()

    def secrets(self) -> list[str]:
        return [v["api_key"] for v in (self.nebius, self.signoz) if v.get("api_key")]


def process_credentials_usable() -> bool:
    return local_development() and ProcessCredentialSettings().allow_process_credentials


def process_refusal(environment: Environment) -> str | None:
    if not local_development():
        return "process_credentials_not_permitted"
    if environment.organization_id is None:
        return None
    if not ProcessCredentialSettings().allow_process_credentials:
        return "process_credentials_not_permitted"
    return None


def _open(
    config: CredentialConfig, environment: Environment, row: EnvironmentCredential
) -> dict[str, str]:
    assert environment.owner_user_id is not None
    try:
        # The environment's current owner, not the row's: a row that outlived an owner
        # change does not authenticate.
        return crypto.unseal(
            config,
            crypto.Sealed(row.key_id, row.nonce, row.ciphertext),
            environment_id=environment.id,
            kind=row.kind,
            owner_user_id=environment.owner_user_id,
        )
    except crypto.CredentialUnreadable:
        raise CredentialRefused("credentials_unavailable") from None


def telemetry_origins_for(
    config: CredentialConfig, organization_id: uuid.UUID | None
) -> frozenset[str]:
    """Allowlisted telemetry origins this organization may use: all of them on a
    developer's machine; elsewhere only those an operator granted it."""
    if local_development():
        return config.telemetry_origins
    return frozenset(
        o
        for o in config.telemetry_origins
        if organization_id is not None
        and organization_id in config.telemetry_origin_grants.get(o, frozenset())
    )


async def bound_key_ids(
    session: AsyncSession, organization_id: uuid.UUID | None, fingerprint: str | None
) -> tuple[str, ...]:
    """Ingestion key ids an operator assigned to the organization on this workspace."""
    if organization_id is None or fingerprint is None:
        return ()
    return tuple(
        await session.scalars(
            select(SigNozTenantKey.key_id)
            .where(
                SigNozTenantKey.organization_id == organization_id,
                SigNozTenantKey.connection_fingerprint == fingerprint,
            )
            .order_by(SigNozTenantKey.key_id)
        )
    )


async def isolation_problem(
    session: AsyncSession,
    organization_id: uuid.UUID | None,
    fingerprint: str | None,
    isolation: TelemetryIsolation | None,
    *,
    process: bool = False,
) -> str | None:
    """Whether this organization may use a SigNoz connection under the isolation policy:
    None, or a key of ISOLATION_PROBLEMS. `process` is the process-wide connection
    (isolation None). Ownerless legacy environments are host-side tooling in local
    development only, so they are not tenants here."""
    if organization_id is None:
        return None
    if isolation is TelemetryIsolation.PROVIDER_SCOPED:
        keys = await bound_key_ids(session, organization_id, fingerprint)
        return None if keys else "tenant_keys_missing"
    reaches_process = process or (
        fingerprint is not None
        and fingerprint == _process_fingerprint(optional_credential_config())
        and process_credentials_usable()
    )
    users = await _connection_users(session, fingerprint, process=reaches_process)
    # Verified: this organization's connection. Anyone else on it is the one refused.
    if (organization_id, "dedicated") in users:
        return None
    if not local_development():
        return "dedication_unverified"
    # Development grants share the operator's own connection, in local development only:
    # several organizations may, but not when it is also a tenant's own connection.
    others = {
        organization
        for organization, how in users
        if organization != organization_id and not (process and how == "process")
    }
    return "connection_shared" if others else None


async def _connection_users(
    session: AsyncSession, fingerprint: str | None, *, process: bool
) -> set[tuple[uuid.UUID, str]]:
    """(organization, how) for everyone reaching a SigNoz connection: stored credentials,
    assigned ingestion keys, an operator's dedicated verification, and, for the
    process-wide connection, development grants. Fingerprints catch equivalent spellings
    only; other DNS names for one backend need the operator's verification (D038)."""
    parts = []
    if fingerprint is not None:
        parts += [
            select(Environment.organization_id, literal("credentials"))
            .join(EnvironmentCredential, EnvironmentCredential.environment_id == Environment.id)
            .where(
                Environment.organization_id.is_not(None),
                EnvironmentCredential.kind == CredentialKind.SIGNOZ,
                EnvironmentCredential.connection_fingerprint == fingerprint,
            ),
            select(SigNozTenantKey.organization_id, literal("keys")).where(
                SigNozTenantKey.connection_fingerprint == fingerprint
            ),
            select(SigNozDedicatedConnection.organization_id, literal("dedicated")).where(
                SigNozDedicatedConnection.connection_fingerprint == fingerprint
            ),
        ]
    if process:
        parts.append(
            select(Environment.organization_id, literal("process")).where(
                Environment.organization_id.is_not(None),
                Environment.process_credentials,
                ~exists().where(EnvironmentCredential.environment_id == Environment.id),
            )
        )
    if not parts:
        return set()
    return {(row[0], row[1]) for row in await session.execute(union(*parts))}


def _check_destinations(
    config: CredentialConfig, nebius: dict[str, str], signoz: dict[str, str]
) -> None:
    # Re-checked on every resolution: the operator may have narrowed the allowlists since
    # the credentials were saved. Addresses are vetted again at connection time.
    try:
        allowed = parse_destination(nebius["base_url"]).origin in config.nebius_origins
        telemetry = parse_destination(signoz["base_url"])
    except (KeyError, DestinationNotAllowed):
        raise CredentialRefused("destination_not_allowed") from None
    if not allowed or (
        telemetry.origin not in config.telemetry_origins and telemetry.scheme != "https"
    ):
        raise CredentialRefused("destination_not_allowed")


async def environment_credentials(session: AsyncSession, environment: Environment) -> Credentials:
    """The credentials for runs in this environment, or CredentialRefused."""
    rows = {
        row.kind: row
        for row in await session.scalars(
            select(EnvironmentCredential).where(
                EnvironmentCredential.environment_id == environment.id
            )
        )
    }
    if environment.organization_id is None or (not rows and environment.process_credentials):
        if code := process_refusal(environment):
            raise CredentialRefused(code)
        fingerprint = _process_fingerprint(optional_credential_config())
        if await isolation_problem(
            session, environment.organization_id, fingerprint, None, process=True
        ):
            raise CredentialRefused("telemetry_isolation_unverified")
        return Credentials(
            source="process",
            environment_id=environment.id,
            organization_id=environment.organization_id,
        )
    if set(rows) != set(CredentialKind):
        raise CredentialRefused("configuration_required")
    config = optional_credential_config()
    if config is None:
        raise CredentialRefused("credentials_unavailable")
    config = dataclasses.replace(
        config, telemetry_origins=telemetry_origins_for(config, environment.organization_id)
    )
    nebius = _open(config, environment, rows[CredentialKind.NEBIUS])
    nebius |= {
        k: v for k, v in rows[CredentialKind.NEBIUS].public_json.items() if isinstance(v, str)
    }
    signoz = _open(config, environment, rows[CredentialKind.SIGNOZ])
    if not {"api_key", "base_url", "main_model", "local_model"} <= set(nebius) or not {
        "api_key",
        "base_url",
    } <= set(signoz):
        raise CredentialRefused("credentials_unavailable")
    _check_destinations(config, nebius, signoz)
    row = rows[CredentialKind.SIGNOZ]
    key_ids: tuple[str, ...] = ()
    if row.isolation is TelemetryIsolation.PROVIDER_SCOPED:
        # Resolved per call: removing a binding stops the next query, nothing falls back.
        key_ids = await bound_key_ids(
            session, environment.organization_id, row.connection_fingerprint
        )
        if not key_ids:
            raise CredentialRefused("telemetry_isolation_unverified")
    elif await isolation_problem(
        session, environment.organization_id, row.connection_fingerprint, row.isolation
    ):
        raise CredentialRefused("telemetry_isolation_unverified")
    return Credentials(
        source="environment",
        environment_id=environment.id,
        nebius=nebius,
        signoz=signoz,
        fingerprint=row.connection_fingerprint,
        config=config,
        organization_id=environment.organization_id,
        isolation=row.isolation,
        tenant_key_ids=key_ids,
    )


async def investigation_credentials(
    session: AsyncSession, investigation_id: uuid.UUID
) -> Credentials:
    """The credentials for the Main Investigator, from the investigation's own environment
    rows. Several environments are accepted only when all of them run on the process-wide
    development credentials: no credential owner is chosen among them (D034)."""
    rows = (
        await session.execute(
            select(Environment, Investigation.organization_id)
            .join(
                InvestigationEnvironment, InvestigationEnvironment.environment_id == Environment.id
            )
            .join(Investigation, Investigation.id == InvestigationEnvironment.investigation_id)
            .where(InvestigationEnvironment.investigation_id == investigation_id)
            .order_by(Environment.id)
        )
    ).all()
    if not rows:
        raise CredentialRefused("configuration_required")
    if any(environment.organization_id != organization for environment, organization in rows):
        raise CredentialRefused("scope_mismatch")
    resolved = [await environment_credentials(session, environment) for environment, _ in rows]
    if len(resolved) > 1 and any(c.source != "process" for c in resolved):
        raise CredentialRefused("multi_environment_unsupported")
    return resolved[0]


async def investigation_refusal(session: AsyncSession, investigation_id: uuid.UUID) -> str | None:
    """Readiness for the API and the runner: the refusal code, or None when runnable."""
    try:
        await investigation_credentials(session, investigation_id)
    except CredentialRefused as refused:
        return refused.code
    return None


def _process_fingerprint(config: CredentialConfig | None) -> str | None:
    if config is None:
        return None
    try:
        return crypto.fingerprint(config, load_signoz_settings().base_url)
    except (SigNozError, DestinationNotAllowed):
        return None


def _own_signoz(fingerprint: str) -> ColumnElement[bool]:
    return exists().where(
        EnvironmentCredential.environment_id == Environment.id,
        EnvironmentCredential.kind == CredentialKind.SIGNOZ,
        EnvironmentCredential.connection_fingerprint == fingerprint,
    )


def shares_telemetry(credentials: Credentials) -> ColumnElement[bool]:
    """Environments of the same organization that may query the same telemetry backend
    as `credentials`, for scope ambiguity checks. Other organizations never count: their
    claims must not make this environment's identities ambiguous (that would let one
    tenant block another's queries), and isolation between organizations comes from the
    connection's isolation mode, not from identity names (D037). Fingerprints only help
    to find shared backends; different URLs can still reach one."""
    organization = credentials.organization_id
    same_organization = (
        Environment.organization_id.is_(None)
        if organization is None
        else Environment.organization_id == organization
    )
    return same_organization & _shares_backend(credentials)


def _shares_backend(credentials: Credentials) -> ColumnElement[bool]:
    has_rows = exists().where(EnvironmentCredential.environment_id == Environment.id)
    process = Environment.organization_id.is_(None) | (Environment.process_credentials & ~has_rows)
    config = credentials.config or optional_credential_config()
    process_fingerprint = _process_fingerprint(config)
    if credentials.source == "process":
        return process | _own_signoz(process_fingerprint) if process_fingerprint else process
    assert credentials.fingerprint is not None
    own = _own_signoz(credentials.fingerprint)
    return own | process if credentials.fingerprint == process_fingerprint else own


async def startup_problems(session: AsyncSession) -> list[str]:
    """Readiness at backend start, for the log: counts and setting names, never values.
    Runs are refused individually either way; this makes the reason visible early."""
    problems = []
    hosted = not local_development()
    key_ids = list(await session.scalars(select(EnvironmentCredential.key_id)))
    config = optional_credential_config()
    if key_ids and config is None:
        problems.append(
            f"{len(key_ids)} stored credentials are unusable: credential storage is not "
            "configured (INQUEST_CREDENTIAL_KEYS, INQUEST_CREDENTIAL_ACTIVE_KEY, "
            "INQUEST_CREDENTIAL_FINGERPRINT_KEY)"
        )
    elif config is not None and (missing := sum(k not in config.keys for k in key_ids)):
        problems.append(
            f"{missing} stored credentials use encryption keys missing from INQUEST_CREDENTIAL_KEYS"
        )
    granted = await session.scalar(
        select(func.count()).where(
            Environment.organization_id.is_not(None),
            Environment.process_credentials,
            # An environment with credentials of its own ignores its grant.
            ~exists().where(EnvironmentCredential.environment_id == Environment.id),
        )
    )
    if granted and (hosted or not ProcessCredentialSettings().allow_process_credentials):
        problems.append(
            f"{granted} environments hold process-wide credential grants that are refused "
            + (
                "outside local development (APP_ENV)"
                if hosted
                else "(INQUEST_ALLOW_PROCESS_CREDENTIALS is off)"
            )
        )
    # Saved connections the isolation policy refuses (D038), e.g. cross-organization reuse
    # saved before D037: counts by reason, never which organizations.
    reasons: dict[str, int] = {}
    for row in await isolation_review(session):
        if row.problem is not None:
            reasons[row.problem] = reasons.get(row.problem, 0) + 1
    for reason, count in sorted(reasons.items()):
        problems.append(
            f"{count} SigNoz connections are refused by the isolation policy "
            f"({ISOLATION_PROBLEMS[reason]}); review with scripts.bind_signoz_tenant review"
        )
    legacy = await session.scalar(select(func.count()).where(Environment.organization_id.is_(None)))
    if legacy and hosted:
        problems.append(
            f"{legacy} ownerless legacy environments cannot run outside local development"
        )
    return problems


@dataclass(frozen=True)
class ReviewedConnection:
    environment_id: uuid.UUID
    organization_id: uuid.UUID
    # Stored connections: their isolation; development grants: "process".
    isolation: str
    fingerprint: str | None
    problem: str | None


async def isolation_review(session: AsyncSession) -> list[ReviewedConnection]:
    """Every tenant SigNoz connection with its standing under the isolation policy, for
    operators: stored credentials, plus development grants while they are usable."""
    reviewed = []
    rows = await session.execute(
        select(Environment.id, Environment.organization_id, EnvironmentCredential)
        .join(EnvironmentCredential, EnvironmentCredential.environment_id == Environment.id)
        .where(
            Environment.organization_id.is_not(None),
            EnvironmentCredential.kind == CredentialKind.SIGNOZ,
        )
        .order_by(EnvironmentCredential.connection_fingerprint, Environment.organization_id)
    )
    for environment_id, organization_id, row in rows.all():
        problem = await isolation_problem(
            session, organization_id, row.connection_fingerprint, row.isolation
        )
        reviewed.append(
            ReviewedConnection(
                environment_id,
                organization_id,
                str(row.isolation),
                row.connection_fingerprint,
                problem,
            )
        )
    if process_credentials_usable():
        fingerprint = _process_fingerprint(optional_credential_config())
        granted = await session.execute(
            select(Environment.id, Environment.organization_id).where(
                Environment.organization_id.is_not(None),
                Environment.process_credentials,
                ~exists().where(EnvironmentCredential.environment_id == Environment.id),
            )
        )
        for environment_id, organization_id in granted.all():
            problem = await isolation_problem(
                session, organization_id, fingerprint, None, process=True
            )
            reviewed.append(
                ReviewedConnection(environment_id, organization_id, "process", fingerprint, problem)
            )
    return reviewed
