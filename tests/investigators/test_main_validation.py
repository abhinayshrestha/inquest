import uuid

import pytest

from src.investigators import policy
from src.investigators.main_validation import build_memory, validate_answer, validate_verdict
from src.investigators.refs import ref
from src.investigators.schemas import (
    AnswerDraft,
    AssignmentRequest,
    ReportConclusion,
    ReportDraft,
    TimelineEntry,
    Verdict,
)
from src.investigators.validation import problem_codes

ENV, EV_TRACE, EV_CPU, FD_OK, FD_PRE, FD_BAD, HY = (uuid.uuid4() for _ in range(7))


def memory(*, empty: bool = False):
    raw = {
        "environments": [{"id": ENV, "name": "ubuntu-vm", "type": "local", "scope": {}}],
        "assignments": [{"round": 1, "environment": "ubuntu-vm", "status": "completed",
                         "objective": "latency"}],
        "findings": [] if empty else [
            {"id": FD_OK, "environment": "ubuntu-vm", "status": "confirmed",
             "title": "GET /slow is slow", "summary": "p95 1.5 s (62 spans)",
             "evidence": [(EV_TRACE, "supports")]},
            {"id": FD_PRE, "environment": "ubuntu-vm", "status": "preliminary",
             "title": "Maybe middleware", "summary": "s", "evidence": [(EV_TRACE, "supports")]},
            {"id": FD_BAD, "environment": "ubuntu-vm", "status": "inconclusive",
             "title": "Logs unknown", "summary": "s", "evidence": []},
        ],
        "hypotheses": [] if empty else [
            {"id": HY, "environment": "ubuntu-vm", "status": "supported", "statement": "slow"}
        ],
        "evidence": [] if empty else [
            {"id": EV_TRACE, "environment": "ubuntu-vm", "type": "trace",
             "title": "Trace stats", "summary": "[GET /slow] 62 spans, 0 errors"},
            {"id": EV_CPU, "environment": "ubuntu-vm", "type": "metric",
             "title": "cpu_utilization", "summary": "mean 0.9%"},
        ],
    }  # fmt: skip
    return build_memory(raw)


def verdict(decision="investigate", *assignments, gaps=()):
    return Verdict(
        decision=decision,
        rationale="r",
        gaps=list(gaps),
        assignments=[AssignmentRequest(environment=e, objective=o) for e, o in assignments],
    )


# --- verdict -------------------------------------------------------------------------------


def test_assignments_resolve_to_scoped_environments():
    clean, problems = validate_verdict(
        verdict("investigate", ("Ubuntu-VM", "Find slow spans"), ("aws", "x"), ("ubuntu-vm", "y")),
        memory(),
        question="Why slow?",
        rounds=0,
    )
    assert clean["assignments"] == [{"environment_id": str(ENV), "objective": "Find slow spans"}]
    assert {"unknown_environment", "duplicate_assignment"} <= problem_codes(problems)


def test_empty_memory_always_investigates_first():
    clean, problems = validate_verdict(
        verdict("answer", gaps=["latency data"]), memory(empty=True), question="Why?", rounds=0
    )
    assert clean["decision"] == "investigate"
    (assignment,) = clean["assignments"]
    assert assignment["environment_id"] == str(ENV)
    assert assignment["objective"] == "Why?\nFocus: latency data"
    assert "no_memory" in problem_codes(problems)


def test_investigate_without_valid_assignments_falls_back_to_scope():
    clean, problems = validate_verdict(
        verdict("investigate", ("nowhere", "x")), memory(), question="Why?", rounds=1
    )
    assert [a["environment_id"] for a in clean["assignments"]] == [str(ENV)]
    assert "no_assignments" in problem_codes(problems)


def test_round_budget_turns_investigate_into_a_limited_answer():
    clean, problems = validate_verdict(
        verdict("investigate", ("ubuntu-vm", "more")),
        memory(),
        question="Why?",
        rounds=policy.MAIN_MAX_ROUNDS,
    )
    assert (clean["decision"], clean["limited"], clean["assignments"]) == ("answer", True, [])
    assert problems == []


def test_answer_with_memory_is_accepted():
    clean, problems = validate_verdict(verdict("answer"), memory(), question="Why?", rounds=1)
    assert clean["decision"] == "answer" and problems == []


# --- answer and report ---------------------------------------------------------------------


