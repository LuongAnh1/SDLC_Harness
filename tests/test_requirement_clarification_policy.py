"""Kiểm thử policy shadow; runtime blocking policy hiện hành không được thay đổi."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from sdlc_harness.agents.requirements import (
    RequirementModelV2,
    build_clarification_policy_shadow,
)


ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    return json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))


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
