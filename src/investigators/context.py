"""What the model sees: briefs, compact tool results, catalogs, and the trimmed message view.

Tool results are rendered as short text with refs; the bounded data payload is clipped.
Empty and not-found outcomes say explicitly that they are not evidence of health, and old
windows say they describe the past.
"""

import json
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import AnyMessage, ToolMessage

from src.investigators import policy
from src.investigators.refs import ref
from src.tools.schemas import ToolExecutionResult

# A query window ending longer ago than this is flagged as describing the past.
STALE_WINDOW_MINUTES = 15
TELEMETRY_NOTES = {
    "yes": "telemetry for this scope did arrive in the window; the filter matched nothing",
    "no": "NO telemetry arrived for this scope in the window: a coverage gap, not health",
    "unknown": "telemetry coverage for this scope is unknown",
}
RETRY_HINT = {True: "may succeed if retried later", False: "change the request"}
# Aggregate tools' summaries already carry every figure; only record-returning tools need
# their data shown, which keeps tool results small.
RECORD_TOOLS = frozenset({"signoz.search_logs", "signoz.search_spans", "signoz.get_trace"})
# Arguments that say what a query covered, for coverage-gap labels.
GAP_ARGS = ("identity", "host", "service", "metric", "severity", "body_contains", "operation")


def age_text(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "time unknown"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def _bullets(lines: list[str], empty: str = "(none)") -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else empty


def evidence_lines(catalog: list[dict[str, Any]], now: datetime) -> list[str]:
    return [
        f"{e['ref']} [{e['type']}] {e['title']} (collected {age_text(e['collected_at'], now)}"
        f"{'' if e['own'] else ', earlier assignment'}): {e['summary']}"
        for e in catalog
    ]


def hypothesis_lines(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{h['ref']} ({h['status']}): {h['statement']}" for h in rows]


def brief_text(
    *,
    objective: str,
    question: str,
    environment: dict[str, Any],
    evidence: list[dict[str, Any]],
    prior_hypotheses: list[dict[str, Any]],
    prior_findings: list[dict[str, Any]],
    now: datetime,
) -> str:
    scope = environment["scope"]
    findings = [f"({f['status']}) {f['title']}: {f['summary']}" for f in prior_findings]
    return f"""\
Objective: {objective}
Investigation question: {question}

Environment: {environment["name"]} ({environment["type"]})
Hosts: {", ".join(scope.get("hosts") or []) or "(none)"}
Services: {", ".join(scope.get("services") or []) or "(none)"}
Queries are automatically restricted to these hosts and services.

Current time (UTC): {now.isoformat(timespec="seconds")}

Existing evidence for this environment (reuse it; do not re-query it):
{_bullets(evidence_lines(evidence, now))}

Earlier hypotheses in this environment:
{_bullets(hypothesis_lines(prior_hypotheses))}

Earlier findings in this environment:
{_bullets(findings)}"""


def _window_note(arguments: Any, now: datetime) -> str | None:
    try:
        end = datetime.fromisoformat(arguments["time_range"]["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end.tzinfo is None:
        return None
    minutes = int((now - end).total_seconds() // 60)
    if minutes > STALE_WINDOW_MINUTES:
        return f"window ended {age_text(end, now)}: describes the past, not the current state"
    return None


def render_result(
    result: ToolExecutionResult, arguments: Any, now: datetime | None = None
) -> tuple[str, str]:
    """(model-facing content, one-line digest for compaction and the conclude tool log)."""
    now = now or datetime.now(UTC)
    refs = [ref("evidence", e) for e in result.evidence_ids]
    head = f"tool: {result.tool_name} | status: {result.status}"
    if result.outcome:
        head += f" | outcome: {result.outcome}"
    if result.telemetry_available:
        head += f" | telemetry: {result.telemetry_available}"
    lines = [head]
    if result.replayed:
        lines.append("note: stored result of an earlier identical call; nothing was re-queried")
    if result.error:
        lines.append(
            f"error: {result.error.code}: {result.error.message} "
            f"({RETRY_HINT[result.error.retryable]})"
        )
    if result.outcome in ("empty", "not_found"):
        lines.append(
            "meaning: the query matched nothing. This is NOT evidence of healthy operation; "
            "no evidence was recorded."
        )
        if result.telemetry_available:
            lines.append(f"coverage: {TELEMETRY_NOTES[result.telemetry_available]}")
    if note := _window_note(arguments, now):
        lines.append(f"staleness: {note}")
    if refs:
        lines.append(f"evidence: {', '.join(refs)}")
    if result.summary:
        lines.append(f"summary: {result.summary}")
    if result.warnings:
        lines.append(f"warnings: {'; '.join(result.warnings)}")
    show_data = result.tool_name in RECORD_TOOLS or not result.summary
    if result.outcome == "observations" and result.data is not None and show_data:
        data = json.dumps(result.data, separators=(",", ":"), default=str)
        if len(data) > policy.TOOL_RESULT_CHARS:
            data = data[: policy.TOOL_RESULT_CHARS] + "…(clipped; full data is in the evidence)"
        lines.append(f"data: {data}")
    elif result.truncated:
        lines.append("note: result was truncated by the executor")

    digest = f"{result.tool_name} {result.status}"
    if result.outcome:
        digest += f"/{result.outcome}"
    if result.telemetry_available:
        digest += f" telemetry={result.telemetry_available}"
    if result.error:
        digest += f" error={result.error.code}"
    if refs:
        digest += f" evidence={','.join(refs)}"
    if result.summary:
        digest += f": {result.summary[:200]}"
    return "\n".join(lines), digest


def coverage_gap(result: ToolExecutionResult, arguments: Any) -> str | None:
    """A query that produced no usable telemetry, worded for a finding's caveats. Empty
    with coverage "yes" is not a gap: telemetry arrived and nothing matched."""
    if result.status in ("failed", "timed_out"):
        reason = f"query failed ({result.error.code if result.error else result.status})"
    elif result.outcome in ("empty", "not_found") and result.telemetry_available != "yes":
        reason = (
            "no telemetry arrived for this scope"
            if result.telemetry_available == "no"
            else "telemetry coverage unknown"
        )
    else:
        return None
    args = arguments if isinstance(arguments, dict) else {}
    parts = [f"{k}={args[k]}" for k in GAP_ARGS if args.get(k)]
    if isinstance(args.get("custom"), dict) and args["custom"].get("metric_name"):
        parts.append(f"metric={args['custom']['metric_name']}")
    label = f"{result.tool_name.removeprefix('signoz.')}({', '.join(parts)})"
    return f"{label}: {reason}"


def model_view(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Older tool results become their digest so the prompt stays bounded; the full text
    remains in the checkpoint and the evidence remains in PostgreSQL."""
    tool_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    keep = set(tool_positions[-policy.FULL_TOOL_RESULTS_KEPT :])
    view: list[AnyMessage] = []
    for i, message in enumerate(messages):
        artifact = message.artifact if isinstance(message, ToolMessage) else None
        digest = (artifact or {}).get("digest")
        if i in tool_positions and i not in keep and digest:
            message = message.model_copy(update={"content": f"[earlier result] {digest}"})
        view.append(message)
    return view
