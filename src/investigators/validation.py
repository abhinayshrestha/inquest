"""Semantic checks on model output, beyond its schema.

Each check returns the cleaned, resolved result plus the problems found. Problems feed one
repair attempt; whatever is still wrong afterwards is dropped, downgraded or annotated
here, so nothing that reaches the database cites invented evidence, claims more than its
evidence supports, or hides what could not be verified.

Every problem starts with a short code ("[absence_claim] ...") so the repair audit can
record categories without keeping model text.

The text rules (absence claims, sample sizes, names, topics) are deliberately narrow
pattern checks: they catch the failure modes seen in live runs without pretending to
understand prose.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.investigators import policy
from src.investigators.refs import ref, resolve
from src.investigators.schemas import Conclusion, EvidenceLink, HypothesisPlan
from src.models.evidence import MAX_SUMMARY_CHARS

# Which stance a hypothesis status needs at least one of (existing links count too).
STATUS_NEEDS = {"supported": "supports", "weakened": "contradicts", "rejected": "contradicts"}
SUPPORTED_FINDING = ("preliminary", "confirmed")

# "no errors", "zero exceptions", "no application-error logs", "healthy", "error-free"
ABSENCE = re.compile(
    r"\b(?:no|zero|without|absence of|free of)\s+(?:[\w-]+\s+){0,3}?"
    r"(?:errors?|exceptions?|failures?|logs?|issues?|problems?)\b"
    r"|\bhealthy\b|\berror[- ]free\b",
    re.IGNORECASE,
)
# A sentence that says the area is unverified is honest, not an absence claim.
HEDGE = re.compile(
    r"unverified|not verified|could not|cannot|can't|unable|not observable|unknown"
    r"|no (?:usable )?(?:telemetry|data|coverage)|missing|gap"
    r"|not (?:exported|collected|instrumented|available)",
    re.IGNORECASE,
)
SAMPLE_MENTION = re.compile(
    r"\b\d+\s*(?:spans?|requests?|samples?|traces?|calls?|observations?|data points?)\b"
    r"|\bn\s*=\s*\d+",
    re.IGNORECASE,
)
# A latency statistic: percentiles, or a duration figure.
LATENCY_FIGURE = re.compile(
    r"\bp(?:50|75|90|95|99)\b|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds?)\b", re.IGNORECASE
)
# URL paths such as /slow or /api/users/{id}; not units like km/h (preceded by a letter).
PATH = re.compile(r"(?<![\w.:/])/[A-Za-z0-9_][\w\-./{}:]*")
SENTENCE = re.compile(r"(?<=[.;!?\n])\s+")
# Anything that looks like an evidence/hypothesis/finding ref, valid or invented.
REF_LIKE = re.compile(r"\b(?:ev|hy|fd)_[A-Za-z0-9_]+")
PROBLEM_CODE = re.compile(r"^\[(\w+)\]")
NOTE_PREFIX = "[Inquest] "
# Objective topics that must be answered explicitly, and how to spot them.
TOPICS = {
    "cpu": (re.compile(r"\bcpu\b", re.IGNORECASE), re.compile(r"cpu", re.IGNORECASE)),
    "memory": (
        re.compile(r"\bmemory\b|\bram\b|\bswap", re.IGNORECASE),
        re.compile(r"memory", re.IGNORECASE),
    ),
}
TOPIC_LABELS = {"cpu": "Host CPU utilization", "memory": "Host memory utilization"}
SIGNAL_WORDS = {"logs": ("log",), "traces": ("trace", "span")}


@dataclass(frozen=True)
class EvidenceFacts:
    type: str
    # Title and summary, for checking that names in model text were actually observed.
    text: str
    # Span counts: trace_stats group -> count; span searches one entry for the page.
    samples: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Known:
    """What refs may resolve to: evidence of this investigation and environment, and this
    assignment's hypotheses (findings can only reference those, by foreign key)."""

    evidence: dict[str, uuid.UUID | None]
    hypotheses: dict[str, uuid.UUID | None]
    # (hypothesis_id, evidence_id) -> stance already recorded; links are insert-only.
    links: dict[tuple[uuid.UUID, uuid.UUID], str]
    facts: dict[uuid.UUID, EvidenceFacts] = field(default_factory=dict)
    # This assignment's hypotheses and their stored status.
    hypothesis_status: dict[uuid.UUID, str] = field(default_factory=dict)
    # Objective and tool log: names seen there count as observed.
    context_text: str = ""
    # Queries that returned no usable telemetry (empty with no/unknown coverage, failed).
    gaps: list[str] = field(default_factory=list)
    # Questions that could not be pursued because their telemetry is known unavailable.
    unresolved: list[str] = field(default_factory=list)
    # Topics the objective explicitly asks about (keys of TOPICS).
    topics: tuple[str, ...] = ()


