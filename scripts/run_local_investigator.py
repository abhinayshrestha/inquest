"""Development-only harness: run one Local Investigator against a running assignment.

Uses the real Nemotron model on Nebius, the running backend's MCP server (so every query
goes through the Tool Executor to SigNoz) and PostgreSQL checkpoints. Create the
assignment with `scripts.test_tool_executor setup` (its question becomes the objective).
Prints the run result, per-call model latency and tokens, and the persisted records.
No credential or token is ever printed.

    uv run python -m scripts.run_local_investigator check
    uv run python -m scripts.run_local_investigator run --assignment-id A
    uv run python -m scripts.run_local_investigator inspect --assignment-id A
"""

import argparse
import asyncio
import dataclasses
import logging
import time
import uuid
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackHandler
from sqlalchemy import select

from scripts.test_tool_executor import emit, fail
from src.config import get_settings
from src.credentials import resolver
from src.db.session import SessionLocal, engine
from src.investigators.checkpoint import open_checkpointer
from src.investigators.llm import NebiusConfigError, load_nebius_settings, local_model
from src.investigators.local_graph import AssignmentNotRunning, run_local_investigator
from src.investigators.refs import ref
from src.mcp_server import McpSettings
from src.mcp_server.auth import MIN_SECRET_CHARS
from src.models import (
    Environment,
    Evidence,
    Finding,
    FindingEvidence,
    Hypothesis,
    HypothesisEvidence,
    InvestigationAssignment,
    ToolCall,
)

SUMMARY_PREVIEW_CHARS = 300


class ModelCallLog(AsyncCallbackHandler):
    """Latency and token usage of each model call, as reported by the provider."""

    def __init__(self) -> None:
        self.started: dict[uuid.UUID, float] = {}
        self.calls: list[dict[str, Any]] = []

    async def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id, **_: Any):
        self.started[run_id] = time.perf_counter()

    async def on_llm_end(self, response: Any, *, run_id, **_: Any) -> None:
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        self.calls.append(
            {
                "latency_ms": round((time.perf_counter() - self.started.pop(run_id)) * 1000),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning"),
                "tool_calls": [c["name"] for c in message.tool_calls],
            }
        )

    async def on_llm_error(self, error: BaseException, *, run_id, **_: Any) -> None:
        started = self.started.pop(run_id, time.perf_counter())
        self.calls.append(
            {
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "error": type(error).__name__,
            }
        )


def _preview(text: str) -> str:
    return text if len(text) <= SUMMARY_PREVIEW_CHARS else text[:SUMMARY_PREVIEW_CHARS] + "…"


async def check(_: argparse.Namespace) -> None:
    report: dict[str, Any] = {}
    try:
        nebius = load_nebius_settings()
        report["nebius"] = {"local_model": nebius.local_model, "api_key_configured": True}
    except NebiusConfigError as exc:
        report["nebius"] = str(exc)
    mcp = McpSettings()
    secret = mcp.token_secret.get_secret_value() if mcp.token_secret else ""
    report["mcp_token_secret"] = (
        "ok"
        if len(secret) >= MIN_SECRET_CHARS
        else f"MCP_TOKEN_SECRET needs {MIN_SECRET_CHARS}+ chars"
    )
    report["mcp_url"] = mcp.url
    try:
        # The private MCP listener (started by the backend) answers 401 without a token.
        async with httpx.AsyncClient(timeout=5) as client:
            probe = await client.post(mcp.url, json={})
        report["mcp_listener"] = "ok" if probe.status_code == 401 else probe.status_code
    except httpx.HTTPError as exc:
        report["mcp_listener"] = (
            f"unreachable ({type(exc).__name__}); start the backend with: uv run poe dev"
        )
    emit(report)


