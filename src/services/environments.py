import re
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.credentials import resolver
from src.credentials.resolver import CredentialRefused, Credentials
from src.integrations.signoz.errors import SigNozError
from src.integrations.signoz.settings import load_signoz_settings
from src.investigators.llm import NebiusConfigError, load_nebius_settings
from src.models import (
    Environment,
    EnvironmentStatus,
    Evidence,
    Investigation,
    InvestigationAssignment,
    InvestigationEnvironment,
    InvestigationStatus,
    ToolCall,
    ToolCallStatus,
)
from src.schemas.environment import (
    AiProviderStatus,
    EnvironmentCreate,
    EnvironmentDetail,
    EnvironmentListItem,
    EnvironmentStats,
    ProviderStatus,
    ScopeProblem,
    SignalCoverage,
    TelemetryCoverage,
    TelemetryScopeRead,
)
from src.services import access, limits
from src.tools.policy import Rejection
from src.tools.scope import SCOPE_KEY, claims, parse_scope

ACTIVE_INVESTIGATION = (
    InvestigationStatus.PENDING,
    InvestigationStatus.INVESTIGATING,
    InvestigationStatus.CORRELATING,
)
# Bounded: coverage is the latest result per signal and identity, so recent calls suffice.
MAX_COVERAGE_CALLS = 500
SIGNALS = {
    "signoz.query_metrics": "metrics",
    "signoz.search_logs": "logs",
    "signoz.log_volume": "logs",
    "signoz.search_spans": "traces",
    "signoz.trace_stats": "traces",
    "signoz.get_trace": "traces",
}
SETTING_NAME = re.compile(r"\b(?:SIGNOZ|NEBIUS)_[A-Z_]+\b")


class EnvironmentNameTaken(Exception):
    pass


