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
from datetime import datetime
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
# Runtime- or dependency-specific mechanisms. Proposing them is speculation unless the
# telemetry already mentions them (live run: JVM GC for a Node.js service).
RUNTIME_TERMS = re.compile(
    r"\b(?:jvm|java|garbage[- ]collect\w*|gc pauses?|safepoints?|heap|\.net|clr|"
    r"goroutines?|go runtime|python gil|thread ?pools?)\b|jvm_\w+",
    re.IGNORECASE,
)


def speculative_terms(text: str, grounding: str) -> list[str]:
    """Runtime-specific terms in `text` that nothing observed mentions."""
    seen = grounding.lower()
    return sorted({m.lower() for m in RUNTIME_TERMS.findall(text) if m.lower() not in seen})


# URL paths such as /slow or /api/users/{id}; not units like km/h (preceded by a letter).
PATH = re.compile(r"(?<![\w.:/])/[A-Za-z0-9_][\w\-./{}:]*")
SENTENCE = re.compile(r"(?<=[.;!?\n])\s+")
# Anything that looks like an evidence/hypothesis/finding ref, valid or invented.
REF_LIKE = re.compile(r"\b(?:ev|hy|fd)_[A-Za-z0-9_]+")
PROBLEM_CODE = re.compile(r"^\[(\w+)\]")
NOTE_PREFIX = "[Inquest] "


@dataclass(frozen=True)
class Topic:
    """A subject an objective can ask about, which then needs its own finding: how text
    mentions it, and which evidence can answer it (types, and words its text must have)."""

    label: str
    mentioned: re.Pattern[str]
    types: tuple[str, ...]
    in_evidence: re.Pattern[str] | None = None
    kind: str = "metric"


# Objective topics that must be answered explicitly. errors and latency were added after
# the live Phase 5 follow-up ("are errors still happening, did /error slow /slow?") was
# concluded with CPU and memory findings only.
TOPICS = {
    "cpu": Topic(
        "Host CPU utilization",
        re.compile(r"\bcpu\b", re.IGNORECASE),
        ("metric",),
        re.compile(r"cpu", re.IGNORECASE),
        "CPU metric",
    ),
    "memory": Topic(
        "Host memory utilization",
        re.compile(r"\bmemory\b|\bram\b|\bswap", re.IGNORECASE),
        ("metric",),
        re.compile(r"memory", re.IGNORECASE),
        "memory metric",
    ),
    "errors": Topic(
        "Application errors",
        re.compile(r"\berrors?\b|\bfailures?\b|\bexceptions?\b", re.IGNORECASE),
        ("trace", "log"),
        kind="trace or log",
    ),
    "latency": Topic(
        "Request latency",
        re.compile(r"\blatenc\w*|\bslow\w*|\bp(?:50|90|95|99)\b|\bduration", re.IGNORECASE),
        ("trace",),
        kind="trace",
    ),
}
SIGNAL_WORDS = {"logs": ("log",), "traces": ("trace", "span")}


@dataclass(frozen=True)
class EvidenceFacts:
    type: str
    # Title and summary, for checking that names in model text were actually observed.
    text: str
    # Span counts: trace_stats group -> count; span searches one entry for the page.
    samples: dict[str, int] = field(default_factory=dict)
    # What the observation can show about time and entities: "aggregate" (one figure per
    # group over the whole window: trace_stats), "series" (values per step: metrics, log
    # volume), "events" (timestamped records: logs), "trace" (spans with trace and parent
    # ids: span search, get_trace).
    granularity: str = "aggregate"
    window: tuple[datetime, datetime] | None = None


@dataclass(frozen=True)
class Known:
    """What refs may resolve to: evidence of this investigation and environment, and this
    assignment's hypotheses (findings can only reference those, by foreign key)."""

    evidence: dict[str, uuid.UUID | None]
    hypotheses: dict[str, uuid.UUID | None]
    # (hypothesis_id, evidence_id) -> stance already recorded; links are insert-only.
    links: dict[tuple[uuid.UUID, uuid.UUID], str]
    facts: dict[uuid.UUID, EvidenceFacts] = field(default_factory=dict)
    # This assignment's hypotheses and their stored status and statement.
    hypothesis_status: dict[uuid.UUID, str] = field(default_factory=dict)
    hypothesis_text: dict[uuid.UUID, str] = field(default_factory=dict)
    # Objective and tool log: names seen there count as observed.
    context_text: str = ""
    # Queries that returned no usable telemetry (empty with no/unknown coverage, failed).
    gaps: list[str] = field(default_factory=list)
    # Questions that could not be pursued because their telemetry is known unavailable.
    unresolved: list[str] = field(default_factory=list)
    # Topics the objective explicitly asks about (keys of TOPICS).
    topics: tuple[str, ...] = ()


