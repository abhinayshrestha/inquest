from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    Environment,
    EnvironmentType,
    Evidence,
    EvidenceStance,
    EvidenceType,
    Finding,
    FindingEvidence,
    FindingStatus,
    Hypothesis,
    HypothesisEvidence,
    HypothesisStatus,
    ImmutableRecordError,
    Investigation,
    InvestigationAssignment,
)
from tests.records import add_assignment  # noqa: E402

KINDS = ["hypothesis", "finding"]


async def _assignment(
    session: AsyncSession,
    investigation: Investigation | None = None,
    environment: Environment | None = None,
    round: int = 1,
) -> InvestigationAssignment:
    assignment = await add_assignment(
        session, investigation=investigation, environment=environment, round=round
    )
    await session.flush()
    return assignment


def _evidence(assignment: InvestigationAssignment) -> Evidence:
    return Evidence(
        assignment=assignment,
        evidence_type=EvidenceType.TRACE,
        source_provider="signoz",
        source_reference="trace:4bf92f3577b34da6a3ce929d0e0e4736",
        title="Slowest /checkout trace spent 2140 ms in pg.query",
        summary="Trace 4bf92f35 took 2310 ms; span pg.query took 2140 ms of it.",
    )


def _claim(
    kind: str, assignment: InvestigationAssignment, **overrides: Any
) -> Hypothesis | Finding:
    if kind == "hypothesis":
        return Hypothesis(
            assignment=assignment,
            statement="Slow /checkout requests are dominated by a slow orders query",
            **overrides,
        )
    return Finding(
        assignment=assignment,
        title="Slow pg.query span dominates the slowest /checkout trace",
        summary="In the slowest sampled trace, pg.query took 2140 of 2310 ms.",
        **overrides,
    )


def _link(
    claim: Hypothesis | Finding,
    evidence: Evidence,
    stance: EvidenceStance = EvidenceStance.SUPPORTS,
) -> HypothesisEvidence | FindingEvidence:
    if isinstance(claim, Hypothesis):
        return HypothesisEvidence(hypothesis=claim, evidence=evidence, stance=stance)
    return FindingEvidence(finding=claim, evidence=evidence, stance=stance)