async def create_environment(
    session: AsyncSession,
    data: EnvironmentCreate,
    *,
    organization_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
) -> Environment:
    """The API passes the caller's checked organization and the caller as owner; without
    them (host-side tooling) the environment is ownerless legacy-style. QuotaExceeded at
    the organization's environment limit, counted under its quota lock."""
    if organization_id is not None:
        await limits.lock(session, "organization", organization_id)
        limits.require(
            "environments_per_organization",
            await session.scalar(
                select(func.count()).where(Environment.organization_id == organization_id)
            ),
        )
    environment = Environment(
        **data.model_dump(include={"name", "type", "metadata_json"}),
        organization_id=organization_id,
        owner_user_id=owner_user_id,
    )
    session.add(environment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Only translate the unique-name violation and the owner's membership ending
        # meanwhile (an owner must be a member); anything else is a real bug.
        constraint = exc.orig.diag.constraint_name
        if constraint == "uq_environments_organization_id_name":
            raise EnvironmentNameTaken(data.name) from exc
        if constraint == "fk_environments_organization_id_organization_memberships":
            raise access.NoAccess(organization_id) from exc
        raise
    return environment


async def list_environments(
    session: AsyncSession, *, organization_id: uuid.UUID, limit: int
) -> list[EnvironmentListItem]:
    rows = await session.execute(
        select(
            Environment,
            func.count(InvestigationEnvironment.investigation_id),
            func.max(Investigation.created_at),
        )
        .outerjoin(
            InvestigationEnvironment, InvestigationEnvironment.environment_id == Environment.id
        )
        .outerjoin(Investigation, Investigation.id == InvestigationEnvironment.investigation_id)
        .where(Environment.organization_id == organization_id)
        .group_by(Environment.id)
        .order_by(Environment.name)
        .limit(limit)
    )
    return [
        EnvironmentListItem(
            id=e.id,
            name=e.name,
            type=e.type,
            status=e.status,
            created_at=e.created_at,
            investigation_count=count,
            last_investigation_at=last,
        )
        for e, count, last in rows.all()
    ]


async def _scope(
    session: AsyncSession, environment: Environment, credentials: Credentials | None
) -> TelemetryScopeRead:
    """The environment's SigNoz scope and the problems the Tool Executor would reject its
    queries for (missing, invalid, or an identity also claimed by another environment)."""
    raw = environment.metadata_json.get(SCOPE_KEY) if environment.metadata_json else None
    try:
        own = parse_scope(environment.metadata_json)
    except Rejection as exc:
        listed = raw if isinstance(raw, dict) else {}

        def names(key: str) -> list[str]:
            values = listed.get(key)
            if not isinstance(values, list):
                return []
            return [v for v in values if isinstance(v, str)][:20]

        return TelemetryScopeRead(
            status="missing" if raw is None else "invalid",
            hosts=names("hosts"),
            services=names("services"),
            deployment_environment=None,
            problems=[ScopeProblem(code=exc.code, message=exc.message)],
        )
    # Ambiguity only matters among environments that may query the same telemetry backend
    # (the executor's rule). Environments without usable credentials query nothing.
    others = (
        (
            await session.scalars(
                select(Environment.metadata_json).where(
                    Environment.id != environment.id,
                    Environment.status == EnvironmentStatus.ACTIVE,
                    resolver.shares_telemetry(credentials),
                )
            )
        ).all()
        if credentials is not None
        else []
    )
    problems = []
    for identity, values in (("host", own.hosts), ("service", own.services)):
        deployment = own.deployment_environment if identity == "service" else None
        for metadata in others:
            claimed, theirs = claims(metadata, identity)
            for value in sorted(claimed.intersection(values)):
                if deployment and theirs and deployment != theirs:
                    continue
                problems.append(
                    ScopeProblem(
                        code="policy.scope_ambiguous",
                        message=f"{identity} {value!r} is also claimed by another active "
                        "environment; queries for it are rejected",
                    )
                )
    if not own.hosts and not own.services:
        problems.append(
            ScopeProblem(
                code="policy.scope_missing_identity",
                message="no hosts or services are listed; every telemetry query is rejected",
            )
        )
    return TelemetryScopeRead(
        status="configured",
        hosts=list(own.hosts),
        services=list(own.services),
        deployment_environment=own.deployment_environment,
        problems=problems,
    )


def _issues(message: str) -> list[str]:
    # Loader errors name settings, never their values.
    return sorted(set(SETTING_NAME.findall(message))) or ["configuration"]


def telemetry_provider_status() -> ProviderStatus:
    try:
        load_signoz_settings()
    except SigNozError as exc:
        return ProviderStatus(
            provider="signoz",
            source="process_environment",
            configured=False,
            issues=_issues(str(exc)),
        )
    return ProviderStatus(
        provider="signoz", source="process_environment", configured=True, issues=[]
    )


def ai_provider_status() -> AiProviderStatus:
    try:
        settings = load_nebius_settings()
    except NebiusConfigError as exc:
        return AiProviderStatus(
            provider="nebius_token_factory",
            source="process_environment",
            configured=False,
            issues=_issues(str(exc)),
            main_model=None,
            local_model=None,
        )
    missing = [
        name
        for name, value in (
            ("NEBIUS_MAIN_MODEL", settings.main_model),
            ("NEBIUS_LOCAL_MODEL", settings.local_model),
        )
        if not value
    ]
    # Model ids are not secrets; the key and base URL are never returned.
    return AiProviderStatus(
        provider="nebius_token_factory",
        source="process_environment",
        configured=not missing,
        issues=missing,
        main_model=settings.main_model,
        local_model=settings.local_model,
    )


async def _stats(session: AsyncSession, environment_id: uuid.UUID) -> EnvironmentStats:
    investigations, active, last = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Investigation.status.in_(ACTIVE_INVESTIGATION)),
                func.max(Investigation.created_at),
            )
            .select_from(InvestigationEnvironment)
            .join(Investigation, Investigation.id == InvestigationEnvironment.investigation_id)
            .where(InvestigationEnvironment.environment_id == environment_id)
        )
    ).one()
    evidence = await session.scalar(
        select(func.count()).select_from(Evidence).where(Evidence.environment_id == environment_id)
    )
    tool_calls = await session.scalar(
        select(func.count())
        .select_from(ToolCall)
        .join(InvestigationAssignment, InvestigationAssignment.id == ToolCall.assignment_id)
        .where(InvestigationAssignment.environment_id == environment_id)
    )
    return EnvironmentStats(
        investigations=investigations,
        active_investigations=active,
        evidence=evidence or 0,
        tool_calls=tool_calls or 0,
        last_investigation_at=last,
    )


async def get_environment(session: AsyncSession, environment_id: uuid.UUID) -> Environment | None:
    return await session.get(Environment, environment_id)


# Detail states from the resolver's codes; the codes themselves name no values.
CREDENTIAL_STATES = {
    "configuration_required": "configuration_required",
    "process_credentials_not_permitted": "configuration_required",
    "credentials_unavailable": "unavailable",
    "destination_not_allowed": "unavailable",
    "telemetry_isolation_unverified": "unavailable",
}