def add_problem(problems: list[str], code: str, message: str) -> None:
    problems.append(f"[{code}] {message}")


def problem_codes(problems: list[str]) -> set[str]:
    return {m.group(1) for p in problems if (m := PROBLEM_CODE.match(p))}


def objective_topics(objective: str) -> tuple[str, ...]:
    return tuple(t for t, topic in TOPICS.items() if topic.mentioned.search(objective))


def mentioned_topics(text: str) -> list[str]:
    """Topics a text mentions, in order of first mention."""
    found = [(m.start(), t) for t, topic in TOPICS.items() if (m := topic.mentioned.search(text))]
    return [t for _, t in sorted(found)]


def topic_evidence(topic: str, facts: dict[uuid.UUID, "EvidenceFacts"]) -> set[uuid.UUID]:
    t = TOPICS[topic]
    return {
        e
        for e, f in facts.items()
        if f.type in t.types and (t.in_evidence is None or t.in_evidence.search(f.text))
    }


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
            add_problem(problems, "duplicate_hypothesis", f"duplicate or empty: {statement[:80]!r}")
            continue
        guessed = speculative_terms(statement, grounding)
        if guessed:
            add_problem(
                problems,
                "speculative_runtime",
                f"hypothesis {statement[:80]!r} assumes {', '.join(guessed)}, which no telemetry "
                "shows; propose only mechanisms the observed telemetry can test",
            )
            continue
        invented = sorted(p for p in paths(statement) if p not in grounding)
        if invented:
            add_problem(
                problems,
                "invented_name",
                f"hypothesis {statement[:80]!r} names {', '.join(invented)}, which no tool "
                "result or brief mentions; describe it generically (e.g. 'one endpoint')",
            )
            continue
        seen.add(key)
        kept.append({"statement": statement, "test_plan": draft.test_plan.strip()})
    if not kept:
        add_problem(problems, "no_hypotheses", "at least one hypothesis is required")
    if len(kept) > policy.MAX_HYPOTHESES:
        add_problem(
            problems, "too_many", f"at most {policy.MAX_HYPOTHESES} hypotheses; extras dropped"
        )
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
            add_problem(
                problems,
                "unknown_evidence",
                f"{where}: evidence {link.evidence_ref!r} does not exist; cite only refs shown",
            )
            continue
        if stances.get(evidence_id, link.stance) != link.stance:
            conflicted.add(evidence_id)
        stances[evidence_id] = link.stance
    for evidence_id in conflicted:
        add_problem(
            problems, "conflicting_stance", f"{where}: evidence cannot support and contradict"
        )
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


def absence_claims(text: str) -> list[tuple[str, set[str], str]]:
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


# --- relationship claims --------------------------------------------------------------
#
# Words only detect that a sentence claims a relationship; whether the claim stands is
# decided by the evidence behind it. Ladder, weakest first:
#   observation      separate facts
#   temporal_overlap both happened within overlapping windows (says nothing more)
#   correlation      they vary together: needs time-resolved evidence for both sides,
#                    and trace evidence linking them when they are different endpoints
#   causal           one drives the other: correlation plus trace evidence of the path
CLAIMS = ("observation", "temporal_overlap", "correlation", "causal")
CORRELATION = re.compile(
    r"\b(?:coincid\w*|correlat\w*|co-?occur\w*|at the same times?|in tandem|"
    r"tracks? with|align(?:s|ed)? with|accompan\w*|concurrent\w*|simultaneous\w*|"
    r"associated with|linked to|tied to|spikes? (?:together|with))\b",
    re.IGNORECASE,
)
CAUSAL = re.compile(
    r"\b(?:caus(?:e|es|ed|ing)|because of|due to|leads? to|led to|result(?:s|ed|ing)? in|"
    r"driv(?:e|es|en|ing)|contribut(?:e|es|ed|ing)|responsible for|trigger(?:s|ed|ing)?|"
    r"root cause|explains?)\b",
    re.IGNORECASE,
)
# A negated or hedged sentence states no relationship ("CPU is not contributing",
# "whether they are related is unverified").
UNASSERTED = re.compile(
    r"\b(?:not|no|never|none|unverified|unclear|unknown|uncertain|unconfirmed|cannot|"
    r"whether|may|might|could|possibly|hypothes\w*|if|unrelated|independent\w*)\b",
    re.IGNORECASE,
)
TIME_RESOLVED = {"series", "events", "trace"}


