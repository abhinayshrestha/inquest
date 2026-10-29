"""Semantic checks on the Main Investigator's output.

validate_verdict turns a model verdict into an executable decision: environments
resolved against the investigation's scope, empty memory always investigated, the
round budget enforced. validate_answer checks the answer and report against memory:
every ref resolves within the investigation, confirmed conclusions need direct evidence
or a confirmed finding, root_cause_identified needs a confirmed causal conclusion, and
absence or invented names are not claimed. Like the Local checks, problems carry codes,
feed one repair, and anything still wrong is dropped or downgraded here. fallback_answer
is what a turn gets when no model answer survives: validated findings only.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.investigators import policy
from src.investigators.refs import ref, ref_index, resolve
from src.investigators.schemas import (
    AnswerDraft,
    ReportConclusion,
    ReportDraft,
    Verdict,
)
from src.investigators.validation import (
    NOTE_PREFIX,
    REF_LIKE,
    SENTENCE,
    UNASSERTED,
    EvidenceFacts,
    absence_claims,
    add_problem,
    claim_rank,
    detected_claim,
    paths,
    problem_codes,
    supported_claim,
)
from src.investigators.windows import Window
from src.services.turns import HEADING_LINE

KINDS = ("evidence", "finding", "hypothesis")
# Strength of a conclusion's support: direct evidence or a confirmed finding is strong.
STRONG_FINDING = {"confirmed"}
FALLBACK_ORDER = {"confirmed": 0, "preliminary": 1, "inconclusive": 2}
SUPPORTING_FINDING = {"confirmed", "preliminary"}
# An outcome names a cause, so it needs a causal conclusion with this status, one that
# survived the relationship checks. Confirmed symptoms, error counts, low CPU or memory and
# same-window overlap are observations, not causes (live Phase 5 report v1).
OUTCOME_NEEDS = {
    "root_cause_identified": {"confirmed"},
    "probable_cause": {"confirmed", "probable"},
}


@dataclass(frozen=True)
class Memory:
    """What the Main Investigator may cite: the whole investigation, every environment."""

    index: dict[str, dict[str, uuid.UUID | None]]
    evidence_type: dict[uuid.UUID, str] = field(default_factory=dict)
    # Granularity and window per evidence, for relationship claims.
    evidence_facts: dict[uuid.UUID, EvidenceFacts] = field(default_factory=dict)
    finding_status: dict[uuid.UUID, str] = field(default_factory=dict)
    # Evidence each finding is supported by.
    finding_support: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)
    # Evidence types each finding links to, for absence claims backed via findings.
    finding_types: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    environments: dict[str, uuid.UUID] = field(default_factory=dict)
    grounding: str = ""

    @property
    def empty(self) -> bool:
        return not self.evidence_type and not self.finding_status


def build_memory(memory: dict[str, Any]) -> Memory:
    evidence_type = {e["id"]: e["type"] for e in memory["evidence"]}
    return Memory(
        index={
            "evidence": ref_index("evidence", [e["id"] for e in memory["evidence"]]),
            "finding": ref_index("finding", [f["id"] for f in memory["findings"]]),
            "hypothesis": ref_index("hypothesis", [h["id"] for h in memory["hypotheses"]]),
        },
        evidence_type=evidence_type,
        evidence_facts={
            e["id"]: EvidenceFacts(
                type=e["type"],
                text="",
                granularity=e.get("granularity") or "aggregate",
                window=e.get("window"),
            )
            for e in memory["evidence"]
        },
        finding_status={f["id"]: f["status"] for f in memory["findings"]},
        finding_support={
            f["id"]: [e for e, stance in f["evidence"] if stance == "supports"]
            for f in memory["findings"]
        },
        finding_types={
            f["id"]: {evidence_type[e] for e, _ in f["evidence"] if e in evidence_type}
            for f in memory["findings"]
        },
        environments={e["name"].lower(): e["id"] for e in memory["environments"]},
        grounding="\n".join(
            [e["title"] + "\n" + e["summary"] for e in memory["evidence"]]
            + [f["title"] + "\n" + f["summary"] for f in memory["findings"]]
            + [h["statement"] for h in memory["hypotheses"]]
            + [a["objective"] for a in memory["assignments"]]
        ),
    )


def _resolve_any(text: str, memory: Memory) -> tuple[str, uuid.UUID] | None:
    for kind in KINDS:
        found = resolve(kind, text, memory.index[kind])
        if found is not None:
            return kind, found
    return None


# --- verdict -------------------------------------------------------------------------------


def validate_verdict(
    v: Verdict,
    memory: Memory,
    *,
    question: str,
    rounds: int,
    window: Window | None = None,
    covered: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """`window`: the time window the question states, if any; `covered`: whether some
    evidence spans it. Freshness is enforced before this turn dispatches anything; after
    a round the model's judgement stands, so a mismatch cannot loop rounds."""
    problems: list[str] = []
    decision = v.decision
    assignments: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for a in v.assignments:
        environment_id = memory.environments.get(a.environment.strip().lower())
        if environment_id is None:
            add_problem(
                problems, "unknown_environment", f"environment {a.environment!r} is not in scope"
            )
            continue
        if environment_id in seen:
            add_problem(
                problems, "duplicate_assignment", f"{a.environment!r} assigned twice; first kept"
            )
            continue
        seen.add(environment_id)
        assignments.append(
            {"environment_id": str(environment_id), "objective": a.objective.strip()}
        )
    assignments = assignments[: policy.MAX_ENVIRONMENTS_PER_ROUND]

    def everywhere() -> list[dict[str, Any]]:
        objective = question.strip()
        if v.gaps:
            objective += "\nFocus: " + "; ".join(g.strip() for g in v.gaps if g.strip())
        return [
            {"environment_id": str(e), "objective": objective[:2000]}
            for e in list(memory.environments.values())[: policy.MAX_ENVIRONMENTS_PER_ROUND]
        ]

    if decision != "investigate" and memory.empty and rounds == 0:
        # Nothing is known yet: answering or asking back would be a guess.
        add_problem(problems, "no_memory", "memory is empty; investigate before answering")
        decision, assignments = "investigate", assignments or everywhere()
    if decision == "answer" and window and not covered and rounds == 0:
        add_problem(
            problems,
            "stale_memory",
            f"the question asks about {window.text()}, which no evidence covers; investigate",
        )
        decision, assignments = "investigate", assignments or everywhere()
    if decision == "investigate" and not assignments:
        add_problem(
            problems, "no_assignments", "investigate needs at least one assignment in scope"
        )
        assignments = everywhere()
    limited = False
    if decision == "investigate" and rounds >= policy.MAIN_MAX_ROUNDS:
        # Budget, not a model error: answer with what exists.
        decision, assignments, limited = "answer", [], True
    if decision != "investigate":
        assignments = []
    if window:
        # The question's window travels with the objective, so Local queries it. It is
        # authoritative: the model restating it differently (live Phase 5: "last 10
        # minutes" for a computed 15-minute "now") must not narrow what Local queries.
        line = (
            f"\nTime window (UTC): {window.text()}. Query this whole window; any other "
            "range above is only a focus inside it."
        )
        for a in assignments:
            a["objective"] = a["objective"][: 2000 - len(line)] + line
    return {
        "decision": decision,
        "rationale": v.rationale.strip(),
        "gaps": [g.strip() for g in v.gaps if g.strip()],
        "assignments": assignments,
        "limited": limited,
    }, problems


