"""Structured outputs requested from the Local Investigator model (json_schema method).

These validate shape only. Semantic checks (refs exist, conclusions are supported) live in
validation.py, because a schema-valid answer can still cite invented evidence.
"""

from typing import Literal

from pydantic import BaseModel, Field

from src.models.evidence import MAX_SUMMARY_CHARS
from src.models.hypothesis import MAX_STATEMENT_CHARS

Stance = Literal["supports", "contradicts", "neutral"]


class HypothesisDraft(BaseModel):
    statement: str = Field(
        min_length=1,
        max_length=MAX_STATEMENT_CHARS,
        description="One testable claim about the cause, e.g. 'GET /checkout is slow because "
        "its database span dominates latency'",
    )
    test_plan: str = Field(
        max_length=500, description="Which telemetry would support or contradict this claim"
    )


class HypothesisPlan(BaseModel):
    hypotheses: list[HypothesisDraft] = Field(description="1 to 5 competing hypotheses")


class EvidenceLink(BaseModel):
    evidence_ref: str = Field(description="Evidence ref exactly as shown, e.g. ev_1a2b3c4d")
    stance: Stance


class HypothesisUpdate(BaseModel):
    hypothesis_ref: str = Field(description="Hypothesis ref exactly as shown, e.g. hy_1a2b3c4d")
    status: Literal["active", "supported", "weakened", "rejected"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[EvidenceLink] = Field(
        description="supported needs a supporting link; weakened/rejected a contradicting one"
    )


class FindingDraft(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    status: Literal["preliminary", "confirmed", "inconclusive"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    hypothesis_ref: str | None = Field(
        default=None, description="Hypothesis this finding bears on, if any"
    )
    claim: Literal["observation", "temporal_overlap", "correlation", "causal"] = Field(
        default="observation",
        description="observation: separate facts; temporal_overlap: they happened in the "
        "same window (nothing more); correlation: they vary together over time; causal: one "
        "drives the other",
    )
    hypothesis_relation: Literal["supports", "contradicts", "inconclusive"] | None = Field(
        default=None,
        description="With hypothesis_ref: supports (confirms it), contradicts (rules it out), "
        "or inconclusive",
    )
    evidence: list[EvidenceLink] = Field(
        description="preliminary/confirmed need a supporting link; include contradicting ones"
    )


class Conclusion(BaseModel):
    verdict: Literal["sufficient", "need_more"] = Field(
        description="need_more only if a specific, still-unchecked query could change findings"
    )
    gaps: list[str] = Field(description="Specific missing evidence; empty when sufficient")
    hypothesis_updates: list[HypothesisUpdate]
    findings: list[FindingDraft] = Field(description="1 to 5 findings")


# --- Main Investigator ---------------------------------------------------------------------


class AssignmentRequest(BaseModel):
    environment: str = Field(description="Environment name exactly as listed")
    objective: str = Field(
        min_length=1,
        max_length=2000,
        description="Targeted objective: what to find out, which signals and time window, "
        "which hypotheses to test. Do not repeat work already in memory.",
    )


class Verdict(BaseModel):
    decision: Literal["answer", "investigate", "clarify"]
    rationale: str = Field(max_length=1000, description="One or two sentences")
    gaps: list[str] = Field(description="Specific evidence the memory lacks")
    assignments: list[AssignmentRequest] = Field(description="Only when decision=investigate")


class ReportConclusion(BaseModel):
    key: str | None = Field(
        default=None,
        description="Stable short id (e.g. slow-endpoint-latency); reuse the key from the "
        "current report for the same conclusion",
    )
    claim: Literal["observation", "temporal_overlap", "correlation", "causal"] = Field(
        default="observation",
        description="Same window is temporal_overlap only; correlation and causal need "
        "evidence that shows them",
    )
    statement: str = Field(min_length=1, max_length=1000)
    status: Literal["confirmed", "probable", "ruled_out", "unverified"]
    confidence: Literal["low", "medium", "high"]
    supporting: list[str] = Field(description="Evidence or finding refs (ev_..., fd_...)")
    contradicting: list[str] = Field(description="Evidence or finding refs against it")


class TimelineEntry(BaseModel):
    at: str = Field(max_length=64, description="UTC time or window, as in the evidence")
    event: str = Field(min_length=1, max_length=500)
    refs: list[str]


class ReportChange(BaseModel):
    change: Literal["added", "revised", "retracted"]
    key: str | None = Field(default=None, description="Key of the conclusion it concerns")
    statement: str = Field(min_length=1, max_length=1000, description="The conclusion")
    reason: str = Field(max_length=1000, description="What new evidence changed it")
    refs: list[str] = Field(description="Evidence or finding refs behind the change")


class ReportDraft(BaseModel):
    outcome: Literal["root_cause_identified", "probable_cause", "inconclusive"]
    summary: str = Field(min_length=1, max_length=4000)
    conclusions: list[ReportConclusion]
    timeline: list[TimelineEntry]
    gaps: list[str] = Field(description="What could not be verified and why")
    next_checks: list[str]
    changes: list[ReportChange] = Field(
        default_factory=list, description="For a revision: what changed since the last report"
    )


class AnswerDraft(BaseModel):
    answer_md: str = Field(
        min_length=1, max_length=8000, description="Answer to the user, citing refs inline"
    )
    report: ReportDraft | None = Field(
        description="Required for the initial question; for a follow-up only if the "
        "investigation's understanding changed, otherwise null"
    )