def detected_claim(text: str) -> str:
    """The strongest relationship a text asserts outright."""
    found = "observation"
    for sentence in SENTENCE.split(text):
        if UNASSERTED.search(sentence):
            continue
        if CAUSAL.search(sentence):
            return "causal"
        if CORRELATION.search(sentence):
            found = "correlation"
    return found


def _overlap(a: tuple[datetime, datetime] | None, b: tuple[datetime, datetime] | None) -> bool:
    return a is not None and b is not None and a[0] < b[1] and b[0] < a[1]


def supported_claim(facts: list[EvidenceFacts], text: str) -> tuple[str, str]:
    """The strongest relationship this evidence can carry, and why not more."""
    overlapping = [
        f for f in facts if any(_overlap(f.window, g.window) for g in facts if g is not f)
    ]
    if len(overlapping) < 2:
        return "observation", "no two observations with overlapping windows"
    resolved = [f for f in overlapping if f.granularity in TIME_RESOLVED]
    if len(resolved) < 2:
        kinds = ", ".join(sorted({f.granularity for f in overlapping}))
        return "temporal_overlap", (
            f"only {kinds} evidence: being in the same window is not correlation"
        )
    has_trace = any(f.granularity == "trace" for f in overlapping)
    entities = paths(text)
    if len(entities) >= 2 and not has_trace:
        return "temporal_overlap", (
            f"relates different endpoints ({', '.join(sorted(entities))}) without trace "
            "evidence linking them"
        )
    if not has_trace:
        return "correlation", "no trace evidence of a causal path"
    return "causal", ""


