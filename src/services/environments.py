import re
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    can_use_process_credentials,
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
    them (host-side tooling) the environment is ownerless legacy-style."""
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
        # Only translate the unique-name violation; anything else is a real bug.
        if exc.orig.diag.constraint_name == "uq_environments_organization_id_name":
            raise EnvironmentNameTaken(data.name) from exc
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


async def _scope(session: AsyncSession, environment: Environment) -> TelemetryScopeRead:
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
    # Ambiguity only matters among environments that query the same telemetry backend:
    # those using the process-wide development connection (the executor's rule). Tenant
    # environments without a grant query nothing, so they neither have nor cause it.
    others = (
        (
            await session.scalars(
                select(Environment.metadata_json).where(
                    Environment.id != environment.id,
                    Environment.status == EnvironmentStatus.ACTIVE,
                    shares_process_telemetry(),
                )
            )
        ).all()
        if can_use_process_credentials(environment)
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


def shares_process_telemetry():
    """SQL form of can_use_process_credentials."""
    return Environment.organization_id.is_(None) | Environment.process_credentials


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


CONFIGURATION_REQUIRED = ["per-environment credentials (not available until Phase 7C)"]


async def environment_detail(session: AsyncSession, environment: Environment) -> EnvironmentDetail:
    shared = can_use_process_credentials(environment)
    # Without a grant, the process-wide settings are not this environment's and are not
    # reported as if they were (D033).
    return EnvironmentDetail(
        id=environment.id,
        name=environment.name,
        type=environment.type,
        status=environment.status,
        created_at=environment.created_at,
        updated_at=environment.updated_at,
        organization_id=environment.organization_id,
        owner_user_id=environment.owner_user_id,
        credentials="process_development" if shared else "configuration_required",
        scope=await _scope(session, environment),
        stats=await _stats(session, environment.id),
        telemetry_provider=(
            telemetry_provider_status()
            if shared
            else ProviderStatus(
                provider="signoz",
                source="environment",
                configured=False,
                issues=CONFIGURATION_REQUIRED,
            )
        ),
        ai_provider=(
            ai_provider_status()
            if shared
            else AiProviderStatus(
                provider="nebius_token_factory",
                source="environment",
                configured=False,
                issues=CONFIGURATION_REQUIRED,
                main_model=None,
                local_model=None,
            )
        ),
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
