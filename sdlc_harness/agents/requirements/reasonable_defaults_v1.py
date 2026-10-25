"""Áp dụng reasonable default do Requirement Agent đề xuất dưới guardrail Python.

Module này không chứa catalog câu trả lời. DeepSeek đề xuất đáp án; Harness chỉ
cho phép áp dụng khi disposition policy xác nhận đây là lựa chọn ít rủi ro, có thể
đảo ngược và không chạm ranh giới nghiệp vụ được bảo vệ.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ...core.errors import HarnessError
from ...loop import canonical_json_hash
from .clarification_policy_v1 import (
    AGENT_DEFAULT,
    REASONABLE_DEFAULT_POLICY_VERSION,
    classify_clarification_question,
    proposed_default_answer,
)
from .model_v2 import RequirementModelV2


AGENT_DEFAULT_ORIGIN = "agent_reasonable_default"
EVALUATION_FIELD = "reasonable_default_evaluation"
ARTIFACT_KEY = "requirement_reasonable_defaults"
ARTIFACT_PREFIX = "requirement_reasonable_defaults_v1"
REPORT_SCHEMA_VERSION = "requirement_reasonable_defaults_v1"
DECISION_KIND_CHALLENGE = "assumption_challenge"


def apply_agent_reasonable_defaults(
    data: dict[str, Any],
    *,
    surface_blockers: set[str],
) -> None:
    """Rebuild agent-owned assumptions deterministically on a mutable model copy."""

    validation = data.get("validation")
    if (
        isinstance(validation, dict)
        and validation.get("clarification_policy_version") == REASONABLE_DEFAULT_POLICY_VERSION
    ):
        validate_agent_reasonable_default_integrity(RequirementModelV2.from_dict(data))

    previous_ids = {
        str(item.get("source_question_id") or ""): str(item.get("id") or "")
        for item in _dict_items(data.get("assumptions"))
        if item.get("origin") == AGENT_DEFAULT_ORIGIN and str(item.get("id") or "")
    }
    reserved_ids = {
        str(item.get("id") or "")
        for item in _dict_items(data.get("assumptions"))
        if str(item.get("id") or "")
    }
    reserved_ids.update(
        str(item.get("challenged_assumption_id") or "")
        for item in _dict_items(data.get("decisions"))
        if item.get("decision_kind") == DECISION_KIND_CHALLENGE
        and str(item.get("challenged_assumption_id") or "")
    )
    _remove_previous_output(data)
    assumptions = data.get("assumptions")
    if not isinstance(assumptions, list):
        return

    used_ids = set(reserved_ids)
    for question in _dict_items(data.get("open_questions")):
        if str(question.get("proposed_disposition") or "") != AGENT_DEFAULT:
            continue
        classification = classify_clarification_question(
            question,
            surface_blockers=surface_blockers,
        )
        reasons = list(classification.get("reason_codes") or [])
        if _has_overlapping_source_conflict(data, question):
            reasons.append("agent_default_source_conflict")
        if _has_human_decision(data, question):
            reasons.append("agent_default_human_decision_exists")
        answer = proposed_default_answer(question)
        matched = classification.get("disposition") == AGENT_DEFAULT and not reasons
        if classification.get("disposition") == AGENT_DEFAULT:
            reasons = []
            if _has_overlapping_source_conflict(data, question):
                reasons.append("agent_default_source_conflict")
            if _has_human_decision(data, question):
                reasons.append("agent_default_human_decision_exists")
            matched = not reasons
        evaluation: dict[str, object] = {
            "status": "matched" if matched else "rejected",
            "policy_version": REASONABLE_DEFAULT_POLICY_VERSION,
            "reason_codes": reasons or list(classification.get("reason_codes") or []),
            "affects": _string_items(question.get("affects")),
            "evidence_refs": _string_items(question.get("evidence_refs")),
            "proposed_answer": answer,
            "assumption_id": None,
        }
        question[EVALUATION_FIELD] = evaluation
        if not matched or answer is None:
            continue

        question_id = str(question.get("id") or "")
        assumption_id = previous_ids.get(question_id) or _next_assumption_id(used_ids)
        used_ids.add(assumption_id)
        proposal = {
            "question_id": question_id,
            "category": str(question.get("category") or ""),
            "answer": answer,
            "why_it_is_safe": str(question.get("why_it_is_safe") or "").strip(),
            "impact": question.get("impact"),
            "uncertainty": question.get("uncertainty"),
            "reversible": question.get("reversible"),
            "affects": _string_items(question.get("affects")),
            "evidence_refs": _string_items(question.get("evidence_refs")),
        }
        assumption = {
            "id": assumption_id,
            "text": answer,
            "status": "assumption",
            "evidence_refs": list(proposal["evidence_refs"]),
            "open_question_refs": [],
            "reason": proposal["why_it_is_safe"],
            "affects": list(proposal["affects"]),
            "origin": AGENT_DEFAULT_ORIGIN,
            "category": proposal["category"],
            "source_question_id": question_id,
            "impact": "low",
            "uncertainty": "low",
            "reversible": True,
            "proposal_hash": canonical_json_hash(proposal),
            "proposed_by": "requirement_agent",
        }
        assumptions.append(assumption)
        _remove_open_question_refs(data, question_id)
        question["status"] = "superseded"
        evaluation["status"] = "applied"
        evaluation["assumption_id"] = assumption_id


def build_agent_reasonable_default_report(model: RequirementModelV2) -> dict[str, object]:
    data = model.to_dict()
    evaluations: list[dict[str, object]] = []
    for question in _dict_items(data.get("open_questions")):
        evaluation = question.get(EVALUATION_FIELD)
        if isinstance(evaluation, dict):
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
        "policy_version": REASONABLE_DEFAULT_POLICY_VERSION,
        "run_id": model.run_id,
        "requirement_revision": model.revision,
        "requirement_model_sha256": canonical_json_hash(data),
        "evaluations": evaluations,
        "summary": {
            "proposal_count": len(evaluations),
            "applied_count": len(applied),
            "rejected_count": len(rejected),
            "assumption_ids": [str(item.get("assumption_id") or "") for item in applied],
        },
    }
    report["report_digest"] = canonical_json_hash(report)
    return report


def reasonable_default_quality_summary(report: dict[str, object]) -> dict[str, object]:
    summary = report.get("summary")
    return {
        "status": "applied" if isinstance(summary, dict) and summary.get("applied_count") else "observed",
        "policy_version": report.get("policy_version"),
        "requirement_revision": report.get("requirement_revision"),
        "report_digest": report.get("report_digest"),
        "summary": dict(summary) if isinstance(summary, dict) else {},
    }


def validate_agent_reasonable_default_integrity(model: RequirementModelV2) -> None:
    data = model.to_dict()
    questions = {
        str(item.get("id") or ""): item
        for item in _dict_items(data.get("open_questions"))
        if str(item.get("id") or "")
    }
    seen: set[str] = set()
    for assumption in _dict_items(data.get("assumptions")):
        if assumption.get("origin") != AGENT_DEFAULT_ORIGIN:
            continue
        question_id = str(assumption.get("source_question_id") or "")
        question = questions.get(question_id)
        evaluation = question.get(EVALUATION_FIELD) if isinstance(question, dict) else None
        expected_answer = proposed_default_answer(question) if isinstance(question, dict) else None
        proposal = (
            {
                "question_id": question_id,
                "category": str(question.get("category") or ""),
                "answer": expected_answer,
                "why_it_is_safe": str(question.get("why_it_is_safe") or "").strip(),
                "impact": question.get("impact"),
                "uncertainty": question.get("uncertainty"),
                "reversible": question.get("reversible"),
                "affects": _string_items(question.get("affects")),
                "evidence_refs": _string_items(question.get("evidence_refs")),
            }
            if isinstance(question, dict)
            else None
        )
        invalid = (
            not question_id
            or question_id in seen
            or not isinstance(question, dict)
            or question.get("status") != "superseded"
            or not isinstance(evaluation, dict)
            or evaluation.get("status") != "applied"
            or evaluation.get("assumption_id") != assumption.get("id")
            or assumption.get("text") != expected_answer
            or assumption.get("status") != "assumption"
            or assumption.get("reversible") is not True
            or assumption.get("impact") != "low"
            or assumption.get("uncertainty") != "low"
            or assumption.get("origin") != AGENT_DEFAULT_ORIGIN
            or assumption.get("proposed_by") != "requirement_agent"
            or not isinstance(proposal, dict)
            or assumption.get("proposal_hash") != canonical_json_hash(proposal)
            or assumption.get("category") != proposal.get("category")
            or assumption.get("reason") != proposal.get("why_it_is_safe")
            or assumption.get("affects") != proposal.get("affects")
            or assumption.get("evidence_refs") != proposal.get("evidence_refs")
        )
        if invalid:
            raise HarnessError(
                "requirement_agent_default_integrity_failed",
                "Agent reasonable-default assumption does not match its source proposal.",
                run_id=model.run_id,
            )
        seen.add(question_id)


def _remove_previous_output(data: dict[str, Any]) -> None:
    assumptions = data.get("assumptions")
    if isinstance(assumptions, list):
        data["assumptions"] = [
            item
            for item in assumptions
            if not isinstance(item, dict) or item.get("origin") != AGENT_DEFAULT_ORIGIN
        ]
    for question in _dict_items(data.get("open_questions")):
        previous = question.pop(EVALUATION_FIELD, None)
        if (
            question.get("status") == "superseded"
            and isinstance(previous, dict)
            and previous.get("status") == "applied"
        ):
            question["status"] = "open"


def _has_overlapping_source_conflict(data: dict[str, Any], question: dict[str, Any]) -> bool:
    question_id = str(question.get("id") or "")
    affected = set(_string_items(question.get("affects")))
    return any(
        str(item.get("id") or "") != question_id
        and item.get("status") == "open"
        and item.get("category") == "source_conflict"
        and bool(affected.intersection(_string_items(item.get("affects"))))
        for item in _dict_items(data.get("open_questions"))
    )


def _has_human_decision(data: dict[str, Any], question: dict[str, Any]) -> bool:
    question_id = str(question.get("id") or "")
    return any(
        item.get("question_id") == question_id
        for item in _dict_items(data.get("decisions"))
    )


def _remove_open_question_refs(value: object, question_id: str) -> None:
    if isinstance(value, dict):
        refs = value.get("open_question_refs")
        if isinstance(refs, list):
            value["open_question_refs"] = [item for item in refs if item != question_id]
        for nested in value.values():
            _remove_open_question_refs(nested, question_id)
    elif isinstance(value, list):
        for nested in value:
            _remove_open_question_refs(nested, question_id)


def _next_assumption_id(used: set[str]) -> str:
    index = 1
    while f"ASM-{index}" in used:
        index += 1
    return f"ASM-{index}"


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