def claim_rank(claim: str) -> int:
    return CLAIMS.index(claim)


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
        for _, types, _ in absence_claims(sentence):
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
) -> tuple[list[dict[str, Any]], dict[uuid.UUID, str], dict[tuple[uuid.UUID, uuid.UUID], str]]:
    """Resolved updates, each hypothesis's resulting status, and every (hypothesis,
    evidence) stance after this conclusion (existing links plus new ones)."""
    updates: list[dict[str, Any]] = []
    status = dict(known.hypothesis_status)
    pairs: dict[tuple[uuid.UUID, uuid.UUID], str] = dict(known.links)
    stances: dict[uuid.UUID, set[str]] = {h: set() for h in known.hypothesis_status}
    for (h, _), s in known.links.items():
        stances.setdefault(h, set()).add(s)
    for u in c.hypothesis_updates:
        where = f"hypothesis update {u.hypothesis_ref}"
        hypothesis_id = resolve("hypothesis", u.hypothesis_ref, known.hypotheses)
        if hypothesis_id is None:
            add_problem(problems, "unknown_hypothesis", f"{where}: update only hypotheses shown")
            continue
        if any(x["hypothesis_id"] == str(hypothesis_id) for x in updates):
            add_problem(problems, "duplicate_update", f"{where}: updated twice; first update kept")
            continue
        links = _links(u.evidence, known, where, problems)
        for link in list(links):
            existing = known.links.get((hypothesis_id, uuid.UUID(link["evidence_id"])))
            if existing is not None and existing != link["stance"]:
                add_problem(
                    problems, "link_conflict", f"{where}: evidence already linked as {existing}"
                )
                links.remove(link)
        stances.setdefault(hypothesis_id, set()).update(x["stance"] for x in links)
        for x in links:
            pairs[(hypothesis_id, uuid.UUID(x["evidence_id"]))] = x["stance"]
        needed = STATUS_NEEDS.get(u.status)
        if needed and needed not in stances[hypothesis_id]:
            # Unsupported status change: keep the new links, leave the status alone.
            add_problem(
                problems,
                "status_unsupported",
                f"{where}: status {u.status} needs at least one {needed} link",
            )
            new_status, confidence = None, None
        else:
            new_status, confidence = u.status, u.confidence
            # Mixed evidence cannot end in certainty either way: supported with a
            # contradicting link, or rejected with a supporting one, is downgraded.
            if new_status == "supported" and "contradicts" in stances[hypothesis_id]:
                add_problem(
                    problems,
                    "mixed_supported",
                    f"{where}: supported, but linked evidence also contradicts it; use active "
                    "or weakened with both links, or remove the link that is wrong",
                )
                new_status, confidence = "active", None
            elif new_status == "rejected" and "supports" in stances[hypothesis_id]:
                add_problem(
                    problems,
                    "mixed_rejected",
                    f"{where}: rejected, but linked evidence also supports it; use weakened, "
                    "or remove the link that is wrong",
                )
                new_status, confidence = "weakened", None
            if new_status == "supported":
                claim = detected_claim(known.hypothesis_text.get(hypothesis_id, ""))
                if claim in ("correlation", "causal"):
                    facts = [
                        known.facts[e]
                        for (h, e), st in pairs.items()
                        if h == hypothesis_id and st == "supports" and e in known.facts
                    ]
                    level, why = supported_claim(
                        facts, known.hypothesis_text.get(hypothesis_id, "")
                    )
                    if claim_rank(claim) > claim_rank(level):
                        add_problem(
                            problems,
                            "relationship_unsupported",
                            f"{where}: supported needs {claim} evidence, but the evidence "
                            f"supports only {level} ({why}); leave it active",
                        )
                        new_status, confidence = "active", None
            status[hypothesis_id] = new_status
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
            add_problem(
                problems,
                "hypothesis_unaddressed",
                f"hypothesis {h} was not addressed; give it a status (active is fine when "
                "the evidence is insufficient) and link the evidence you relied on",
            )
        if status[hypothesis_id] == "active" and stances.get(hypothesis_id) == {"contradicts"}:
            # Flagged, not forced: only the model can judge whether it is truly ruled out.
            add_problem(
                problems,
                "active_contradicted",
                f"hypothesis {h} has only contradicting evidence but remains active; weaken "
                "or reject it, or link the evidence that still supports it",
            )
    return updates, status, pairs


RELATION_STATUS = {"supports": {"supported"}, "contradicts": {"weakened", "rejected"}}
OPPOSITE = {"supports": "contradicts", "contradicts": "supports"}


def _check_relation(
    relation: str | None,
    hypothesis_id: uuid.UUID,
    state: str,
    supporting: list[uuid.UUID],
    status: dict[uuid.UUID, str],
    pairs: dict[tuple[uuid.UUID, uuid.UUID], str],
    where: str,
    problems: list[str],
    conflicts: dict[uuid.UUID, set[uuid.UUID]],
) -> uuid.UUID | None:
    """A finding that references a hypothesis declares how it bears on it; that must
    agree with how the finding's evidence is linked to the hypothesis and with the
    hypothesis's status. Structural agreement, not wording: ruling a cause out (a
    confirmed "CPU is low" finding, relation contradicts) is consistent with rejecting
    "CPU saturation caused it". Returns the hypothesis id to keep, or None to unlink."""
    h = ref("hypothesis", hypothesis_id)
    if relation is None:
        add_problem(
            problems,
            "relation_missing",
            f"{where}: references {h}; set hypothesis_relation (supports, contradicts or "
            "inconclusive)",
        )
        return None
    if relation == "inconclusive":
        return hypothesis_id
    linked = {pairs[(hypothesis_id, e)] for e in supporting if (hypothesis_id, e) in pairs}
    opposite = OPPOSITE[relation]
    if OPPOSITE[relation] in linked:
        add_problem(
            problems,
            "relation_conflict",
            f"{where}: says it {relation} {h}, but its evidence is linked to {h} as {opposite}",
        )
        conflicts.setdefault(hypothesis_id, set()).update(
            e for e in supporting if pairs.get((hypothesis_id, e)) == opposite
        )
        return None
    if state not in ("preliminary", "confirmed"):
        return hypothesis_id
    if relation not in linked:
        add_problem(
            problems,
            "relation_unlinked",
            f"{where}: says it {relation} {h}; link the evidence it relies on to {h} as "
            f"{relation}, or drop hypothesis_ref",
        )
        return None
    resulting = status.get(hypothesis_id)
    if resulting in RELATION_STATUS[relation]:
        return hypothesis_id
    if resulting == "active":
        wanted = " or ".join(sorted(RELATION_STATUS[relation]))
        add_problem(
            problems,
            "finding_hypothesis_active",
            f"{where}: {relation} {h} but {h} stays active; mark it {wanted}, or set the "
            "relation to inconclusive",
        )
        return hypothesis_id
    add_problem(
        problems,
        "relation_conflict",
        f"{where}: says it {relation} {h}, but {h} is {resulting}",
    )
    conflicts.setdefault(hypothesis_id, set()).update(supporting)
    return None


