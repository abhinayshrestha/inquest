"""Regression tests for the rules added after the first live run: absence claims, sparse
samples, invented names, hypothesis consistency, coverage gaps, compact tool schemas."""

import json
import uuid

import pytest

from src.investigators import policy
from src.investigators.context import coverage_gap
from src.investigators.mcp_tools import compact_schema
from src.investigators.refs import ref, ref_index
from src.investigators.schemas import (
    Conclusion,
    EvidenceLink,
    FindingDraft,
    HypothesisDraft,
    HypothesisPlan,
    HypothesisUpdate,
)
from src.investigators.validation import (
    EvidenceFacts,
    Known,
    is_known_unavailable,
    objective_topics,
    problem_codes,
    sanitize_note,
    unavailable_key,
    validate_conclusion,
    validate_plan,
)
from src.models import ToolCallStatus
from src.tools import list_tools
from src.tools.schemas import Outcome, ToolError, ToolExecutionResult

TRACE, CPU, MEM, H1, H2 = (uuid.uuid4() for _ in range(5))
TRACE_TEXT = "Trace stats by operation: [GET /slow] 2 spans p95 1514ms; [GET /] 500 spans"


def known(
    *, statuses: dict | None = None, links: dict | None = None, gaps=(), sparse=True, topics=()
):
    samples = {"GET /slow": 2, "GET /": 500} if sparse else {"GET /slow": 400, "GET /": 500}
    return Known(
        evidence=ref_index("evidence", [TRACE, CPU, MEM]),
        hypotheses=ref_index("hypothesis", [H1, H2]),
        links=links or {},
        facts={
            TRACE: EvidenceFacts(type="trace", text=TRACE_TEXT, samples=samples),
            CPU: EvidenceFacts(type="metric", text="Metric cpu_utilization\ncpu mean 0.9%"),
            MEM: EvidenceFacts(type="metric", text="Metric memory_utilization\nmemory mean 26%"),
        },
        hypothesis_status=statuses if statuses is not None else {H1: "active", H2: "active"},
        context_text="objective: investigate latency",
        gaps=list(gaps),
        topics=tuple(topics),
    )


def ev(evidence_id: uuid.UUID, stance: str = "supports") -> EvidenceLink:
    return EvidenceLink(evidence_ref=ref("evidence", evidence_id), stance=stance)  # type: ignore[arg-type]


def update(h: uuid.UUID, status: str, *links: EvidenceLink) -> HypothesisUpdate:
    return HypothesisUpdate(
        hypothesis_ref=ref("hypothesis", h), status=status, evidence=list(links)
    )  # type: ignore[arg-type]


def finding(
    summary: str,
    *links: EvidenceLink,
    status="preliminary",
    h=None,
    relation=None,
    title="Latency",
):
    return FindingDraft(
        title=title,
        summary=summary,
        status=status,  # type: ignore[arg-type]
        hypothesis_ref=ref("hypothesis", h) if h else None,
        hypothesis_relation=relation,
        evidence=list(links),
    )


def check(*findings: FindingDraft, updates=None, **known_kw):
    updates = updates if updates is not None else [update(H1, "active"), update(H2, "active")]
    c = Conclusion(
        verdict="sufficient", gaps=[], hypothesis_updates=updates, findings=list(findings)
    )
    return validate_conclusion(c, known(**known_kw))


# --- absence claims ------------------------------------------------------------------------


def test_absence_claim_from_an_empty_query_is_flagged_annotated_and_downgraded():
    clean, problems = check(
        finding(
            "No application-error logs were observed in 2 spans.", ev(TRACE), status="confirmed"
        )
    )
    assert any("no application-error logs" in p.lower() for p in problems)
    (f,) = clean["findings"]
    assert f["status"] == "preliminary"
    assert "[Inquest] The claim" in f["summary"] and "not backed by evidence" in f["summary"]


def test_absence_backed_by_linked_trace_evidence_is_allowed():
    clean, problems = check(
        finding("Spans show zero errors across 500 spans.", ev(TRACE), status="confirmed"),
        sparse=False,
    )
    assert problems == [] and clean["findings"][0]["status"] == "confirmed"


def test_hedged_absence_is_honest_not_a_claim():
    _, problems = check(
        finding("Error logs are unverified: the service exports no logs (no telemetry).", ev(CPU))
    )
    assert problems == []


# --- sparse samples ------------------------------------------------------------------------