def conclusion(
    status, supporting=(), contradicting=(), statement="GET /slow has the highest latency"
):
    return ReportConclusion(
        statement=statement,
        status=status,
        confidence="high",
        supporting=list(supporting),
        contradicting=list(contradicting),
    )


def answer(*conclusions, outcome="root_cause_identified", text=None, report=True, timeline=()):
    return AnswerDraft(
        answer_md=text if text is not None else f"Slow route {ref('finding', FD_OK)}.",
        report=ReportDraft(
            outcome=outcome,
            summary="s",
            conclusions=list(conclusions),
            timeline=list(timeline),
            gaps=[],
            next_checks=[],
        )
        if report
        else None,
    )


def check(draft, **kw):
    return validate_answer(draft, memory(), report_required=True, **kw)


def test_confirmed_observation_stands_but_does_not_identify_a_root_cause():
    clean, problems = check(answer(conclusion("confirmed", [ref("finding", FD_OK)])))
    assert problem_codes(problems) == {"unsupported_outcome"}
    assert clean["report"]["outcome"] == "inconclusive"
    (c,) = clean["report"]["conclusions"]
    assert c["status"] == "confirmed"
    assert c["supporting"] == [{"ref": ref("finding", FD_OK), "kind": "finding", "id": str(FD_OK)}]
    assert clean["refs"] == [{"ref": ref("finding", FD_OK), "kind": "finding", "id": str(FD_OK)}]


def test_confirmed_on_a_preliminary_finding_becomes_probable_and_outcome_follows():
    clean, problems = check(answer(conclusion("confirmed", [ref("finding", FD_PRE)])))
    assert {"unsupported_confirmed", "unsupported_outcome"} <= problem_codes(problems)
    assert clean["report"]["conclusions"][0]["status"] == "probable"
    # A probable observation is still no cause.
    assert clean["report"]["outcome"] == "inconclusive"


def test_inconclusive_findings_and_hypotheses_support_nothing():
    clean, problems = check(
        answer(conclusion("probable", [ref("finding", FD_BAD), ref("hypothesis", HY)]))
    )
    assert {"unsupported_probable", "hypothesis_as_support"} <= problem_codes(problems)
    assert clean["report"]["conclusions"][0]["status"] == "unverified"
    assert clean["report"]["outcome"] == "inconclusive"


def test_invented_refs_are_removed_everywhere():
    clean, problems = check(
        answer(
            conclusion("confirmed", ["ev_deadbeef", ref("evidence", EV_TRACE)]),
            text="See ev_deadbeef and 【fd_00000000】.",
            timeline=[TimelineEntry(at="14:00", event="spike ev_deadbeef", refs=["fd_badbad00"])],
        )
    )
    assert "unknown_ref" in problem_codes(problems)
    assert "ev_deadbeef" not in clean["answer_md"] and "(no such ref)" in clean["answer_md"]
    assert clean["report"]["conclusions"][0]["status"] == "confirmed"  # real evidence remains
    assert clean["report"]["timeline"][0]["refs"] == []
    assert "uncited_answer" in problem_codes(problems)


def test_contradicted_confirmed_is_probable_and_ruled_out_needs_a_contradiction():
    clean, problems = check(
        answer(
            conclusion("confirmed", [ref("evidence", EV_TRACE)], [ref("evidence", EV_CPU)]),
            conclusion("ruled_out", statement="CPU is not the cause"),
        )
    )
    statuses = [c["status"] for c in clean["report"]["conclusions"]]
    assert statuses == ["probable", "unverified"]
    assert {"confirmed_contradicted", "unsupported_ruled_out"} <= problem_codes(problems)


def test_absence_and_invented_names_are_unverified():
    clean, problems = check(
        answer(
            conclusion("probable", [ref("evidence", EV_CPU)], statement="There are no errors"),
            conclusion("confirmed", [ref("evidence", EV_TRACE)], statement="/heavy is slow"),
            outcome="probable_cause",
        )
    )
    assert [c["status"] for c in clean["report"]["conclusions"]] == ["unverified", "unverified"]
    assert {"absence_claim", "invented_name"} <= problem_codes(problems)


def test_absence_backed_by_trace_evidence_is_allowed():
    _, problems = check(
        answer(
            conclusion("confirmed", [ref("evidence", EV_TRACE)], statement="Spans show no errors"),
            outcome="inconclusive",
        )
    )
    assert problems == []


