"""Gate L1.13/L1.14 cho registry và policy assumption deterministic."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from sdlc_harness.agents.requirements import (
    CLARIFICATION_DISPOSITION_FIELD,
    POLICY_DEFAULT,
    POLICY_ID_QUALITY_NONE,
    REVIEWED_DEFAULT_POLICY_VERSION,
    SAFE_DEFAULT_EVALUATION_FIELD,
    RequirementModelV2RefinementResult,
    build_reviewed_safe_default_report,
    normalize_and_validate_requirement_model_v2,
    render_requirement_packet_v2,
    select_current_requirement_question,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.core.config import load_settings
from sdlc_harness.sources.fixtures import UseCaseSource
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


ROOT = Path(__file__).resolve().parents[1]


def _case() -> tuple[dict[str, object], dict[str, str]]:
    payload = json.loads(
        (ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8")
    )
    source = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8").strip()
    source = source.replace(
        "Quality requirements:\nThe system requirements will not accept duplicates cultural heritage.",
        "Quality requirements: None specified",
    )
    digest = "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
    payload["source"]["fingerprint"] = digest
    payload["provenance_ledger"]["sources"][0]["fingerprint"] = digest
    payload["evidence"][-1]["quote"] = "Quality requirements: None specified"
    payload["open_questions"].append(
        {
            "id": "Q-5",
            "question": "Should this use case introduce any additional quality target?",
            "category": "business_rule",
            "coverage_category": "quality_attributes",
            "proposed_blocking": True,
            "effective_blocking": True,
            "affects": ["UC-1"],
            "depends_on": [],
            "status": "open",
            "answer_guidance": None,
            "answer_mode": "multiple_choice",
            "options": [
                {"key": "none_required", "answer": "No additional target is required."},
                {"key": "define_targets", "answer": "Define measurable targets."},
            ],
            "recommended_option": "none_required",
            "suggested_answer": "Untrusted free-form answer must be ignored.",
            "suggested_policy_id": POLICY_ID_QUALITY_NONE,
            "impact": "low",
            "uncertainty": "low",
            "evidence_refs": ["E-13"],
        }
    )
    return payload, {"SRC-1": source}


def _normalize(payload: dict[str, object], sources: dict[str, str]):
    return normalize_and_validate_requirement_model_v2(
        payload,
        sources,
        clarification_policy_version=REVIEWED_DEFAULT_POLICY_VERSION,
    )


def test_reviewed_quality_none_policy_creates_traceable_assumption_idempotently() -> None:
    payload, sources = _case()

    first = _normalize(payload, sources)
    second = _normalize(first.model, sources)

    assert first.model.to_dict() == second.model.to_dict()
    question = next(item for item in first.model.open_questions if item["id"] == "Q-5")
    assert question["status"] == "superseded"
    assert question["effective_blocking"] is False
    assert question[CLARIFICATION_DISPOSITION_FIELD]["value"] == POLICY_DEFAULT
    assert question[SAFE_DEFAULT_EVALUATION_FIELD]["status"] == "applied"
    assumption = next(item for item in first.model.assumptions if item.get("origin") == POLICY_DEFAULT)
    assert assumption["id"] == "ASM-1"
    assert assumption["policy_id"] == POLICY_ID_QUALITY_NONE
    assert assumption["source_question_id"] == "Q-5"
    assert assumption["evidence_refs"] == ["E-13"]
    assert not any(item.get("question_id") == "Q-5" for item in first.model.decisions)
    assert select_current_requirement_question(first.model).remaining_blocking_count == 4
    assert "POLICY DEFAULT `quality_none_specified_v1`" in render_requirement_packet_v2(first.model)

    report = build_reviewed_safe_default_report(first.model)
    assert report["summary"]["applied_count"] == 1
    assert report["summary"]["assumption_ids"] == ["ASM-1"]


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        (lambda p: p["open_questions"][-1].update(suggested_policy_id="unknown_v1"), "safe_default_policy_unknown"),
        (lambda p: p["open_questions"][-1].update(affects=["AC-1"]), "safe_default_affects_not_allowed"),
        (lambda p: p["open_questions"][-1].update(recommended_option="define_targets"), "safe_default_recommendation_not_allowed"),
        (lambda p: p["open_questions"][-1].update(depends_on=["Q-1"]), "safe_default_question_has_dependencies"),
        (lambda p: p["evidence"][-1].update(quote="Quality requirements are unknown."), "safe_default_explicit_marker_missing"),
        (lambda p: p["quality_requirements"].append(deepcopy(p["business_rules"][0])), "safe_default_quality_requirement_exists"),
    ],
)
def test_reviewed_quality_none_policy_rejects_outside_allowlist(mutation, reason_code: str) -> None:
    payload, sources = _case()
    mutation(payload)

    result = _normalize(payload, sources)

    question = next(item for item in result.model.open_questions if item["id"] == "Q-5")
    assert question["status"] == "open"
    assert question["effective_blocking"] is True
    assert question[SAFE_DEFAULT_EVALUATION_FIELD]["status"] == "rejected"
    assert reason_code in question[SAFE_DEFAULT_EVALUATION_FIELD]["reason_codes"]
    assert not any(item.get("origin") == POLICY_DEFAULT for item in result.model.assumptions)


def test_legacy_and_v1_policies_never_apply_reviewed_defaults() -> None:
    payload, sources = _case()

    for version in ("legacy_blocking_v1", "clarification_disposition_v1"):
        candidate = deepcopy(payload)
        candidate["validation"].pop("clarification_policy_version", None)
        result = normalize_and_validate_requirement_model_v2(
            candidate,
            sources,
            clarification_policy_version=version,
        )
        question = next(item for item in result.model.open_questions if item["id"] == "Q-5")
        assert question["status"] == "open"
        assert question["effective_blocking"] is True
        assert SAFE_DEFAULT_EVALUATION_FIELD not in question
        assert not any(item.get("origin") == POLICY_DEFAULT for item in result.model.assumptions)


def test_canonical_policy_assumption_tampering_fails_closed() -> None:
    payload, sources = _case()
    canonical = _normalize(payload, sources).model.to_dict()
    assumption = next(item for item in canonical["assumptions"] if item.get("origin") == POLICY_DEFAULT)
    assumption["text"] = "Trust a modified runtime value."

    with pytest.raises(HarnessError) as raised:
        _normalize(canonical, sources)

    assert raised.value.error_code == "requirement_policy_default_integrity_failed"


def test_phase1_applies_policy_before_building_clarification_checkpoint() -> None:
    payload, sources = _case()
    source_text = sources["SRC-1"]
    source = UseCaseSource(
        dataset="controlled",
        use_case="SAFE-QUALITY",
        source_ref="controlled:SAFE-QUALITY",
        source_path=None,
        text=source_text,
        locator="controlled:SAFE-QUALITY",
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        settings = load_settings(
            root=ROOT,
            runtime_root=Path(temp_dir) / ".runtime",
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
            },
        )
        workflow = PhaseOneWorkflow(settings)

        def fake_refine(*_args, **kwargs):
            candidate = deepcopy(payload)
            candidate["run_id"] = str(kwargs["run_id"])
            candidate["source"].update(
                source_ref=source.source_ref,
                artifact_ref=str(kwargs["source_artifact_ref"]),
            )
            candidate["provenance_ledger"]["sources"][0].update(
                source_ref=source.source_ref,
                locator=source.locator,
                artifact_ref=str(kwargs["source_artifact_ref"]),
            )
            validation = normalize_and_validate_requirement_model_v2(candidate, sources)
            return RequirementModelV2RefinementResult(
                model=validation.model,
                initial_report=None,
                final_report=validation.report,
                repaired=False,
                attempts=[],
            )

        with patch.object(workflow.refiner_v2, "refine", side_effect=fake_refine):
            response = workflow._start_from_source(source=source, requested_by="test")

        assert response["pending_checkpoint"]["policy_default_count"] == 1
        assert response["pending_checkpoint"]["policy_default_assumption_ids"] == ["ASM-1"]
        assert response["quality_reports"]["requirement_policy_defaults"]["status"] == "applied"
        assert "requirement_policy_defaults" in response["latest_artifacts"]
        model_path = ROOT / response["latest_artifacts"]["requirement_model"]
        model = json.loads(model_path.read_text(encoding="utf-8"))
        question = next(item for item in model["open_questions"] if item["id"] == "Q-5")
        assert question["status"] == "superseded"
        assert any(item.get("policy_id") == POLICY_ID_QUALITY_NONE for item in model["assumptions"])
