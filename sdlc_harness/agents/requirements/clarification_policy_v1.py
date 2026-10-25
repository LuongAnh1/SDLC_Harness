"""Phân loại shadow cho câu hỏi Requirement mà không có decision authority.

Module này chỉ quan sát Requirement Model V2 đã chuẩn hóa. Kết quả không được
dùng để sửa ``effective_blocking``, queue clarification hoặc checkpoint hiện tại.
"""

from __future__ import annotations

from typing import Any

from ...loop import canonical_json_hash
from .model_v2 import RequirementModelV2


SHADOW_SCHEMA_VERSION = "requirement_clarification_policy_shadow_v1"
SHADOW_POLICY_VERSION = "clarification_disposition_v1"
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
POLICY_DEFAULT = "policy_default"
IRRELEVANT = "irrelevant"


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
        "shadow_policy_default_count": _count(entries, POLICY_DEFAULT),
        "shadow_irrelevant_count": _count(entries, IRRELEVANT),
        "would_change_count": sum(1 for item in entries if item["would_change"] is True),
    }
    report: dict[str, object] = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "policy_version": SHADOW_POLICY_VERSION,
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
        "policy_version": SHADOW_POLICY_VERSION,
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

    if category == "source_conflict":
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_source_conflict"]
    elif category in HARD_BLOCKING_CATEGORIES:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_hard_category"]
    elif behavior_refs:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_behavior_reference"]
    elif question_id in blockers:
        disposition = HUMAN_REQUIRED
        reasons = ["shadow_surface_clarification_required"]
    elif category == "terminology" and coverage_category in {"", "terminology"}:
        disposition = DEFERRED_TO_DESIGN
        reasons = ["shadow_terminology_only", "shadow_no_hard_impact"]
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


def _count(entries: list[dict[str, object]], disposition: str) -> int:
    return sum(1 for item in entries if item.get("shadow_disposition") == disposition)


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