def test_sparse_latency_needs_its_sample_size_and_cannot_be_confirmed():
    clean, problems = check(finding("/slow p95 is 1.5 s.", ev(TRACE), status="confirmed"))
    assert any(p.startswith("[sample_size]") for p in problems)
    assert any(p.startswith("[sparse_confirmed]") for p in problems)
    (f,) = clean["findings"]
    assert f["status"] == "preliminary"
    assert f"Sample: {ref('evidence', TRACE)}: GET /slow = 2 spans" in f["summary"]


def test_stated_sample_size_satisfies_the_rule():
    clean, problems = check(finding("/slow p95 is 1.5 s (2 spans).", ev(TRACE)))
    assert problems == [] and "Sample:" not in clean["findings"][0]["summary"]


def test_well_sampled_latency_still_needs_its_actual_counts():
    # Live run #2: /slow had 31 and 62 spans, above the sparse threshold, and the
    # confirmed finding gave no count at all.
    clean, problems = check(
        finding("GET / p95 stays near 20 ms.", ev(TRACE), status="confirmed"), sparse=False
    )
    assert [p for p in problems if p.startswith("[sample_size]")]
    (f,) = clean["findings"]
    assert f["status"] == "confirmed"  # well sampled: only the count was missing
    assert f"Sample: {ref('evidence', TRACE)}: GET / = 500 spans" in f["summary"]


def test_the_sample_is_the_group_the_text_talks_about():
    # "/" alone is well sampled; only /slow is sparse.
    clean, problems = check(
        finding("GET / p95 stays near 20 ms (500 spans).", ev(TRACE), status="confirmed")
    )
    assert problems == [] and clean["findings"][0]["status"] == "confirmed"


def test_latency_rule_ignores_findings_without_latency_figures():
    _, problems = check(finding("The slow route is the outlier.", ev(TRACE)))
    assert problems == []


# --- invented names ------------------------------------------------------------------------


def test_invented_endpoint_in_a_finding_makes_it_inconclusive():
    clean, problems = check(finding("/heavy-compute is slow (2 spans).", ev(TRACE)))
    assert any("/heavy-compute" in p for p in problems)
    assert clean["findings"][0]["status"] == "inconclusive"


def test_invented_endpoint_in_a_hypothesis_is_dropped_grounded_one_kept():
    plan = HypothesisPlan(
        hypotheses=[
            HypothesisDraft(statement="/heavy-compute is slow", test_plan="trace_stats"),
            HypothesisDraft(statement="/slow is slow", test_plan="trace_stats"),
            HypothesisDraft(statement="One endpoint is slow", test_plan="trace_stats"),
        ]
    )
    kept, problems = validate_plan(plan, grounding="brief mentions GET /slow")
    assert [k["statement"] for k in kept] == ["/slow is slow", "One endpoint is slow"]
    assert any("/heavy-compute" in p and "generically" in p for p in problems)


# --- hypothesis consistency ----------------------------------------------------------------


def test_every_hypothesis_must_be_addressed():
    _, problems = check(finding("2 spans", ev(TRACE)), updates=[update(H1, "active")])
    assert any(ref("hypothesis", H2) in p and "not addressed" in p for p in problems)


def test_active_with_only_contradicting_evidence_is_flagged_not_forced():
    clean, problems = check(
        finding("2 spans", ev(TRACE)),
        updates=[update(H1, "active", ev(CPU, "contradicts")), update(H2, "active")],
    )
    assert any("only contradicting evidence but remains active" in p for p in problems)
    assert clean["hypothesis_updates"][0]["status"] == "active"


def test_mixed_evidence_may_stay_active():
    _, problems = check(
        finding("2 spans", ev(TRACE)),
        updates=[update(H1, "active", ev(CPU, "contradicts"), ev(TRACE)), update(H2, "active")],
    )
    assert problems == []


def test_finding_that_resolves_an_active_hypothesis_triggers_repair():
    clean, problems = check(
        finding("/slow is slow (2 spans)", ev(TRACE), h=H1, relation="supports"),
        updates=[update(H1, "active", ev(TRACE)), update(H2, "active")],
    )
    assert "finding_hypothesis_active" in problem_codes(problems)
    assert clean["findings"][0]["hypothesis_id"] == str(H1)


def test_consistent_finding_and_hypothesis_pass():
    _, problems = check(
        finding("/slow is slow (2 spans)", ev(TRACE), h=H1, relation="supports"),
        updates=[update(H1, "supported", ev(TRACE)), update(H2, "active")],
    )
    assert problems == []


