"""Kiểm thử coverage gate và phiên làm rõ kiểu Spec Kit."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from sdlc_harness.agents.requirements import (
    RequirementCoverageReviewer,
    RequirementModelV2,
    close_clarification_session,
    normalize_and_validate_requirement_model_v2,
    record_session_answer,
    resolve_clarification_answer,
    select_current_requirement_question,
    session_pending_ids,
    start_clarification_session,
)
from sdlc_harness.agents.requirements.coverage_v1 import (
    _adopt_candidates,
    _deterministic_analysis,
    _merge_analysis,
    _validate_analysis,
)
from sdlc_harness.core.config import LlmCallProfile, load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmClient, LlmCompletion


ROOT = Path(__file__).resolve().parents[1]


def _payload_without_questions() -> dict[str, object]:
    payload = json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))
    payload["open_questions"] = []
    for value in _walk(payload):
        if isinstance(value, dict) and "open_question_refs" in value:
            value["open_question_refs"] = []
        if isinstance(value, dict) and value.get("status") == "unknown":
            value["status"] = "explicit" if value.get("evidence_refs") else "assumption"
    return payload


def _walk(value):
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _reviewer() -> RequirementCoverageReviewer:
    directory = TemporaryDirectory()
    settings = load_settings(
        root=ROOT,
        runtime_root=Path(directory.name),
        overrides={"SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock"},
    )
    reviewer = RequirementCoverageReviewer(LlmClient(settings))
    reviewer._test_directory = directory  # type: ignore[attr-defined]
    return reviewer


def test_coverage_gate_turns_unknown_branch_behavior_into_blocking_questions() -> None:
    source_text = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8")
    before = normalize_and_validate_requirement_model_v2(_payload_without_questions(), {"SRC-1": source_text})
    assert before.report.status == "invalid"
    assert "branch_behavior_without_clarification" in {item.code for item in before.report.issues}
    branch_issue = next(
        item
        for item in before.report.issues
        if item.code == "branch_behavior_without_clarification" and item.subject_ref == "AF-1"
    )
    assert branch_issue.missing_fields == ("steps", "outcome")

    result = _reviewer().review(
        model=before.model,
        source_text_by_id={"SRC-1": source_text},
        validation_report=before.report,
    )

    assert result.validation_report.status == "clarification_required"
    assert len(result.added_question_ids) == 3
    affected = {
        ref
        for question in result.model.open_questions
        for ref in question.get("affects", [])
    }
    assert {"AF-1", "EF-1", "EF-2"}.issubset(affected)
    question_by_flow = {
        flow_id: question
        for question in result.model.open_questions
        for flow_id in question.get("affects", [])
        if str(flow_id).startswith(("AF-", "EF-"))
    }
    assert "AC-3" in question_by_flow["AF-1"]["affects"]
    assert "AC-4" in question_by_flow["EF-1"]["affects"]
    assert "AC-5" in question_by_flow["EF-2"]["affects"]
    assert all(question.get("why_it_matters") for question in result.model.open_questions)
    assert result.analysis["review_mode"] == "deterministic"


def test_non_fake_reviewer_executes_semantic_contract_path() -> None:
    source_text = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8")
    normalized = normalize_and_validate_requirement_model_v2(
        json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8")),
        {"SRC-1": source_text},
    )
    payload = _deterministic_analysis(normalized.model)

    class ScriptedSemanticClient:
        settings = SimpleNamespace(
            llm_mode="deepseek",
            llm_structured_max_tokens=8192,
            phase1_llm_profiles={
                "coverage": LlmCallProfile("coverage-model", "high", 24576, 180)
            },
        )

        def __init__(self):
            self.calls = 0
            self.request: dict[str, object] = {}

        def complete_with_metadata(self, **kwargs):
            self.calls += 1
            self.request = dict(kwargs)
            return LlmCompletion(content=json.dumps(payload), finish_reason="stop")

    client = ScriptedSemanticClient()
    result = RequirementCoverageReviewer(client).review(  # type: ignore[arg-type]
        model=normalized.model,
        source_text_by_id={"SRC-1": source_text},
        validation_report=normalized.report,
    )

    assert client.calls == 1
    assert client.request["model"] == "coverage-model"
    assert client.request["reasoning_effort"] == "high"
    assert client.request["max_tokens"] == 24576
    assert client.request["timeout_seconds"] == 180
    assert "absence alone is not a gap" in str(client.request["system"])
    prompt = str(client.request["prompt"])
    assert "Absence is not ambiguity" in prompt
    assert "Do not infer authorization roles" in prompt
    assert "do not ask about hard delete, soft delete, retention, archive, or audit" in prompt
    assert "A named failure does not put other possible failures in scope" in prompt
    assert "Apply this counterfactual test" in prompt
    assert "exists only after adding a choice" in prompt
    assert result.analysis["review_mode"] == "hybrid"


def test_semantic_candidate_expands_existing_open_question_refs_to_stable_requirements() -> None:
    source_text = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8")
    normalized = normalize_and_validate_requirement_model_v2(
        json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8")),
        {"SRC-1": source_text},
    )
    question = normalized.model.open_questions[0]
    payload = _deterministic_analysis(normalized.model)
    candidate = deepcopy(payload["question_candidates"][0])
    candidate["affects"] = [str(question["id"])]
    candidate["evidence_refs"] = list(question["evidence_refs"])
    payload["question_candidates"] = [candidate]

    result = _validate_analysis(payload, normalized.model)

    assert result["question_candidates"][0]["affects"] == question["affects"]
    assert result["normalizations"] == [
        {
            "kind": "existing_open_question_affects_expanded",
            "candidate_index": 0,
            "question_refs": [question["id"]],
            "requirement_refs": question["affects"],
        }
    ]


def test_semantic_candidate_keeps_unknown_question_ref_fail_closed() -> None:
    source_text = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8")
    normalized = normalize_and_validate_requirement_model_v2(
        json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8")),
        {"SRC-1": source_text},
    )
    payload = _deterministic_analysis(normalized.model)
    candidate = deepcopy(payload["question_candidates"][0])
    candidate["affects"] = ["Q-999"]
    payload["question_candidates"] = [candidate]

    with pytest.raises(HarnessError) as exc_info:
        _validate_analysis(payload, normalized.model)

    assert exc_info.value.error_code == "requirement_coverage_invalid"
    assert "Q-999" in exc_info.value.message


def test_clarification_session_exposes_at_most_five_questions() -> None:
    payload = _payload_without_questions()
    template = {
        "question": "Please clarify this behavior?",
        "category": "observable_behavior",
        "proposed_blocking": True,
        "effective_blocking": True,
        "affects": ["AF-1"],
        "depends_on": [],
        "status": "open",
        "answer_guidance": None,
        "evidence_refs": ["E-10"],
    }
    payload["open_questions"] = [dict(deepcopy(template), id=f"Q-{index}") for index in range(1, 8)]
    model = RequirementModelV2.from_dict(payload)

    session = start_clarification_session(model)
    assert session is not None
    assert session["question_ids"] == ["Q-1", "Q-2", "Q-3", "Q-4", "Q-5"]
    assert str(session["coverage_digest"]).startswith("sha256:")
    initial_decision_count = int(session["decision_count"])
    for question_id in list(session["question_ids"]):
        session = record_session_answer(session, str(question_id))
    assert session_pending_ids(session) == set()
    assert session["decision_count"] == initial_decision_count + 5
    assert close_clarification_session(session, exhausted=True)["status"] == "exhausted"


def test_coverage_reviewer_never_opens_more_than_five_blocking_questions() -> None:
    model = RequirementModelV2.from_dict(_payload_without_questions())
    candidates = [
        {
            "category": "completion_signals",
            "question": f"Which observable signal completes criterion {criterion_id}?",
            "why_it_matters": "The answer changes acceptance testing.",
            "answer_mode": "short",
            "options": [],
            "recommended_option": None,
            "suggested_answer": None,
            "impact": "high",
            "uncertainty": "medium",
            "affects": [criterion_id],
            "evidence_refs": [evidence_id],
        }
        for criterion_id, evidence_id in (
            ("AC-1", "E-13"),
            ("AC-2", "E-12"),
            ("AC-3", "E-10"),
            ("AC-4", "E-7"),
            ("AC-5", "E-11"),
        )
    ]

    revised, added = _adopt_candidates(model, candidates, max_new_questions=3)

    assert len(added) == 3
    assert len(revised.open_questions) == 3


def test_question_priority_uses_impact_before_legacy_category_order() -> None:
    payload = _payload_without_questions()
    payload["open_questions"] = [
        {
            "id": "Q-1", "question": "Medium data question?", "category": "data_identity",
            "proposed_blocking": True, "effective_blocking": True, "affects": ["BR-1"],
            "depends_on": [], "status": "open", "answer_guidance": None,
            "evidence_refs": ["E-13"], "impact": "medium", "uncertainty": "high",
        },
        {
            "id": "Q-2", "question": "High branch question?", "category": "observable_behavior",
            "proposed_blocking": True, "effective_blocking": True, "affects": ["AF-1"],
            "depends_on": [], "status": "open", "answer_guidance": None,
            "evidence_refs": ["E-10"], "impact": "high", "uncertainty": "high",
        },
    ]
    selection = select_current_requirement_question(RequirementModelV2.from_dict(payload))

    assert selection.current_question is not None
    assert selection.current_question.id == "Q-2"


def test_multiple_choice_answer_key_is_resolved_before_llm_update() -> None:
    question = {
        "options": [
            {"key": "A", "answer": "Keep the draft", "implication": "No deletion"},
            {"key": "B", "answer": "Discard the draft", "implication": "Delete it"},
        ]
    }
    assert resolve_clarification_answer(question, "b") == "Discard the draft"
    assert resolve_clarification_answer(question, "Custom answer") == "Custom answer"
    assert resolve_clarification_answer(
        {"options": [{"key": "notification_only", "label": "Notification only"}]},
        "notification_only",
    ) == "Notification only"


def test_semantic_candidate_cannot_overwrite_or_cross_link_deterministic_branch_gaps() -> None:
    model = RequirementModelV2.from_dict(_payload_without_questions())
    deterministic = _deterministic_analysis(model)
    semantic = deepcopy(deterministic)
    semantic["question_candidates"] = [
        {
            **deepcopy(deterministic["question_candidates"][0]),
            "question": "What combined result should AF-1 and EF-2 produce?",
            "affects": ["AF-1", "EF-2"],
        }
    ]

    merged = _merge_analysis(deterministic, semantic)

    assert len(merged["question_candidates"]) == len(deterministic["question_candidates"])
    assert all(set(item["affects"]) != {"AF-1", "EF-2"} for item in merged["question_candidates"])


def test_candidates_outside_five_question_boundary_are_recorded_as_deferred() -> None:
    template = {
        "category": "completion_signals",
        "question": "Which observable signal confirms completion?",
        "why_it_matters": "It changes acceptance testing.",
        "answer_mode": "short",
        "options": [],
        "recommended_option": None,
        "suggested_answer": None,
        "impact": "high",
        "uncertainty": "medium",
        "evidence_refs": ["E-1"],
    }
    deterministic = {
        "question_candidates": [dict(template, affects=[f"D-{index}"]) for index in range(3)],
        "deferred": [],
    }
    semantic = {
        "question_candidates": [dict(template, affects=[f"S-{index}"]) for index in range(5)],
        "deferred": [],
    }

    merged = _merge_analysis(deterministic, semantic)

    assert len(merged["question_candidates"]) == 5
    assert len(merged["deferred"]) == 3
    assert all(item["reason"] == "coverage_candidate_limit" for item in merged["deferred"])


def test_adopted_question_is_mapped_only_to_surfaces_that_own_affected_requirement() -> None:
    payload = json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))
    model = RequirementModelV2.from_dict(payload)
    candidate = {
        "category": "completion_signals",
        "question": "Which observable signal proves the successful insertion is complete?",
        "why_it_matters": "The answer determines the acceptance assertion.",
        "answer_mode": "short",
        "options": [],
        "recommended_option": None,
        "suggested_answer": None,
        "impact": "high",
        "uncertainty": "medium",
        "affects": ["AC-2"],
        "evidence_refs": ["E-12"],
    }

    revised, added = _adopt_candidates(model, [candidate])
    scenario = revised.scenario_contract["scenarios"][0]
    question_id = added[0]

    assert question_id not in scenario["surfaces"]["ui"]["open_question_refs"]
    assert question_id in scenario["surfaces"]["api"]["open_question_refs"]


def test_semantic_candidate_without_existing_surface_mapping_is_deferred() -> None:
    payload = json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))
    scenario = payload["scenario_contract"]["scenarios"][0]
    for surface in scenario["surfaces"].values():
        surface["requirement_refs"] = [ref for ref in surface["requirement_refs"] if ref != "AC-2"]
    model = RequirementModelV2.from_dict(payload)
    candidate = {
        "category": "completion_signals",
        "question": "Which signal proves this scenario is complete?",
        "why_it_matters": "The answer changes acceptance testing.",
        "answer_mode": "short",
        "options": [],
        "recommended_option": None,
        "suggested_answer": None,
        "impact": "medium",
        "uncertainty": "medium",
        "affects": ["AC-2"],
        "evidence_refs": ["E-12"],
    }

    _, added = _adopt_candidates(model, [candidate])

    assert added == []
