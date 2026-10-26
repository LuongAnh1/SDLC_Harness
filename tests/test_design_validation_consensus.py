from __future__ import annotations

import json

from sdlc_harness.agents.validator import DesignValidator, TraceabilityResult


class StaticLlmClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, *, system: str, prompt: str) -> str:
        return self.responses.pop(0)


def semantic_response(*findings: dict[str, object]) -> str:
    return json.dumps(
        {
            "ok": not any(item.get("severity") == "blocking" for item in findings),
            "summary": "test review",
            "findings": list(findings),
        }
    )


def finding(
    message: str,
    *,
    severity: str = "blocking",
    subject_ref: str = "sequence_diagram_1:msg-4",
    category: str = "behavior_order",
    requirement_ids: list[str] | None = None,
    diagram_ids: list[str] | None = None,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "id": "SEM-001",
        "category": category,
        "severity": severity,
        "message": message,
        "expected_change": "Move the guard before the destructive call.",
        "requirement_ids": requirement_ids if requirement_ids is not None else ["MAIN-1"],
        "diagram_ids": diagram_ids if diagram_ids is not None else ["sequence_diagram_1"],
        "subject_ref": subject_ref,
        "evidence": evidence
        if evidence is not None
        else [{"source": "design", "ref": subject_ref, "excerpt": "delete before guard"}],
    }


def empty_traceability() -> TraceabilityResult:
    return TraceabilityResult(
        requirement_graph={"requirements": [], "count": 0},
        class_trace=[],
        sequence_trace=[],
        missing_links={"class_diagram": [], "sequence_diagram": []},
    )


def review(responses: list[str]):
    return DesignValidator(StaticLlmClient(responses)).review(
        requirement_packet="approved requirement",
        design_artifact="approved design",
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=empty_traceability(),
        validation_trials=3,
    )


def test_two_of_three_blocker_reaches_quorum() -> None:
    result = review(
        [
            semantic_response(finding("Guard is too late.")),
            semantic_response(finding("The validation guard occurs after deletion.")),
            semantic_response(),
        ]
    )

    assert not result.ok
    assert len(result.blocking_findings) == 1
    assert result.blocking_findings[0].support_count == 2
    assert result.semantic_consensus["quorum"] == 2


def test_one_of_three_blocker_is_retained_as_disputed() -> None:
    result = review([semantic_response(finding("Guard is too late.")), semantic_response(), semantic_response()])

    assert result.ok
    assert not result.blocking_findings
    assert len(result.disputed_findings) == 1
    assert result.disputed_findings[0].severity == "blocking"


def test_mixed_severity_majority_is_consensus_warning() -> None:
    result = review(
        [
            semantic_response(finding("Guard is too late.")),
            semantic_response(finding("Guard ordering is ambiguous.", severity="warning")),
            semantic_response(),
        ]
    )

    assert result.ok
    assert len(result.warnings) == 1
    assert result.warnings[0].support_count == 2


def test_paraphrases_share_vote_through_stable_subject_ref() -> None:
    result = review(
        [
            semantic_response(finding("The cancel branch is nested incorrectly.")),
            semantic_response(finding("Cancellation cannot be reached for invalid input.")),
            semantic_response(),
        ]
    )

    assert len(result.blocking_findings) == 1
    assert result.blocking_findings[0].canonical_key.endswith("sequence_diagram_1:msg-4")


def test_reference_sets_are_audit_metadata_not_consensus_identity() -> None:
    result = review(
        [
            semantic_response(
                finding(
                    "Duplicate branch is implicit.",
                    severity="warning",
                    requirement_ids=["AC-1", "BR-1"],
                    diagram_ids=["sequence_sc_1", "sequence_sc_5"],
                    subject_ref="sequence_sc_1:fragment:0008",
                )
            ),
            semantic_response(
                finding(
                    "Duplicate result has no explicit alternative.",
                    severity="warning",
                    requirement_ids=["AC-1", "BR-1", "MF-4"],
                    diagram_ids=["sequence_sc_1"],
                    subject_ref="sequence_sc_1:fragment:0008",
                )
            ),
            semantic_response(),
        ]
    )

    assert len(result.warnings) == 1
    consensus = result.warnings[0]
    assert consensus.support_count == 2
    assert consensus.canonical_key == "behavior_order|sequence_sc_1:fragment:0008"
    assert consensus.requirement_ids == ["AC-1", "BR-1", "MF-4"]
    assert consensus.diagram_ids == ["sequence_sc_1", "sequence_sc_5"]
    assert consensus.evidence_signature.startswith("sha256:")


def test_same_location_with_different_category_does_not_reach_consensus() -> None:
    result = review(
        [
            semantic_response(
                finding(
                    "Terminal outcome is ambiguous.",
                    category="terminal_state",
                    subject_ref="sequence_sc_1:message:0011",
                )
            ),
            semantic_response(
                finding(
                    "The flow assumes a terminal outcome.",
                    category="unsupported_assumption",
                    subject_ref="sequence_sc_1:message:0011",
                )
            ),
            semantic_response(),
        ]
    )

    assert not result.blocking_findings
    assert not result.warnings
    assert len(result.disputed_findings) == 2


def test_same_category_at_different_locations_does_not_reach_consensus() -> None:
    result = review(
        [
            semantic_response(finding("First location.", subject_ref="sequence_sc_1:message:0001")),
            semantic_response(finding("Second location.", subject_ref="sequence_sc_1:message:0002")),
            semantic_response(),
        ]
    )

    assert not result.blocking_findings
    assert not result.warnings
    assert len(result.disputed_findings) == 2


def test_unlocated_findings_cannot_vote_across_trials() -> None:
    unlocated = finding(
        "No stable location.",
        subject_ref="",
        requirement_ids=["MAIN-1"],
        diagram_ids=[],
        evidence=[],
    )
    result = review([semantic_response(unlocated), semantic_response(unlocated), semantic_response()])

    assert not result.blocking_findings
    assert not result.warnings
    assert len(result.disputed_findings) == 2


def test_invalid_trial_makes_result_inconclusive() -> None:
    result = review([semantic_response(), "not-json", semantic_response()])

    assert result.inconclusive
    assert not result.ok
    assert result.semantic_consensus["valid_trials"] == 2


def test_all_clean_trials_pass_with_full_consistency() -> None:
    result = review([semantic_response(), semantic_response(), semantic_response()])

    assert result.ok
    assert result.semantic_consensus["consistency_score"] == 1.0
    assert all(item["jaccard"] == 1.0 for item in result.semantic_consensus["pairwise_jaccard"])


def test_deterministic_blocker_is_never_voted_away() -> None:
    traceability = TraceabilityResult(
        requirement_graph={"requirements": [{"id": "MAIN-1", "content": "Required"}], "count": 1},
        class_trace=[],
        sequence_trace=[],
        missing_links={
            "class_diagram": [{"requirement": "MAIN-1", "content": "Required"}],
            "sequence_diagram": [],
        },
    )
    result = DesignValidator(
        StaticLlmClient([semantic_response(), semantic_response(), semantic_response()])
    ).review(
        requirement_packet="approved requirement",
        design_artifact="approved design",
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=traceability,
        validation_trials=3,
    )

    assert not result.ok
    assert any(item.origin == "deterministic" for item in result.blocking_findings)