# --- answer and report ---------------------------------------------------------------------


def _clean_text(text: str, memory: Memory, where: str, problems: list[str]) -> tuple[str, list]:
    cited: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        found = _resolve_any(token, memory)
        if found is None:
            add_problem(problems, "unknown_ref", f"{where}: {token!r} is not in the memory")
            return "(no such ref)"
        kind, record_id = found
        entry = {"ref": ref(kind, record_id), "kind": kind, "id": str(record_id)}
        if entry not in cited:
            cited.append(entry)
        return entry["ref"]

    return REF_LIKE.sub(replace, text), cited


def _refs(raw: list[str], memory: Memory, where: str, problems: list[str]) -> list[dict]:
    out: list[dict[str, str]] = []
    for token in raw:
        found = _resolve_any(token, memory)
        if found is None:
            add_problem(problems, "unknown_ref", f"{where}: {token!r} is not in the memory")
            continue
        kind, record_id = found
        if kind == "hypothesis":
            add_problem(
                problems,
                "hypothesis_as_support",
                f"{where}: cite evidence or findings, not {token}",
            )
            continue
        entry = {"ref": ref(kind, record_id), "kind": kind, "id": str(record_id)}
        if entry not in out:
            out.append(entry)
    return out


def _strength(refs: list[dict[str, str]], memory: Memory) -> str:
    """strong: direct evidence or a confirmed finding; weak: only preliminary findings."""
    best = "none"
    for r in refs:
        record_id = uuid.UUID(r["id"])
        if r["kind"] == "evidence" or memory.finding_status.get(record_id) in STRONG_FINDING:
            return "strong"
        if memory.finding_status.get(record_id) in SUPPORTING_FINDING:
            best = "weak"
    return best


