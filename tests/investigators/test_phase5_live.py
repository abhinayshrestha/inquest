"""Phase 5 hardening: regressions from the live API run (investigation 89efc27e,
2026-10-21). The evidence, findings, objectives and report drafts below are the ones that
run produced, with their refs; only the UUID tails are synthetic.

Defects reproduced: report v1 named a root cause with observation conclusions only; turn
2 persisted a heading-only, uncited answer; report v2 (causal claims, all downgraded)
replaced v1; round 2's Local findings answered CPU and memory but not the errors and
latency the objective asked about."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from src.db.session import SessionLocal
from src.investigators.main_validation import (
    ANSWER_BLOCKING,
    build_memory,
    conclusion_as_of,
    fallback_answer,
    merge_report,
    substantive,
    validate_answer,
    validate_verdict,
)
from src.investigators.refs import ref
from src.investigators.schemas import (
    AnswerDraft,
    AssignmentRequest,
    Conclusion,
    EvidenceLink,
    FindingDraft,
    ReportConclusion,
    ReportDraft,
    Verdict,
)
from src.investigators.validation import (
    EvidenceFacts,
    Known,
    objective_topics,
    problem_codes,
    validate_conclusion,
)
from src.investigators.windows import Window
from src.models import (
    Finding,
    InvestigationReport,
    InvestigationTurn,
    TurnDecision,
    TurnStatus,
)
from src.services import turns as turn_services
from src.services.turns import InvalidAnswer, answer_has_content
from tests.investigators.conftest import ScriptedModel
from tests.investigators.test_followups import (  # noqa: F401
    ask,
    first_turn,
    follow_up_answer,
    mocked_signoz,
    reports,
)
from tests.investigators.test_main_graph import (  # noqa: F401
    ANSWER,
    PLAN,
    MainWorld,
    go,
    investigate,
    local_script,
    main_world,
    rows,
)
from tests.mcp_server.conftest import signoz  # noqa: F401


def uid(prefix: str) -> uuid.UUID:
    return uuid.UUID(f"{prefix}-0000-4000-8000-000000000000")


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return datetime(2026, 10, 21, int(h), int(m), tzinfo=UTC)


R1 = (at("17:16"), at("17:46"))
R2 = (at("17:38"), at("17:48"))
TURN_1, TURN_2, ENV = uid("1b5854ab"), uid("4ff94922"), uid("c9d9ff8f")
HOST = "Metric {m}, host in [abbyvm-QEMU-Virtual-Machine]"
SVC = "service in [inquest-demo]"

# ref prefix: (type, granularity, window, title, summary)
EVIDENCE = {
    "c2f75439": ("trace", "aggregate", R1, f"Trace stats by operation, {SVC}",
                 "trace stats by operation: 8 groups, 504 spans, 28 errors (5.5556%). Errors: "
                 "[GET /error] 28 spans, 28 errors (100.0%). Slowest p95: [request handler - "
                 "/slow] 56 spans, p95 1535ms; [GET /slow] 28 spans, p95 1527ms"),
    "8579ccb5": ("log", "series", R1, f"Log volume, {SVC}",
                 "log volume, step 60s: 56 logs in 1 series. peak 34/step at 17:45:00Z"),
    "2c0a209e": ("log", "events", R1, f"Log records, {SVC}",
                 "logs: returned records 1-5; GET /error status_code 500"),
    "a02ae509": ("metric", "series", R1, HOST.format(m="cpu_utilization"),
                 "cpu_utilization: mean 1.381, max 5.79 at 17:24:00Z percent"),
    "00c5ed87": ("metric", "series", R1, HOST.format(m="memory_utilization"),
                 "memory_utilization: mean 26.3237, max 27.144 at 17:25:00Z percent"),
    "1f5967bf": ("trace", "aggregate", R1, f"Trace stats by operation, {SVC}",
                 "trace stats by operation: 8 groups, 504 spans, 28 errors (5.5556%). Errors: "
                 "[GET /error] 28 spans. Slowest p95: [request handler - /slow] 56 spans, "
                 "p95 1535ms; [GET /slow] 28 spans, p95 1527ms; [middleware - patched] 168 "
                 "spans, p95 1508ms"),
    "ef16fe7d": ("trace", "aggregate", R2, f"Trace stats by operation, {SVC}",
                 "trace stats by operation: 8 groups, 540 spans, 30 errors (5.5556%). Errors: "
                 "[GET /error] 30 spans, 30 errors (100.0%). Slowest p95: [request handler - "
                 "/slow] 60 spans, p95 1535ms"),
    "4be1d5ff": ("log", "series", R2, f"Log volume, {SVC}",
                 "log volume, step 60s: 150 logs in 1 series. peak 85/step at 17:45:00Z"),
    "d4e21eb5": ("trace", "trace", R2, f"Spans, {SVC}",
                 "spans: returned spans 1-30. 30 of the 30 returned spans have errors. "
                 "Slowest returned: inquest-demo/GET /error 10.9373ms"),
    "3b12b264": ("trace", "trace", R2, f"Spans, {SVC}",
                 "spans: returned spans 1-30. 0 of the 30 returned spans have errors. "
                 "Slowest returned: inquest-demo/GET /slow 1588.6088ms"),
    "92a5065f": ("metric", "series", R2, HOST.format(m="cpu_utilization"),
                 "cpu_utilization, step 10s: mean 1.2204, max 1.919 at 17:44:00Z percent"),
}  # fmt: skip

# ref prefix: (turn, status, title, [(evidence prefix, stance)])
FINDINGS = {
    "46e29040": (TURN_1, "preliminary", "High-latency endpoints", [("1f5967bf", "supports")]),
    "68d3222a": (TURN_1, "confirmed", "Error rate spike",
                 [("1f5967bf", "supports"), ("8579ccb5", "supports"), ("2c0a209e", "supports")]),
    "8825f4e2": (TURN_1, "confirmed", "Host CPU utilization low", [("a02ae509", "supports")]),
    "173a01a4": (TURN_1, "confirmed", "Host memory utilization low", [("00c5ed87", "supports")]),
    "208e19c6": (TURN_1, "confirmed", "Telemetry present",
                 [("1f5967bf", "supports"), ("8579ccb5", "supports")]),
    "87ba1aef": (TURN_2, "inconclusive",
                 "Host CPU utilization shows no spike aligning with error bursts",
                 [("92a5065f", "neutral"), ("4be1d5ff", "neutral")]),
    "2ea59e87": (TURN_2, "inconclusive",
                 "Host memory utilization remains stable with no spikes during error periods",
                 [("00c5ed87", "neutral"), ("4be1d5ff", "neutral")]),
}  # fmt: skip

OBJECTIVE_1 = (
    "Collect API latency distributions (p50/p95/p99 per endpoint), application error "
    "rates/logs, and host CPU/memory utilization for inquest-demo over the last 30 minutes. "
    "Identify any endpoints exceeding 500ms p95 latency, error spikes, and correlate with "
    "host resource saturation."
)
OBJECTIVE_2 = (
    "Collect traces, error logs, and host metrics for inquest-demo over the last 10 minutes "
    "(17:38-17:48 UTC) to determine: (a) whether GET /error errors are still occurring at "
    "17:46-17:48 UTC, and (b) whether any trace-level correlation exists between /error and "
    "/slow (shared spans, middleware contention, or resource spikes at finer granularity). "
    "Focus on trace stats by operation, log volume for /error, and per-second host "
    "CPU/memory to catch brief contention."
)


def ev(prefix: str) -> str:
    return ref("evidence", uid(prefix))


def fd(prefix: str) -> str:
    return ref("finding", uid(prefix))


def raw_memory(*, turn_2_findings: bool = True) -> dict[str, Any]:
    findings = {k: v for k, v in FINDINGS.items() if turn_2_findings or v[0] != TURN_2}
    return {
        "environments": [{"id": ENV, "name": "ubuntu-vm", "type": "local", "scope": {}}],
        "assignments": [
            {"round": 1, "environment": "ubuntu-vm", "status": "completed",
             "objective": OBJECTIVE_1},
            {"round": 2, "environment": "ubuntu-vm", "status": "completed",
             "objective": OBJECTIVE_2},
        ],
        "findings": [
            {"id": uid(k), "environment": "ubuntu-vm", "turn_id": t, "status": st, "title": ti,
             "summary": ti, "evidence": [(uid(e), s) for e, s in links]}
            for k, (t, st, ti, links) in findings.items()
        ],
        "hypotheses": [],
        "evidence": [
            {"id": uid(k), "environment": "ubuntu-vm", "type": ty, "title": ti, "summary": su,
             "granularity": g, "window": w}
            for k, (ty, g, w, ti, su) in EVIDENCE.items()
        ],
    }  # fmt: skip


def conclusion(key, statement, status, supporting, claim="observation", confidence="high"):
    return ReportConclusion(
        key=key,
        claim=claim,
        statement=statement,
        status=status,
        confidence=confidence,
        supporting=supporting,
        contradicting=[],
    )


def report(outcome: str, *conclusions: ReportConclusion) -> ReportDraft:
    return ReportDraft(
        outcome=outcome,  # type: ignore[arg-type]
        summary="s",
        conclusions=list(conclusions),
        timeline=[],
        gaps=[],
        next_checks=[],
    )


# The live report v1 draft: five confirmed observations, one unverified, "root cause".
V1 = report(
    "root_cause_identified",
    conclusion("slow-endpoints-identified", "Three operation groups exceed 500 ms p95 latency: "
               "request handler for /slow (56 spans, p95 1535 ms), GET /slow (28 spans, p95 "
               "1527 ms).", "confirmed", [ev("1f5967bf"), ev("c2f75439"), fd("46e29040")]),
    conclusion("error-spike-get-error", "Application error rate spiked to 5.56 % (28 error "
               "spans), all from GET /error returning HTTP 500.", "confirmed",
               [ev("1f5967bf"), ev("8579ccb5"), ev("2c0a209e"), fd("68d3222a")]),
    conclusion("host-cpu-low", "Host CPU utilization remained low throughout the window "
               "(average 1.38 %, max 5.79 %).", "confirmed", [ev("a02ae509"), fd("8825f4e2")]),
    conclusion("host-memory-low", "Host memory utilization remained low throughout the window "
               "(average 26.3 %).", "confirmed", [ev("00c5ed87"), fd("173a01a4")]),
    conclusion("host-resource-contribution-unverified", "The relationship between host "
               "resource levels and the observed latency/error spikes is unverified.",
               "unverified", [], confidence="low"),
    conclusion("telemetry-coverage-adequate", "Telemetry collection is complete: 504 spans and "
               "56 logs captured.", "confirmed", [ev("1f5967bf"), ev("8579ccb5"),
                                                  fd("208e19c6")]),
)  # fmt: skip

# The live report v2 draft: every conclusion declared causal on round-1 evidence.
V2 = report(
    "root_cause_identified",
    conclusion("error-endpoint-100pct-errors", "The /error endpoint intentionally returns HTTP "
               "500 errors on every request (100% error rate, 28/28 spans).", "confirmed",
               [ev("2c0a209e"), ev("1f5967bf"), ev("c2f75439")], claim="causal"),
    conclusion("slow-endpoint-high-latency-no-errors", "The /slow endpoint exhibits high "
               "latency (p95 ~1.5s) across 28 spans with 0% error rate.", "confirmed",
               [ev("1f5967bf"), ev("c2f75439")], claim="causal"),
    conclusion("error-endpoint-intentional-500s", "Log records confirm /error endpoint returns "
               "HTTP 500 status codes intentionally.", "confirmed", [ev("2c0a209e")],
               claim="causal"),
)  # fmt: skip

HEADINGS = [
    "## Answer to Follow-up Question  ",
    "## Investigation Report: Error Endpoint Analysis  ",
]


# --- Priority 2: outcome -------------------------------------------------------------------


def test_live_v1_observations_do_not_identify_a_root_cause():
    clean, problems = validate_answer(
        AnswerDraft(answer_md=f"Slow and failing endpoints {fd('68d3222a')}.", report=V1),
        build_memory(raw_memory(turn_2_findings=False)),
        report_required=True,
    )
    assert problem_codes(problems) == {"unsupported_outcome"}
    r = clean["report"]
    assert r["outcome"] == "inconclusive"
    # The observations themselves stand.
    assert [c["status"] for c in r["conclusions"]] == ["confirmed"] * 4 + [
        "unverified",
        "confirmed",
    ]
    assert clean["report_problems"] == problems


def test_a_causal_conclusion_on_trace_and_series_evidence_identifies_a_root_cause():
    causal = conclusion(
        "error-burst-log-volume",
        "The GET /error failures drive the error log volume peak at 17:45.",
        "confirmed",
        [ev("d4e21eb5"), ev("4be1d5ff")],
        claim="causal",
    )
    clean, problems = validate_answer(
        AnswerDraft(answer_md=f"Errors drive the log peak {ev('d4e21eb5')}.",
                    report=report("root_cause_identified", causal)),
        build_memory(raw_memory()),
        report_required=True,
    )  # fmt: skip
    assert "unsupported_outcome" not in problem_codes(problems)
    assert clean["report"]["outcome"] == "root_cause_identified"


def test_causal_reading_of_a_correlation_is_a_probable_cause_at_most():
    correlated = conclusion(
        "cpu-tracks-logs",
        "CPU usage is driven by the log volume.",
        "confirmed",
        [ev("92a5065f"), ev("4be1d5ff")],
        claim="causal",
    )
    clean, problems = validate_answer(
        AnswerDraft(answer_md=f"See {ev('92a5065f')}.",
                    report=report("root_cause_identified", correlated)),
        build_memory(raw_memory()),
        report_required=True,
    )  # fmt: skip
    assert {"relationship_unsupported", "unsupported_outcome"} <= problem_codes(problems)
    assert clean["report"]["conclusions"][0]["status"] == "probable"
    assert clean["report"]["outcome"] == "probable_cause"


# --- Priority 1: a valid answer, or a labelled fallback, or failure -------------------------


@pytest.mark.parametrize("heading", HEADINGS)
def test_live_heading_only_answers_are_unusable(heading):
    assert not substantive(heading) and not answer_has_content(heading)
    _, problems = validate_answer(
        AnswerDraft(answer_md=heading, report=V2),
        build_memory(raw_memory()),
        report_required=False,
    )
    assert {"unusable_answer", "uncited_answer"} <= problem_codes(problems)
    assert problem_codes(problems) & ANSWER_BLOCKING


@pytest.mark.parametrize(
    "md", ["", "   \n", "# A\n## B", f"## Refs\n{fd('68d3222a')}, {ev('4be1d5ff')}"]
)
def test_empty_heading_or_ref_only_answers_are_not_substantive(md):
    assert not substantive(md)


def test_short_real_answers_are_substantive():
    assert substantive(f"Yes, still failing {fd('68d3222a')}.") and answer_has_content("?")


def test_live_turn_2_gets_a_labelled_fallback_from_its_own_findings():
    memory = build_memory(raw_memory())
    unresolved = ["[uncited_answer] x", "[unusable_answer] y", "[unsupported_outcome] z"]
    out = fallback_answer(
        raw_memory(), memory, turn_id=TURN_2, unresolved=unresolved, initial=False,
        report_version=1,
    )  # fmt: skip
    assert out is not None
    clean, problems = out
    md = clean["answer_md"]
    assert clean["fallback"] is True and clean["report"] is None
    assert md.startswith("**Inquest fallback answer.**")
    assert "uncited_answer, unsupported_outcome, unusable_answer" in md
    assert "this turn's investigation" in md and "report (v1) stands unchanged" in md
    # Only this turn's validated findings, with their evidence and observation windows.
    assert fd("87ba1aef") in md and fd("2ea59e87") in md and fd("68d3222a") not in md
    assert ev("92a5065f") in md and "2026-10-21T17:38Z to 2026-10-21T17:48Z" in md
    cited = {r["ref"] for r in clean["refs"]}
    assert cited <= {fd(k) for k in FINDINGS} | {ev(k) for k in EVIDENCE}
    assert not problem_codes(problems) & ANSWER_BLOCKING and substantive(md)


def test_fallback_uses_the_investigation_when_the_turn_found_nothing():
    clean, _ = fallback_answer(
        raw_memory(turn_2_findings=False), build_memory(raw_memory(turn_2_findings=False)),
        turn_id=TURN_2, unresolved=[], initial=False, report_version=1,
    )  # fmt: skip
    md = clean["answer_md"]
    assert "this investigation" in md
    # Confirmed first.
    assert md.index(fd("68d3222a")) < md.index(fd("46e29040"))


def test_initial_fallback_has_an_inconclusive_report_of_observations():
    raw = raw_memory(turn_2_findings=False)
    clean, _ = fallback_answer(
        raw, build_memory(raw), turn_id=TURN_1, unresolved=["[schema] bad"], initial=True,
        report_version=None,
    )  # fmt: skip
    r = clean["report"]
    assert r["outcome"] == "inconclusive"
    statuses = {c["statement"]: c["status"] for c in r["conclusions"]}
    assert statuses["Error rate spike"] == "confirmed"
    assert statuses["High-latency endpoints"] == "probable"
    assert all(c["claim"] == "observation" for c in r["conclusions"])


def test_no_fallback_without_findings_that_link_evidence():
    raw = raw_memory()
    for f in raw["findings"]:
        f["evidence"] = []
    assert (
        fallback_answer(raw, build_memory(raw), turn_id=TURN_2, unresolved=[], initial=False,
                        report_version=1)
        is None
    )  # fmt: skip


# --- Priority 3: report continuity ---------------------------------------------------------


def test_live_v2_is_a_report_revision_that_stays_wrong():
    clean, _ = validate_answer(
        AnswerDraft(answer_md=f"Errors continue {ev('ef16fe7d')}.", report=V2),
        build_memory(raw_memory()),
        report_required=False,
    )
    assert {"relationship_unsupported", "unsupported_outcome"} <= problem_codes(
        clean["report_problems"]
    )
    assert "uncited_answer" not in problem_codes(clean["report_problems"])


def test_restated_old_observations_keep_their_observation_time():
    memory = build_memory(raw_memory())
    clean, _ = validate_answer(
        AnswerDraft(answer_md=f"x {ev('ef16fe7d')}", report=V2), memory, report_required=False
    )
    turn_2_end = "2026-10-21T17:48+00:00"
    as_of = [conclusion_as_of(c, memory, turn_2_end) for c in clean["report"]["conclusions"]]
    # Cited round-1 evidence only: observed at 17:46, not "now".
    assert as_of == ["2026-10-21T17:46+00:00"] * 3
    fresh = {"supporting": [{"kind": "evidence", "id": str(uid("ef16fe7d"))}]}
    assert conclusion_as_of(fresh, memory, "x") == turn_2_end
    assert conclusion_as_of({"supporting": []}, memory, "default") == "default"


def test_a_valid_revision_keeps_v1_observations_as_dated_history():
    memory = build_memory(raw_memory())
    v1, _ = validate_answer(
        AnswerDraft(answer_md=f"x {ev('1f5967bf')}", report=V1), memory, report_required=True
    )
    current = merge_report(v1["report"], None, as_of="2026-10-21T17:46+00:00") | {"version": 1}
    errors_now = conclusion(
        "errors-continue", "GET /error still fails: 30 of 540 spans are errors.", "confirmed",
        [ev("ef16fe7d")],
    )  # fmt: skip
    v2, _ = validate_answer(
        AnswerDraft(answer_md=f"x {ev('ef16fe7d')}", report=report("inconclusive", errors_now)),
        memory,
        report_required=False,
    )
    merged = merge_report(v2["report"], current, as_of="2026-10-21T17:48+00:00")
    assert [c["key"] for c in merged["conclusions"]] == ["errors-continue"]
    history = {h["key"]: h for h in merged["history"]}
    assert len(history) == 6
    assert {h["scope"] for h in history.values()} == {"historical"}
    assert {h["from_version"] for h in history.values()} == {1}
    assert history["error-spike-get-error"]["as_of"] == "2026-10-21T17:46+00:00"


# --- Priority 4: findings answer the objective's subjects ----------------------------------


def known(objective: str) -> Known:
    return Known(
        evidence={ev(k): uid(k) for k in EVIDENCE},
        hypotheses={},
        links={},
        facts={
            uid(k): EvidenceFacts(type=ty, text=f"{ti}\n{su}", granularity=g, window=w)
            for k, (ty, g, w, ti, su) in EVIDENCE.items()
        },
        context_text=objective,
        topics=objective_topics(objective),
    )


def live_findings(*keys: str, stance: str | None = None) -> list[FindingDraft]:
    status = {"confirmed": "confirmed", "preliminary": "preliminary"}
    return [
        FindingDraft(
            title=FINDINGS[k][2],
            summary=FINDINGS[k][2],
            status=status.get(FINDINGS[k][1], "inconclusive"),  # type: ignore[arg-type]
            evidence=[
                EvidenceLink(evidence_ref=ev(e), stance=stance or s)  # type: ignore[arg-type]
                for e, s in FINDINGS[k][3]
            ],
        )  # fmt: skip
        for k in keys
    ]


def test_objective_subjects():
    assert objective_topics(OBJECTIVE_1) == ("cpu", "memory", "errors", "latency")
    assert objective_topics(OBJECTIVE_2) == ("cpu", "memory", "errors", "latency")
    assert objective_topics("Find the failing span") == ()


def test_live_round_1_findings_answer_every_subject():
    findings = live_findings("46e29040", "68d3222a", "8825f4e2", "173a01a4", "208e19c6")
    _, problems = validate_conclusion(
        Conclusion(verdict="sufficient", gaps=[], hypothesis_updates=[], findings=findings),
        known(OBJECTIVE_1),
    )
    assert "topic_unanswered" not in problem_codes(problems)


def test_live_round_2_cpu_and_memory_findings_do_not_answer_errors_or_latency():
    clean, problems = validate_conclusion(
        Conclusion(verdict="sufficient", gaps=[], hypothesis_updates=[],
                   findings=live_findings("87ba1aef", "2ea59e87")),
        known(OBJECTIVE_2),
    )  # fmt: skip
    unanswered = [p for p in problems if p.startswith("[topic_unanswered]")]
    assert len(unanswered) == 2
    assert "about errors" in unanswered[0] and "about latency" in unanswered[1]
    added = {f["title"]: f for f in clean["findings"][2:]}
    assert set(added) == {"Application errors: not assessed", "Request latency: not assessed"}
    for f in added.values():
        assert f["status"] == "inconclusive"
        cited = {uuid.UUID(x["evidence_id"]) for x in f["evidence"]}
        # Newest evidence of the right kind: round 2's traces and logs, never metrics.
        assert cited and cited <= {uid(k) for k, v in EVIDENCE.items() if v[2] == R2}
        assert all(EVIDENCE[e.hex[:8]][0] in ("trace", "log") for e in cited)


def test_a_mention_with_real_support_answers_a_secondary_subject():
    # The same CPU finding, but its error-log link supports it: errors is answered too.
    findings = live_findings("87ba1aef", stance="supports")
    _, problems = validate_conclusion(
        Conclusion(verdict="sufficient", gaps=[], hypothesis_updates=[], findings=findings),
        known("Are errors related to CPU?"),
    )
    assert "topic_unanswered" not in problem_codes(problems)


def test_an_honest_inconclusive_finding_answers_its_subject():
    honest = FindingDraft(
        title="Errors after 17:46 could not be determined",
        summary="The log volume shows the 17:45 peak only.",
        status="inconclusive",
        evidence=[EvidenceLink(evidence_ref=ev("4be1d5ff"), stance="neutral")],
    )
    _, problems = validate_conclusion(
        Conclusion(verdict="sufficient", gaps=[], hypothesis_updates=[], findings=[honest]),
        known("Are errors still happening?"),
    )
    assert problems == []


# --- secondary: the turn's window is authoritative -----------------------------------------


def test_the_computed_window_is_authoritative_in_the_live_objective():
    memory = build_memory(raw_memory())
    window = Window(at("17:33"), at("17:48"), "now")
    clean, _ = validate_verdict(
        Verdict(decision="investigate", rationale="r", gaps=[],
                assignments=[AssignmentRequest(environment="ubuntu-vm", objective=OBJECTIVE_2)]),
        memory, question="are errors still happening right now?", rounds=0, window=window,
        covered=False,
    )  # fmt: skip
    objective = clean["assignments"][0]["objective"]
    assert objective.startswith(OBJECTIVE_2)
    assert objective.endswith(
        "Time window (UTC): now (2026-10-21T17:33Z to 2026-10-21T17:48Z). Query this whole "
        "window; any other range above is only a focus inside it."
    )


def test_a_long_objective_never_loses_the_window():
    memory = build_memory(raw_memory())
    window = Window(at("17:33"), at("17:48"), "now")
    clean, _ = validate_verdict(
        Verdict(decision="investigate", rationale="r", gaps=[],
                assignments=[AssignmentRequest(environment="ubuntu-vm", objective="x" * 2000)]),
        memory, question="q", rounds=0, window=window, covered=False,
    )  # fmt: skip
    objective = clean["assignments"][0]["objective"]
    assert len(objective) <= 2000 and "Time window (UTC): now" in objective


# --- end to end: real graphs, scripted models ----------------------------------------------


def heading_only(_: list[Any]) -> AnswerDraft:
    return AnswerDraft(answer_md=HEADINGS[1], report=None)


async def test_follow_up_with_a_heading_only_answer_completes_with_the_fallback(go, main_world):  # noqa: F811
    await first_turn(go, main_world)
    (v1,) = await reports(main_world)
    turn_2 = await ask(main_world, "Which operation dominates the latency?")

    main = ScriptedModel(structured=[ANSWER, heading_only, heading_only])
    result = await go(main_world, main, ScriptedModel(), turn_id=turn_2)

    assert result.status == TurnStatus.COMPLETED
    async with SessionLocal() as s:
        t2 = await s.get(InvestigationTurn, turn_2)
    a = t2.answer_json
    assert a["fallback"] is True and a["refs"] and a["report_version"] is None
    assert a["answer_md"].startswith("**Inquest fallback answer.**")
    assert "report (v1) stands unchanged" in a["answer_md"]
    assert any("answer: fallback (uncited_answer, unusable_answer)" in x
               for x in a["validation_audit"])  # fmt: skip
    # v1 untouched.
    assert [r.report_json for r in await reports(main_world)] == [v1.report_json]


async def test_initial_turn_with_a_heading_only_answer_gets_a_fallback_report(
    go,  # noqa: F811
    main_world,  # noqa: F811
    signoz,  # noqa: F811
):
    signoz("trace_stats_scalar.json")
    main = ScriptedModel(structured=[investigate(main_world), ANSWER, heading_only, heading_only])

    result = await go(main_world, main, local_script(main_world.service))

    assert (result.status, result.outcome, result.report_version) == (
        TurnStatus.COMPLETED, "inconclusive", 1,
    )  # fmt: skip
    (r,) = await rows(main_world, InvestigationReport)
    (c,) = r.report_json["conclusions"]
    assert (c["statement"], c["status"], c["claim"]) == (
        "One operation dominates checkout latency", "confirmed", "observation",
    )  # fmt: skip


async def test_no_validated_finding_means_the_turn_fails_and_keeps_its_records(go, main_world):  # noqa: F811
    failing_local = ScriptedModel(
        replies=[RuntimeError("down")], structured=[PLAN, RuntimeError("down")]
    )
    main = ScriptedModel(structured=[investigate(main_world), ANSWER, heading_only, heading_only])

    result = await go(main_world, main, failing_local)

    assert (result.status, result.error_code) == (TurnStatus.FAILED, "answer_invalid")
    assert await rows(main_world, InvestigationReport) == []
    (finding,) = await rows(main_world, Finding)  # the Local fallback finding stays
    assert finding.status.value == "inconclusive"


async def test_follow_up_report_revision_still_wrong_after_repair_is_refused(go, main_world):  # noqa: F811
    await first_turn(go, main_world)
    (v1,) = await reports(main_world)
    turn_2 = await ask(main_world, "Did the latency cause errors?")

    def causal(messages: list[Any]) -> ReportDraft:
        draft = follow_up_answer()(messages)
        cite = draft.answer_md.split()[-1].rstrip(".")
        return report(
            "root_cause_identified",
            conclusion("latency-causes-errors", "The slow operation causes the errors.",
                       "confirmed", [cite], claim="causal"),
        )  # fmt: skip

    bad = follow_up_answer(report=causal)
    main = ScriptedModel(structured=[ANSWER, bad, bad])
    result = await go(main_world, main, ScriptedModel(), turn_id=turn_2)

    assert result.status == TurnStatus.COMPLETED
    async with SessionLocal() as s:
        a = (await s.get(InvestigationTurn, turn_2)).answer_json
    assert a["fallback"] is False and a["report_version"] is None
    assert {"relationship_unsupported", "unsupported_outcome"} <= set(a["report_rejected"])
    assert "report v1 remains current" in a["answer_md"]
    assert [r.report_json for r in await reports(main_world)] == [v1.report_json]


async def test_complete_turn_refuses_a_heading_only_answer(main_world):  # noqa: F811
    async with SessionLocal() as s:
        await turn_services.start_turn(s, main_world.turn_id)
    async with SessionLocal() as s:
        with pytest.raises(InvalidAnswer):
            await turn_services.complete_turn(
                s, main_world.turn_id, decision=TurnDecision.INVESTIGATED,
                answer_json={"answer_md": HEADINGS[1]},
            )  # fmt: skip
    async with SessionLocal() as s:
        assert (await s.get(InvestigationTurn, main_world.turn_id)).status == TurnStatus.RUNNING


def test_compose_diagnostics_keep_shape_not_reasoning_or_report_text():
    from langchain_core.messages import AIMessage

    from src.investigators.main_graph import _compose_diagnostics

    raw = AIMessage(
        content="private chain of thought",
        additional_kwargs={"reasoning_content": "secret reasoning about Bearer eyJ"},
        tool_calls=[{"id": "1", "name": "AnswerDraft", "args": {
            "answer_md": HEADINGS[1],
            "report": {"outcome": "root_cause_identified", "summary": "GET /error 28 spans",
                       "conclusions": [{"statement": "trace abc"}]},
        }}],
        response_metadata={"finish_reason": "tool_calls"},
        usage_metadata={"input_tokens": 9000, "output_tokens": 700, "total_tokens": 9700,
                        "output_token_details": {"reasoning": 600}},
    )  # fmt: skip
    diag = _compose_diagnostics({"raw": raw, "parsed": None, "parsing_error": None})
    text = str(diag)
    assert diag["finish_reason"] == "tool_calls" and diag["reasoning_tokens"] == 600
    assert diag["reasoning_chars"] == len("secret reasoning about Bearer eyJ")
    (call,) = diag["tool_calls"]
    assert call["answer_md_head"] == HEADINGS[1] and call["answer_md_lines"] == 1
    assert (call["report"], call["report_conclusions"]) == ("dict", 1)
    for leaked in ("chain of thought", "secret", "Bearer", "GET /error", "trace abc"):
        assert leaked not in text
