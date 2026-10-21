"""Regression tests for the Phase 4 live run: unsupported correlation/causation, evidence
link direction, report continuity and speculative runtime work. The scenario is the real
one: two /error HTTP 500 records and slow /slow requests in the same 15-minute window."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.investigators.context import brief_text
from src.investigators.main_validation import (
    build_memory,
    merge_report,
    report_diff,
    validate_answer,
)
from src.investigators.refs import ref, ref_index
from src.investigators.schemas import (
    AnswerDraft,
    Conclusion,
    EvidenceLink,
    FindingDraft,
    HypothesisDraft,
    HypothesisPlan,
    HypothesisUpdate,
    ReportConclusion,
    ReportDraft,
)
from src.investigators.validation import (
    EvidenceFacts,
    Known,
    detected_claim,
    problem_codes,
    speculative_terms,
    supported_claim,
    validate_conclusion,
    validate_plan,
)

END = datetime(2026, 10, 21, 17, 6, tzinfo=UTC)
W = (END - timedelta(minutes=15), END)
LOGS, TRACE, CPU, VOLUME, LATENCY, SPANS = (uuid.uuid4() for _ in range(6))
H_ERR, H_HOST = uuid.uuid4(), uuid.uuid4()
FACTS = {
    LOGS: EvidenceFacts("log", "Log records: 2 ERROR GET /error status 500", granularity="events",
                        window=W),
    TRACE: EvidenceFacts("trace",
                         "Trace stats: [GET /slow] 15 spans p95 1534ms; [GET /error] 1 span",
                         samples={"GET /slow": 15, "GET /error": 1}, granularity="aggregate",
                         window=W),
    CPU: EvidenceFacts("metric", "Metric cpu_utilization max 5.1 percent", granularity="series",
                       window=W),
    VOLUME: EvidenceFacts("log", "Log volume per minute", granularity="series", window=W),
    LATENCY: EvidenceFacts("metric", "Metric http_server_p99_seconds per minute",
                           granularity="series", window=W),
    SPANS: EvidenceFacts("trace", "Spans of trace abc: GET /slow calls /error", granularity="trace",
                         window=W),
}  # fmt: skip
STATEMENTS = {
    H_ERR: "Spikes in application ERROR logs occur at the same times as latency elevations, "
    "indicating that error handling contributes to slowdowns.",
    H_HOST: "Host CPU or memory saturation is causing the latency.",
}


def known() -> Known:
    return Known(
        evidence=ref_index("evidence", list(FACTS)),
        hypotheses=ref_index("hypothesis", list(STATEMENTS)),
        links={},
        facts=FACTS,
        hypothesis_status={h: "active" for h in STATEMENTS},
        hypothesis_text=STATEMENTS,
    )


def ev(e, stance="supports"):
    return EvidenceLink(evidence_ref=ref("evidence", e), stance=stance)


def upd(h, status, *links):
    return HypothesisUpdate(
        hypothesis_ref=ref("hypothesis", h), status=status, evidence=list(links)
    )


def check(*findings, updates=None):
    updates = updates or [upd(H_ERR, "active"), upd(H_HOST, "active")]
    c = Conclusion(
        verdict="sufficient", gaps=[], hypothesis_updates=updates, findings=list(findings)
    )
    return validate_conclusion(c, known())


def finding(title, summary, *links, status="confirmed", claim="observation", **kw):
    return FindingDraft(title=title, summary=summary, status=status, claim=claim,
                        evidence=list(links), **kw)  # fmt: skip


# --- detection -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "claim"),
    [
        ("Application ERROR logs coincide with latency window.", "correlation"),
        ("Error spikes correlate with /slow latency.", "correlation"),
        ("The /error failures are causing the /slow latency.", "causal"),
        ("Error handling contributes to slowdowns.", "causal"),
        ("Both occurred in 16:51-17:06Z; whether they are related is unverified.", "observation"),
        ("Host CPU is not contributing to latency.", "observation"),
        ("GET /slow p95 is 1534 ms (15 spans).", "observation"),
    ],
)
def test_relationship_detection(text, claim):
    assert detected_claim(text) == claim


def test_ladder_of_what_evidence_can_carry():
    f = FACTS
    assert supported_claim([f[LOGS]], "x")[0] == "observation"
    # The live case: one log sample and an aggregate over the same window.
    level, why = supported_claim([f[LOGS], f[TRACE]], "/error and /slow")
    assert level == "temporal_overlap" and "same window is not correlation" in why
    # Two time series, one entity: correlation; still no causal path.
    assert supported_claim([f[VOLUME], f[LATENCY]], "errors and latency")[0] == "correlation"
    # Two time series about different endpoints need trace evidence linking them.
    level, why = supported_claim([f[VOLUME], f[LATENCY]], "/error and /slow")
    assert level == "temporal_overlap" and "without trace evidence" in why
    assert supported_claim([f[VOLUME], f[LATENCY], f[SPANS]], "/error and /slow")[0] == "causal"
    # Different windows: not even overlap.
    old = EvidenceFacts(
        "log",
        "",
        granularity="series",
        window=(W[0] - timedelta(hours=3), W[0] - timedelta(hours=2)),
    )
    assert supported_claim([old, f[LATENCY]], "x")[0] == "observation"


# --- Local findings and hypotheses ---------------------------------------------------------


def test_live_correlation_finding_is_downgraded_and_observations_kept():
    clean, problems = check(
        finding("Application ERROR logs coincide with latency window",
                "Two ERROR records for GET /error at 17:02:13Z fall within the window where "
                "/slow latency is elevated (15 spans).", ev(LOGS), ev(TRACE))
    )  # fmt: skip
    assert {"undeclared_relationship", "relationship_unsupported"} <= problem_codes(problems)
    (f,) = clean["findings"]
    assert f["status"] == "inconclusive"
    assert "Relationship unverified: the evidence supports only temporal_overlap" in f["summary"]
    assert "The observations stand separately" in f["summary"]


def test_honest_temporal_overlap_is_accepted():
    clean, problems = check(
        finding("ERROR records and slow requests in the same window",
                "Two /error ERROR records and slow /slow requests (15 spans) both fall in "
                "16:51-17:06Z; whether they are related is unverified.",
                ev(LOGS), ev(TRACE), claim="temporal_overlap")
    )  # fmt: skip
    assert problems == [] and clean["findings"][0]["status"] == "confirmed"


def test_live_relationship_hypothesis_cannot_be_supported_by_overlap():
    clean, problems = check(
        finding("Errors seen", "Two ERROR records for /error (1 span).", ev(LOGS)),
        updates=[upd(H_ERR, "supported", ev(LOGS), ev(TRACE)), upd(H_HOST, "active")],
    )
    assert "relationship_unsupported" in problem_codes(problems)
    assert clean["hypothesis_updates"][0]["status"] == "active"
    assert len(clean["hypothesis_updates"][0]["evidence"]) == 2  # links kept, not status


def test_correlation_with_time_resolved_evidence_is_accepted():
    _, problems = check(
        finding("Error volume tracks latency", "Per-minute error volume correlates with p99.",
                ev(VOLUME), ev(LATENCY), claim="correlation")
    )  # fmt: skip
    assert problems == []


def test_causal_finding_needs_trace_evidence():
    clean, problems = check(
        finding("Errors drive latency", "Error volume drives p99 latency.",
                ev(VOLUME), ev(LATENCY), claim="causal")
    )  # fmt: skip
    assert "relationship_unsupported" in problem_codes(problems)
    assert clean["findings"][0]["status"] == "inconclusive"
    _, problems = check(
        finding("Errors drive latency", "Error volume drives p99 latency.",
                ev(VOLUME), ev(LATENCY), ev(SPANS), claim="causal")
    )  # fmt: skip
    assert problems == []


# --- evidence-link direction ---------------------------------------------------------------


def test_measurement_linked_against_its_own_finding_is_explained():
    clean, problems = check(
        finding("Host CPU is low", "CPU max 5.1%.", ev(CPU, "contradicts"),
                hypothesis_ref=ref("hypothesis", H_HOST), hypothesis_relation="contradicts"),
        updates=[upd(H_ERR, "active"), upd(H_HOST, "rejected", ev(CPU, "contradicts"))],
    )  # fmt: skip
    assert "finding_stance_direction" in problem_codes(problems)
    assert clean["findings"][0]["status"] == "inconclusive"


def test_measurement_supports_the_finding_and_contradicts_the_hypothesis():
    clean, problems = check(
        finding("Host CPU is low", "CPU max 5.1%.", ev(CPU),
                hypothesis_ref=ref("hypothesis", H_HOST), hypothesis_relation="contradicts"),
        updates=[upd(H_ERR, "active"), upd(H_HOST, "rejected", ev(CPU, "contradicts"))],
    )  # fmt: skip
    assert problems == []
    (f,) = clean["findings"]
    assert (f["status"], f["hypothesis_id"]) == ("confirmed", str(H_HOST))
    assert clean["hypothesis_updates"][1]["status"] == "rejected"


# --- speculative runtime work --------------------------------------------------------------


def test_jvm_hypothesis_for_a_node_service_is_dropped():
    plan = HypothesisPlan(hypotheses=[
        HypothesisDraft(statement="JVM garbage-collection pauses add latency", test_plan="gc"),
        HypothesisDraft(statement="One endpoint is slow", test_plan="trace_stats"),
    ])  # fmt: skip
    kept, problems = validate_plan(plan, grounding="service inquest-demo, Node.js")
    assert [k["statement"] for k in kept] == ["One endpoint is slow"]
    assert "speculative_runtime" in problem_codes(problems)
    kept, _ = validate_plan(plan, grounding="process.runtime.name=jvm garbage-collection seen")
    assert len(kept) == 2


def test_speculative_gaps_are_detected():
    assert speculative_terms("query jvm_gc_pause_ms for the host", "trace stats ...") == [
        "jvm_gc_pause_ms"
    ]
    assert speculative_terms("query error log volume", "trace stats ...") == []


# --- Main conclusions and answers ----------------------------------------------------------

FD_ERR = uuid.uuid4()


def main_memory():
    raw = {
        "environments": [{"id": uuid.uuid4(), "name": "ubuntu-vm", "type": "local", "scope": {}}],
        "assignments": [],
        "findings": [{"id": FD_ERR, "environment": "ubuntu-vm", "status": "confirmed",
                      "title": "ERROR logs on /error", "summary": "2 records",
                      "evidence": [(LOGS, "supports")]}],
        "hypotheses": [],
        "evidence": [{"id": e, "environment": "ubuntu-vm", "type": f.type, "title": f.text,
                      "summary": f.text, "granularity": f.granularity, "window": f.window}
                     for e, f in FACTS.items()],
    }  # fmt: skip
    return build_memory(raw)


def main_conclusion(statement, status, *refs, claim="observation"):
    return ReportConclusion(statement=statement, status=status, confidence="medium",
                            supporting=list(refs), contradicting=[], claim=claim)  # fmt: skip


def answer(*conclusions, text=None):
    return AnswerDraft(
        answer_md=text or f"Errors on /error {ref('finding', FD_ERR)}.",
        report=ReportDraft(outcome="probable_cause", summary="s", conclusions=list(conclusions),
                           timeline=[], gaps=[], next_checks=[]),
    )  # fmt: skip


def test_live_main_conclusion_about_coinciding_errors_is_unverified():
    clean, problems = validate_answer(
        answer(main_conclusion(
            "Error spikes and /slow latency elevation coincide temporally in the current window.",
            "probable", ref("evidence", LOGS), ref("evidence", TRACE), ref("finding", FD_ERR))),
        main_memory(), report_required=True,
    )  # fmt: skip
    assert "relationship_unsupported" in problem_codes(problems)
    (c,) = clean["report"]["conclusions"]
    assert (c["status"], c["claim"]) == ("unverified", "correlation")


def test_causal_conclusion_on_correlation_is_at_most_probable():
    clean, problems = validate_answer(
        answer(main_conclusion("Error volume drives latency.", "confirmed",
                               ref("evidence", VOLUME), ref("evidence", LATENCY), claim="causal")),
        main_memory(), report_required=True,
    )  # fmt: skip
    assert clean["report"]["conclusions"][0]["status"] == "probable"
    assert "relationship_unsupported" in problem_codes(problems)


def test_answer_sentence_asserting_causation_is_marked():
    text = (
        f"The /error failures are causing the /slow latency {ref('evidence', LOGS)}. "
        f"Whether they are related is unverified {ref('evidence', TRACE)}."
    )
    clean, problems = validate_answer(answer(text=text), main_memory(), report_required=False)
    assert "answer_relationship" in problem_codes(problems)
    assert clean["answer_md"].count("Not established by evidence") == 1
    assert "causing the /slow latency" in clean["answer_md"].split("[Inquest]")[1]


# --- report continuity ---------------------------------------------------------------------


def concl(key, statement, status, derived=False):
    return {"key": key, "key_derived": derived, "statement": statement, "status": status,
            "claim": "observation", "supporting": [], "contradicting": [],
            "confidence": "medium"}  # fmt: skip


V1 = {
    "version": 1,
    "outcome": "probable_cause",
    "conclusions": [
        {"key": "slow-latency", "statement": "GET /slow p95 is 1,514 ms over 30 minutes",
         "status": "probable", "as_of": "2026-10-18T03:28"},
        {"key": "errors-0318", "statement": "Errors on /error at 03:18:05Z", "status": "confirmed",
         "as_of": "2026-10-18T03:28"},
        {"key": "cpu-low", "statement": "Host CPU is low", "status": "confirmed",
         "as_of": "2026-10-18T03:28"},
    ],
}  # fmt: skip


def test_report_continues_keys_and_keeps_history():
    new = {"outcome": "probable_cause", "changes": [], "conclusions": [
        # Re-scoped wording, no key from the model: matched by endpoint and topic.
        concl("get-slow-is-still-slow", "GET /slow is still slow: p95 1,534 ms (15 spans)",
              "confirmed", derived=True),
        concl("cpu-low", "Host CPU is low (max 5.1%)", "confirmed"),
    ]}  # fmt: skip
    merged = merge_report(new, V1, as_of="2026-10-21T17:06")

    assert [c["key"] for c in merged["conclusions"]] == ["slow-latency", "cpu-low"]
    assert all(c["scope"] == "current" and c["as_of"] == "2026-10-21T17:06"
               for c in merged["conclusions"])  # fmt: skip
    (h,) = merged["history"]
    assert (h["key"], h["scope"], h["from_version"], h["as_of"], h["status"]) == (
        "errors-0318", "historical", 1, "2026-10-18T03:28", "confirmed",
    )  # fmt: skip
    diff = report_diff(merged, V1)
    assert diff["status_changed"] == [["slow-latency", "probable", "confirmed"]]
    assert diff["no_longer_current"] == ["errors-0318"] and diff["material"] is True


def test_rewording_and_carry_forward_alone_are_not_a_new_version():
    new = {"outcome": "probable_cause", "changes": [], "conclusions": [
        concl("x", "Host CPU is low", "confirmed", derived=True),
        concl("slow-latency", "GET /slow remains slow", "probable"),
    ]}  # fmt: skip
    merged = merge_report(new, V1, as_of="t")
    assert report_diff(merged, V1)["material"] is False
    assert [h["key"] for h in merged["history"]] == ["errors-0318"]


def test_explicit_retraction_is_material_and_recorded():
    new = {"outcome": "probable_cause", "conclusions": [
        concl("slow-latency", "GET /slow remains slow", "probable"),
        concl("cpu-low", "Host CPU is low", "confirmed"),
    ], "changes": [{"change": "retracted", "key": "errors-0318", "statement": "Errors at 03:18",
                    "reason": "r", "refs": []}]}  # fmt: skip
    merged = merge_report(new, V1, as_of="t")
    assert merged["history"][0]["status"] == "retracted"
    assert report_diff(merged, V1)["retracted"] == ["errors-0318"]


def test_history_is_bounded_and_not_duplicated():
    older = {**V1, "version": 3, "history": [
        {"key": f"h{i}", "statement": "s", "status": "probable", "scope": "historical"}
        for i in range(30)
    ]}  # fmt: skip
    merged = merge_report({"outcome": "inconclusive", "conclusions": [], "changes": []}, older,
                          as_of="t")  # fmt: skip
    keys = [h["key"] for h in merged["history"]]
    assert len(keys) == 20 and len(set(keys)) == 20 and keys[:3] == [
        "slow-latency", "errors-0318", "cpu-low"]  # fmt: skip


# --- brief size ----------------------------------------------------------------------------


def test_brief_shortens_earlier_evidence_but_not_this_assignments():
    now = END
    long = "x" * 600
    evidence = [
        {"ref": "ev_00000001", "own": False, "type": "trace", "title": "old", "summary": long,
         "collected_at": now - timedelta(hours=85)},
        {"ref": "ev_00000002", "own": True, "type": "trace", "title": "new", "summary": long,
         "collected_at": now},
    ]  # fmt: skip
    text = brief_text(objective="o", question="q", environment={"name": "e", "type": "local",
                      "scope": {}}, evidence=evidence, prior_hypotheses=[], prior_findings=[],
                      now=now)  # fmt: skip
    old_line, new_line = [line for line in text.splitlines() if line.startswith("- ev_")]
    assert old_line.count("x") == 199 and new_line.count("x") == 600