def test_finding_that_rules_a_cause_out_may_reference_the_rejected_hypothesis():
    # Live run 3: "CPU is low" (confirmed) against "CPU saturation caused the latency".
    clean, problems = check(
        finding("CPU stays near 0.9%.", ev(CPU), status="confirmed", h=H2,
                relation="contradicts", title="Host CPU"),
        updates=[update(H1, "active"), update(H2, "rejected", ev(CPU, "contradicts"))],
    )  # fmt: skip
    assert problems == []
    (f,) = clean["findings"]
    assert (f["status"], f["hypothesis_id"]) == ("confirmed", str(H2))
    assert clean["hypothesis_updates"][1]["status"] == "rejected"


def test_finding_that_supports_a_rejected_hypothesis_is_a_conflict():
    clean, problems = check(
        finding("/slow is slow (2 spans)", ev(CPU), status="confirmed", h=H1,
                relation="supports"),
        updates=[update(H1, "rejected", ev(CPU, "contradicts")), update(H2, "active")],
    )  # fmt: skip
    assert "relation_conflict" in problem_codes(problems)
    (f,) = clean["findings"]
    assert f["hypothesis_id"] is None
    # The contradicting link and the rejection are not kept as fact either.
    (u, _) = clean["hypothesis_updates"]
    assert (u["status"], u["evidence"]) == ("active", [])


def test_relation_must_agree_with_the_evidence_links():
    # Live run 3: the finding says errors do not coincide with latency, while its
    # evidence was linked to that hypothesis as supporting it.
    clean, problems = check(
        finding("Errors and latency do not overlap (2 spans).", ev(TRACE), h=H1,
                relation="contradicts"),
        updates=[update(H1, "active", ev(TRACE)), update(H2, "active")],
    )  # fmt: skip
    assert "relation_conflict" in problem_codes(problems)
    assert clean["findings"][0]["hypothesis_id"] is None
    assert clean["hypothesis_updates"][0]["evidence"] == []


def test_relation_without_linked_evidence_is_unlinked():
    clean, problems = check(
        finding("/slow is slow (2 spans)", ev(TRACE), h=H1, relation="supports"),
        updates=[update(H1, "supported", ev(CPU)), update(H2, "active")],
    )
    assert "relation_unlinked" in problem_codes(problems)
    assert clean["findings"][0]["hypothesis_id"] is None


def test_missing_relation_is_unlinked():
    clean, problems = check(finding("/slow is slow (2 spans)", ev(TRACE), h=H1))
    assert "relation_missing" in problem_codes(problems)
    assert clean["findings"][0]["hypothesis_id"] is None


def test_inconclusive_relation_needs_no_status_change():
    _, problems = check(
        finding("Overlap unclear (2 spans).", ev(TRACE), status="inconclusive", h=H1,
                relation="inconclusive"),
    )  # fmt: skip
    assert problems == []


@pytest.mark.parametrize(
    ("status", "links", "expected", "code"),
    [
        ("supported", ["supports"], "supported", None),
        ("weakened", ["contradicts"], "weakened", None),
        ("rejected", ["contradicts"], "rejected", None),
        ("weakened", ["supports", "contradicts"], "weakened", None),  # mixed: allowed
        ("active", ["supports", "contradicts"], "active", None),  # mixed: allowed
        ("supported", ["supports", "contradicts"], "active", "mixed_supported"),
        ("rejected", ["supports", "contradicts"], "weakened", "mixed_rejected"),
    ],
)
def test_hypothesis_status_against_mixed_evidence(status, links, expected, code):
    evidence = [ev(e, s) for e, s in zip((TRACE, CPU), links, strict=False)]
    clean, problems = check(
        finding("Latency (2 spans).", ev(TRACE)),
        updates=[update(H1, status, *evidence), update(H2, "active")],
    )
    assert clean["hypothesis_updates"][0]["status"] == expected
    assert (
        (code in problem_codes(problems))
        if code
        else not ({"mixed_supported", "mixed_rejected"} & problem_codes(problems))
    )


def test_mixed_evidence_counts_links_already_stored():
    clean, problems = check(
        finding("Latency (2 spans).", ev(TRACE)),
        updates=[update(H1, "supported", ev(TRACE)), update(H2, "active")],
        links={(H1, CPU): "contradicts"},
    )
    assert "mixed_supported" in problem_codes(problems)
    assert clean["hypothesis_updates"][0]["status"] == "active"


# --- coverage gaps -------------------------------------------------------------------------


def test_gaps_are_attached_to_every_finding():
    clean, _ = check(
        finding("2 spans", ev(TRACE)),
        gaps=["log_volume(identity=service, service=demo): no telemetry arrived for this scope"],
    )
    assert (
        "[Inquest] Unverified (no usable telemetry): log_volume" in clean["findings"][0]["summary"]
    )


def result(**kw) -> ToolExecutionResult:
    base = {"tool_call_id": uuid.uuid4(), "tool_name": "signoz.log_volume",
            "status": ToolCallStatus.SUCCEEDED}  # fmt: skip
    return ToolExecutionResult(**(base | kw))


