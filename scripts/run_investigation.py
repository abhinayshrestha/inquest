"""Development-only harness: run the first complete investigation (Main + Local).

Creates an investigation through the normal service (turn 1 and the round-1 assignment,
not started), then runs turn 1 with the Main Investigator (Nemotron Ultra) dispatching
Local Investigators (Nemotron Super) through the running backend's MCP server. Prints
the result, per-call model latency and tokens, the answer, the report and the records.
No credential or token is ever printed.

    uv run python -m scripts.run_investigation check
    uv run python -m scripts.run_investigation setup --question "..." [--env-name ubuntu-vm]
    uv run python -m scripts.run_investigation run --investigation-id I
    uv run python -m scripts.run_investigation ask --investigation-id I --question "..."
    uv run python -m scripts.run_investigation inspect --investigation-id I
    uv run python -m scripts.run_investigation recover [--apply]
"""

import argparse
import asyncio
import dataclasses
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from scripts.run_local_investigator import ModelCallLog
from scripts.run_local_investigator import check as local_check
from scripts.test_tool_executor import emit, fail
from src.config import get_settings
from src.credentials import resolver
from src.db.session import SessionLocal, engine
from src.investigators.checkpoint import open_checkpointer
from src.investigators.llm import NebiusConfigError, local_model, main_model
from src.investigators.refs import ref
from src.investigators.runner import RunnerBusy, recovery_plan, run_turn
from src.investigators.runner import recover as apply_recovery
from src.models import (
    ACTIVE_TURN_STATUSES,
    Environment,
    EnvironmentStatus,
    Evidence,
    Finding,
    InvestigationAssignment,
    InvestigationTurn,
    ToolCall,
)
from src.schemas.investigation import InvestigationCreate
from src.services import investigations, progress, turns
from src.services.investigations import InvalidTransition

PREVIEW_CHARS = 400


def _preview(text: str) -> str:
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


async def check(args: argparse.Namespace) -> None:
    try:
        main_model()
        print("main model: configured")
    except NebiusConfigError as exc:
        print(f"main model: {exc}")
    await local_check(args)


async def setup(args: argparse.Namespace) -> None:
    async with SessionLocal() as s:
        environment = await s.scalar(select(Environment).where(Environment.name == args.env_name))
        if environment is None or environment.status != EnvironmentStatus.ACTIVE:
            fail(
                f"no active environment {args.env_name!r}; "
                "create it with scripts.test_tool_executor setup"
            )
        investigation = await investigations.create_investigation(
            s,
            InvestigationCreate(
                environment_id=environment.id, title=args.title, question=args.question
            ),
        )
        emit(
            {
                "investigation_id": investigation.id,
                "status": investigation.status,
                "environment": environment.name,
                "signoz_scope": (environment.metadata_json or {}).get("signoz"),
                "note": "not started: run it with the `run` command",
            }
        )


async def _active_turn(investigation_id: uuid.UUID) -> uuid.UUID:
    async with SessionLocal() as s:
        turn_id = await s.scalar(
            select(InvestigationTurn.id).where(
                InvestigationTurn.investigation_id == investigation_id,
                InvestigationTurn.status.in_(ACTIVE_TURN_STATUSES),
            )
        )
    if turn_id is None:
        fail(f"investigation {investigation_id} has no pending or running turn")
    return turn_id  # type: ignore[return-value]


async def records(investigation_id: uuid.UUID) -> dict[str, Any]:
    async with SessionLocal() as s:
        assignments = await s.scalars(
            select(InvestigationAssignment)
            .where(InvestigationAssignment.investigation_id == investigation_id)
            .order_by(InvestigationAssignment.round, InvestigationAssignment.created_at)
        )
        evidence = await s.scalars(
            select(Evidence).where(Evidence.investigation_id == investigation_id)
        )
        findings = await s.scalars(
            select(Finding).where(Finding.investigation_id == investigation_id)
        )
        turn_rows = await turns.list_turns(s, investigation_id)
        report = await turns.latest_report(s, investigation_id)
        versions = await turns.list_reports(s, investigation_id)
        calls = await s.scalars(
            select(InvestigationAssignment.round, ToolCall.id)
            .join(ToolCall, ToolCall.assignment_id == InvestigationAssignment.id)
            .where(InvestigationAssignment.investigation_id == investigation_id)
        )
        events = await progress.list_progress_events(s, investigation_id, limit=500)
        return {
            "turns": [
                {"seq": t.seq, "status": t.status, "decision": t.decision,
                 "error_code": t.error_code,
                 "report_version": (t.answer_json or {}).get("report_version"),
                 "rounds": (t.answer_json or {}).get("rounds"),
                 "answer_md": _preview((t.answer_json or {}).get("answer_md", ""))}
                for t in turn_rows
            ],
            "tool_calls": len(list(calls)),
            "report_versions": [
                {"version": r.version, "turn_id": r.turn_id,
                 "outcome": r.report_json.get("outcome"),
                 "diff": r.report_json.get("diff")}
                for r in versions
            ],
            "assignments": [
                {"round": a.round, "status": a.status, "objective": _preview(a.objective)}
                for a in assignments
            ],
            "evidence": [{"ref": ref("evidence", e.id), "title": e.title} for e in evidence],
            "findings": [
                {"ref": ref("finding", f.id), "status": f.status, "title": f.title}
                for f in findings
            ],
            "report": {"version": report.version, **report.report_json} if report else None,
            "events": [e.type for e in events],
        }  # fmt: skip


