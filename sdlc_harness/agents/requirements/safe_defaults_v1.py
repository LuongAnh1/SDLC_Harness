"""Registry safe-default đã review cho Requirement Model V2.

LLM chỉ được đề xuất ``suggested_policy_id``. Module này sở hữu điều kiện match,
câu trả lời cố định và mutation tạo assumption. Mọi trường hợp không chắc chắn đều
giữ nguyên question để con người quyết định.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from ...core.errors import HarnessError
from ...loop import canonical_json_hash
from .model_v2 import RequirementModelV2


REGISTRY_VERSION = "reviewed_safe_defaults_v1"
POLICY_VERSION = "clarification_disposition_v2"
POLICY_ID_QUALITY_NONE = "quality_none_specified_v1"
POLICY_DEFAULT = "policy_default"
POLICY_ORIGIN = "policy_default"
EVALUATION_FIELD = "safe_default_evaluation"
ARTIFACT_KEY = "requirement_policy_defaults"
ARTIFACT_PREFIX = "requirement_policy_defaults_v1"
REPORT_SCHEMA_VERSION = "requirement_policy_defaults_v1"

FIXED_QUALITY_NONE_TEXT = (
    "No additional quality or non-functional requirement is assumed beyond "
    "the explicitly stated functional acceptance criteria."
)
FIXED_QUALITY_NONE_REASON = (
    "Reviewed safe default applied because the cited source explicitly states "
    "that no quality requirements are specified."
)
QUALITY_NONE_REASON_CODE = "safe_default_quality_none_explicit"

_QUALITY_NONE_MARKER = re.compile(
    r"^\s*(?:quality|non-functional) requirements?\s*:\s*none specified\.?\s*$",
    re.IGNORECASE,
)
_ALLOWED_AFFECT_PREFIXES = ("UC-", "GOAL-")


def safe_default_prompt_catalog() -> str:
    """Catalog nhỏ cho prompt; không trao câu trả lời hoặc authority cho LLM."""

    return (
        f"- `{POLICY_ID_QUALITY_NONE}`: propose only when a cited evidence quote is an exact "
        "line saying `Quality requirements: None specified` (or `Non-functional requirements: "
        "None specified`), there are no quality requirements, the question affects only UC/GOAL, "
        "and a multiple-choice option key `none_required` is recommended. Otherwise use null."
    )


def apply_reviewed_safe_defaults(
    data: dict[str, Any],
    *,
    source_text_by_id: dict[str, str],
    surface_blockers: set[str],
) -> None:
    """Rebuild policy-owned output deterministically on a mutable model copy."""

    validation = data.get("validation")
    if (
        isinstance(validation, dict)
        and validation.get("clarification_policy_version") == POLICY_VERSION
    ):
        _validate_existing_policy_output(data)
    previous_policy_ids = {
        (str(item.get("source_question_id") or ""), str(item.get("policy_id") or "")): str(item.get("id") or "")
        for item in _dict_items(data.get("assumptions"))
        if item.get("origin") == POLICY_ORIGIN and str(item.get("id") or "")
    }
    reserved_assumption_ids = {
        str(item.get("id") or "")
        for item in _dict_items(data.get("assumptions"))
        if str(item.get("id") or "")
    }
    reserved_assumption_ids.update(
        str(item.get("challenged_assumption_id") or "")
        for item in _dict_items(data.get("decisions"))
        if item.get("decision_kind") == "policy_default_challenge"
        and str(item.get("challenged_assumption_id") or "")
    )
    _remove_previous_policy_output(data)
    evidence_by_id = {
        str(item.get("id") or ""): item
        for item in _dict_items(data.get("evidence"))
        if str(item.get("id") or "")
    }
    requirements_by_id = _requirement_items_by_id(data)
    questions = _dict_items(data.get("open_questions"))
    used_assumption_ids = set(reserved_assumption_ids)
    assumptions = data.get("assumptions")
    if not isinstance(assumptions, list):
        return

    for question in questions:
        question_id = str(question.get("id") or "")
        proposed_policy = str(question.get("suggested_policy_id") or "").strip()
        if not proposed_policy:
            continue
        evaluation = _evaluate_question(
            data=data,
            question=question,
            proposed_policy=proposed_policy,
            evidence_by_id=evidence_by_id,
            requirements_by_id=requirements_by_id,
            source_text_by_id=source_text_by_id,
            surface_blockers=surface_blockers,
        )
        question[EVALUATION_FIELD] = evaluation
        if evaluation["status"] != "matched":
            continue
        assumption_id = previous_policy_ids.get((question_id, proposed_policy)) or _next_assumption_id(
            used_assumption_ids
        )
        used_assumption_ids.add(assumption_id)
        assumption = {
            "id": assumption_id,
            "text": FIXED_QUALITY_NONE_TEXT,
            "status": "assumption",
            "evidence_refs": list(evaluation["evidence_refs"]),
            "open_question_refs": [],
            "reason": FIXED_QUALITY_NONE_REASON,
            "affects": list(evaluation["affects"]),
            "origin": POLICY_ORIGIN,
            "policy_id": POLICY_ID_QUALITY_NONE,
            "policy_registry_version": REGISTRY_VERSION,
            "source_question_id": str(question.get("id") or ""),
            "reason_code": QUALITY_NONE_REASON_CODE,
        }
        assumptions.append(assumption)
        question["status"] = "superseded"
        evaluation["status"] = "applied"
        evaluation["assumption_id"] = assumption_id


def build_reviewed_safe_default_report(model: RequirementModelV2) -> dict[str, object]:
    """Tạo public audit report từ canonical model sau normalization."""

    data = model.to_dict()
    evaluations = []
    for question in _dict_items(data.get("open_questions")):
        evaluation = question.get(EVALUATION_FIELD)
        if not isinstance(evaluation, dict):
            continue
        evaluations.append(
            {
                "question_id": str(question.get("id") or ""),
                "question": str(question.get("question") or ""),
                **deepcopy(evaluation),
            }
        )
    applied = [item for item in evaluations if item.get("status") == "applied"]
    rejected = [item for item in evaluations if item.get("status") == "rejected"]
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "registry_version": REGISTRY_VERSION,
        "policy_version": POLICY_VERSION,
        "run_id": model.run_id,
        "requirement_revision": model.revision,
        "requirement_model_sha256": canonical_json_hash(data),
        "evaluations": evaluations,
        "summary": {
            "proposal_count": len(evaluations),
            "applied_count": len(applied),
            "rejected_count": len(rejected),
            "assumption_ids": [str(item.get("assumption_id") or "") for item in applied],
            "policy_ids": sorted({str(item.get("policy_id") or "") for item in applied}),
        },
    }
    report["report_digest"] = canonical_json_hash(report)
    return report


def safe_default_quality_summary(report: dict[str, object]) -> dict[str, object]:
    summary = report.get("summary")
    return {
        "status": "applied" if isinstance(summary, dict) and summary.get("applied_count") else "observed",
        "registry_version": report.get("registry_version"),
        "policy_version": report.get("policy_version"),
        "requirement_revision": report.get("requirement_revision"),
        "report_digest": report.get("report_digest"),
        "summary": dict(summary) if isinstance(summary, dict) else {},
    }


def _evaluate_question(
    *,
    data: dict[str, Any],
    question: dict[str, Any],
    proposed_policy: str,
    evidence_by_id: dict[str, dict[str, Any]],
    requirements_by_id: dict[str, dict[str, Any]],
    source_text_by_id: dict[str, str],
    surface_blockers: set[str],
) -> dict[str, object]:
    reasons: list[str] = []
    question_id = str(question.get("id") or "")
    affects = _string_items(question.get("affects"))
    evidence_refs = _string_items(question.get("evidence_refs"))

    if proposed_policy != POLICY_ID_QUALITY_NONE:
        reasons.append("safe_default_policy_unknown")
    if question.get("status") != "open":
        reasons.append("safe_default_question_not_open")
    if question.get("category") != "business_rule":
        reasons.append("safe_default_category_not_allowed")
    if question.get("coverage_category") != "quality_attributes":
        reasons.append("safe_default_coverage_category_not_allowed")
    if _string_items(question.get("depends_on")):
        reasons.append("safe_default_question_has_dependencies")
    if question.get("answer_mode") != "multiple_choice":
        reasons.append("safe_default_answer_mode_not_allowed")
    option_keys = {
        str(item.get("key") or "")
        for item in _dict_items(question.get("options"))
        if str(item.get("key") or "")
    }
    if question.get("recommended_option") != "none_required" or "none_required" not in option_keys:
        reasons.append("safe_default_recommendation_not_allowed")
    if not affects or any(not ref.startswith(_ALLOWED_AFFECT_PREFIXES) for ref in affects):
        reasons.append("safe_default_affects_not_allowed")
    if any(not _target_is_explicit(data, ref, requirements_by_id) for ref in affects):
        reasons.append("safe_default_target_not_explicit")
    if question_id in surface_blockers:
        reasons.append("safe_default_surface_blocker")
    if _has_overlapping_source_conflict(data, question_id=question_id, affects=affects):
        reasons.append("safe_default_source_conflict")
    if _dict_items(data.get("quality_requirements")):
        reasons.append("safe_default_quality_requirement_exists")
    if any(
        item.get("question_id") == question_id and item.get("status") == "accepted"
        for item in _dict_items(data.get("decisions"))
    ):
        reasons.append("safe_default_human_decision_exists")
    matching_evidence = [
        ref
        for ref in evidence_refs
        if _evidence_has_exact_marker(evidence_by_id.get(ref), source_text_by_id)
    ]
    if not matching_evidence:
        reasons.append("safe_default_explicit_marker_missing")

    return {
        "status": "rejected" if reasons else "matched",
        "policy_id": proposed_policy,
        "registry_version": REGISTRY_VERSION,
        "reason_code": QUALITY_NONE_REASON_CODE if not reasons else "safe_default_policy_rejected",
        "reason_codes": reasons or [QUALITY_NONE_REASON_CODE],
        "affects": affects,
        "evidence_refs": matching_evidence,
        "fixed_answer": FIXED_QUALITY_NONE_TEXT if not reasons else None,
        "assumption_id": None,
    }


def _remove_previous_policy_output(data: dict[str, Any]) -> None:
    assumptions = data.get("assumptions")
    if isinstance(assumptions, list):
        data["assumptions"] = [
            item
            for item in assumptions
            if not isinstance(item, dict) or item.get("origin") != POLICY_ORIGIN
        ]
    for question in _dict_items(data.get("open_questions")):
        previous = question.pop(EVALUATION_FIELD, None)
        if (
            question.get("status") == "superseded"
            and isinstance(previous, dict)
            and previous.get("status") == "applied"
        ):
            question["status"] = "open"


def _validate_existing_policy_output(data: dict[str, Any]) -> None:
    questions = {
        str(item.get("id") or ""): item
        for item in _dict_items(data.get("open_questions"))
        if str(item.get("id") or "")
    }
    seen_questions: set[str] = set()
    for assumption in _dict_items(data.get("assumptions")):
        if assumption.get("origin") != POLICY_ORIGIN:
            continue
        question_id = str(assumption.get("source_question_id") or "")
        question = questions.get(question_id)
        evaluation = question.get(EVALUATION_FIELD) if isinstance(question, dict) else None
        expected = {
            "text": FIXED_QUALITY_NONE_TEXT,
            "status": "assumption",
            "reason": FIXED_QUALITY_NONE_REASON,
            "policy_id": POLICY_ID_QUALITY_NONE,
            "policy_registry_version": REGISTRY_VERSION,
            "reason_code": QUALITY_NONE_REASON_CODE,
        }
        invalid = (
            not question_id
            or question_id in seen_questions
            or not isinstance(question, dict)
            or question.get("status") != "superseded"
            or not isinstance(evaluation, dict)
            or evaluation.get("status") != "applied"
            or evaluation.get("assumption_id") != assumption.get("id")
            or any(assumption.get(key) != value for key, value in expected.items())
            or _string_items(assumption.get("affects")) != _string_items(question.get("affects"))
            or _string_items(assumption.get("evidence_refs")) != _string_items(evaluation.get("evidence_refs"))
        )
        if invalid:
            raise HarnessError(
                "requirement_policy_default_integrity_failed",
                "Canonical policy-default assumption does not match the reviewed registry.",
                run_id=str(data.get("run_id") or "") or None,
            )
        seen_questions.add(question_id)
    for question_id, question in questions.items():
        evaluation = question.get(EVALUATION_FIELD)
        if (
            isinstance(evaluation, dict)
            and evaluation.get("status") == "applied"
            and question_id not in seen_questions
        ):
            raise HarnessError(
                "requirement_policy_default_integrity_failed",
                "Canonical policy-default question is missing its assumption.",
                run_id=str(data.get("run_id") or "") or None,
            )


def validate_reviewed_safe_default_integrity(model: RequirementModelV2) -> None:
    """Fail closed nếu policy assumption hiện hành không khớp reviewed registry."""

    _validate_existing_policy_output(model.to_dict())


def _evidence_has_exact_marker(
    evidence: dict[str, Any] | None,
    source_text_by_id: dict[str, str],
) -> bool:
    if not isinstance(evidence, dict):
        return False
    quote = str(evidence.get("quote") or "")
    if not any(_QUALITY_NONE_MARKER.fullmatch(line) for line in quote.splitlines()):
        return False
    source_id = str(evidence.get("source_id") or "")
    source_text = source_text_by_id.get(source_id)
    return isinstance(source_text, str) and quote in source_text


def _has_overlapping_source_conflict(
    data: dict[str, Any],
    *,
    question_id: str,
    affects: list[str],
) -> bool:
    affected = set(affects)
    return any(
        str(item.get("id") or "") != question_id
        and item.get("status") == "open"
        and item.get("category") == "source_conflict"
        and bool(affected.intersection(_string_items(item.get("affects"))))
        for item in _dict_items(data.get("open_questions"))
    )


def _requirement_items_by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            item_id = value.get("id")
            if isinstance(item_id, str) and item_id and not item_id.startswith(("Q-", "D-", "E-", "SC-", "SRC-")):
                found[item_id] = value
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(data)
    return found


def _target_is_explicit(
    data: dict[str, Any],
    ref: str,
    requirements_by_id: dict[str, dict[str, Any]],
) -> bool:
    item = requirements_by_id.get(ref)
    if not isinstance(item, dict):
        return False
    if ref.startswith("UC-"):
        use_case = data.get("use_case")
        goal = use_case.get("goal") if isinstance(use_case, dict) else None
        return isinstance(goal, dict) and goal.get("status") == "explicit"
    return item.get("status") == "explicit"


def _next_assumption_id(used: set[str]) -> str:
    index = 1
    while f"ASM-{index}" in used:
        index += 1
    return f"ASM-{index}"


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