def _topic_finding(topic: str, known: Known) -> dict[str, Any]:
    """Deterministic answer for an objective topic the model left out: the observation,
    explicitly not assessed. Built only from evidence titles and summaries, newest first."""
    t = TOPICS[topic]
    newest = sorted(
        topic_evidence(topic, known.facts),
        key=lambda e: (w[1].timestamp() if (w := known.facts[e].window) else 0.0, e.hex),
        reverse=True,
    )[: policy.TOPIC_FINDING_EVIDENCE]
    observed = [(e, known.facts[e]) for e in newest]
    if observed:
        summary = (
            "Observed: "
            + "; ".join(
                f"{ref('evidence', e)}: {f.text.splitlines()[-1][: policy.TOPIC_FINDING_CHARS]}"
                for e, f in observed
            )
            + ". Whether this answers the objective was not assessed."
        )
        links = [{"evidence_id": str(e), "stance": "neutral"} for e, _ in observed]
    else:
        summary = f"Not observed: no {t.kind} evidence for it was collected in this assignment."
        links = []
    return {
        "title": f"{t.label}: not assessed",
        "summary": f"{NOTE_PREFIX}{summary}"[:MAX_SUMMARY_CHARS],
        "status": "inconclusive",
        "confidence": None,
        "hypothesis_id": None,
        "evidence": links,
    }


def _covers(topic: str, finding: dict[str, Any], evidence: set[uuid.UUID]) -> bool:
    """Whether a finding answers an objective topic. Its main subject (the first topic its
    title names, else its summary) is answered with any link to fitting evidence, or with
    none when none was collected; a topic it only mentions needs a supporting or
    contradicting link to such evidence. So "CPU shows no spike during error bursts",
    linking the error logs as neutral, answers CPU, not errors (live Phase 5, round 2)."""
    subjects = list(
        dict.fromkeys(mentioned_topics(finding["title"]) + mentioned_topics(finding["summary"]))
    )
    if topic not in subjects:
        return False
    fitting = [x for x in finding["evidence"] if uuid.UUID(x["evidence_id"]) in evidence]
    if subjects[0] == topic:
        return bool(fitting) or not evidence
    return any(x["stance"] != "neutral" for x in fitting)