async def run(args: argparse.Namespace) -> None:
    await _run(args.investigation_id, await _active_turn(args.investigation_id))


async def ask(args: argparse.Namespace) -> None:
    """A follow-up question on an idle investigation: a new turn, run end to end."""
    async with SessionLocal() as s:
        try:
            turn = await turns.create_follow_up_turn(s, args.investigation_id, args.question)
        except turns.TurnConflict as exc:
            fail(str(exc))
        except InvalidTransition as exc:
            fail(str(exc))
    before = (await records(args.investigation_id))["tool_calls"]
    await _run(args.investigation_id, turn.id, tool_calls_before=before)


async def _run(
    investigation_id: uuid.UUID, turn_id: uuid.UUID, *, tool_calls_before: int | None = None
) -> None:
    # The models below use the process-wide .env key: never for an environment that has
    # credentials of its own (D034).
    async with SessionLocal() as s:
        try:
            source = (await resolver.investigation_credentials(s, investigation_id)).source
        except resolver.CredentialRefused as exc:
            fail(f"refused: {exc.code}")
    if source != "process":
        fail("this investigation uses its environment's own credentials; run it through the API")
    main_log, local_log = ModelCallLog(), ModelCallLog()
    try:
        main = main_model().model_copy(update={"callbacks": [main_log]})
        local = local_model().model_copy(update={"callbacks": [local_log]})
    except NebiusConfigError as exc:
        fail(str(exc))
    started = time.perf_counter()
    async with open_checkpointer() as checkpointer:
        try:
            result = await run_turn(turn_id, checkpointer, main=main, local=local)
        except RunnerBusy as exc:
            fail(str(exc))

    def totals(log: ModelCallLog) -> dict[str, Any]:
        calls = log.calls
        return {
            "calls": len(calls),
            "latency_ms": sum(c["latency_ms"] for c in calls),
            "input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
            "output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
            "reasoning_tokens": sum(c.get("reasoning_tokens") or 0 for c in calls),
            "errors": [c["error"] for c in calls if "error" in c],
        }

    after = await records(investigation_id)
    emit(
        {
            "result": dataclasses.asdict(result),
            "wall_clock_s": round(time.perf_counter() - started, 1),
            "main_model": totals(main_log),
            "local_model": totals(local_log),
            "main_calls": main_log.calls,
            "new_tool_calls": (
                after["tool_calls"] - tool_calls_before if tool_calls_before is not None else None
            ),
            "records": after,
        }
    )


async def inspect(args: argparse.Namespace) -> None:
    emit(await records(args.investigation_id))


async def recover(args: argparse.Namespace) -> None:
    async with open_checkpointer() as checkpointer:
        if args.apply:
            # Reconciles legacy turns only; checkpointed turns are resumed with `run`.
            emit(await apply_recovery(checkpointer, resume=False))
        else:
            emit({"dry_run": True, "plan": await recovery_plan(checkpointer)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="model config, MCP secret and backend reachability")
    p = sub.add_parser("setup", help="create an investigation (turn 1, not started)")
    p.add_argument("--env-name", default="ubuntu-vm")
    p.add_argument("--title", default="Main Investigator live test")
    p.add_argument("--question", required=True)
    for name, text in (("run", "run or resume the active turn"), ("inspect", "show records")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--investigation-id", type=uuid.UUID, required=True)
    p = sub.add_parser("ask", help="ask a follow-up question on an idle investigation")
    p.add_argument("--investigation-id", type=uuid.UUID, required=True)
    p.add_argument("--question", required=True)
    p = sub.add_parser("recover", help="plan (default) or apply reconciliation of legacy turns")
    p.add_argument("--apply", action="store_true")
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    if get_settings().app_env == "production":
        fail("development-only script; refusing to run with APP_ENV=production")
    logging.basicConfig(level=logging.WARNING)
    try:
        await {
            "check": check,
            "ask": ask,
            "setup": setup,
            "run": run,
            "inspect": inspect,
            "recover": recover,
        }[args.command](args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