ARGS = {"identity": "service", "service": "demo", "severity": ["ERROR"], "time_range": {}}


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"outcome": Outcome.EMPTY, "telemetry_available": "no"}, "no telemetry arrived"),
        ({"outcome": Outcome.EMPTY, "telemetry_available": "unknown"}, "coverage unknown"),
        ({"outcome": Outcome.EMPTY, "telemetry_available": "yes"}, None),
        ({"outcome": Outcome.OBSERVATIONS}, None),
        (
            {
                "status": ToolCallStatus.FAILED,
                "error": ToolError(code="signoz_timeout", message=""),
            },
            "query failed (signoz_timeout)",
        ),
        (
            {"status": ToolCallStatus.REJECTED, "error": ToolError(code="policy.x", message="")},
            None,
        ),
    ],
)
def test_coverage_gap_labels(kw, expected):
    gap = coverage_gap(result(**kw), ARGS)
    if expected is None:
        assert gap is None
    else:
        assert gap.startswith("log_volume(identity=service, service=demo, severity=['ERROR'])")
        assert expected in gap


# --- compact tool schemas ------------------------------------------------------------------


def test_compact_schemas_are_smaller_and_keep_what_the_model_needs():
    for tool in list_tools():
        full = tool.input_schema
        small = compact_schema(full)
        assert len(json.dumps(small)) < len(json.dumps(full))
        assert small["required"] == full["required"]
        assert set(small["properties"]) == set(full["properties"])
        text = json.dumps(small)
        assert '"title"' not in text and '{"type": "null"}' not in text
        if "$defs" in full:
            assert set(small["$defs"]) == set(full["$defs"])
    log = compact_schema(
        next(t for t in list_tools() if t.name == "signoz.log_volume").input_schema
    )
    assert log["properties"]["identity"] == {
        "default": "host",
        "enum": ["host", "service"],
        "type": "string",
    }
    assert log["properties"]["host"] == {"type": "string"}
    assert log["properties"]["step_seconds"]["maximum"] == 3600


def test_a_property_named_title_survives():
    schema = {"properties": {"title": {"title": "Title", "type": "string"}}, "title": "X"}
    assert compact_schema(schema) == {"properties": {"title": {"type": "string"}}}


def test_log_tool_descriptions_explain_identity():
    descriptions = {t.name: t.description for t in list_tools()}
    for name in ("signoz.search_logs", "signoz.log_volume"):
        assert (
            "identity='host'" in descriptions[name] and "identity='service'" in descriptions[name]
        )
    assert "guessed name returns no data" in descriptions["signoz.query_metrics"]
    assert policy.SPARSE_SAMPLE > 2


# --- final hardening pass: notes, need_more, audit codes, topics ---------------------------


def test_note_loses_fabricated_refs_and_marks_unbacked_health_claims():
    cpu = ref("evidence", CPU)
    note = (
        "Latency is on /slow 【" + ref("evidence", TRACE) + "】. "
        "No error-level logs were found 【ev_log_volume_empty】. "
        "Host resources are healthy. "
        f"CPU is healthy at 0.9% ({cpu}). "
        "Error logs are unverified: no telemetry. See fd_1234abcd."
    )
    text, changes = sanitize_note(note, known())
    assert "ev_log_volume_empty" not in text and "fd_1234abcd" not in text
    assert ref("evidence", TRACE) in text and cpu in text
    assert changes == {"fabricated_ref": 2, "unsupported_claim": 2}
    assert "[UNSUPPORTED: no cited evidence observed this] No error-level logs" in text
    assert "[UNSUPPORTED: no cited evidence observed this] Host resources are healthy" in text
    # Backed by cited metric evidence, and hedged: both left alone.
    assert "[UNSUPPORTED: no cited evidence observed this] CPU" not in text
    assert "[UNSUPPORTED: no cited evidence observed this] Error logs are unverified" not in text


@pytest.mark.parametrize(
    ("tool", "args", "key"),
    [
        ("signoz.log_volume", {"identity": "service"}, "logs|service|"),
        ("signoz.search_logs", {"identity": "service", "service": "demo"}, "logs|service|demo"),
        ("signoz.search_logs", {}, "logs|host|"),
        (
            "signoz.query_metrics",
            {"custom": {"metric_name": "request_count"}},
            "metrics|request_count",
        ),
        ("signoz.trace_stats", {"service": "demo"}, "traces|service|demo"),
    ],
)
def test_unavailable_keys(tool, args, key):
    assert unavailable_key(tool, args) == key