def _p(problems: list[str], code: str, message: str) -> None:
    problems.append(f"[{code}] {message}")


def problem_codes(problems: list[str]) -> set[str]:
    return {m.group(1) for p in problems if (m := PROBLEM_CODE.match(p))}


def objective_topics(objective: str) -> tuple[str, ...]:
    return tuple(t for t, (asked, _) in TOPICS.items() if asked.search(objective))


def paths(text: str) -> set[str]:
    return {p.rstrip(".,:;)") for p in PATH.findall(text)}


def validate_plan(
    plan: HypothesisPlan, grounding: str = ""
) -> tuple[list[dict[str, str]], list[str]]:
    """`grounding`: what the model was shown (the brief); names outside it are invented."""
    problems: list[str] = []
    seen: set[str] = set()
    kept: list[dict[str, str]] = []
    for draft in plan.hypotheses:
        statement = " ".join(draft.statement.split())
        key = statement.lower()
        if not statement or key in seen:
            _p(problems, "duplicate_hypothesis", f"duplicate or empty: {statement[:80]!r}")
            continue
        invented = sorted(p for p in paths(statement) if p not in grounding)
        if invented:
            _p(
                problems,
                "invented_name",
                f"hypothesis {statement[:80]!r} names {', '.join(invented)}, which no tool "
                "result or brief mentions; describe it generically (e.g. 'one endpoint')",
            )
            continue
        seen.add(key)
        kept.append({"statement": statement, "test_plan": draft.test_plan.strip()})
    if not kept:
        _p(problems, "no_hypotheses", "at least one hypothesis is required")
    if len(kept) > policy.MAX_HYPOTHESES:
        _p(problems, "too_many", f"at most {policy.MAX_HYPOTHESES} hypotheses; extras dropped")
        kept = kept[: policy.MAX_HYPOTHESES]
    return kept, problems


def _links(
    links: list[EvidenceLink], known: Known, where: str, problems: list[str]
) -> list[dict[str, str]]:
    stances: dict[uuid.UUID, str] = {}
    conflicted: set[uuid.UUID] = set()
    for link in links:
        evidence_id = resolve("evidence", link.evidence_ref, known.evidence)
        if evidence_id is None:
            _p(
                problems,
                "unknown_evidence",
                f"{where}: evidence {link.evidence_ref!r} does not exist; cite only refs shown",
            )
            continue
        if stances.get(evidence_id, link.stance) != link.stance:
            conflicted.add(evidence_id)
        stances[evidence_id] = link.stance
    for evidence_id in conflicted:
        _p(problems, "conflicting_stance", f"{where}: evidence cannot support and contradict")
        del stances[evidence_id]
    return [{"evidence_id": str(e), "stance": s} for e, s in stances.items()]


def _mentioned_samples(facts: EvidenceFacts, text: str) -> dict[str, int]:
    """Span counts for the groups the text talks about (all groups if none is named)."""
    mentioned = {
        group: count
        for group, count in facts.samples.items()
        if group and (group in text or any(p in text for p in paths(group)))
    }
    return mentioned or dict(facts.samples)


def _sample(facts: EvidenceFacts, text: str) -> int | None:
    samples = _mentioned_samples(facts, text)
    return min(samples.values()) if samples else None


def _absence_claims(text: str) -> list[tuple[str, set[str], str]]:
    """(claim, evidence types that could have observed it, sentence) per unhedged claim."""
    claims = []
    for sentence in SENTENCE.split(text):
        match = ABSENCE.search(sentence)
        if match is None or HEDGE.search(sentence):
            continue
        claim = match.group(0)
        if re.search(r"\blogs?\b", claim, re.IGNORECASE):
            types = {"log"}
        elif re.search(r"healthy|error[- ]free", claim, re.IGNORECASE):
            types = {"trace", "log", "metric"}
        else:
            types = {"trace", "log"}
        claims.append((claim, types, sentence))
    return claims


