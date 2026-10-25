"""Gate cho clarification_disposition_v3 và agent reasonable defaults."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from sdlc_harness.agents.requirements import (
    AGENT_DEFAULT,
    AGENT_DEFAULT_ORIGIN,
    CLARIFICATION_DISPOSITION_FIELD,
    REASONABLE_DEFAULT_POLICY_VERSION,
    RequirementModelV2,
    build_agent_reasonable_default_report,
    build_clarification_policy_shadow,
    challenge_agent_assumption,
    normalize_and_validate_requirement_model_v2,
)
from sdlc_harness.core.errors import HarnessError


ROOT = Path(__file__).resolve().parents[1]


def _case() -> tuple[dict[str, object], dict[str, str]]:
    payload = json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))
    source = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8").strip()
    digest = "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
    payload["source"]["fingerprint"] = digest
    payload["provenance_ledger"]["sources"][0]["fingerprint"] = digest
    payload["open_questions"].append(
        {
            "id": "Q-5",
            "question": "Which low-risk default ordering should the initial list use?",
            "category": "conventional_ux_behavior",
            "coverage_category": "interaction_ux",
            "proposed_disposition": "agent_default",
            "proposed_blocking": False,
            "effective_blocking": False,
            "affects": ["UC-1"],
            "depends_on": [],
            "status": "open",
            "answer_guidance": None,
            "answer_mode": "multiple_choice",
            "options": [
                {"key": "newest_first", "answer": "Show the newest items first."},
                {"key": "oldest_first", "answer": "Show the oldest items first."},
            ],
            "recommended_option": "newest_first",
            "suggested_answer": None,
            "why_it_is_safe": "The ordering is visible, low impact, and reversible.",
            "impact": "low",
            "uncertainty": "low",
            "reversible": True,
            "evidence_refs": ["E-1"],
        }
    )
    return payload, {"SRC-1": source}


def _normalize(payload: object, sources: dict[str, str]):
    return normalize_and_validate_requirement_model_v2(
        payload,
        sources,
        clarification_policy_version=REASONABLE_DEFAULT_POLICY_VERSION,
    )


def test_agent_default_uses_llm_answer_and_creates_traceable_assumption() -> None:
    payload, sources = _case()

    first = _normalize(payload, sources)
    second = _normalize(first.model, sources)

    assert first.model.to_dict() == second.model.to_dict()
    question = next(item for item in first.model.open_questions if item["id"] == "Q-5")
    assert question["status"] == "superseded"
    assert question[CLARIFICATION_DISPOSITION_FIELD]["value"] == AGENT_DEFAULT
    assumption = next(item for item in first.model.assumptions if item.get("origin") == AGENT_DEFAULT_ORIGIN)
    assert assumption["id"] == "ASM-1"
    assert assumption["text"] == "Show the newest items first."
    assert assumption["source_question_id"] == "Q-5"
    assert assumption["reversible"] is True
    report = build_agent_reasonable_default_report(first.model)
    assert report["summary"]["applied_count"] == 1
    assert report["summary"]["assumption_ids"] == ["ASM-1"]


def test_shadow_reports_agent_default_without_mutating_model() -> None:
    payload, _ = _case()
    model = RequirementModelV2.from_dict(payload)
    before = model.to_dict()

    report = build_clarification_policy_shadow(model)

    assert model.to_dict() == before
    assert report["summary"]["shadow_agent_default_count"] == 1
    question = next(item for item in report["questions"] if item["question_id"] == "Q-5")
    assert question["shadow_disposition"] == AGENT_DEFAULT


@pytest.mark.parametrize(
    "mutation",
    [
        lambda q: q.update(category="business_rule"),
        lambda q: q.update(impact="high"),
        lambda q: q.update(uncertainty="medium"),
        lambda q: q.update(reversible=False),
        lambda q: q.update(why_it_is_safe=""),
        lambda q: q.update(recommended_option=None),
        lambda q: q.update(affects=["AC-1"]),
        lambda q: q.update(depends_on=["Q-1"]),
    ],
)
def test_unsafe_agent_default_fails_safe_to_human_required(mutation) -> None:
    payload, sources = _case()
    question = payload["open_questions"][-1]
    mutation(question)

    result = _normalize(payload, sources)
    normalized = next(item for item in result.model.open_questions if item["id"] == "Q-5")

    assert normalized["status"] == "open"
    assert normalized["effective_blocking"] is True
    assert not any(item.get("origin") == AGENT_DEFAULT_ORIGIN for item in result.model.assumptions)


def test_agent_default_rejects_protected_contract_references() -> None:
    payload, sources = _case()
    payload["open_questions"][-1]["affects"] = ["BR-1"]

    result = _normalize(payload, sources)
    question = next(item for item in result.model.open_questions if item["id"] == "Q-5")

    assert question["effective_blocking"] is True
    assert not any(item.get("origin") == AGENT_DEFAULT_ORIGIN for item in result.model.assumptions)


def test_agent_default_can_reference_a_non_blocking_flow_item() -> None:
    payload, sources = _case()
    payload["open_questions"][-1]["affects"] = ["AF-1"]
    payload["flows"]["alternatives"][0]["open_question_refs"].append("Q-5")
    scenario = next(item for item in payload["scenario_contract"]["scenarios"] if item["id"] == "SC-2")
    scenario["open_question_refs"].append("Q-5")
    scenario["surfaces"]["ui"]["open_question_refs"].append("Q-5")

    result = _normalize(payload, sources)

    assert any(item.get("origin") == AGENT_DEFAULT_ORIGIN for item in result.model.assumptions)
    applied = result.model.to_dict()
    assert "Q-5" not in applied["flows"]["alternatives"][0]["open_question_refs"]
    applied_scenario = next(item for item in applied["scenario_contract"]["scenarios"] if item["id"] == "SC-2")
    assert "Q-5" not in applied_scenario["open_question_refs"]
    assert "Q-5" not in applied_scenario["surfaces"]["ui"]["open_question_refs"]

    challenged = challenge_agent_assumption(
        result.model,
        assumption_id="ASM-1",
        instruction="Ask the owner instead.",
        challenged_by="owner",
    ).model.to_dict()
    assert "Q-5" in challenged["flows"]["alternatives"][0]["open_question_refs"]
    challenged_scenario = next(item for item in challenged["scenario_contract"]["scenarios"] if item["id"] == "SC-2")
    assert "Q-5" in challenged_scenario["open_question_refs"]
    assert "Q-5" in challenged_scenario["surfaces"]["ui"]["open_question_refs"]


def test_agent_default_can_use_suggested_answer_when_choice_keys_are_unavailable() -> None:
    payload, sources = _case()
    question = payload["open_questions"][-1]
    question["recommended_option"] = None
    question["suggested_answer"] = "Show the newest items first."

    result = _normalize(payload, sources)
    assumption = next(item for item in result.model.assumptions if item.get("origin") == AGENT_DEFAULT_ORIGIN)

    assert assumption["text"] == "Show the newest items first."


def test_malformed_multiple_choice_contract_is_invalid_even_with_suggested_answer() -> None:
    payload, sources = _case()
    question = payload["open_questions"][-1]
    question["options"] = ["Newest first", "Oldest first"]
    question["recommended_option"] = "Newest first"
    question["suggested_answer"] = "Show the newest items first."

    result = _normalize(payload, sources)

    assert result.report.ok is False
    assert {item.code for item in result.report.issues} >= {
        "invalid_question_option",
        "invalid_recommended_option",
    }


def test_design_owned_visual_choice_is_deferred_without_assumption() -> None:
    payload, sources = _case()
    question = payload["open_questions"][-1]
    question.update(
        category="visual_style",
        proposed_disposition="deferred_to_design",
        proposed_blocking=False,
        affects=["ACT-1"],
    )

    result = _normalize(payload, sources)
    normalized = next(item for item in result.model.open_questions if item["id"] == "Q-5")

    assert normalized["status"] == "open"
    assert normalized["effective_blocking"] is False
    assert normalized[CLARIFICATION_DISPOSITION_FIELD]["value"] == "deferred_to_design"
    assert not any(item.get("origin") == AGENT_DEFAULT_ORIGIN for item in result.model.assumptions)


def test_challenge_withdraws_agent_default_and_reopens_human_question() -> None:
    payload, sources = _case()
    applied = _normalize(payload, sources)

    challenge = challenge_agent_assumption(
        applied.model,
        assumption_id="ASM-1",
        instruction="Use an explicit owner decision instead.",
        challenged_by="owner",
    )
    validated = _normalize(challenge.model, sources)
    question = next(item for item in validated.model.open_questions if item["id"] == "Q-5")

    assert question["status"] == "open"
    assert question["effective_blocking"] is True
    assert question["proposed_disposition"] == "human_required"
    assert not any(item.get("id") == "ASM-1" for item in validated.model.assumptions)
    assert challenge.decision["decision_kind"] == "assumption_challenge"


def test_agent_default_assumption_tampering_fails_closed() -> None:
    payload, sources = _case()
    canonical = _normalize(payload, sources).model.to_dict()
    assumption = next(item for item in canonical["assumptions"] if item.get("origin") == AGENT_DEFAULT_ORIGIN)
    assumption["proposal_hash"] = "sha256:tampered"

    with pytest.raises(HarnessError) as raised:
        _normalize(canonical, sources)

    assert getattr(raised.value, "error_code", "") == "requirement_agent_default_integrity_failed"


def test_explicit_quality_none_without_question_does_not_create_assumption() -> None:
    payload, sources = _case()
    payload["open_questions"] = [item for item in payload["open_questions"] if item["id"] != "Q-5"]
    sources["SRC-1"] += "\nQuality requirements: None specified."
    digest = "sha256:" + hashlib.sha256(sources["SRC-1"].encode("utf-8")).hexdigest()
    payload["source"]["fingerprint"] = digest
    payload["provenance_ledger"]["sources"][0]["fingerprint"] = digest

    result = _normalize(payload, sources)

    assert not any(item.get("origin") == AGENT_DEFAULT_ORIGIN for item in result.model.assumptions)
