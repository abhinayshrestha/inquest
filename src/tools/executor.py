"""Deterministic tool executor: the only path from an investigator to SigNoz.

Lifecycle per call:
  phase 1  short transaction: lock the assignment row, authorize, insert the ToolCall
           (rejected, or running) and commit before any network I/O;
  phase 2  no database connection held: run the adapter under one deadline, retry once on
           UNAVAILABLE, probe coverage when the result is empty;
  phase 3  short transaction: re-check the assignment, record the outcome and Evidence
           atomically. Data is returned only after this commits.

Phases 1 and 3 run to completion even if the caller is cancelled, so no row is left
half-written by a cancellation; the caller's CancelledError is re-raised afterwards.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import SessionLocal
from src.integrations.signoz import (
    SigNozError,
    SigNozErrorCode,
    SigNozSettings,
    load_signoz_settings,
)
from src.integrations.signoz.normalize import json_size
from src.integrations.signoz.schemas import Scope, SigNozResult
from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentStatus,
    Evidence,
    Investigation,
    InvestigationAssignment,
    InvestigationStatus,
    ToolCall,
    ToolCallStatus,
    ToolCategory,
)
from src.models.evidence import MAX_DATA_JSON_BYTES
from src.models.evidence import MAX_SUMMARY_CHARS as MAX_EVIDENCE_SUMMARY_CHARS
from src.models.tool_call import (
    MAX_ERROR_MESSAGE_CHARS,
    MAX_INPUT_JSON_BYTES,
    MAX_OUTPUT_METADATA_JSON_BYTES,
    MAX_RESULT_SUMMARY_CHARS,
)
from src.tools import policy
from src.tools.evidence import build_evidence, empty_summary, evidence_data
from src.tools.policy import Rejection
from src.tools.registry import TOOLS, Operation, ToolSpec, coverage_count
from src.tools.sanitize import sanitize_arguments, scrub, scrub_json, validation_message
from src.tools.schemas import ExecutionContext, Outcome, ToolError, ToolExecutionResult
from src.tools.scope import parse_scope, resolve_scope

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

MAX_ERROR_CODE_CHARS = 100
MAX_TOOL_NAME_CHARS = 150
# Shorter secrets would match ordinary words in telemetry and mangle it.
MIN_SECRET_CHARS = 8
SCOPE_WARNINGS = {
    "signoz.get_trace": "spans from services outside the environment scope are not returned",
}


@dataclass
class _Admitted:
    tool_call_id: uuid.UUID
    assignment_id: uuid.UUID
    scope: Scope


@dataclass
class _Run:
    status: ToolCallStatus
    result: SigNozResult[Any] | None = None
    error: ToolError | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] | None = None
    duration_ms: int = 0


def _ms(since: float) -> int:
    return int((time.monotonic() - since) * 1000)


def _internal(exc: BaseException) -> ToolError:
    # The class name is enough to diagnose; exception text can carry hosts or payloads.
    return ToolError(code="executor.internal", message=f"internal error ({type(exc).__name__})")


CANCELLED = ToolError(code="executor.cancelled", message="tool call was cancelled")


async def _uncancellable[T](coro: Coroutine[Any, Any, T]) -> tuple[T, bool]:
    """Awaits coro to completion even if the caller is cancelled meanwhile, and reports
    whether it was. shield() alone would let the caller move on while the write is still
    in flight; this keeps waiting, so the caller re-raises only once the row has landed."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True


def _load_settings(
    settings: SigNozSettings | None,
) -> tuple[SigNozSettings | None, SigNozError | None]:
    if settings is not None:
        return settings, None
    try:
        return load_signoz_settings(), None
    except SigNozError as exc:
        return None, exc


def _secrets(settings: SigNozSettings | None) -> list[str]:
    if settings is None:
        return []
    key = settings.api_key.get_secret_value()
    return [key] if len(key) >= MIN_SECRET_CHARS else []


def _validate(spec: ToolSpec | None, arguments: Any) -> tuple[BaseModel | None, str | None]:
    if spec is None:
        return None, None
    try:
        return spec.request_model.model_validate(arguments), None
    except ValidationError as exc:
        return None, validation_message(exc)