def sanitize_note(note: str, known: Known) -> tuple[str, dict[str, int]]:
    """The agent's free-text summary, made safe to hand to conclude: refs that resolve to
    nothing are removed, and absence or health claims that no cited evidence of a fitting
    type backs are marked. Returns the text and what was changed, by category."""
    counts = {"fabricated_ref": 0, "unsupported_claim": 0}

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        kind = {"ev": "evidence", "hy": "hypothesis", "fd": "finding"}[token[:2]]
        index = known.evidence if kind == "evidence" else known.hypotheses
        if kind != "finding" and resolve(kind, token, index) is not None:
            return token
        counts["fabricated_ref"] += 1
        return "(no such ref)"

    cleaned = REF_LIKE.sub(replace, note)
    sentences = []
    for sentence in SENTENCE.split(cleaned):
        for _, types, _ in _absence_claims(sentence):
            cited = {
                known.facts[e].type
                for r in REF_LIKE.findall(sentence)
                if (e := resolve("evidence", r, known.evidence)) and e in known.facts
            }
            if not cited & types:
                counts["unsupported_claim"] += 1
                sentence = f"[UNSUPPORTED: no cited evidence observed this] {sentence}"
                break
        sentences.append(sentence)
    return " ".join(sentences), counts


def unavailable_key(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """What a query with telemetry_available="no" proves is missing, as signal|scope."""
    if tool_name in ("signoz.log_volume", "signoz.search_logs"):
        identity = arguments.get("identity") or "host"
        return f"logs|{identity}|{arguments.get(identity) or ''}"
    if tool_name in ("signoz.trace_stats", "signoz.search_spans", "signoz.get_trace"):
        return f"traces|service|{arguments.get('service') or ''}"
    if tool_name == "signoz.query_metrics":
        custom = arguments.get("custom")
        name = custom.get("metric_name") if isinstance(custom, dict) else arguments.get("metric")
        return f"metrics|{name}" if name else None
    return None


def is_known_unavailable(gap: str, unavailable: list[str]) -> bool:
    """Whether a requested follow-up only asks again for telemetry already shown absent."""
    text = gap.lower()
    for key in unavailable:
        signal, *rest = key.split("|")
        if signal == "metrics":
            if rest[0].lower() in text:
                return True
            continue
        identity, narrow = rest
        if not any(w in text for w in SIGNAL_WORDS[signal]):
            continue
        other = "host" if identity == "service" else "service"
        if narrow and narrow.lower() in text:
            return True
        if not narrow and identity in text and other not in text:
            return True
    return False


def _hypothesis_updates(
    c: Conclusion, known: Known, problems: list[str]
) -> tuple[list[dict[str, Any]], dict[uuid.UUID, str]]:
    updates: list[dict[str, Any]] = []
    status = dict(known.hypothesis_status)
    stances: dict[uuid.UUID, set[str]] = {h: set() for h in known.hypothesis_status}
    for (h, _), s in known.links.items():
        stances.setdefault(h, set()).add(s)
    for u in c.hypothesis_updates:
        where = f"hypothesis update {u.hypothesis_ref}"
        hypothesis_id = resolve("hypothesis", u.hypothesis_ref, known.hypotheses)
        if hypothesis_id is None:
            _p(problems, "unknown_hypothesis", f"{where}: update only hypotheses shown")
            continue
        if any(x["hypothesis_id"] == str(hypothesis_id) for x in updates):
            _p(problems, "duplicate_update", f"{where}: updated twice; first update kept")
            continue
        links = _links(u.evidence, known, where, problems)
        for link in list(links):
            existing = known.links.get((hypothesis_id, uuid.UUID(link["evidence_id"])))
            if existing is not None and existing != link["stance"]:
                _p(problems, "link_conflict", f"{where}: evidence already linked as {existing}")
                links.remove(link)
        stances.setdefault(hypothesis_id, set()).update(x["stance"] for x in links)
        needed = STATUS_NEEDS.get(u.status)
        if needed and needed not in stances[hypothesis_id]:
            # Unsupported status change: keep the new links, leave the status alone.
            _p(
                problems,
                "status_unsupported",
                f"{where}: status {u.status} needs at least one {needed} link",
            )
            new_status, confidence = None, None
        else:
            new_status, confidence = u.status, u.confidence
            status[hypothesis_id] = u.status
        updates.append(
            {
                "hypothesis_id": str(hypothesis_id),
                "status": new_status,
                "confidence": confidence,
                "evidence": links,
            }
        )

    addressed = {uuid.UUID(u["hypothesis_id"]) for u in updates}
    for hypothesis_id in known.hypothesis_status:
        h = ref("hypothesis", hypothesis_id)
        if hypothesis_id not in addressed:
            _p(
                problems,
                "hypothesis_unaddressed",
                f"hypothesis {h} was not addressed; give it a status (active is fine when "
                "the evidence is insufficient) and link the evidence you relied on",
            )
        if status[hypothesis_id] == "active" and stances.get(hypothesis_id) == {"contradicts"}:
            # Flagged, not forced: only the model can judge whether it is truly ruled out.
            _p(
                problems,
                "active_contradicted",
                f"hypothesis {h} has only contradicting evidence but remains active; weaken "
                "or reject it, or link the evidence that still supports it",
            )
    return updates, status


def _topic_finding(topic: str, known: Known) -> dict[str, Any]:
    """Deterministic answer for an objective topic the model left out: the observation,
    explicitly not assessed. Built only from evidence titles and summaries."""
    _, in_evidence = TOPICS[topic]
    observed = [
        (e, f) for e, f in known.facts.items() if f.type == "metric" and in_evidence.search(f.text)
    ]
    if observed:
        summary = (
            "Observed: "
            + "; ".join(f"{ref('evidence', e)}: {f.text.splitlines()[-1]}" for e, f in observed)
            + ". Whether this contributes to the objective was not assessed."
        )
        links = [{"evidence_id": str(e), "stance": "neutral"} for e, _ in observed]
    else:
        summary = "Not observed: no metric evidence for it was collected in this assignment."
        links = []
    return {
        "title": f"{TOPIC_LABELS[topic]}: not assessed",
        "summary": f"{NOTE_PREFIX}{summary}"[:MAX_SUMMARY_CHARS],
        "status": "inconclusive",
        "confidence": None,
        "hypothesis_id": None,
        "evidence": links,
    }


def validate_conclusion(c: Conclusion, known: Known) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    updates, status = _hypothesis_updates(c, known, problems)
    grounding = known.context_text + "\n" + "\n".join(f.text for f in known.facts.values())

    findings: list[dict[str, Any]] = []
    for i, f in enumerate(c.findings[: policy.MAX_FINDINGS]):
        where = f"finding {i + 1} ({f.title[:60]!r})"
        text = f"{f.title}\n{f.summary}"
        notes: list[str] = []
        hypothesis_id = None
        if f.hypothesis_ref:
            hypothesis_id = resolve("hypothesis", f.hypothesis_ref, known.hypotheses)
            if hypothesis_id is None:
                _p(problems, "unknown_hypothesis", f"{where}: unknown {f.hypothesis_ref!r}")
        links = _links(f.evidence, known, where, problems)
        supporting = [uuid.UUID(x["evidence_id"]) for x in links if x["stance"] == "supports"]
        linked_types = {
            known.facts[uuid.UUID(x["evidence_id"])].type
            for x in links
            if uuid.UUID(x["evidence_id"]) in known.facts
        }
        state, confidence = f.status, f.confidence

        if state in SUPPORTED_FINDING and not supporting:
            _p(problems, "finding_unsupported", f"{where}: {state} needs a supporting ref")
            state, confidence = "inconclusive", None

        invented = sorted(p for p in paths(text) if p not in grounding)
        if invented:
            _p(
                problems,
                "invented_name",
                f"{where}: names {', '.join(invented)}, which appear in no evidence or query",
            )
            notes.append(f"{', '.join(invented)} was not observed in any evidence.")
            state, confidence = "inconclusive", None

        for claim, types, _ in _absence_claims(text):
            if not linked_types & types:
                _p(
                    problems,
                    "absence_claim",
                    f"{where}: claims {claim!r} without linked {'/'.join(sorted(types))} "
                    "evidence that observed it. Empty queries are not evidence; cite evidence "
                    "that shows it (e.g. span error counts) or say the area is unverified",
                )
                notes.append(f"The claim {claim!r} is not backed by evidence.")
                if state == "confirmed":
                    state = "preliminary"

        # Latency figures from trace evidence must come with their span counts.
        traced = [
            (e, _mentioned_samples(known.facts[e], text))
            for e in supporting
            if e in known.facts and known.facts[e].samples
        ]
        if traced and LATENCY_FIGURE.search(text) and not SAMPLE_MENTION.search(text):
            detail = "; ".join(
                f"{ref('evidence', e)}: "
                + ", ".join(f"{g or 'spans'} = {n} spans" for g, n in list(s.items())[:5])
                for e, s in traced
            )
            _p(problems, "sample_size", f"{where}: state the span counts behind it ({detail})")
            notes.append(f"Sample: {detail}.")
        sparse = [e for e, s in traced if s and min(s.values()) < policy.SPARSE_SAMPLE]
        if state == "confirmed" and sparse and len(sparse) == len(supporting):
            _p(problems, "sparse_confirmed", f"{where}: confirmed rests only on sparse samples")
            state = "preliminary"

        if hypothesis_id and state in SUPPORTED_FINDING and supporting:
            h = ref("hypothesis", hypothesis_id)
            resulting = status.get(hypothesis_id)
            if resulting == "active":
                _p(
                    problems,
                    "finding_hypothesis_active",
                    f"{where}: resolves {h} but {h} stays active; mark it supported with the "
                    "same supporting evidence, or drop hypothesis_ref",
                )
            elif resulting in ("weakened", "rejected"):
                _p(
                    problems,
                    "finding_hypothesis_disagree",
                    f"{where}: supports {h}, which is {resulting}; they disagree",
                )
                hypothesis_id = None

        if known.gaps:
            gaps = known.gaps[: policy.MAX_GAPS_NOTED]
            notes.append("Unverified (no usable telemetry): " + "; ".join(gaps) + ".")
        if known.unresolved:
            notes.append(
                "Unresolved (telemetry known unavailable): "
                + "; ".join(known.unresolved[: policy.MAX_GAPS_NOTED])
                + "."
            )
        summary = f.summary.strip()
        if notes:
            note = "\n\n" + "\n".join(NOTE_PREFIX + n for n in notes)
            summary = summary[: MAX_SUMMARY_CHARS - len(note)] + note
        findings.append(
            {
                "title": f.title.strip(),
                "summary": summary[:MAX_SUMMARY_CHARS],
                "status": state,
                "confidence": confidence,
                "hypothesis_id": str(hypothesis_id) if hypothesis_id else None,
                "evidence": links,
            }
        )
    if len(c.findings) > policy.MAX_FINDINGS:
        _p(problems, "too_many", f"at most {policy.MAX_FINDINGS} findings; extras dropped")
    if not findings:
        _p(problems, "no_findings", "at least one finding is required; inconclusive is fine")

    for topic in known.topics:
        asked, in_evidence = TOPICS[topic]
        evidence = {
            e for e, f in known.facts.items() if f.type == "metric" and in_evidence.search(f.text)
        }
        covering = [f for f in findings if asked.search(f"{f['title']}\n{f['summary']}")]
        cites = any(
            uuid.UUID(x["evidence_id"]) in evidence for f in covering for x in f["evidence"]
        )
        if not covering or (evidence and not cites):
            refs = ", ".join(ref("evidence", e) for e in evidence) or "none collected"
            _p(
                problems,
                "topic_unanswered",
                f"the objective asks about {topic}; add a finding on it that cites its metric "
                f"evidence ({refs}) with an honest status, or inconclusive if unverified",
            )
            # The safe fallback, replaced if the repair answers the topic itself.
            findings.append(_topic_finding(topic, known))

    return {
        "verdict": c.verdict,
        "gaps": [g.strip() for g in c.gaps if g.strip()],
        "hypothesis_updates": updates,
        "findings": findings,
    }, problems
