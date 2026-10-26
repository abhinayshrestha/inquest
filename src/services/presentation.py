"""Deterministic presentation of a validated answer as response blocks (D023).

Input: a completed turn's validated answer (answer_json) and the recorded evidence of
its investigation. Output: Markdown/code blocks from the answer text, and metric, table
and chart blocks projected from evidence the answer cites. No model is involved, and no
number is invented: values are copied or aggregated from evidence data_json, time
series keep only the points stored with the evidence, and nothing is interpolated,
baselined or correlated. Evidence that cannot carry a block (too few points, one group,
unknown shape) simply produces none; Markdown alone is a complete presentation.

Block choice follows the question: its topics (validation.TOPICS: cpu, memory, errors,
latency) and whether it asks for charts or tables.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.investigators.refs import ref
from src.investigators.validation import mentioned_topics
from src.investigators.windows import parse_range
from src.models import Evidence, FindingEvidence, InvestigationTurn, ToolCall, TurnStatus
from src.schemas.presentation import (
    PRESENTATION_VERSION,
    ChartBlock,
    ChartPoint,
    CodeBlock,
    Column,
    EvidenceBlock,
    EvidenceItem,
    MarkdownBlock,
    MetricBlock,
    MetricItem,
    Presentation,
    Series,
    TableBlock,
    Window,
)

# Trace groups with fewer spans are small samples (same threshold as answer validation).
SPARSE_SPANS = 30
MAX_DATA_BLOCKS = 4
MAX_EVIDENCE_ITEMS = 8
MAX_TABLE_ROWS = 20
MAX_CHART_SERIES = 4
MAX_EVIDENCE_ROWS = 500
SUMMARY_CHARS = 400

CHART_WORDS = re.compile(
    r"\b(chart|charts|graph|graphs|plot|plots|visuali[sz]e|trend|trends|over time|timeline)\b",
    re.IGNORECASE,
)
TABLE_WORDS = re.compile(
    r"\b(table|tables|breakdown|compare|comparison|list|rank)\b", re.IGNORECASE
)
FENCE = re.compile(r"^```([a-z0-9+#-]{0,20})[^\n]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
REF_IN_TEXT = re.compile(r"\b(ev|fd)_([0-9a-f]{8})\b")
AUDIT_LINE = re.compile(r"^\[Inquest\] Validation audit:.*$", re.MULTILINE)


class TurnNotFound(Exception):
    pass


class NotPresentable(Exception):
    """The turn has no validated answer (pending, running, failed or cancelled)."""


# --- evidence access -----------------------------------------------------------------------


def _kind(data: dict[str, Any]) -> str | None:
    """Shape of evidence data_json["data"] (src/integrations/signoz/schemas.py)."""
    if isinstance(data.get("groups"), list):
        return "trace_stats"
    if isinstance(data.get("series"), list):
        return "log_volume" if "total_count" in data else "metric"
    if isinstance(data.get("records"), list):
        return "logs"
    if isinstance(data.get("spans"), list):
        return "spans" if "trace_id" not in data else "trace"
    return None


def _window(evidence: Evidence) -> Window | None:
    parsed = parse_range(evidence.data_json.get("time_range"))
    return Window(start=parsed[0], end=parsed[1]) if parsed else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if value == value and abs(value) != float("inf") else None


async def _cited_evidence(
    session: AsyncSession, turn: InvestigationTurn
) -> list[tuple[Evidence, str | None]]:
    """Evidence the answer cites, in citation order, with its tool name. Every id is
    re-resolved against this investigation's own records: refs from another investigation
    or that match nothing are dropped (the boundary citation inspection uses too)."""
    answer = turn.answer_json or {}
    rows = (
        await session.execute(
            select(Evidence, ToolCall.tool_name)
            .outerjoin(ToolCall, ToolCall.id == Evidence.tool_call_id)
            .where(Evidence.investigation_id == turn.investigation_id)
            .order_by(Evidence.collected_at)
            .limit(MAX_EVIDENCE_ROWS)
        )
    ).all()
    by_id = {e.id: (e, tool) for e, tool in rows}
    by_prefix: dict[str, list[uuid.UUID]] = {}
    for e in by_id:
        by_prefix.setdefault(e.hex[:8], []).append(e)

    links = (
        await session.execute(
            select(FindingEvidence.finding_id, FindingEvidence.evidence_id).where(
                FindingEvidence.investigation_id == turn.investigation_id
            )
        )
    ).all()
    finding_evidence: dict[uuid.UUID, list[uuid.UUID]] = {}
    for finding_id, evidence_id in links:
        finding_evidence.setdefault(finding_id, []).append(evidence_id)

    order: list[uuid.UUID] = []

    def add(evidence_id: uuid.UUID) -> None:
        if evidence_id in by_id and evidence_id not in order:
            order.append(evidence_id)

    def add_finding(finding_id: uuid.UUID) -> None:
        for evidence_id in finding_evidence.get(finding_id, []):
            add(evidence_id)

    for entry in answer.get("refs") or []:
        if not isinstance(entry, dict):
            continue
        try:
            record_id = uuid.UUID(str(entry.get("id")))
        except ValueError:
            continue
        if entry.get("kind") == "evidence":
            add(record_id)
        elif entry.get("kind") == "finding":
            add_finding(record_id)
    # Refs written in the text but missing from refs (e.g. fallback answers): resolved by
    # prefix within this investigation; an ambiguous prefix resolves to nothing.
    for tag, prefix in REF_IN_TEXT.findall(str(answer.get("answer_md") or "")):
        if tag == "ev" and len(by_prefix.get(prefix, [])) == 1:
            add(by_prefix[prefix][0])
        elif tag == "fd":
            matches = {f for f in finding_evidence if f.hex[:8] == prefix}
            if len(matches) == 1:
                add_finding(matches.pop())
    return [by_id[e] for e in order]


# --- projections ---------------------------------------------------------------------------


def _trace_blocks(e: Evidence, window: Window, topics: list[str], wants_chart: bool) -> list:
    data = e.data_json.get("data") or {}
    group_by = "Service" if data.get("group_by") == "service" else "Operation"
    groups = []
    for g in data.get("groups") or []:
        count = g.get("count") if isinstance(g, dict) else None
        if not isinstance(count, int) or count <= 0 or not isinstance(g.get("group"), str):
            continue
        p95, errors = _num(g.get("p95_ms")), g.get("error_count")
        if p95 is None or not isinstance(errors, int):
            continue
        groups.append(g)
    if not groups:
        return []
    evidence_ref = ref("evidence", e.id)
    sparse = [g["group"] for g in groups if g["count"] < SPARSE_SPANS]
    note = (
        f"{len(sparse)} group(s) have fewer than {SPARSE_SPANS} spans; their percentiles are "
        "small-sample estimates."
        if sparse
        else None
    )
    if data.get("has_more"):
        note = ((note + " ") if note else "") + "More groups exist than were recorded."
    blocks: list = []
    total = sum(g["count"] for g in groups)
    total_errors = sum(g["error_count"] for g in groups)
    blocks.append(
        MetricBlock(
            title=f"Trace totals by {group_by.lower()}",
            items=[
                MetricItem(
                    label="Spans",
                    stat="count",
                    value=total,
                    unit="spans",
                    sample_count=total,
                    window=window,
                    evidence_ref=evidence_ref,
                ),
                MetricItem(
                    label="Error spans",
                    stat="count",
                    value=total_errors,
                    unit="spans",
                    sample_count=total,
                    window=window,
                    evidence_ref=evidence_ref,
                ),
                MetricItem(
                    label="Error rate",
                    stat="rate",
                    value=round(100 * total_errors / total, 2),
                    unit="%",
                    sample_count=total,
                    window=window,
                    evidence_ref=evidence_ref,
                ),
            ],
        )  # fmt: skip
    )
    if "latency" in topics or wants_chart or not topics:
        by_p95 = sorted(groups, key=lambda g: g["p95_ms"], reverse=True)[:MAX_TABLE_ROWS]
        if len(by_p95) >= 2:
            blocks.append(
                ChartBlock(
                    kind="bar",
                    title=f"p95 latency by {group_by.lower()}",
                    x_label=group_by,
                    series=[Series(key="p95", label="p95 latency", unit="ms")],
                    points=[
                        ChartPoint(
                            x=g["group"][:120],
                            values={"p95": round(g["p95_ms"], 2)},
                            sample_count=g["count"],
                        )
                        for g in by_p95
                    ],  # fmt: skip
                    evidence_refs=[evidence_ref],
                    window=window,
                    note=note,
                )
            )
    if "errors" in topics and total_errors > 0:
        with_errors = sorted(
            (g for g in groups if g["error_count"] > 0),
            key=lambda g: g["error_count"],
            reverse=True,
        )
        if len(with_errors) >= 2:
            blocks.append(
                ChartBlock(
                    kind="bar",
                    title=f"Error spans by {group_by.lower()}",
                    x_label=group_by,
                    series=[Series(key="errors", label="Error spans", unit="spans")],
                    points=[
                        ChartPoint(
                            x=g["group"][:120],
                            values={"errors": g["error_count"]},
                            sample_count=g["count"],
                        )
                        for g in with_errors[:MAX_TABLE_ROWS]
                    ],  # fmt: skip
                    evidence_refs=[evidence_ref],
                    window=window,
                )
            )
    blocks.append(
        TableBlock(
            title=f"Trace statistics by {group_by.lower()}",
            columns=[
                Column(key="group", label=group_by, kind="text"),
                Column(key="spans", label="Spans", kind="number", unit="spans"),
                Column(key="errors", label="Errors", kind="number", unit="spans"),
                Column(key="error_rate", label="Error rate", kind="number", unit="%"),
                Column(key="p50", label="p50", kind="number", unit="ms"),
                Column(key="p95", label="p95", kind="number", unit="ms"),
                Column(key="p99", label="p99", kind="number", unit="ms"),
            ],
            rows=[
                {
                    "group": g["group"][:200],
                    "spans": g["count"],
                    "errors": g["error_count"],
                    "error_rate": round(100 * g["error_count"] / g["count"], 2),
                    "p50": _num(g.get("p50_ms")),
                    "p95": round(g["p95_ms"], 2),
                    "p99": _num(g.get("p99_ms")),
                }
                for g in sorted(groups, key=lambda g: g["p95_ms"], reverse=True)[:MAX_TABLE_ROWS]
            ],
            evidence_refs=[evidence_ref],
            window=window,
            note=note,
        )
    )
    return blocks


def _metric_title(name: str) -> str:
    """cpu_utilization -> CPU utilization (titles are shown as given)."""
    words = name.replace("_", " ").replace(".", " ").split()
    words = [w.upper() if w in {"cpu", "io", "rss"} else w for w in words]
    text = " ".join(words) or "Metric"
    return text[0].upper() + text[1:]


def _series_label(labels: Any, fallback: str) -> str:
    if not isinstance(labels, dict) or not labels:
        return fallback
    # host.name is the scope, not a distinguishing label, unless it is all there is.
    parts = [f"{v}" for k, v in sorted(labels.items()) if k != "host.name"] or [
        str(v) for v in labels.values()
    ]
    return ", ".join(parts)[:120] or fallback


def _series_blocks(
    e: Evidence, window: Window, title: str, metric_name: str, *, chart: bool, total: Any = None
) -> list:
    data = e.data_json.get("data") or {}
    step = data.get("step_seconds") if isinstance(data.get("step_seconds"), int) else None
    evidence_ref = ref("evidence", e.id)
    series = []
    for s in data.get("series") or []:
        if not isinstance(s, dict) or not isinstance(s.get("points"), list):
            continue
        points = []
        for point in s["points"]:
            if isinstance(point, list | tuple) and len(point) == 2:
                value = _num(point[1])
                if isinstance(point[0], str) and value is not None:
                    points.append((point[0], value))
        if points:
            series.append((s, sorted(points)))
    if not series:
        return []
    blocks: list = []
    items = []
    for s, points in series[:2]:
        label = _series_label(s.get("labels"), metric_name)
        unit = s.get("unit") if isinstance(s.get("unit"), str) and s.get("unit") else None
        for stat in ("mean", "max", "last"):
            value = _num(s.get(stat))
            if value is not None:
                items.append(
                    MetricItem(label=label if len(series) > 1 else metric_name, stat=stat,
                               value=value, unit=unit[:24] if unit else None,
                               sample_count=len(points), step_seconds=step, window=window,
                               evidence_ref=evidence_ref)
                )  # fmt: skip
    if total is not None and _num(total) is not None:
        items.insert(
            0,
            MetricItem(label="Total log records", stat="total", value=float(total),
                       unit="records", sample_count=None, step_seconds=step, window=window,
                       evidence_ref=evidence_ref),
        )  # fmt: skip
    if items:
        blocks.append(MetricBlock(title=title, items=items[:8]))
    drawable = [(s, p) for s, p in series if len(p) >= 2][:MAX_CHART_SERIES]
    if chart and drawable:
        keys = {}
        merged: dict[str, dict[str, float]] = {}
        for i, (s, points) in enumerate(drawable):
            key = f"s{i}"
            keys[key] = s
            for x, value in points:
                merged.setdefault(x, {})[key] = value
        notes = ["Only points recorded with the evidence are drawn; gaps are not filled."]
        if any(len(p) >= 58 for _, p in drawable):
            notes.append("Long ranges are stored as each bucket's minimum and maximum.")
        if len(series) > len(drawable):
            notes.append(f"{len(series) - len(drawable)} more series not shown.")
        units = {
            key: (s.get("unit")[:24] if isinstance(s.get("unit"), str) and s.get("unit") else None)
            for key, s in keys.items()
        }
        blocks.append(
            ChartBlock(
                kind="line",
                title=title,
                x_label="Time (UTC)",
                series=[
                    Series(
                        key=key, label=_series_label(s.get("labels"), metric_name), unit=units[key]
                    )
                    for key, s in keys.items()
                ],  # fmt: skip
                points=[ChartPoint(x=x, values=merged[x]) for x in sorted(merged)][:120],
                evidence_refs=[evidence_ref],
                window=window,
                note=" ".join(notes),
            )
        )
    return blocks


def _data_blocks(
    cited: list[tuple[Evidence, str | None]], question: str
) -> tuple[list, list[tuple[Evidence, str | None]]]:
    topics = mentioned_topics(question)
    wants_chart = bool(CHART_WORDS.search(question))
    wants_table = bool(TABLE_WORDS.search(question))
    groups: list[list] = []
    seen_kinds: set[str] = set()
    for e, _tool in cited:
        data = e.data_json.get("data")
        window = _window(e)
        if not isinstance(data, dict) or window is None:
            continue
        kind = _kind(data)
        try:
            if kind == "trace_stats" and "trace_stats" not in seen_kinds:
                relevant = bool({"latency", "errors"} & set(topics)) or wants_chart or wants_table
                if relevant or not topics:
                    blocks = _trace_blocks(e, window, topics, wants_chart)
                    seen_kinds.add("trace_stats")
                else:
                    blocks = []
            elif kind == "metric":
                name = str(data.get("metric") or "metric")[:120]
                # A CPU question charts the CPU metric, not every metric that was queried.
                about = any(t in name.lower() for t in ("cpu", "memory") if t in topics)
                if about or (wants_chart and not topics):
                    blocks = _series_blocks(e, window, _metric_title(name), name, chart=True)
                elif not topics and not wants_table:
                    blocks = _series_blocks(e, window, _metric_title(name), name, chart=False)
                else:
                    blocks = []
            elif kind == "log_volume":
                relevant = "errors" in topics or wants_chart
                blocks = (
                    _series_blocks(e, window, "Log records per interval", "log records",
                                   chart=True, total=data.get("total_count"))
                    if relevant
                    else []
                )  # fmt: skip
            else:
                blocks = []
        except ValidationError:
            # Evidence whose data does not fit a block is shown as evidence only.
            blocks = []
        if blocks:
            groups.append(blocks)
    # Each cited evidence first contributes its most useful block (a table when tables
    # were asked for, else a chart, else its first block); remaining blocks fill what is
    # left of MAX_DATA_BLOCKS. Output stays grouped per evidence, in citation order, so
    # the answer text remains the focus and related blocks sit together.
    preferred = TableBlock if wants_table else ChartBlock

    def primary(blocks: list) -> int:
        return next((i for i, b in enumerate(blocks) if isinstance(b, preferred)), 0)

    chosen: set[tuple[int, int]] = set()
    for g, blocks in enumerate(groups):
        if len(chosen) < MAX_DATA_BLOCKS:
            chosen.add((g, primary(blocks)))
    for g, blocks in enumerate(groups):
        for i in range(len(blocks)):
            if len(chosen) < MAX_DATA_BLOCKS:
                chosen.add((g, i))
    ordered = [groups[g][i] for g, i in sorted(chosen)]
    return ordered, cited


def _text_blocks(answer_md: str) -> list:
    """The validated answer split around fenced code, so code gets its own block."""
    blocks: list = []
    last = 0
    for match in FENCE.finditer(answer_md):
        before = answer_md[last : match.start()].strip()
        if before:
            blocks.append(MarkdownBlock(markdown=before[:8000]))
        code = match.group(2).rstrip("\n")
        if code.strip():
            blocks.append(CodeBlock(language=match.group(1), code=code[:4000]))
        last = match.end()
    rest = answer_md[last:].strip()
    if rest:
        blocks.append(MarkdownBlock(markdown=rest[:8000]))
    return blocks


def _evidence_block(cited: list[tuple[Evidence, str | None]]) -> EvidenceBlock | None:
    items = []
    for e, _tool in cited[:MAX_EVIDENCE_ITEMS]:
        summary = AUDIT_LINE.sub("", e.summary).strip()
        items.append(
            EvidenceItem(
                ref=ref("evidence", e.id),
                title=e.title[:255],
                evidence_type=e.evidence_type.value,
                summary=summary[:SUMMARY_CHARS],
                window=_window(e),
            )
        )
    return EvidenceBlock(items=items) if items else None


async def build_presentation(session: AsyncSession, turn: InvestigationTurn) -> Presentation:
    answer = turn.answer_json or {}
    answer_md = str(answer.get("answer_md") or "")
    cited = await _cited_evidence(session, turn)
    data, cited = _data_blocks(cited, turn.question)
    blocks: list = _text_blocks(answer_md) + data
    evidence = _evidence_block(cited)
    if evidence:
        blocks.append(evidence)
    if not blocks:
        blocks = [MarkdownBlock(markdown="(No answer text.)")]
    return Presentation(turn_id=turn.id, generated_at=datetime.now(UTC), blocks=blocks[:16])


def _stored(turn: InvestigationTurn) -> Presentation | None:
    if not turn.presentation_json:
        return None
    try:
        stored = Presentation.model_validate(turn.presentation_json)
    except ValidationError:
        return None
    return stored if stored.version == PRESENTATION_VERSION and stored.turn_id == turn.id else None


async def turn_presentation(
    session: AsyncSession, investigation_id: uuid.UUID, turn_id: uuid.UUID
) -> Presentation:
    """The stored presentation of a completed turn, built and stored on first request.
    Rebuilt only when missing, invalid or from an older version; never a model call."""
    turn = await session.scalar(
        select(InvestigationTurn).where(
            InvestigationTurn.id == turn_id, InvestigationTurn.investigation_id == investigation_id
        )
    )
    if turn is None:
        raise TurnNotFound(turn_id)
    if turn.status != TurnStatus.COMPLETED or not turn.answer_json:
        raise NotPresentable(turn_id)
    stored = _stored(turn)
    if stored is not None:
        return stored
    presentation = await build_presentation(session, turn)
    await session.execute(
        update(InvestigationTurn)
        .where(InvestigationTurn.id == turn.id, InvestigationTurn.status == TurnStatus.COMPLETED)
        .values(presentation_json=presentation.model_dump(mode="json"))
    )
    await session.commit()
    return presentation
