"""Phân loại disposition cho câu hỏi Requirement bằng guardrail tất định.

LLM được đề xuất ``human_required``, ``agent_default`` hoặc
``deferred_to_design``. Python mới sở hữu quyết định cuối cùng và luôn fail-safe
về ``human_required`` khi proposal thiếu dữ liệu hoặc vượt ranh giới an toàn.
"""

from __future__ import annotations

from typing import Any

from ...loop import canonical_json_hash
from .model_v2 import RequirementModelV2


SHADOW_SCHEMA_VERSION = "requirement_clarification_policy_shadow_v1"
SHADOW_POLICY_VERSION = "clarification_disposition_v1"
REASONABLE_DEFAULT_POLICY_VERSION = "clarification_disposition_v3"
REASONABLE_DEFAULT_SHADOW_POLICY_VERSION = "clarification_disposition_v3_shadow"
LEGACY_BLOCKING_POLICY_VERSION = "legacy_blocking_v1"
CLARIFICATION_DISPOSITION_FIELD = "clarification_disposition"
SHADOW_ARTIFACT_KEY = "requirement_clarification_policy_shadow"
SHADOW_ARTIFACT_PREFIX = "requirement_clarification_policy_shadow_v1"

HARD_BLOCKING_CATEGORIES = frozenset(
    {
        "business_rule",
        "acceptance_criteria",
        "data_identity",
        "api_contract",
        "observable_behavior",
        "source_conflict",
    }
)
BEHAVIOR_PREFIXES = ("MF-", "AF-", "EF-", "POST-", "BR-", "AC-", "QR-")

HUMAN_REQUIRED = "human_required"
DEFERRED_TO_DESIGN = "deferred_to_design"
AGENT_DEFAULT = "agent_default"
IRRELEVANT = "irrelevant"

DESIGN_OWNED_CATEGORIES = frozenset(
    {
        "terminology",
        "presentation_preference",
        "visual_style",
        "layout",
        "copy_wording",
        "implementation_preference",
    }
)
AGENT_DEFAULT_CATEGORIES = frozenset(
    {
        "optional_context",
        "conventional_ux_behavior",
        "noncritical_recovery",
    }
)
AGENT_DEFAULT_FORBIDDEN_PREFIXES = ("BR-", "AC-", "QR-")


def build_clarification_policy_shadow(model: RequirementModelV2) -> dict[str, object]:
    """Tạo báo cáo deterministic; không mutate model hoặc quyết định workflow."""

    data = model.to_dict()
    surface_blockers = surface_blocking_question_ids(data)
    entries: list[dict[str, object]] = []
    resolved_question_count = 0
    for question in _dict_items(data.get("open_questions")):
        if question.get("status") != "open":
            resolved_question_count += 1
            continue
        entries.append(_classify_open_question(question, surface_blockers=surface_blockers))

    summary = {
        "open_question_count": len(entries),
        "resolved_or_superseded_question_count": resolved_question_count,
        "current_blocking_count": sum(1 for item in entries if item["current_effective_blocking"] is True),
        "shadow_human_required_count": _count(entries, HUMAN_REQUIRED),
        "shadow_deferred_to_design_count": _count(entries, DEFERRED_TO_DESIGN),
        "shadow_agent_default_count": _count(entries, AGENT_DEFAULT),
        "shadow_irrelevant_count": _count(entries, IRRELEVANT),
        "would_change_count": sum(1 for item in entries if item["would_change"] is True),
    }
    report: dict[str, object] = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "policy_version": REASONABLE_DEFAULT_SHADOW_POLICY_VERSION,
        "target_policy_version": REASONABLE_DEFAULT_POLICY_VERSION,
        "mode": "shadow",
        "run_id": model.run_id,
        "requirement_revision": model.revision,
        "requirement_model_sha256": canonical_json_hash(data),
        "questions": entries,
        "summary": summary,
    }
    report["report_digest"] = canonical_json_hash(report)
    return report


def shadow_quality_summary(report: dict[str, object]) -> dict[str, object]:
    """Chiếu báo cáo thành summary nhỏ để gắn vào workflow state."""

    summary = report.get("summary")
    return {
        "status": "observed",
        "mode": "shadow",
        "policy_version": report.get("policy_version"),
        "requirement_revision": report.get("requirement_revision"),
        "report_digest": report.get("report_digest"),
        "summary": dict(summary) if isinstance(summary, dict) else {},
    }


def shadow_error_summary(exc: Exception) -> dict[str, object]:
    """Tạo warning an toàn; shadow error không được làm workflow fail."""

    error_code = getattr(exc, "error_code", "clarification_policy_shadow_failed")
    return {
        "status": "warning",
        "mode": "shadow",
        "policy_version": REASONABLE_DEFAULT_SHADOW_POLICY_VERSION,
        "error_code": str(error_code),
    }