async def execute_tool(
    tool_name: str,
    arguments: Any,
    context: ExecutionContext,
    *,
    session_factory: SessionFactory = SessionLocal,
    signoz_settings: SigNozSettings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ToolExecutionResult:
    """Runs one tool call for an assignment. Tool-level failures come back as a result,
    never as an exception; only the caller's own cancellation is raised."""
    settings, settings_error = _load_settings(signoz_settings)
    secrets = _secrets(settings)
    spec = TOOLS.get(tool_name)
    req, invalid = _validate(spec, arguments)

    admitted, cancelled = await _uncancellable(
        _admit(session_factory, tool_name, spec, req, invalid, arguments, context, secrets)
    )
    if isinstance(admitted, ToolExecutionResult):
        if cancelled:
            raise asyncio.CancelledError
        return admitted
    assert spec is not None and req is not None

    started = time.monotonic()
    if cancelled:
        run = _Run(ToolCallStatus.CANCELLED, error=CANCELLED)
    else:
        try:
            run = await _run(spec, req, admitted.scope, settings, settings_error, http_client)
        except asyncio.CancelledError:
            run = _Run(ToolCallStatus.CANCELLED, error=CANCELLED)
            cancelled = True
    run.duration_ms = _ms(started)

    result, cancelled_late = await _uncancellable(
        _finalize(session_factory, admitted, spec, req, run, secrets)
    )
    if cancelled or cancelled_late:
        raise asyncio.CancelledError
    return result


# --- phase 1: admission -----------------------------------------------------------------


async def _admit(
    session_factory: SessionFactory,
    tool_name: str,
    spec: ToolSpec | None,
    req: BaseModel | None,
    invalid: str | None,
    arguments: Any,
    context: ExecutionContext,
    secrets: list[str],
) -> ToolExecutionResult | _Admitted:
    try:
        async with asyncio.timeout(policy.DB_TIMEOUT_SECONDS):
            async with session_factory() as session, session.begin():
                return await _admit_in(
                    session, tool_name, spec, req, invalid, arguments, context, secrets
                )
    except (SQLAlchemyError, OSError, TimeoutError):
        logger.exception("tool call admission failed for assignment %s", context.assignment_id)
        # Fail closed: without an audit row the query does not run.
        return ToolExecutionResult(
            tool_call_id=None,
            tool_name=tool_name[:MAX_TOOL_NAME_CHARS],
            status=ToolCallStatus.FAILED,
            error=ToolError(
                code="executor.persistence_unavailable",
                message="investigation database unavailable; the tool was not run",
                retryable=True,
            ),
        )


async def _admit_in(
    session: AsyncSession,
    tool_name: str,
    spec: ToolSpec | None,
    req: BaseModel | None,
    invalid: str | None,
    arguments: Any,
    context: ExecutionContext,
    secrets: list[str],
) -> ToolExecutionResult | _Admitted:
    name = scrub(tool_name, secrets, MAX_TOOL_NAME_CHARS)
    # The row lock serializes admission per assignment across workers, so budget counts,
    # the concurrency limit and idempotency lookups cannot race each other.
    row = (
        await session.execute(
            select(InvestigationAssignment, Investigation.status, Environment)
            .join(Investigation, Investigation.id == InvestigationAssignment.investigation_id)
            .join(Environment, Environment.id == InvestigationAssignment.environment_id)
            .where(InvestigationAssignment.id == context.assignment_id)
            .with_for_update(of=InvestigationAssignment)
        )
    ).one_or_none()
    if row is None:
        # Nothing to attach an audit row to: tool_calls.assignment_id is a foreign key.
        return ToolExecutionResult(
            tool_call_id=None,
            tool_name=name,
            status=ToolCallStatus.REJECTED,
            error=ToolError(code="policy.unknown_assignment", message="assignment does not exist"),
        )
    assignment, investigation_status, environment = row
    await _expire_stale(session, assignment.id)

    input_json = (
        scrub_json(req.model_dump(mode="json"), secrets)
        if req is not None
        else sanitize_arguments(arguments, secrets)
    )
    if json_size(input_json) > MAX_INPUT_JSON_BYTES:
        input_json = {"_omitted": f"arguments exceeded {MAX_INPUT_JSON_BYTES} bytes"}

    try:
        if (assignment.investigation_id, assignment.environment_id) != (
            context.investigation_id,
            context.environment_id,
        ):
            raise Rejection(
                "policy.context_mismatch", "execution context does not match the assignment"
            )

        if context.idempotency_key is not None:
            existing = await session.scalar(
                select(ToolCall).where(
                    ToolCall.assignment_id == assignment.id,
                    ToolCall.idempotency_key == context.idempotency_key,
                )
            )
            if existing is not None:
                if req is not None and (existing.tool_name, existing.input_json) == (
                    name,
                    input_json,
                ):
                    return await _replay(session, existing)
                raise Rejection(
                    "policy.idempotency_conflict",
                    "idempotency key was already used for a different request",
                )

        counts: dict[ToolCallStatus, int] = dict(
            (
                await session.execute(
                    select(ToolCall.status, func.count())
                    .where(ToolCall.assignment_id == assignment.id)
                    .group_by(ToolCall.status)
                )
            ).all()  # type: ignore[arg-type]
        )
        rejected = counts.get(ToolCallStatus.REJECTED, 0)
        if rejected >= policy.MAX_REJECTIONS_PER_ASSIGNMENT:
            raise Rejection(
                "policy.rejection_budget_exhausted",
                f"assignment reached {policy.MAX_REJECTIONS_PER_ASSIGNMENT} rejected requests",
            )
        if spec is None:
            raise Rejection("policy.unknown_tool", f"unknown tool {name!r}")
        if invalid is not None or req is None:
            raise Rejection("policy.invalid_arguments", invalid or "invalid arguments")
        if (
            assignment.status != AssignmentStatus.RUNNING
            or investigation_status != InvestigationStatus.INVESTIGATING
        ):
            raise Rejection(
                "policy.assignment_not_running",
                f"assignment is {assignment.status}, investigation is {investigation_status}",
            )
        if (
            environment.status != EnvironmentStatus.ACTIVE
            or environment.type not in spec.environment_types
        ):
            raise Rejection(
                "policy.environment_not_allowed",
                f"{spec.name} is not allowed for this environment",
            )
        policy.check_lookback(req.time_range.start)  # type: ignore[attr-defined]
        others = (
            await session.scalars(
                select(Environment.metadata_json).where(
                    Environment.status == EnvironmentStatus.ACTIVE,
                    Environment.id != environment.id,
                )
            )
        ).all()
        scope = resolve_scope(req, parse_scope(environment.metadata_json), others)
        if sum(counts.values()) - rejected >= policy.MAX_TOOL_CALLS_PER_ASSIGNMENT:
            raise Rejection(
                "policy.call_budget_exhausted",
                f"assignment reached {policy.MAX_TOOL_CALLS_PER_ASSIGNMENT} tool calls",
            )
        if counts.get(ToolCallStatus.RUNNING, 0) >= policy.MAX_CONCURRENT_PER_ASSIGNMENT:
            raise Rejection(
                "policy.concurrency_limit",
                f"assignment already has {policy.MAX_CONCURRENT_PER_ASSIGNMENT} running calls",
                retryable=True,
            )
    except Rejection as rejection:
        call = ToolCall(
            id=uuid.uuid4(),
            assignment_id=assignment.id,
            tool_name=name,
            tool_category=spec.category if spec else ToolCategory.OTHER,
            status=ToolCallStatus.REJECTED,
            input_json=input_json,
            completed_at=func.now(),
            error_code=rejection.code[:MAX_ERROR_CODE_CHARS],
            error_message=scrub(rejection.message, secrets, MAX_ERROR_MESSAGE_CHARS),
            output_metadata_json={"error_retryable": rejection.retryable},
        )
        session.add(call)
        return ToolExecutionResult(
            tool_call_id=call.id,
            tool_name=name,
            status=ToolCallStatus.REJECTED,
            error=ToolError(
                code=call.error_code or rejection.code,
                message=call.error_message or "",
                retryable=rejection.retryable,
            ),
        )

    call = ToolCall(
        id=uuid.uuid4(),
        assignment_id=assignment.id,
        tool_name=spec.name,
        tool_category=spec.category,
        status=ToolCallStatus.RUNNING,
        input_json=input_json,
        idempotency_key=context.idempotency_key,
        started_at=func.now(),
    )
    session.add(call)
    return _Admitted(call.id, assignment.id, scope)


async def _replay(session: AsyncSession, existing: ToolCall) -> ToolExecutionResult:
    if existing.status in (ToolCallStatus.PENDING, ToolCallStatus.RUNNING):
        return ToolExecutionResult(
            tool_call_id=existing.id,
            tool_name=existing.tool_name,
            status=existing.status,
            error=ToolError(
                code="executor.duplicate_in_progress",
                message="this request is already running",
                retryable=True,
            ),
            replayed=True,
        )
    evidence = (
        await session.scalars(
            select(Evidence).where(Evidence.tool_call_id == existing.id).order_by(Evidence.id)
        )
    ).all()
    return _stored_result(existing, list(evidence))


def _stored_result(call: ToolCall, evidence: list[Evidence]) -> ToolExecutionResult:
    meta = call.output_metadata_json or {}
    return ToolExecutionResult(
        tool_call_id=call.id,
        tool_name=call.tool_name,
        status=call.status,
        outcome=meta.get("outcome"),
        summary=call.result_summary,
        data=evidence[0].data_json.get("data") if evidence else None,
        truncated=meta.get("truncated", False),
        warnings=meta.get("warnings", []),
        telemetry_available=meta.get("telemetry_available"),
        evidence_ids=[e.id for e in evidence],
        error=ToolError(
            code=call.error_code,
            message=call.error_message or "",
            retryable=meta.get("error_retryable", False),
        )
        if call.error_code
        else None,
        replayed=True,
    )


async def _expire_stale(session: AsyncSession, assignment_id: uuid.UUID | None = None) -> int:
    stmt = (
        update(ToolCall)
        .where(
            ToolCall.status == ToolCallStatus.RUNNING,
            ToolCall.started_at < func.now() - policy.STALE_AFTER,
        )
        .values(
            status=ToolCallStatus.TIMED_OUT,
            completed_at=func.now(),
            error_code="executor.abandoned",
            error_message="executor stopped before recording an outcome",
        )
        .execution_options(synchronize_session=False)
    )
    if assignment_id is not None:
        stmt = stmt.where(ToolCall.assignment_id == assignment_id)
    return (await session.execute(stmt)).rowcount  # type: ignore[attr-defined]


async def sweep_stale_tool_calls(session: AsyncSession) -> int:
    """Marks running calls whose executor died as timed out. For the orchestrator; the
    caller commits. Admission also sweeps its own assignment, so limits self-heal."""
    return await _expire_stale(session)


# --- phase 2: execution (no database connection held) -----------------------------------


async def _call(
    op: Operation,
    req: BaseModel,
    scope: Scope,
    settings: SigNozSettings,
    client: httpx.AsyncClient | None,
    attempts: list[dict[str, Any]],
) -> SigNozResult[Any]:
    while True:
        attempt: dict[str, Any] = {"n": len(attempts) + 1}
        attempts.append(attempt)
        started = time.monotonic()
        try:
            return await op(req, scope=scope, settings=settings, client=client)
        except SigNozError as exc:
            attempt["error_code"] = exc.code.value
            # Only UNAVAILABLE (5xx, 429, connection) is worth repeating: a timed-out query
            # stays slow and an invalid one stays invalid.
            if exc.code is not SigNozErrorCode.UNAVAILABLE or attempt["n"] > policy.MAX_RETRIES:
                raise
        finally:
            attempt["duration_ms"] = _ms(started)
        await asyncio.sleep(policy.RETRY_BACKOFF_SECONDS)


async def _run(
    spec: ToolSpec,
    req: BaseModel,
    scope: Scope,
    settings: SigNozSettings | None,
    settings_error: SigNozError | None,
    client: httpx.AsyncClient | None,
) -> _Run:
    if settings is None:
        assert settings_error is not None
        return _Run(
            ToolCallStatus.FAILED,
            error=ToolError(code=settings_error.code.value, message=settings_error.message),
        )
    deadline = asyncio.get_running_loop().time() + policy.EXECUTION_DEADLINE_SECONDS
    attempts: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout_at(deadline):
            result = await _call(spec.run, req, scope, settings, client, attempts)
    except TimeoutError:
        return _Run(
            ToolCallStatus.TIMED_OUT,
            error=ToolError(
                code="executor.timeout",
                message=f"tool exceeded the {policy.EXECUTION_DEADLINE_SECONDS}s deadline; "
                "narrow the time range or filters",
                retryable=True,
            ),
            attempts=attempts,
        )
    except SigNozError as exc:
        status = (
            ToolCallStatus.TIMED_OUT
            if exc.code is SigNozErrorCode.TIMEOUT
            else ToolCallStatus.FAILED
        )
        return _Run(
            status,
            error=ToolError(code=exc.code.value, message=exc.message, retryable=exc.retryable),
            attempts=attempts,
        )
    except Exception as exc:
        logger.exception("tool %s raised unexpectedly", spec.name)
        return _Run(ToolCallStatus.FAILED, error=_internal(exc), attempts=attempts)

    coverage = None
    if not spec.has_observations(result.data):
        coverage = await _probe(spec, req, scope, result, settings, client, deadline)
    return _Run(ToolCallStatus.SUCCEEDED, result=result, attempts=attempts, coverage=coverage)


async def _probe(
    spec: ToolSpec,
    req: BaseModel,
    scope: Scope,
    primary: SigNozResult[Any],
    settings: SigNozSettings,
    client: httpx.AsyncClient | None,
    deadline: float,
) -> dict[str, Any]:
    """Coverage for an empty result. Never raises: a failed probe leaves the primary
    result intact and coverage unknown."""
    try:
        op, probe_req = spec.probe(req)
    except Exception as exc:
        logger.exception("coverage probe for %s could not be built", spec.name)
        return {"telemetry_available": "unknown", "error_code": _internal(exc).code}
    info: dict[str, Any] = {"operation": f"signoz.{op.__name__}"}
    if op is spec.run and probe_req == req:
        # The primary query already was the bare coverage query, and it came back empty.
        return {
            **info,
            "telemetry_available": "no",
            "count": 0,
            "source_reference": primary.source_reference,
            "reused_primary": True,
        }
    if deadline - asyncio.get_running_loop().time() < policy.MIN_PROBE_SECONDS:
        return {**info, "telemetry_available": "unknown", "skipped": "insufficient_time"}
    try:
        async with asyncio.timeout_at(deadline):
            result = await op(probe_req, scope=scope, settings=settings, client=client)
        count = coverage_count(result.data)
    except TimeoutError:
        return {**info, "telemetry_available": "unknown", "error_code": "executor.timeout"}
    except SigNozError as exc:
        return {**info, "telemetry_available": "unknown", "error_code": exc.code.value}
    except Exception as exc:
        logger.exception("coverage probe for %s raised unexpectedly", spec.name)
        return {**info, "telemetry_available": "unknown", "error_code": _internal(exc).code}
    return {
        **info,
        "telemetry_available": "yes" if count else "no",
        "count": count,
        "source_reference": result.source_reference,
    }


# --- phase 3: finalization --------------------------------------------------------------


async def _finalize(
    session_factory: SessionFactory,
    admitted: _Admitted,
    spec: ToolSpec,
    req: BaseModel,
    run: _Run,
    secrets: list[str],
) -> ToolExecutionResult:
    for _ in range(2):
        try:
            async with asyncio.timeout(policy.DB_TIMEOUT_SECONDS):
                async with session_factory() as session, session.begin():
                    return await _finalize_in(session, admitted, spec, req, run, secrets)
        except IntegrityError:
            # A constraint refused the outcome. Not transient: record a failure instead.
            logger.exception(
                "tool call %s: outcome rejected by a constraint", admitted.tool_call_id
            )
            if run.result is None:
                break
            run = _Run(
                ToolCallStatus.FAILED,
                error=ToolError(
                    code="executor.persistence_rejected",
                    message="the outcome could not be stored; result withheld",
                ),
                attempts=run.attempts,
                duration_ms=run.duration_ms,
            )
        except (SQLAlchemyError, OSError, TimeoutError):
            logger.exception("tool call %s: could not record the outcome", admitted.tool_call_id)
    # The row stays running until a sweep marks it abandoned. Unpersisted data is never
    # returned: every conclusion must trace back to stored evidence.
    return ToolExecutionResult(
        tool_call_id=admitted.tool_call_id,
        tool_name=spec.name,
        status=ToolCallStatus.FAILED,
        error=ToolError(
            code="executor.persistence_failed",
            message="the outcome could not be recorded; result withheld",
            retryable=True,
        ),
    )


def _fit_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if json_size(metadata) <= MAX_OUTPUT_METADATA_JSON_BYTES:
        return metadata
    keep = ("outcome", "telemetry_available", "time_range", "source_reference", "evidence_ids")
    return {k: metadata[k] for k in keep if k in metadata} | {"_trimmed": True}


async def _finalize_in(
    session: AsyncSession,
    admitted: _Admitted,
    spec: ToolSpec,
    req: BaseModel,
    run: _Run,
    secrets: list[str],
) -> ToolExecutionResult:
    # Same lock order as admission (assignment first). FOR SHARE lets finalizations run
    # side by side while making an orchestrator status change wait for this commit.
    assignment = await session.scalar(
        select(InvestigationAssignment)
        .where(InvestigationAssignment.id == admitted.assignment_id)
        .with_for_update(read=True)
    )
    call = await session.scalar(
        select(ToolCall).where(ToolCall.id == admitted.tool_call_id).with_for_update()
    )
    assert assignment is not None and call is not None
    if call.status is not ToolCallStatus.RUNNING:
        # A sweep already gave up on this call; its recorded outcome stands.
        return _stored_result(call, []).model_copy(update={"replayed": False})

    if run.status is ToolCallStatus.SUCCEEDED and assignment.status != AssignmentStatus.RUNNING:
        # Evidence must not land after the round closed and Main began correlating.
        run = _Run(
            ToolCallStatus.CANCELLED,
            error=ToolError(
                code="executor.assignment_closed",
                message="assignment closed before the result was recorded; result withheld",
            ),
            attempts=run.attempts,
            duration_ms=run.duration_ms,
        )

    scope = admitted.scope
    metadata: dict[str, Any] = {
        "time_range": req.time_range.model_dump(mode="json"),  # type: ignore[attr-defined]
        "scope": scope.model_dump(mode="json"),
        "attempts": run.attempts,
    }
    status, error = run.status, run.error
    summary = data = outcome = telemetry = None
    truncated, warnings, evidence = False, [], None
    result = run.result if run.status is ToolCallStatus.SUCCEEDED else None

    if result is not None:
        observed = spec.has_observations(result.data)
        outcome = Outcome.OBSERVATIONS if observed else spec.empty_outcome
        warnings = [scrub(w, secrets, 300) for w in result.warnings]
        if spec.name in SCOPE_WARNINGS:
            warnings.append(SCOPE_WARNINGS[spec.name])
        truncated = result.truncated
        if observed:
            summary = scrub(result.summary, secrets, MAX_EVIDENCE_SUMMARY_CHARS)
            data_json = scrub_json(evidence_data(result, scope), secrets)
            if json_size(data_json) > MAX_DATA_JSON_BYTES:
                status = ToolCallStatus.FAILED
                error = ToolError(
                    code="executor.output_too_large",
                    message="result exceeded the evidence size limit; narrow the query",
                )
                summary = outcome = None
            else:
                evidence = build_evidence(
                    evidence_type=spec.evidence_type,
                    result=result,
                    time_range=req.time_range,  # type: ignore[attr-defined]
                    scope=scope,
                    summary=summary,
                    data_json=data_json,
                    assignment=assignment,
                    tool_call=call,
                )
                data = data_json["data"]
        else:
            telemetry = (run.coverage or {}).get("telemetry_available", "unknown")
            summary = scrub(
                empty_summary(result.summary, scope, telemetry), secrets, MAX_RESULT_SUMMARY_CHARS
            )
            data = scrub_json(result.data.model_dump(mode="json"), secrets)
        if status is ToolCallStatus.SUCCEEDED:
            metadata |= {
                "outcome": outcome,
                "telemetry_available": telemetry,
                "coverage_probe": run.coverage,
                "stats": result.stats.model_dump(),
                "truncated": truncated,
                "warnings": warnings,
                "source_reference": result.source_reference,
                "evidence_ids": [str(evidence.id)] if evidence else [],
            }

    if error is not None:
        error = ToolError(
            code=error.code[:MAX_ERROR_CODE_CHARS],
            message=scrub(error.message, secrets, MAX_ERROR_MESSAGE_CHARS),
            retryable=error.retryable,
        )
        metadata["error_retryable"] = error.retryable
        data = summary = outcome = telemetry = None
        warnings, evidence = [], None

    call.status = status
    call.completed_at = func.now()  # type: ignore[assignment]
    call.duration_ms = run.duration_ms
    call.attempt_number = max(1, len(run.attempts))
    call.result_summary = summary
    call.output_metadata_json = _fit_metadata(metadata)
    call.error_code = error.code if error else None
    call.error_message = error.message if error else None
    if evidence is not None:
        session.add(evidence)

    return ToolExecutionResult(
        tool_call_id=call.id,
        tool_name=spec.name,
        status=status,
        outcome=outcome,
        summary=summary,
        data=data,
        truncated=truncated,
        warnings=warnings,
        telemetry_available=telemetry,
        evidence_ids=[evidence.id] if evidence else [],
        error=error,
    )