def _types(refs: list[dict[str, str]], memory: Memory) -> set[str]:
    types: set[str] = set()
    for r in refs:
        record_id = uuid.UUID(r["id"])
        if r["kind"] == "evidence":
            types.add(memory.evidence_type.get(record_id, ""))
        else:
            types |= memory.finding_types.get(record_id, set())
    return types


def _relationship_facts(refs: list[dict[str, str]], memory: Memory) -> list[EvidenceFacts]:
    """Evidence behind refs: cited evidence, and the supporting evidence of cited findings."""
    ids: list[uuid.UUID] = []
    for r in refs:
        record_id = uuid.UUID(r["id"])
        if r["kind"] == "evidence":
            ids.append(record_id)
        else:
            ids += memory.finding_support.get(record_id, [])
    return [memory.evidence_facts[e] for e in dict.fromkeys(ids) if e in memory.evidence_facts]


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:8])[:64] or "conclusion"


def _conclusion(c: ReportConclusion, i: int, memory: Memory, problems: list[str]) -> dict[str, Any]:
    where = f"conclusion {i + 1} ({c.statement[:60]!r})"
    supporting = _refs(c.supporting, memory, where, problems)
    contradicting = _refs(c.contradicting, memory, where, problems)
    status = c.status
    strength = _strength(supporting, memory)
    if status == "confirmed":
        if strength != "strong":
            add_problem(
                problems,
                "unsupported_confirmed",
                f"{where}: confirmed needs direct evidence or a confirmed finding",
            )
            status = "probable" if strength == "weak" else "unverified"
        elif contradicting:
            add_problem(problems, "confirmed_contradicted", f"{where}: confirmed but contradicted")
            status = "probable"
    if status == "probable" and strength == "none":
        add_problem(problems, "unsupported_probable", f"{where}: probable needs supporting refs")
        status = "unverified"
    if status == "ruled_out" and not contradicting:
        add_problem(
            problems, "unsupported_ruled_out", f"{where}: ruled_out needs a contradicting ref"
        )
        status = "unverified"
    invented = sorted(p for p in paths(c.statement) if p not in memory.grounding)
    if invented:
        add_problem(
            problems, "invented_name", f"{where}: names {', '.join(invented)}, not in memory"
        )
        status = "unverified"
    detected = detected_claim(c.statement)
    claim = max(c.claim, detected, key=claim_rank)
    if claim_rank(detected) > claim_rank(c.claim):
        add_problem(
            problems,
            "undeclared_relationship",
            f"{where}: states a {detected} relationship but declares claim={c.claim}",
        )
    if claim != "observation" and status in ("confirmed", "probable"):
        level, why = supported_claim(_relationship_facts(supporting, memory), c.statement)
        if claim_rank(claim) > claim_rank(level):
            add_problem(
                problems,
                "relationship_unsupported",
                f"{where}: claims {claim}, but the evidence supports only {level} ({why}); "
                "state the observations separately and the relationship as unverified",
            )
            # A causal reading of real correlation is a hypothesis: probable at most.
            status = "probable" if claim == "causal" and level == "correlation" else "unverified"
    for absent, types, _ in absence_claims(c.statement):
        if not _types(supporting, memory) & types:
            add_problem(
                problems,
                "absence_claim",
                f"{where}: claims {absent!r} without evidence that saw it",
            )
            if status in ("confirmed", "probable"):
                status = "unverified"
    return {
        "key": slug(c.key) if c.key else slug(c.statement),
        # A derived key may be replaced by the matching key of the previous report.
        "key_derived": not c.key,
        "claim": claim,
        "statement": c.statement.strip(),
        "status": status,
        "confidence": c.confidence if status != "unverified" else "low",
        "supporting": supporting,
        "contradicting": contradicting,
    }