@pytest.mark.parametrize(
    ("gap", "redundant"),
    [
        ("log_volume(identity=service, service=inquest-demo, severity=['error'])", True),
        ("error logs for the service", True),
        ("search the application's error logs", False),  # names neither identity
        ("host system logs for OOM kills", False),  # host logs were never shown absent
        ("request_count metric for the service", True),
        ("p99 latency from trace spans", False),
    ],
)
def test_requested_gaps_already_known_unavailable(gap, redundant):
    unavailable = ["logs|service|", "metrics|request_count"]
    assert is_known_unavailable(gap, unavailable) is redundant


def test_problems_carry_codes_for_the_audit():
    _, problems = check(finding("/heavy-compute p95 1.5 s", ev(TRACE), status="confirmed"))
    assert {"invented_name", "sample_size"} <= problem_codes(problems)


def test_objective_topics():
    assert objective_topics("Is host CPU or memory utilization contributing?") == ("cpu", "memory")
    assert objective_topics("Find the failing span") == ()


def test_topic_answered_with_its_metric_evidence_passes():
    _, problems = check(
        finding("Latency on /slow (2 spans).", ev(TRACE)),
        # The CPU evidence supports the finding "CPU is not a contributor".
        finding("CPU stays near 0.9%; not a contributor.", ev(CPU)),
        topics=["cpu"],
    )
    assert problems == []


def test_topic_mentioned_without_its_evidence_is_flagged():
    _, problems = check(
        finding("Latency on /slow (2 spans); CPU looked fine.", ev(TRACE)), topics=["cpu"]
    )
    assert "topic_unanswered" in problem_codes(problems)


def test_unanswered_topic_gets_a_deterministic_inconclusive_finding():
    clean, problems = check(finding("Latency on /slow (2 spans).", ev(TRACE)), topics=["memory"])
    assert "topic_unanswered" in problem_codes(problems)
    topic = clean["findings"][-1]
    assert topic["title"] == "Host memory utilization: not assessed"
    assert topic["status"] == "inconclusive"
    assert topic["evidence"] == [{"evidence_id": str(MEM), "stance": "neutral"}]
    assert "memory mean 26%" in topic["summary"] and "was not assessed" in topic["summary"]


def test_unanswered_topic_without_evidence_says_not_observed():
    k = known(topics=["cpu"])
    facts = {e: f for e, f in k.facts.items() if e != CPU}
    import dataclasses

    k = dataclasses.replace(k, facts=facts)
    c = Conclusion(
        verdict="sufficient",
        gaps=[],
        hypothesis_updates=[update(H1, "active"), update(H2, "active")],
        findings=[finding("Latency on /slow (2 spans).", ev(TRACE))],
    )
    clean, _ = validate_conclusion(c, k)
    assert "Not observed" in clean["findings"][-1]["summary"]
    assert clean["findings"][-1]["evidence"] == []


# --- critical evidence survives clipped model context --------------------------------------


def _live_run_3_summary() -> str:
    from datetime import UTC, datetime

    from src.integrations.signoz import normalize
    from src.integrations.signoz.schemas import TimeRange
    from tests.signoz.test_normalize import LIVE_RUN_3_GROUPS, trace_stats

    window = TimeRange(
        start=datetime(2026, 10, 18, 2, 58, tzinfo=UTC),
        end=datetime(2026, 10, 18, 3, 28, tzinfo=UTC),
    )
    return normalize.summarize_trace_stats(trace_stats(LIVE_RUN_3_GROUPS), window)


def test_late_failing_operation_reaches_the_local_conclude_catalog():
    from datetime import UTC, datetime

    from src.investigators.context import evidence_lines
    from src.investigators.records import _clip

    now = datetime.now(UTC)
    catalog = [{"ref": "ev_8d479c9c", "own": True, "type": "trace", "title": "Trace stats",
                "summary": _clip(_live_run_3_summary()), "collected_at": now}]  # fmt: skip
    (line,) = evidence_lines(catalog, now)
    assert "[GET /error] 4 spans, 4 errors (100.0%)" in line
    assert "[GET /slow] 1 span" in line


def test_late_failing_operation_reaches_main_memory():
    from src.investigators.context import memory_text

    memory = {
        "findings": [], "hypotheses": [], "assignments": [], "environments": [],
        "evidence": [{"id": uuid.uuid4(), "environment": "ubuntu-vm", "type": "trace",
                      "title": "Trace stats", "summary": _live_run_3_summary()}],
    }  # fmt: skip
    text = memory_text(memory, policy.MEMORY_CHARS)
    assert "[GET /error] 4 spans, 4 errors (100.0%)" in text