async def test_scope_is_copied_from_assignment(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    hypothesis = _claim("hypothesis", assignment)
    finding = _claim("finding", assignment)
    evidence = _evidence(assignment)
    session.add_all([hypothesis, finding, evidence])
    await session.flush()

    for record in (hypothesis, finding, evidence):
        assert record.investigation_id == assignment.investigation_id
        assert record.environment_id == assignment.environment_id
    assert hypothesis.status is HypothesisStatus.ACTIVE
    assert finding.status is FindingStatus.PRELIMINARY
    assert hypothesis.confidence is None


async def test_scope_must_match_assignment(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    other = await _assignment(session)
    session.add(
        Hypothesis(
            assignment_id=assignment.id,
            investigation_id=other.investigation_id,
            environment_id=assignment.environment_id,
            statement="mismatched scope",
        )
    )

    with pytest.raises(IntegrityError, match="fk_hypotheses_assignment_id_investigation"):
        await session.flush()


@pytest.mark.parametrize("kind", KINDS)
async def test_link_evidence_from_earlier_round_same_environment(
    session: AsyncSession, kind: str
) -> None:
    round_1 = await _assignment(session)
    round_2 = await _assignment(
        session, investigation=round_1.investigation, environment=round_1.environment, round=2
    )
    link = _link(_claim(kind, round_2), _evidence(round_1))
    session.add(link)
    await session.flush()

    assert link.investigation_id == round_1.investigation_id
    assert link.created_at.tzinfo is not None


@pytest.mark.parametrize("kind", KINDS)
async def test_link_evidence_from_other_environment_is_rejected(
    session: AsyncSession, kind: str
) -> None:
    ubuntu = await _assignment(session)
    aws = await _assignment(session, investigation=ubuntu.investigation)
    session.add(_link(_claim(kind, ubuntu), _evidence(aws)))

    with pytest.raises(IntegrityError, match=f"fk_{kind}_evidence_evidence_id_evidence"):
        await session.flush()


@pytest.mark.parametrize("kind", KINDS)
async def test_link_evidence_from_other_investigation_is_rejected(
    session: AsyncSession, kind: str
) -> None:
    mine = await _assignment(session)
    theirs = await _assignment(session, environment=mine.environment)
    session.add(_link(_claim(kind, mine), _evidence(theirs)))

    with pytest.raises(IntegrityError, match=f"fk_{kind}_evidence_evidence_id_evidence"):
        await session.flush()


async def test_finding_hypothesis_must_share_assignment(session: AsyncSession) -> None:
    round_1 = await _assignment(session)
    round_2 = await _assignment(
        session, investigation=round_1.investigation, environment=round_1.environment, round=2
    )
    hypothesis = _claim("hypothesis", round_1)
    session.add(_claim("finding", round_1, hypothesis=hypothesis))
    await session.flush()

    # Same investigation and environment is not enough: the hypothesis is round 1's.
    session.add(_claim("finding", round_2, hypothesis=hypothesis))
    with pytest.raises(IntegrityError, match="fk_findings_hypothesis_id_hypotheses"):
        await session.flush()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("confidence", [0.0, 1.0])
async def test_confidence_bounds_allowed(
    session: AsyncSession, kind: str, confidence: float
) -> None:
    session.add(_claim(kind, await _assignment(session), confidence=confidence))
    await session.flush()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("confidence", [-0.01, 1.01])
async def test_confidence_out_of_range_is_rejected(
    session: AsyncSession, kind: str, confidence: float
) -> None:
    session.add(_claim(kind, await _assignment(session), confidence=confidence))

    table = "hypotheses" if kind == "hypothesis" else "findings"
    with pytest.raises(IntegrityError, match=f"ck_{table}_confidence_range"):
        await session.flush()


@pytest.mark.parametrize(
    ("kind", "overrides", "constraint"),
    [
        ("hypothesis", {"statement": "x" * 1_001}, "ck_hypotheses_statement_length"),
        ("finding", {"summary": "x" * 4_001}, "ck_findings_summary_length"),
    ],
)
async def test_text_length_limits(
    session: AsyncSession, kind: str, overrides: dict, constraint: str
) -> None:
    assignment = await _assignment(session)
    claim = _claim(kind, assignment)
    for field, value in overrides.items():
        setattr(claim, field, value)
    session.add(claim)

    with pytest.raises(IntegrityError, match=constraint):
        await session.flush()


async def test_hypothesis_statement_is_immutable(session: AsyncSession) -> None:
    hypothesis = _claim("hypothesis", await _assignment(session))
    session.add(hypothesis)
    await session.flush()

    hypothesis.statement = "a different claim"
    with pytest.raises(ImmutableRecordError):
        await session.flush()


async def test_hypothesis_status_and_confidence_can_evolve(session: AsyncSession) -> None:
    hypothesis = _claim("hypothesis", await _assignment(session))
    session.add(hypothesis)
    await session.flush()

    hypothesis.status = HypothesisStatus.SUPPORTED
    hypothesis.confidence = 0.6
    await session.flush()


@pytest.mark.parametrize("kind", KINDS)
async def test_evidence_links_are_insert_only(session: AsyncSession, kind: str) -> None:
    assignment = await _assignment(session)
    link = _link(_claim(kind, assignment), _evidence(assignment))
    session.add(link)
    await session.flush()

    link.stance = EvidenceStance.CONTRADICTS
    with pytest.raises(ImmutableRecordError):
        await session.flush()


@pytest.mark.parametrize("kind", KINDS)
async def test_deleting_linked_evidence_is_restricted(session: AsyncSession, kind: str) -> None:
    assignment = await _assignment(session)
    evidence = _evidence(assignment)
    session.add(_link(_claim(kind, assignment), evidence))
    await session.flush()

    await session.delete(evidence)
    with pytest.raises(IntegrityError, match=f"fk_{kind}_evidence_evidence_id_evidence"):
        await session.flush()


@pytest.mark.parametrize(
    ("make_dependent", "constraint"),
    [
        (
            lambda h, a: _link(h, _evidence(a)),
            "fk_hypothesis_evidence_hypothesis_id_hypotheses",
        ),
        (lambda h, a: _claim("finding", a, hypothesis=h), "fk_findings_hypothesis_id_hypotheses"),
    ],
)
async def test_deleting_referenced_hypothesis_is_restricted(
    session: AsyncSession, make_dependent, constraint: str
) -> None:
    assignment = await _assignment(session)
    hypothesis = _claim("hypothesis", assignment)
    session.add(make_dependent(hypothesis, assignment))
    await session.flush()

    await session.delete(hypothesis)
    with pytest.raises(IntegrityError, match=constraint):
        await session.flush()


async def test_deleting_assignment_with_reasoning_is_restricted(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    session.add(_claim("hypothesis", assignment))
    await session.flush()

    await session.delete(assignment)
    with pytest.raises(IntegrityError, match="fk_hypotheses_assignment_id_investigation"):
        await session.flush()


async def test_inquest_demo_finding_cites_supporting_and_contradicting_evidence(
    session: AsyncSession,
) -> None:
    assignment = await _assignment(
        session, environment=Environment(name="ubuntu-vm", type=EnvironmentType.LOCAL)
    )
    slow_trace = _evidence(assignment)
    cpu = Evidence(
        assignment=assignment,
        evidence_type=EvidenceType.METRIC,
        source_provider="signoz",
        title="ubuntu-vm CPU peaked at 34%",
        summary="Host CPU utilization on ubuntu-vm peaked at 34% between 01:00 and 01:30 UTC.",
        entity_type="host",
        entity_identifier="ubuntu-vm",
    )
    db_query = Hypothesis(
        assignment=assignment,
        statement="Slow /checkout requests on inquest-demo are dominated by a slow orders query",
        status=HypothesisStatus.SUPPORTED,
        confidence=0.6,
    )
    cpu_saturation = Hypothesis(
        assignment=assignment,
        statement="CPU saturation on ubuntu-vm slowed inquest-demo",
        status=HypothesisStatus.REJECTED,
        confidence=0.1,
    )
    finding = Finding(
        assignment=assignment,
        hypothesis=db_query,
        title="Slow pg.query span dominates the slowest /checkout trace",
        summary=(
            "In the slowest sampled trace, pg.query took 2140 of 2310 ms. Host CPU peaked at "
            "34% in the same window, so CPU saturation is not supported."
        ),
        confidence=0.6,
    )
    session.add_all(
        [
            _link(db_query, slow_trace),
            _link(db_query, cpu, EvidenceStance.NEUTRAL),
            _link(cpu_saturation, cpu, EvidenceStance.CONTRADICTS),
            _link(finding, slow_trace),
            _link(finding, cpu, EvidenceStance.CONTRADICTS),
        ]
    )
    await session.flush()
    session.expunge_all()

    loaded = await session.scalar(
        select(Finding).options(
            selectinload(Finding.hypothesis),
            selectinload(Finding.evidence_links).selectinload(FindingEvidence.evidence),
        )
    )

    assert loaded is not None
    assert loaded.status is FindingStatus.PRELIMINARY
    assert loaded.hypothesis is not None
    assert loaded.hypothesis.status is HypothesisStatus.SUPPORTED
    stances = {link.evidence.evidence_type: link.stance for link in loaded.evidence_links}
    assert stances == {
        EvidenceType.TRACE: EvidenceStance.SUPPORTS,
        EvidenceType.METRIC: EvidenceStance.CONTRADICTS,
    }