def _answer_relationships(answer_md: str, memory: Memory, problems: list[str]) -> str:
    """A sentence of the answer that asserts correlation or causation must cite evidence
    that can show it; otherwise the relationship is marked unverified (kept, not erased,
    so the separate observations in it still read correctly)."""
    unsupported: list[str] = []
    for sentence in SENTENCE.split(answer_md):
        if UNASSERTED.search(sentence):
            continue
        claim = detected_claim(sentence)
        if claim == "observation":
            continue
        refs = []
        for token in REF_LIKE.findall(sentence):
            found = _resolve_any(token, memory)
            if found and found[0] != "hypothesis":
                refs.append({"kind": found[0], "id": str(found[1])})
        level, why = supported_claim(_relationship_facts(refs, memory), sentence)
        if claim_rank(claim) > claim_rank(level):
            add_problem(
                problems,
                "answer_relationship",
                f"answer: {sentence.strip()[:120]!r} asserts {claim}, but its evidence supports "
                f"only {level} ({why}); state the observations separately",
            )
            unsupported.append(sentence.strip()[:160])
    if unsupported:
        answer_md += (
            "\n\n[Inquest] Not established by evidence (the observations stand separately; "
            "their relationship is unverified): " + " | ".join(unsupported)
        )
    return answer_md


def substantive(answer_md: str) -> bool:
    """Prose beyond headings, refs and Inquest's own notes: a heading or a list of refs is
    not an answer."""
    for line in answer_md.splitlines():
        if HEADING_LINE.match(line) or line.strip().startswith(NOTE_PREFIX):
            continue
        if re.search(r"[A-Za-z]{2,}", REF_LIKE.sub(" ", line)):
            return True
    return False


# Through function calling, Ultra sometimes returns the whole markdown answer on one line,
# with runs of spaces where its line breaks were (live run 2026-10-29). A leading heading
# then swallows the answer: "no prose", and one giant heading in the UI.
LOST_BREAKS = re.compile(r" {2,}")


def restore_line_breaks(answer_md: str) -> str:
    """Only a single-line answer: multi-line markdown is kept exactly as written."""
    if "\n" in answer_md.strip():
        return answer_md
    return LOST_BREAKS.sub("\n\n", answer_md.strip())


# Problems that leave the answer itself unusable: it is replaced by fallback_answer.
ANSWER_BLOCKING = {"uncited_answer", "unusable_answer"}