async def records(assignment_id: uuid.UUID) -> dict[str, Any]:
    async with SessionLocal() as s:
        calls = await s.scalars(
            select(ToolCall)
            .where(ToolCall.assignment_id == assignment_id)
            .order_by(ToolCall.created_at, ToolCall.id)
        )
        evidence = await s.scalars(select(Evidence).where(Evidence.assignment_id == assignment_id))
        hypotheses = list(
            await s.scalars(select(Hypothesis).where(Hypothesis.assignment_id == assignment_id))
        )
        findings = list(
            await s.scalars(select(Finding).where(Finding.assignment_id == assignment_id))
        )
        h_links = await s.execute(
            select(HypothesisEvidence).where(
                HypothesisEvidence.hypothesis_id.in_([h.id for h in hypotheses])
            )
        )
        f_links = await s.execute(
            select(FindingEvidence).where(FindingEvidence.finding_id.in_([f.id for f in findings]))
        )
        h_links_by: dict[uuid.UUID, list[str]] = {}
        for (link,) in h_links:
            h_links_by.setdefault(link.hypothesis_id, []).append(
                f"{link.stance.value}:{ref('evidence', link.evidence_id)}"
            )
        f_links_by: dict[uuid.UUID, list[str]] = {}
        for (link,) in f_links:
            f_links_by.setdefault(link.finding_id, []).append(
                f"{link.stance.value}:{ref('evidence', link.evidence_id)}"
            )
        return {
            "tool_calls": [
                {
                    "tool": c.tool_name,
                    "status": c.status,
                    "idempotency_key": c.idempotency_key,
                    "duration_ms": c.duration_ms,
                    "telemetry_available": (c.output_metadata_json or {}).get(
                        "telemetry_available"
                    ),
                    "error": c.error_code,
                    "arguments": c.input_json,
                }
                for c in calls
            ],
            "evidence": [
                {"ref": ref("evidence", e.id), "title": e.title, "summary": _preview(e.summary)}
                for e in evidence
            ],
            "hypotheses": [
                {
                    "ref": ref("hypothesis", h.id),
                    "status": h.status,
                    "confidence": h.confidence,
                    "statement": h.statement,
                    "evidence": h_links_by.get(h.id, []),
                }
                for h in hypotheses
            ],
            "findings": [
                {
                    "status": f.status,
                    "confidence": f.confidence,
                    "title": f.title,
                    "summary": _preview(f.summary),
                    "hypothesis": ref("hypothesis", f.hypothesis_id) if f.hypothesis_id else None,
                    "evidence": f_links_by.get(f.id, []),
                }
                for f in findings
            ],
        }


async def run(args: argparse.Namespace) -> None:
    # The model below uses the process-wide .env key: never for an environment that has
    # credentials of its own (D034).
    async with SessionLocal() as s:
        assignment = await s.get(InvestigationAssignment, args.assignment_id)
        environment = assignment and await s.get(Environment, assignment.environment_id)
        if environment is None:
            fail("assignment not found")
        try:
            source = (await resolver.environment_credentials(s, environment)).source
        except resolver.CredentialRefused as exc:
            fail(f"refused: {exc.code}")
    if source != "process":
        fail("this environment uses its own credentials; run it through the API")
    log = ModelCallLog()
    try:
        model = local_model().model_copy(update={"callbacks": [log]})
    except NebiusConfigError as exc:
        fail(str(exc))
    started = time.perf_counter()
    async with open_checkpointer() as checkpointer:
        try:
            result = await run_local_investigator(args.assignment_id, checkpointer, model=model)
        except AssignmentNotRunning as exc:
            fail(str(exc))
    latencies = [c["latency_ms"] for c in log.calls]
    emit(
        {
            "result": dataclasses.asdict(result),
            "wall_clock_s": round(time.perf_counter() - started, 1),
            "model_calls": log.calls,
            "model_latency_ms": {
                "total": sum(latencies),
                "max": max(latencies, default=0),
                "mean": round(sum(latencies) / len(latencies)) if latencies else 0,
            },
            "records": await records(args.assignment_id),
        }
    )


async def inspect(args: argparse.Namespace) -> None:
    emit(await records(args.assignment_id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="model config, MCP secret and backend reachability")
    for name, text in (("run", "run or resume the investigator"), ("inspect", "show records")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--assignment-id", type=uuid.UUID, required=True)
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    if get_settings().app_env == "production":
        fail("development-only script; refusing to run with APP_ENV=production")
    logging.basicConfig(level=logging.WARNING)
    try:
        await {"check": check, "run": run, "inspect": inspect}[args.command](args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
