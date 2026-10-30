from __future__ import annotations

import json
from types import SimpleNamespace

from sdlc_harness.agents.validator import DesignValidator, TraceabilityResult
from sdlc_harness.core.config import LlmCallProfile
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion


class StaticLlmClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class MetadataLlmClient:
    def __init__(self):
        self.calls: list[dict[str, object]] = []
        self.settings = SimpleNamespace(
            llm_structured_max_tokens=8192,
            phase2_llm_profiles={
                "design_validator": LlmCallProfile(
                    model="validator-model",
                    thinking_effort="high",
                    max_tokens=32768,
                    timeout_seconds=300,
                )
            },
        )

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        return LlmCompletion(content=semantic_response())


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


def strict_review_inputs() -> tuple[dict[str, object], dict[str, object]]:
    canonical = {
        "use_case": {"id": "UC-1", "name": "Delete item"},
        "scenario_contract": {
            "scenarios": [
                {
                    "id": "SC-1",
                    "name": "Delete after confirmation",
                    "kind": "main",
                    "requirement_refs": ["MF-1"],
                    "acceptance_refs": ["AC-1"],
                    "surfaces": {
                        "ui": {
                            "status": "required",
                            "reason": "The operator confirms deletion.",
                            "requirement_refs": ["MF-1", "AC-1"],
                        }
                    },
                }
            ]
        },
        "flows": {"main": [{"id": "MF-1", "action": "Confirm and delete the item."}]},
        "acceptance_criteria": [{"id": "AC-1", "text": "The confirmed item is deleted."}],
        "preconditions": [],
        "postconditions": [],
        "quality_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "decisions": [],
    }
    graph = {
        "artifact_digest": "sha256:design",
        "nodes": [
            {
                "id": "VDN-ui",
                "scenario_id": "SC-1",
                "component_id": "web",
                "surface": "ui",
                "kind": "ui_interaction",
                "requirement_refs": ["MF-1", "AC-1"],
                "surface_contract": {"transitions": [{"action": "confirm deletion"}]},
            }
        ],
        "edges": [],
        "unresolved_items": [],
    }
    return canonical, graph


def strict_assessments(*, design_ref: str = "VDN-ui") -> list[dict[str, object]]:
    return [
        {
            "claim_id": claim_id,
            "status": "satisfied",
            "design_refs": [design_ref],
            "reason": "The UI contract represents the approved behavior.",
        }
        for claim_id in ("SC-1:MF-1", "SC-1:AC-1", "SC-1:surface:ui")
    ]


def review(responses: list[str]):
    return DesignValidator(StaticLlmClient(responses)).review(
        requirement_packet="approved requirement",
        design_artifact="approved design",
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=empty_traceability(),
        validation_trials=3,
    )


def test_semantic_validator_uses_its_phase2_llm_profile() -> None:
    client = MetadataLlmClient()

    result = DesignValidator(client).review(
        requirement_packet="approved requirement",
        design_artifact="approved design",
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=empty_traceability(),
        validation_trials=1,
    )

    assert result.ok
    assert len(client.calls) == 1
    assert {
        key: client.calls[0][key]
        for key in ("model", "reasoning_effort", "max_tokens", "timeout_seconds")
    } == {
        "model": "validator-model",
        "reasoning_effort": "high",
        "max_tokens": 32768,
        "timeout_seconds": 300,
    }
    prompt = str(client.calls[0]["prompt"])
    assert "neither the UI contract nor the interaction path represents that confirmation" in prompt
    assert "Do not require a preliminary backend verification round trip" in prompt
    assert "return-to-human and separate confirmation interaction" not in prompt


def test_semantic_validator_uses_compact_canonical_source_order_and_claims() -> None:
    canonical, graph = strict_review_inputs()
    client = StaticLlmClient(
        [
            json.dumps(
                {
                    "ok": True,
                    "summary": "all claims covered",
                    "assessments": strict_assessments(),
                    "findings": [],
                }
            )
        ]
    )

    result = DesignValidator(client).review(
        requirement_packet="REQUIREMENT_PACKET_SENTINEL",
        design_artifact="DESIGN_MARKDOWN_SENTINEL",
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        traceability=empty_traceability(),
        canonical_requirement=canonical,
        vertical_design_graph=graph,
        validation_trials=1,
    )

    assert result.ok
    prompt = client.prompts[0]
    assert "Source-of-truth order:" in prompt
    assert "Canonical Requirement review packet:" in prompt
    assert "Canonical Vertical Design Graph semantic view:" in prompt
    assert "REQUIREMENT_PACKET_SENTINEL" not in prompt
    assert "DESIGN_MARKDOWN_SENTINEL" not in prompt
    assert "Compatibility trace summary:" not in prompt


def test_semantic_validator_rejects_missing_claim_or_unknown_design_ref() -> None:
    canonical, graph = strict_review_inputs()
    missing_claim = StaticLlmClient(
        [
            json.dumps(
                {
                    "ok": True,
                    "summary": "incomplete",
                    "assessments": strict_assessments()[:-1],
                    "findings": [],
                }
            )
        ]
    )
    unknown_ref = StaticLlmClient(
        [
            json.dumps(
                {
                    "ok": True,
                    "summary": "bad ref",
                    "assessments": strict_assessments(design_ref="VDN-invented"),
                    "findings": [],
                }
            )
        ]
    )

    for client in (missing_claim, unknown_ref):
        try:
            DesignValidator(client).review(
                requirement_packet="approved requirement",
                design_artifact="approved design",
                class_graph={"nodes": []},
                sequence_graph={"sequenceDiagrams": []},
                traceability=empty_traceability(),
                canonical_requirement=canonical,
                vertical_design_graph=graph,
                validation_trials=1,
            )
        except HarnessError as exc:
            assert exc.error_code == "design_semantic_review_failed"
        else:
            raise AssertionError("Invalid semantic assessment contract was accepted.")


def test_semantic_validator_rejects_finding_with_invented_requirement_ref() -> None:
    canonical, graph = strict_review_inputs()
    assessments = strict_assessments()
    assessments[0] = {**assessments[0], "status": "contradicted"}
    client = StaticLlmClient(
        [
            json.dumps(
                {
                    "ok": False,
                    "summary": "invented evidence",
                    "assessments": assessments,
                    "findings": [
                        {
                            "id": "SEM-001",
                            "category": "behavior_order",
                            "severity": "blocking",
                            "message": "The behavior is contradicted.",
                            "expected_change": "Correct the behavior.",
                            "claim_ids": ["SC-1:MF-1"],
                            "requirement_ids": ["REQ-INVENTED"],
                            "diagram_ids": [],
                            "subject_ref": "VDN-ui",
                            "evidence": [
                                {
                                    "source": "requirement",
                                    "ref": "REQ-INVENTED",
                                    "excerpt": "invented",
                                }
                            ],
                        }
                    ],
                }
            )
        ]
    )

    try:
        DesignValidator(client).review(
            requirement_packet="approved requirement",
            design_artifact="approved design",
            class_graph={"nodes": []},
            sequence_graph={"sequenceDiagrams": []},
            traceability=empty_traceability(),
            canonical_requirement=canonical,
            vertical_design_graph=graph,
            validation_trials=1,
        )
    except HarnessError as exc:
        assert exc.error_code == "design_semantic_review_failed"
    else:
        raise AssertionError("Invented Requirement evidence was accepted.")


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