def validate_conclusion(c: Conclusion, known: Known) -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    updates, status, pairs = _hypothesis_updates(c, known, problems)
    # hypothesis -> evidence whose links to it contradict a finding's declared relation.
    conflicts: dict[uuid.UUID, set[uuid.UUID]] = {}
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
                add_problem(
                    problems, "unknown_hypothesis", f"{where}: unknown {f.hypothesis_ref!r}"
                )
        links = _links(f.evidence, known, where, problems)
        supporting = [uuid.UUID(x["evidence_id"]) for x in links if x["stance"] == "supports"]
        linked_types = {
            known.facts[uuid.UUID(x["evidence_id"])].type
            for x in links
            if uuid.UUID(x["evidence_id"]) in known.facts
        }
        state, confidence = f.status, f.confidence

        if state in SUPPORTED_FINDING and not supporting:
            if any(x["stance"] == "contradicts" for x in links):
                # The live mistake: "CPU is low" linked to the CPU metric as contradicts.
                add_problem(
                    problems,
                    "finding_stance_direction",
                    f"{where}: {state} but its evidence is linked as contradicting it. A "
                    "finding's evidence stance is relative to the finding itself: a "
                    "measurement showing CPU is low SUPPORTS the finding 'CPU is low', even "
                    "though it contradicts the hypothesis 'CPU saturation caused it'",
                )
            else:
                add_problem(
                    problems, "finding_unsupported", f"{where}: {state} needs a supporting ref"
                )
            state, confidence = "inconclusive", None

        invented = sorted(p for p in paths(text) if p not in grounding)
        if invented:
            add_problem(
                problems,
                "invented_name",
                f"{where}: names {', '.join(invented)}, which appear in no evidence or query",
            )
            notes.append(f"{', '.join(invented)} was not observed in any evidence.")
            state, confidence = "inconclusive", None

        for claim, types, _ in absence_claims(text):
            if not linked_types & types:
                add_problem(
                    problems,
                    "absence_claim",
                    f"{where}: claims {claim!r} without linked {'/'.join(sorted(types))} "
                    "evidence that observed it. Empty queries are not evidence; cite evidence "
                    "that shows it (e.g. span error counts) or say the area is unverified",
                )
                notes.append(f"The claim {claim!r} is not backed by evidence.")
                if state == "confirmed":
                    state = "preliminary"

        # Relationship claims stand only on evidence that can show them (see CLAIMS).
        detected = detected_claim(text)
        claim = max(f.claim, detected, key=claim_rank)
        if claim_rank(detected) > claim_rank(f.claim):
            add_problem(
                problems,
                "undeclared_relationship",
                f"{where}: states a {detected} relationship but declares claim={f.claim}; "
                "declare it, or state the observations separately",
            )
        if claim != "observation" and state in SUPPORTED_FINDING:
            facts = [known.facts[e] for e in supporting if e in known.facts]
            level, why = supported_claim(facts, text)
            if claim_rank(claim) > claim_rank(level):
                add_problem(
                    problems,
                    "relationship_unsupported",
                    f"{where}: claims {claim}, but the evidence supports only {level} ({why}); "
                    "keep the observations separate and say their relationship is unverified",
                )
                notes.append(
                    f"Relationship unverified: the evidence supports only {level} ({why}). "
                    "The observations stand separately."
                )
                state, confidence = "inconclusive", None

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
            add_problem(
                problems, "sample_size", f"{where}: state the span counts behind it ({detail})"
            )
            notes.append(f"Sample: {detail}.")
        sparse = [e for e, s in traced if s and min(s.values()) < policy.SPARSE_SAMPLE]
        if state == "confirmed" and sparse and len(sparse) == len(supporting):
            add_problem(
                problems, "sparse_confirmed", f"{where}: confirmed rests only on sparse samples"
            )
            state = "preliminary"

        if hypothesis_id:
            hypothesis_id = _check_relation(
                f.hypothesis_relation,
                hypothesis_id,
                state,
                supporting,
                status,
                pairs,
                where,
                problems,
                conflicts,
            )

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
        add_problem(problems, "too_many", f"at most {policy.MAX_FINDINGS} findings; extras dropped")
    if not findings:
        add_problem(
            problems, "no_findings", "at least one finding is required; inconclusive is fine"
        )

    # A finding contradicted its hypothesis's links: which side is wrong is unknown, so
    # neither is kept as fact. The new links involved are dropped and the hypothesis goes
    # back to active (links already stored are insert-only and stay).
    for u in updates:
        evidence = conflicts.get(uuid.UUID(u["hypothesis_id"]))
        if evidence:
            u["evidence"] = [
                x for x in u["evidence"] if uuid.UUID(x["evidence_id"]) not in evidence
            ]
            if u["status"] not in (None, "active"):
                u["status"], u["confidence"] = "active", None

    for topic in known.topics:
        evidence = topic_evidence(topic, known.facts)
        if not any(_covers(topic, f, evidence) for f in findings):
            refs = ", ".join(ref("evidence", e) for e in evidence) or "none collected"
            add_problem(
                problems,
                "topic_unanswered",
                f"the objective asks about {topic}; add a finding mainly about it that cites "
                f"its {TOPICS[topic].kind} evidence ({refs}) with an honest status, or "
                "inconclusive if unverified. A finding about something else that mentions "
                f"{topic} in passing does not answer it",
            )
            # The safe fallback, replaced if the repair answers the topic itself.
            findings.append(_topic_finding(topic, known))

    return {
        "verdict": c.verdict,
        "gaps": [g.strip() for g in c.gaps if g.strip()],
        "hypothesis_updates": updates,
        "findings": findings,
    }, problems