def classify_clarification_question(
    question: dict[str, Any],
    *,
    surface_blockers: set[str] | None = None,
) -> dict[str, object]:
    """Phân loại một open question bằng rule dùng chung cho shadow và cutover."""

    question_id = str(question.get("id") or "")
    category = str(question.get("category") or "")
    coverage_category = str(question.get("coverage_category") or "")
    affects = _string_items(question.get("affects"))
    behavior_refs = [ref for ref in affects if ref.startswith(BEHAVIOR_PREFIXES)]
    blockers = surface_blockers or set()
    proposed_disposition = str(question.get("proposed_disposition") or "")

    if category == "source_conflict":
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_source_conflict"]
    elif category in HARD_BLOCKING_CATEGORIES:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_hard_category"]
    elif question_id in blockers:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_surface_clarification_required"]
    elif proposed_disposition == AGENT_DEFAULT:
        default_reasons = _agent_default_rejection_reasons(question)
        if default_reasons:
            disposition = HUMAN_REQUIRED
            reasons = default_reasons
        else:
            disposition = AGENT_DEFAULT
            reasons = ["agent_default_low_risk", "agent_default_reversible"]
    elif behavior_refs:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_behavior_reference"]
    elif category == "terminology" and coverage_category in {"", "terminology"}:
        disposition = DEFERRED_TO_DESIGN
        reasons = [
            "shadow_terminology_only",
            "shadow_no_hard_impact",
        ]
    elif proposed_disposition == DEFERRED_TO_DESIGN and category in DESIGN_OWNED_CATEGORIES:
        disposition = DEFERRED_TO_DESIGN
        reasons = ["shadow_design_owned", "shadow_no_hard_impact"]
    else:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_fail_safe"]
    return {
        "disposition": disposition,
        "effective_blocking": disposition == HUMAN_REQUIRED,
        "reason_codes": reasons,
    }


def _classify_open_question(
    question: dict[str, Any],
    *,
    surface_blockers: set[str],
) -> dict[str, object]:
    question_id = str(question.get("id") or "")
    category = str(question.get("category") or "")
    coverage_category = str(question.get("coverage_category") or "")
    affects = _string_items(question.get("affects"))
    proposed = question.get("proposed_blocking") is True
    current_effective = question.get("effective_blocking") is True
    behavior_refs = [ref for ref in affects if ref.startswith(BEHAVIOR_PREFIXES)]

    current_reasons: list[str] = []
    if proposed:
        current_reasons.append("current_llm_proposed")
    if category in HARD_BLOCKING_CATEGORIES:
        current_reasons.append("current_hard_category")
    if behavior_refs:
        current_reasons.append("current_behavior_reference")
    if not current_reasons:
        current_reasons.append("current_non_blocking")

    classification = classify_clarification_question(question, surface_blockers=surface_blockers)
    disposition = str(classification["disposition"])
    shadow_reasons = list(classification["reason_codes"])
    shadow_effective = classification["effective_blocking"] is True
    return {
        "question_id": question_id,
        "question": str(question.get("question") or ""),
        "category": category,
        "coverage_category": coverage_category or None,
        "status": "open",
        "proposed_blocking": proposed,
        "proposed_disposition": str(question.get("proposed_disposition") or "") or None,
        "current_effective_blocking": current_effective,
        "current_reason_codes": current_reasons,
        "affects": affects,
        "shadow_disposition": disposition,
        "shadow_effective_blocking": shadow_effective,
        "shadow_reason_codes": shadow_reasons,
        "would_change": current_effective != shadow_effective,
    }


def surface_blocking_question_ids(data: dict[str, Any]) -> set[str]:
    contract = data.get("scenario_contract")
    scenarios = contract.get("scenarios") if isinstance(contract, dict) else []
    question_ids: set[str] = set()
    for scenario in _dict_items(scenarios):
        surfaces = scenario.get("surfaces")
        if not isinstance(surfaces, dict):
            continue
        for surface in surfaces.values():
            if not isinstance(surface, dict) or surface.get("status") != "clarification_required":
                continue
            question_ids.update(_string_items(surface.get("open_question_refs")))
    return question_ids


def proposed_default_answer(question: dict[str, Any]) -> str | None:
    """Lấy đúng đáp án LLM đề xuất mà không tự suy diễn nội dung mới."""

    answer_mode = str(question.get("answer_mode") or "short")
    if answer_mode == "multiple_choice":
        recommended = str(question.get("recommended_option") or "")
        for option in _dict_items(question.get("options")):
            if str(option.get("key") or "") == recommended:
                answer = str(option.get("answer") or "").strip()
                return answer or None
        fallback = str(question.get("suggested_answer") or "").strip()
        return fallback or None
    answer = str(question.get("suggested_answer") or "").strip()
    return answer or None


def _agent_default_rejection_reasons(question: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    category = str(question.get("category") or "")
    affects = _string_items(question.get("affects"))
    if category not in AGENT_DEFAULT_CATEGORIES:
        reasons.append("agent_default_category_not_allowed")
    if question.get("proposed_blocking") is True:
        reasons.append("agent_default_proposed_blocking")
    if question.get("impact") != "low" or question.get("uncertainty") != "low":
        reasons.append("agent_default_risk_not_low")
    if question.get("reversible") is not True:
        reasons.append("agent_default_not_reversible")
    if not str(question.get("why_it_is_safe") or "").strip():
        reasons.append("agent_default_safety_reason_missing")
    if _string_items(question.get("depends_on")):
        reasons.append("agent_default_has_dependencies")
    if not affects or any(ref.startswith(AGENT_DEFAULT_FORBIDDEN_PREFIXES) for ref in affects):
        reasons.append("agent_default_affects_not_allowed")
    if proposed_default_answer(question) is None:
        reasons.append("agent_default_answer_missing")
    return reasons


def _count(entries: list[dict[str, object]], disposition: str) -> int:
    return sum(1 for item in entries if item.get("shadow_disposition") == disposition)


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
