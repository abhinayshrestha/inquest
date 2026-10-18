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
        default=None, description="Hypothesis this finding resolves, if any"
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