def _refused_status(code: str) -> tuple[ProviderStatus, AiProviderStatus]:
    issues = [code]
    return (
        ProviderStatus(provider="signoz", source="environment", configured=False, issues=issues),
        AiProviderStatus(
            provider="nebius_token_factory",
            source="environment",
            configured=False,
            issues=issues,
            main_model=None,
            local_model=None,
        ),
    )


async def environment_detail(session: AsyncSession, environment: Environment) -> EnvironmentDetail:
    # The same resolution runs use; the process-wide settings are reported only for an
    # environment that would really run on them (D033, D034). Secrets are discarded here.
    try:
        credentials: Credentials | None = await resolver.environment_credentials(
            session, environment
        )
    except CredentialRefused as refused:
        credentials = None
        state = CREDENTIAL_STATES[refused.code]
        telemetry, ai = _refused_status(refused.code)
    else:
        if credentials.source == "process":
            state = "process_development"
            telemetry, ai = telemetry_provider_status(), ai_provider_status()
        else:
            state = "environment"
            telemetry = ProviderStatus(
                provider="signoz", source="environment", configured=True, issues=[]
            )
            # Model ids are not secrets (D034); the key and base URL never leave here.
            ai = AiProviderStatus(
                provider="nebius_token_factory",
                source="environment",
                configured=True,
                issues=[],
                main_model=credentials.nebius["main_model"],
                local_model=credentials.nebius["local_model"],
            )
    return EnvironmentDetail(
        id=environment.id,
        name=environment.name,
        type=environment.type,
        status=environment.status,
        created_at=environment.created_at,
        updated_at=environment.updated_at,
        organization_id=environment.organization_id,
        owner_user_id=environment.owner_user_id,
        credentials=state,  # type: ignore[arg-type]
        scope=await _scope(session, environment, credentials),
        stats=await _stats(session, environment.id),
        telemetry_provider=telemetry,
        ai_provider=ai,
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


async def telemetry_coverage(session: AsyncSession, environment_id: uuid.UUID) -> TelemetryCoverage:
    """Latest recorded result per signal and scope identity, from tool calls made by this
    environment's investigations. Observed means a query returned data; no_data means the
    executor's coverage probe found nothing for that identity in the window. Neither is a
    live check: each entry says when it was recorded."""
    rows = (
        await session.execute(
            select(
                ToolCall.tool_name,
                ToolCall.status,
                ToolCall.output_metadata_json,
                ToolCall.completed_at,
                InvestigationAssignment.investigation_id,
            )
            .join(InvestigationAssignment, InvestigationAssignment.id == ToolCall.assignment_id)
            .where(
                InvestigationAssignment.environment_id == environment_id,
                ToolCall.status.in_(
                    (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED, ToolCallStatus.TIMED_OUT)
                ),
                ToolCall.completed_at.is_not(None),
            )
            .order_by(ToolCall.completed_at.desc())
            .limit(MAX_COVERAGE_CALLS)
        )
    ).all()
    latest: dict[tuple[str, str, str], SignalCoverage] = {}
    checked = failed = 0
    for tool, status, meta, completed_at, investigation_id in rows:
        if status != ToolCallStatus.SUCCEEDED:
            failed += 1
            continue
        checked += 1
        signal = SIGNALS.get(tool)
        meta = meta or {}
        scope = meta.get("scope") if isinstance(meta.get("scope"), dict) else {}
        identity = scope.get("identity")
        values = scope.get("values") if isinstance(scope.get("values"), list) else []
        if signal is None or identity not in ("host", "service"):
            continue
        if meta.get("outcome") == "observations":
            result = "observed"
        elif meta.get("telemetry_available") == "no":
            result = "no_data"
        else:
            result = "unknown"
        window = meta.get("time_range") if isinstance(meta.get("time_range"), dict) else {}
        for value in values:
            key = (signal, identity, str(value))
            if isinstance(value, str) and key not in latest:
                latest[key] = SignalCoverage(
                    signal=signal,
                    identity=identity,
                    value=value[:255],
                    status=result,
                    window_start=_datetime(window.get("start")),
                    window_end=_datetime(window.get("end")),
                    checked_at=completed_at,
                    investigation_id=investigation_id,
                )
    return TelemetryCoverage(
        signals=sorted(latest.values(), key=lambda c: (c.signal, c.identity, c.value)),
        queries_checked=checked,
        queries_failed=failed,
    )