def validate_answer(
    draft: AnswerDraft, memory: Memory, *, report_required: bool, clarify: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """The cleaned answer also carries `report_problems`: the subset about the report, so a
    follow-up can refuse a revision that is still wrong without discarding the answer."""
    problems: list[str] = []
    answer_md, cited = _clean_text(restore_line_breaks(draft.answer_md), memory, "answer", problems)
    answer_md = _answer_relationships(answer_md, memory, problems)
    if not substantive(answer_md):
        add_problem(
            problems,
            "unusable_answer",
            "the answer has no prose (only headings or refs); write the answer itself",
        )
    if not cited and not memory.empty and not clarify:
        add_problem(problems, "uncited_answer", "the answer cites no evidence or finding refs")

    report_problems: list[str] = []
    report: dict[str, Any] | None = None
    if draft.report is None:
        if report_required:
            add_problem(report_problems, "report_required", "the initial question needs a report")
            report = {
                "outcome": "inconclusive",
                "summary": answer_md[:4000],
                "conclusions": [],
                "timeline": [],
                "gaps": ["No structured report was produced."],
                "next_checks": [],
            }
    else:
        r = draft.report
        rp = report_problems
        summary, _ = _clean_text(r.summary, memory, "report summary", rp)
        conclusions = [_conclusion(c, i, memory, rp) for i, c in enumerate(r.conclusions)]
        outcome = r.outcome
        needed = OUTCOME_NEEDS.get(outcome)
        causal = {c["status"] for c in conclusions if c["claim"] == "causal"}
        if needed and not causal & needed:
            add_problem(
                rp,
                "unsupported_outcome",
                f"outcome {outcome} needs a {' or '.join(sorted(needed))} causal conclusion "
                "supported by evidence; confirmed symptoms, error counts, low CPU or memory "
                "and same-window overlap are observations, not causes",
            )
            outcome = (
                "probable_cause" if causal & OUTCOME_NEEDS["probable_cause"] else "inconclusive"
            )
        timeline = []
        for i, t in enumerate(r.timeline):
            refs = _refs(t.refs, memory, f"timeline {i + 1}", rp)
            event, _ = _clean_text(t.event, memory, f"timeline {i + 1}", rp)
            timeline.append({"at": t.at.strip(), "event": event, "refs": refs})
        changes = [
            {
                "change": ch.change,
                "statement": ch.statement.strip(),
                "reason": ch.reason.strip(),
                "refs": _refs(ch.refs, memory, f"change {i + 1}", rp),
            }
            for i, ch in enumerate(r.changes)
        ]
        report = {
            "outcome": outcome,
            "summary": summary,
            "conclusions": conclusions,
            "timeline": timeline,
            "gaps": [g.strip() for g in r.gaps if g.strip()],
            "next_checks": [n.strip() for n in r.next_checks if n.strip()],
            "changes": changes,
        }
    return {
        "answer_md": answer_md,
        "refs": cited,
        "report": report,
        "report_problems": report_problems,
    }, problems + report_problems


def _window_text(start: datetime, end: datetime) -> str:
    return f"{start:%Y-%m-%dT%H:%M}Z to {end:%Y-%m-%dT%H:%M}Z"


def fallback_answer(
    raw: dict[str, Any],
    memory: Memory,
    *,
    turn_id: uuid.UUID,
    unresolved: list[str],
    initial: bool,
    report_version: int | None,
) -> tuple[dict[str, Any], list[str]] | None:
    """A conservative answer for a turn whose model answer failed validation after its
    repair: the validated findings (this turn's, else the investigation's) that link
    evidence, each with its status, refs and observation window, and nothing else; no
    synthesis, no relationship. An initial turn also gets an inconclusive report built
    from the same findings. The result passes through validate_answer like any answer.
    None when no finding with evidence exists: then the turn fails instead."""
    findings = [f for f in raw["findings"] if f.get("turn_id") == turn_id]
    scope = "this turn's investigation"
    if not any(f["evidence"] for f in findings):
        findings, scope = raw["findings"], "this investigation"
    findings = sorted(
        (f for f in findings if f["evidence"]), key=lambda f: FALLBACK_ORDER.get(f["status"], 9)
    )[: policy.FALLBACK_FINDINGS]
    if not findings:
        return None
    windows = {e["id"]: e["window"] for e in raw["evidence"] if e.get("window")}
    lines = []
    for f in findings:
        evidence = list(dict.fromkeys(e for e, _ in f["evidence"]))
        spans = [windows[e] for e in evidence if e in windows]
        when = (
            f" (observed {_window_text(min(s for s, _ in spans), max(e for _, e in spans))})"
            if spans
            else ""
        )
        lines.append(
            f"- {ref('finding', f['id'])} ({f['status']}): {' '.join(f['title'].split())}. "
            f"Evidence: {', '.join(ref('evidence', e) for e in evidence)}{when}."
        )
    codes = ", ".join(sorted(problem_codes(unresolved))) or "none recorded"
    if initial:
        tail = "The report lists the same findings; its outcome is inconclusive."
    elif report_version is not None:
        tail = f"The current report (v{report_version}) stands unchanged."
    else:
        tail = "No report has been written yet."
    answer_md = (
        f"**Inquest fallback answer.** The model's answer could not be validated "
        f"(unresolved: {codes}), so this lists only the validated findings from {scope} "
        "with their evidence, without synthesis. It does not establish any relationship "
        "between them, and it may not address every part of the question.\n\n"
        + "\n".join(lines)
        + f"\n\n{tail}"
    )
    report = None
    if initial:
        status = {"confirmed": "confirmed", "preliminary": "probable"}
        report = ReportDraft(
            outcome="inconclusive",
            summary="Fallback report: the model's report could not be validated. Validated "
            "findings are listed as observations; no cause is identified.",
            conclusions=[
                ReportConclusion(
                    key=slug(f["title"]),
                    claim="observation",
                    statement=" ".join(f["title"].split())[:1000],
                    status=status[f["status"]],
                    confidence="medium" if f["status"] == "confirmed" else "low",
                    supporting=[ref("finding", f["id"])],
                    contradicting=[],
                )
                for f in findings
                if f["status"] in status
            ],
            timeline=[],
            gaps=[
                f"The model's answer failed validation ({codes}); nothing beyond the "
                "listed findings was assessed."
            ],  # fmt: skip
            next_checks=[],
        )
    clean, problems = validate_answer(
        AnswerDraft(answer_md=answer_md[:8000], report=report),
        memory,
        report_required=initial,
    )
    if not clean["refs"] or problem_codes(problems) & ANSWER_BLOCKING:
        return None
    return clean | {"fallback": True}, problems


def conclusion_as_of(c: dict[str, Any], memory: Memory, default: str) -> str:
    """When a conclusion was observed: the latest end of its evidence windows (cited
    directly or through findings). A follow-up that restates an old observation keeps the
    old time, so stale evidence is never dated as current."""
    ends = [
        f.window[1]
        for f in _relationship_facts(c.get("supporting", []), memory)
        if f.window is not None
    ]
    return max(ends).isoformat(timespec="minutes") if ends else default


# --- report revisions ----------------------------------------------------------------------

# Conclusions that carry a claim; adding an unverified one changes nothing.
CLAIMS = {"confirmed", "probable", "ruled_out"}
HISTORY_MAX = 20
# Words that identify what a conclusion is about, for matching across versions when the
# model did not reuse a key.
TOPIC = re.compile(r"\b(cpu|memory|errors?|latency|slow|logs?|traces?|disk|network)\b", re.I)


def _key_of(c: dict[str, Any]) -> str:
    return c.get("key") or slug(c["statement"])


def _signature(statement: str) -> frozenset[str]:
    topics = {t.lower().rstrip("s") for t in TOPIC.findall(statement)}
    return frozenset(paths(statement) | topics)


def _match(c: dict[str, Any], previous: list[dict[str, Any]]) -> str | None:
    """The previous conclusion this one continues: same key, same wording, or the only one
    about the same endpoints and topics. Deterministic; None when ambiguous."""
    if not c.get("key_derived"):
        return c["key"]
    norm = " ".join(c["statement"].lower().split())
    for p in previous:
        if " ".join(p["statement"].lower().split()) == norm:
            return _key_of(p)
    sig = _signature(c["statement"])
    same = [p for p in previous if sig and _signature(p["statement"]) == sig]
    return _key_of(same[0]) if len(same) == 1 else None


def merge_report(
    new: dict[str, Any], current: dict[str, Any] | None, *, as_of: str
) -> dict[str, Any]:
    """The next report: the new conclusions, marked current and dated, with keys continued
    from the previous version; previous conclusions not restated move to `history`, marked
    historical with the version and time they were current. Old observations stay
    available without being presented as the current state."""
    previous = (current or {}).get("conclusions", [])
    conclusions: list[dict[str, Any]] = []
    used: set[str] = set()
    for c in new.get("conclusions", []):
        c = {k: v for k, v in c.items() if k != "key_derived"} | {
            "key": _match(c, previous) or c["key"],
            "scope": "current",
            "as_of": c.get("as_of") or as_of,
        }
        while c["key"] in used:
            c["key"] += "-2"
        used.add(c["key"])
        conclusions.append(c)
    retracted = {
        slug(ch["key"]) if ch.get("key") else slug(ch["statement"])
        for ch in new.get("changes", [])
        if ch["change"] == "retracted"
    }
    history = [
        {
            "key": _key_of(p),
            "statement": p["statement"],
            "status": "retracted" if _key_of(p) in retracted else p["status"],
            "supporting": p.get("supporting", []),
            "as_of": p.get("as_of"),
            "from_version": (current or {}).get("version"),
            "scope": "historical",
        }
        for p in previous
        if _key_of(p) not in used
    ]
    seen = used | {h["key"] for h in history}
    history += [h for h in (current or {}).get("history", []) if h["key"] not in seen]
    return {**new, "conclusions": conclusions, "history": history[:HISTORY_MAX]}


def report_diff(new: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    """Whether the next report (already merged) materially changes the current one, by
    conclusion key. Material: a different outcome, a status change, a claim-carrying
    conclusion added, or an explicit retraction. A conclusion that is merely not
    restated moves to history; that alone is not a new version."""
    if old is None:
        return {"material": True, "outcome": [None, new["outcome"]], "added": [],
                "status_changed": [], "no_longer_current": [], "retracted": []}  # fmt: skip
    before = {_key_of(c): c["status"] for c in old.get("conclusions", [])}
    after = {_key_of(c): c["status"] for c in new.get("conclusions", [])}
    added = sorted(k for k in after.keys() - before.keys() if after[k] in CLAIMS)
    status_changed = sorted(
        [k, before[k], after[k]] for k in after.keys() & before.keys() if before[k] != after[k]
    )
    retracted = sorted(h["key"] for h in new.get("history", []) if h["status"] == "retracted"
                       and h["key"] in before)  # fmt: skip
    no_longer_current = sorted(before.keys() - after.keys())
    outcome = [old.get("outcome"), new["outcome"]]
    return {
        "material": bool(added or status_changed or retracted or outcome[0] != outcome[1]),
        "outcome": outcome,
        "added": added,
        "status_changed": status_changed,
        "no_longer_current": no_longer_current,
        "retracted": retracted,
    }
