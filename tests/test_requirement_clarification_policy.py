"""Kiểm thử policy shadow; runtime blocking policy hiện hành không được thay đổi."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import pytest

from sdlc_harness.agents.requirements import (
    CLARIFICATION_DISPOSITION_FIELD,
    DEFERRED_TO_DESIGN,
    LEGACY_BLOCKING_POLICY_VERSION,
    RequirementModelV2,
    SHADOW_POLICY_VERSION,
    build_clarification_policy_shadow,
    normalize_and_validate_requirement_model_v2,
    render_formal_use_case_v2,
    render_requirement_packet_v2,
    select_current_requirement_question,
    session_pending_ids,
    start_clarification_session,
)
from sdlc_harness.core.errors import HarnessError


ROOT = Path(__file__).resolve().parents[1]
GATE_FIXTURE_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "requirement_clarification_policy"
    / "l1_12_gate_cases.json"
)


def _payload() -> dict[str, object]:
    return json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))


def _source_text_by_id() -> dict[str, str]:
    text = (ROOT / "fixtures" / "etour" / "UC2.txt").read_text(encoding="utf-8").strip()
    return {"SRC-1": text}


def _single_question_payload(
    *,
    category: str,
    affects: list[str],
    proposed: bool,
    coverage_category: str | None = None,
    surface_blocker: bool = False,
) -> dict[str, object]:
    payload = _payload()
    question = {
        "id": "Q-1",
        "question": "Cần gọi actor là khách hay du khách?",
        "category": category,
        "proposed_blocking": proposed,
        "effective_blocking": proposed,
        "affects": affects,
        "depends_on": [],
        "status": "open",
        "answer_guidance": None,
        "evidence_refs": ["E-2"],
    }
    if coverage_category is not None:
        question["coverage_category"] = coverage_category
    payload["open_questions"] = [question]
    for value in _walk(payload):
        if isinstance(value, dict) and "open_question_refs" in value:
            value["open_question_refs"] = []
    if surface_blocker:
        scenario = payload["scenario_contract"]["scenarios"][0]
        surface = scenario["surfaces"]["ui"]
        surface["status"] = "clarification_required"
        surface["open_question_refs"] = ["Q-1"]
    return payload


def _walk(value):
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def _entry(payload: dict[str, object]) -> dict[str, object]:
    report = build_clarification_policy_shadow(RequirementModelV2.from_dict(payload))
    return report["questions"][0]


def _gate_payload() -> tuple[dict[str, object], dict[str, object]]:
    fixture = json.loads(GATE_FIXTURE_PATH.read_text(encoding="utf-8"))
    payload = _payload()
    payload["open_questions"] = []
    for value in _walk(payload):
        if isinstance(value, dict) and "open_question_refs" in value:
            value["open_question_refs"] = []

    scenarios = payload["scenario_contract"]["scenarios"]
    scenario_by_id = {scenario["id"]: scenario for scenario in scenarios}
    for case in fixture["cases"]:
        question = {
            "id": case["id"],
            "question": case["question"],
            "category": case["category"],
            "coverage_category": case["coverage_category"],
            "proposed_blocking": case["proposed_blocking"],
            "effective_blocking": case["effective_blocking"],
            "affects": case["affects"],
            "depends_on": [],
            "status": "open",
            "answer_guidance": None,
            "evidence_refs": ["E-2"],
        }
        payload["open_questions"].append(question)
        blocker = case.get("surface_blocker")
        if blocker is not None:
            surface = scenario_by_id[blocker["scenario_id"]]["surfaces"][blocker["surface"]]
            surface["status"] = "clarification_required"
            surface["open_question_refs"] = [case["id"]]
    return payload, fixture


def test_proposed_blocker_can_be_shadow_deferred_when_it_is_terminology_only() -> None:
    entry = _entry(
        _single_question_payload(
            category="terminology",
            coverage_category="terminology",
            affects=["ACT-1"],
            proposed=True,
        )
    )

    assert entry["current_effective_blocking"] is True
    assert entry["shadow_disposition"] == "deferred_to_design"
    assert entry["shadow_effective_blocking"] is False
    assert entry["would_change"] is True


def test_hard_category_remains_human_required_even_when_llm_proposes_non_blocking() -> None:
    entry = _entry(
        _single_question_payload(
            category="data_identity",
            affects=["ACT-1"],
            proposed=False,
        )
    )

    assert entry["shadow_disposition"] == "human_required"
    assert entry["shadow_reason_codes"] == ["shadow_hard_category"]


def test_behavior_reference_remains_human_required() -> None:
    entry = _entry(
        _single_question_payload(
            category="terminology",
            affects=["AF-1"],
            proposed=False,
        )
    )

    assert entry["shadow_disposition"] == "human_required"
    assert entry["shadow_reason_codes"] == ["shadow_behavior_reference"]


def test_clarification_required_surface_remains_human_required() -> None:
    entry = _entry(
        _single_question_payload(
            category="terminology",
            affects=["ACT-1"],
            proposed=False,
            surface_blocker=True,
        )
    )

    assert entry["shadow_disposition"] == "human_required"
    assert entry["shadow_reason_codes"] == ["shadow_surface_clarification_required"]


def test_unknown_soft_category_fails_safe_to_human_required() -> None:
    entry = _entry(
        _single_question_payload(
            category="presentation_preference",
            affects=["ACT-1"],
            proposed=True,
        )
    )

    assert entry["shadow_disposition"] == "human_required"
    assert entry["shadow_reason_codes"] == ["shadow_fail_safe"]


def test_terminology_coverage_does_not_override_an_unknown_question_category() -> None:
    entry = _entry(
        _single_question_payload(
            category="presentation_preference",
            coverage_category="terminology",
            affects=["ACT-1"],
            proposed=True,
        )
    )

    assert entry["shadow_disposition"] == "human_required"
    assert entry["shadow_reason_codes"] == ["shadow_fail_safe"]


def test_shadow_report_is_deterministic_and_does_not_mutate_model() -> None:
    payload = _single_question_payload(
        category="terminology",
        affects=["ACT-1"],
        proposed=True,
    )
    model = RequirementModelV2.from_dict(payload)
    before = deepcopy(model.to_dict())

    first = build_clarification_policy_shadow(model)
    second = build_clarification_policy_shadow(model)

    assert first == second
    assert first["report_digest"] == second["report_digest"]
    assert model.to_dict() == before
    assert first["summary"]["shadow_policy_default_count"] == 0
    assert first["summary"]["shadow_irrelevant_count"] == 0


def test_resolved_questions_are_counted_but_not_classified() -> None:
    payload = _single_question_payload(
        category="business_rule",
        affects=["BR-1"],
        proposed=True,
    )
    payload["open_questions"][0]["status"] = "resolved"
    report = build_clarification_policy_shadow(RequirementModelV2.from_dict(payload))

    assert report["questions"] == []
    assert report["summary"]["open_question_count"] == 0
    assert report["summary"]["resolved_or_superseded_question_count"] == 1


def test_l1_12_controlled_gate_fixture_has_one_safe_downgrade_and_keeps_boundaries() -> None:
    payload, fixture = _gate_payload()
    before = deepcopy(payload)

    report = build_clarification_policy_shadow(RequirementModelV2.from_dict(payload))
    repeated = build_clarification_policy_shadow(RequirementModelV2.from_dict(payload))

    assert report == repeated
    assert payload == before
    assert report["summary"] == fixture["expected_summary"]
    by_id = {entry["question_id"]: entry for entry in report["questions"]}
    assert set(by_id) == {case["id"] for case in fixture["cases"]}
    for case in fixture["cases"]:
        entry = by_id[case["id"]]
        assert entry["shadow_disposition"] == case["expected_disposition"]
        assert entry["shadow_reason_codes"] == case["expected_reason_codes"]
        assert entry["would_change"] is case["expected_would_change"]


def test_l1_12_cutover_normalizes_only_the_safe_fixture_case() -> None:
    payload, fixture = _gate_payload()

    result = normalize_and_validate_requirement_model_v2(
        payload,
        _source_text_by_id(),
        clarification_policy_version=SHADOW_POLICY_VERSION,
    )

    by_id = {item["id"]: item for item in result.model.open_questions}
    deferred = by_id["Q-GATE-1"]
    assert deferred["effective_blocking"] is False
    assert deferred[CLARIFICATION_DISPOSITION_FIELD] == {
        "value": DEFERRED_TO_DESIGN,
        "policy_version": SHADOW_POLICY_VERSION,
        "reason_codes": ["shadow_terminology_only", "shadow_no_hard_impact"],
    }
    assert all(by_id[case["id"]]["effective_blocking"] is True for case in fixture["cases"][1:])
    assert all(CLARIFICATION_DISPOSITION_FIELD not in by_id[case["id"]] for case in fixture["cases"][1:])
    assert result.model.validation["clarification_policy_version"] == SHADOW_POLICY_VERSION
    assert result.report.blocking_question_ids == [
        "Q-GATE-2",
        "Q-GATE-3",
        "Q-GATE-4",
        "Q-GATE-5",
        "Q-GATE-6",
    ]
    session = start_clarification_session(result.model)
    assert session is not None
    assert "Q-GATE-1" not in session_pending_ids(session)
    selection = select_current_requirement_question(
        result.model,
        allowed_question_ids=session_pending_ids(session),
    )
    assert selection.current_question is not None
    assert selection.current_question.id != "Q-GATE-1"
    assert selection.remaining_blocking_count == 5


def test_l1_12_legacy_model_keeps_the_old_blocking_semantics() -> None:
    payload = _single_question_payload(
        category="terminology",
        coverage_category="terminology",
        affects=["ACT-1"],
        proposed=True,
    )

    result = normalize_and_validate_requirement_model_v2(
        payload,
        _source_text_by_id(),
        clarification_policy_version=LEGACY_BLOCKING_POLICY_VERSION,
    )

    question = result.model.open_questions[0]
    assert question["effective_blocking"] is True
    assert CLARIFICATION_DISPOSITION_FIELD not in question
    assert "clarification_policy_version" not in result.model.validation


def test_l1_12_active_policy_is_idempotent_and_renders_the_handoff_label() -> None:
    payload = _single_question_payload(
        category="terminology",
        coverage_category="terminology",
        affects=["ACT-1"],
        proposed=True,
    )
    first = normalize_and_validate_requirement_model_v2(
        payload,
        _source_text_by_id(),
        clarification_policy_version=SHADOW_POLICY_VERSION,
    )
    second = normalize_and_validate_requirement_model_v2(
        first.model,
        _source_text_by_id(),
        clarification_policy_version=SHADOW_POLICY_VERSION,
    )

    assert first.model.to_dict() == second.model.to_dict()
    assert first.report.blocking_question_ids == []
    assert "DEFERRED-TO-DESIGN" in render_requirement_packet_v2(first.model)
    assert "DEFERRED-TO-DESIGN" in render_formal_use_case_v2(first.model)


def test_l1_12_unknown_or_mismatched_policy_fails_closed() -> None:
    payload = _single_question_payload(
        category="terminology",
        affects=["ACT-1"],
        proposed=True,
    )
    with pytest.raises(HarnessError) as unknown:
        normalize_and_validate_requirement_model_v2(
            payload,
            _source_text_by_id(),
            clarification_policy_version="unknown_policy",
        )
    assert unknown.value.error_code == "requirement_clarification_policy_invalid"

    active = normalize_and_validate_requirement_model_v2(
        payload,
        _source_text_by_id(),
        clarification_policy_version=SHADOW_POLICY_VERSION,
    )
    with pytest.raises(HarnessError) as mismatch:
        normalize_and_validate_requirement_model_v2(
            active.model,
            _source_text_by_id(),
            clarification_policy_version=LEGACY_BLOCKING_POLICY_VERSION,
        )
    assert mismatch.value.error_code == "requirement_clarification_policy_mismatch"