def test_initial_turn_needs_a_report_and_gets_a_safe_one():
    clean, problems = check(answer(report=False))
    assert "report_required" in problem_codes(problems)
    assert clean["report"]["outcome"] == "inconclusive"
    clean, problems = validate_answer(answer(report=False), memory(), report_required=False)
    assert clean["report"] is None and problems == []


@pytest.mark.parametrize("clarify", [True, False])
def test_uncited_answer_rule(clarify):
    _, problems = check(answer(conclusion("unverified"), outcome="inconclusive", text="Which API?"),
                        clarify=clarify)  # fmt: skip
    assert ("uncited_answer" in problem_codes(problems)) is (not clarify)


# --- Phase 4: freshness and report revisions -----------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402

from src.investigators.main_validation import report_diff  # noqa: E402
from src.investigators.windows import Window, covers, requested_window  # noqa: E402

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("question", "minutes"),
    [
        ("Any errors in the last 30 minutes?", 30),
        ("What happened over the past hour?", 60),
        ("last 2 days", 2880),
        ("Is it still failing right now?", 15),
        ("Is it currently slow?", 15),
        ("Last 45 mins only", 45),
    ],
)
def test_explicit_windows(question, minutes):
    w = requested_window(question, NOW)
    assert w is not None and (w.end, w.end - w.start) == (NOW, timedelta(minutes=minutes))


def test_no_window_stated():
    assert requested_window("Is CPU involved?", NOW) is None


def test_lookback_is_capped_at_the_executor_limit():
    w = requested_window("in the last 30 days", NOW)
    assert w.end - w.start == timedelta(days=7)


def test_coverage_needs_an_evidence_window_spanning_the_request():
    w = Window(NOW - timedelta(minutes=30), NOW, "last 30 minutes")
    fresh = (NOW - timedelta(minutes=31), NOW - timedelta(minutes=2))
    old = (NOW - timedelta(hours=3), NOW - timedelta(hours=2))
    partial = (NOW - timedelta(minutes=10), NOW)
    assert covers(w, [fresh]) and not covers(w, [old]) and not covers(w, [partial, old])


def test_stale_memory_forces_investigation_before_the_first_round_only():
    w = Window(NOW - timedelta(minutes=15), NOW, "now")
    clean, problems = validate_verdict(
        verdict("answer"), memory(), question="Still slow right now?", rounds=0,
        window=w, covered=False,
    )  # fmt: skip
    assert clean["decision"] == "investigate" and "stale_memory" in problem_codes(problems)
    assert (
        f"Time window (UTC): {w.text()}. Query this whole window"
        in (clean["assignments"][0]["objective"])
    )
    # After a round the model's judgement stands: no loop on a window mismatch.
    clean, problems = validate_verdict(
        verdict("answer"), memory(), question="q", rounds=1, window=w, covered=False
    )
    assert clean["decision"] == "answer" and problems == []
    # Covered: answering from memory is fine.
    clean, _ = validate_verdict(
        verdict("answer"), memory(), question="q", rounds=0, window=w, covered=True
    )
    assert clean["decision"] == "answer"


def _report(outcome="probable_cause", *conclusions):
    return {"outcome": outcome, "conclusions": [
        {"statement": s, "status": st} for s, st in conclusions
    ]}  # fmt: skip


def test_report_diff_ignores_wording_and_unverified_additions():
    old = _report("probable_cause", ("GET /slow is slow", "probable"))
    same = _report("probable_cause", ("get /slow   is slow", "probable"), ("maybe X", "unverified"))
    assert report_diff(same, old)["material"] is False


@pytest.mark.parametrize(
    ("new", "field"),
    [
        (_report("root_cause_identified", ("GET /slow is slow", "probable")), "outcome"),
        (_report("probable_cause", ("GET /slow is slow", "confirmed")), "status_changed"),
        (_report("probable_cause", ("GET /slow is slow", "probable"), ("CPU", "ruled_out")),
         "added"),
    ],
)  # fmt: skip
def test_report_diff_detects_material_changes(new, field):
    old = _report("probable_cause", ("GET /slow is slow", "probable"))
    diff = report_diff(new, old)
    assert diff["material"] is True
    assert (
        diff[field] if field != "outcome" else diff["outcome"] == ["probable_cause", new["outcome"]]
    )


def test_first_report_is_always_material():
    assert report_diff(_report(), None)["material"] is True
