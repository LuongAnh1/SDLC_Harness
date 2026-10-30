"""Rà soát độ đầy đủ của Requirement V2 theo phong cách Spec Kit clarify."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from ...core.config import phase1_llm_call_options
from ...core.errors import HarnessError
from ...llm.client import LlmClient
from ...llm.json_utils import parse_first_json_object
from .clarification_policy_v1 import CLARIFICATION_DISPOSITION_FIELD, DEFERRED_TO_DESIGN
from .model_v2 import RequirementModelV2
from .validator_v2 import RequirementModelV2ValidationReport, normalize_and_validate_requirement_model_v2


SCHEMA_VERSION = "clarification_analysis_v1"
DEFAULT_QUESTION_LIMIT = 5
COVERAGE_CATEGORIES = (
    "functional_scope",
    "domain_data",
    "interaction_ux",
    "quality_attributes",
    "integrations",
    "edge_failures",
    "constraints_tradeoffs",
    "terminology",
    "completion_signals",
    "placeholders",
)
_PROMPT_PATH = Path(__file__).with_name("prompts") / "requirement_coverage_v1.md"
_COVERAGE_STATUSES = {"clear", "partial", "missing"}
_ANSWER_MODES = {"multiple_choice", "short"}
_LEVELS = {"high", "medium", "low"}
_LEVEL_RANK = {"high": 0, "medium": 1, "low": 2}
_IMPLEMENTATION_PATTERN = re.compile(
    r"\b(?:react|fastify|prisma|postgres(?:ql)?|api route|database schema|file path|class diagram)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RequirementCoverageResult:
    model: RequirementModelV2
    validation_report: RequirementModelV2ValidationReport
    analysis: dict[str, Any]
    added_question_ids: list[str]


class RequirementCoverageReviewer:
    """Kết hợp coverage tất định với một lượt semantic review có giới hạn."""

    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def review(
        self,
        *,
        model: RequirementModelV2,
        source_text_by_id: dict[str, str],
        validation_report: RequirementModelV2ValidationReport,
        semantic: bool = True,
        question_limit: int = DEFAULT_QUESTION_LIMIT,
    ) -> RequirementCoverageResult:
        deterministic = _deterministic_analysis(model)
        semantic_analysis: dict[str, Any] | None = None
        if semantic and self.llm_client.settings.llm_mode != "fake":
            semantic_analysis = _semantic_analysis(
                self.llm_client,
                model=model,
                source_text_by_id=source_text_by_id,
                validation_report=validation_report,
            )
        analysis = _merge_analysis(deterministic, semantic_analysis)
        existing_blockers = sum(
            1
            for item in model.open_questions
            if isinstance(item, dict)
            and item.get("status") == "open"
            and item.get("effective_blocking") is True
        )
        available = max(0, question_limit - existing_blockers)
        revised, added = _adopt_candidates(
            model,
            analysis["question_candidates"],
            max_new_questions=available,
        )
        validated = normalize_and_validate_requirement_model_v2(revised, source_text_by_id)
        if not validated.report.ok:
            issue = validated.report.issues[0]
            raise HarnessError(
                "requirement_coverage_invalid",
                f"Coverage adoption produced an invalid Requirement V2 model: {issue.code} at {issue.path}.",
                run_id=model.run_id,
            )
        analysis["adopted_question_ids"] = added
        analysis["question_limit"] = question_limit
        analysis["existing_blocking_question_count"] = existing_blockers
        open_affects = [
            set(_string_list(item.get("affects")))
            for item in revised.open_questions
            if isinstance(item, dict) and item.get("status") == "open"
        ]
        represented_candidates = sum(
            1
            for candidate in analysis["question_candidates"]
            if set(_string_list(candidate.get("affects")))
            and any(
                set(_string_list(candidate.get("affects"))).intersection(affected)
                for affected in open_affects
            )
        )
        analysis["deferred_candidate_count"] = max(
            0,
            len(analysis["question_candidates"]) - represented_candidates,
        )
        if analysis["deferred_candidate_count"]:
            deferred = analysis.get("deferred")
            if not isinstance(deferred, list):
                deferred = []
            deferred.append(
                {
                    "reason": "clarification_session_limit_or_existing_coverage",
                    "count": analysis["deferred_candidate_count"],
                    "detail": (
                        "Candidates outside the current five-question boundary remain "
                        "outstanding until the user explicitly requests clarify_more."
                    ),
                }
            )
            analysis["deferred"] = deferred
        analysis["blocking_question_ids"] = list(validated.report.blocking_question_ids)
        return RequirementCoverageResult(
            model=validated.model,
            validation_report=validated.report,
            analysis=analysis,
            added_question_ids=added,
        )


def build_requirement_quality_checklist(
    *,
    model: RequirementModelV2,
    validation_report: RequirementModelV2ValidationReport,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tạo quality gate nhỏ, machine-readable cho checkpoint Requirement."""

    issue_codes = {item.code for item in validation_report.issues}
    blocking_ids = list(validation_report.blocking_question_ids)
    deferred_ids = [
        str(item.get("id") or "")
        for item in model.open_questions
        if isinstance(item, dict)
        and item.get("status") == "open"
        and isinstance(item.get(CLARIFICATION_DISPOSITION_FIELD), dict)
        and item[CLARIFICATION_DISPOSITION_FIELD].get("value") == DEFERRED_TO_DESIGN
        and str(item.get("id") or "")
    ]
    coverage = analysis.get("coverage") if isinstance(analysis, dict) else []
    covered_categories = {
        str(item.get("category") or "")
        for item in coverage if isinstance(item, dict)
    }
    checks = [
        {
            "id": "branch_behavior_complete_or_blocked",
            "passed": "branch_behavior_without_clarification" not in issue_codes,
            "detail": "Every alternative/exception branch is complete or linked to a blocking question.",
        },
        {
            "id": "acceptance_traceable",
            "passed": not {"acceptance_without_verifies", "acceptance_placeholder"}.intersection(issue_codes),
            "detail": "Acceptance criteria identify verifiable behavior and contain no placeholders.",
        },
        {
            "id": "coverage_taxonomy_reviewed",
            "passed": not analysis or covered_categories == set(COVERAGE_CATEGORIES),
            "detail": "All ten clarification coverage categories were reviewed when analysis is available.",
        },
        {
            "id": "blocking_clarifications_resolved",
            "passed": not blocking_ids,
            "detail": f"Open blocking questions: {len(blocking_ids)}.",
        },
    ]
    failures = [str(item["id"]) for item in checks if not item["passed"]]
    return {
        "artifact_version": "requirement_quality_checklist_v1",
        "run_id": model.run_id,
        "requirement_revision": model.revision,
        "status": "ready" if not failures and validation_report.ok else "blocked",
        "blocking_failure_count": len(failures) + len(validation_report.issues),
        "blocking_question_ids": blocking_ids,
        "deferred_to_design_count": len(deferred_ids),
        "deferred_to_design_question_ids": deferred_ids,
        "checks": checks,
        "validation_issue_codes": sorted(issue_codes),
    }

def _semantic_analysis(
    llm_client: LlmClient,
    *,
    model: RequirementModelV2,
    source_text_by_id: dict[str, str],
    validation_report: RequirementModelV2ValidationReport,
) -> dict[str, Any]:
    source_id = str(model.source.get("source_id") or "")
    source_text = source_text_by_id.get(source_id)
    if not isinstance(source_text, str):
        raise HarnessError(
            "source_snapshot_corrupted",
            f"Primary Requirement source snapshot is unavailable: {source_id}",
            run_id=model.run_id,
        )
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        template.replace("{{SOURCE_REF}}", str(model.source.get("source_ref") or ""))
        .replace("{{REQUIREMENT_REVISION}}", str(model.revision))
        .replace("{{SOURCE_TEXT}}", source_text)
        .replace("{{REQUIREMENT_MODEL}}", json.dumps(model.to_dict(), ensure_ascii=False, indent=2))
        .replace("{{VALIDATION_REPORT}}", json.dumps(validation_report.to_dict(), ensure_ascii=False, indent=2))
    )
    request_options = phase1_llm_call_options(
        llm_client.settings,
        "coverage",
        fallback_max_tokens=int(getattr(llm_client.settings, "llm_structured_max_tokens", 8192)),
    )
    completion = llm_client.complete_with_metadata(
        system=(
            "You review source-scoped requirement ambiguity. The supplied source closes scope: "
            "absence alone is not a gap. Return strict JSON only."
        ),
        prompt=prompt,
        json_mode=True,
        **request_options,
    )
    if completion.finish_reason.strip().lower() == "length":
        raise HarnessError(
            "requirement_coverage_truncated",
            "Requirement coverage review was truncated.",
            run_id=model.run_id,
        )
    try:
        payload = parse_first_json_object(
            completion.content,
            expected_keys={"schema_version", "source_ref", "requirement_revision", "coverage", "question_candidates", "deferred"},
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HarnessError(
            "requirement_coverage_invalid",
            "Requirement coverage review did not return the required JSON contract.",
            run_id=model.run_id,
        ) from exc
    return _validate_analysis(payload, model)


def _deterministic_analysis(model: RequirementModelV2) -> dict[str, Any]:
    data = model.to_dict()
    candidates: list[dict[str, Any]] = []
    incomplete: list[str] = []
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    for group in ("alternatives", "exceptions"):
        values = flows.get(group) if isinstance(flows, dict) else []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            flow_id = str(item.get("id") or "")
            missing_steps = not _string_list(item.get("steps"))
            missing_outcome = not _non_empty(item.get("outcome"))
            if not (missing_steps or missing_outcome):
                continue
            incomplete.append(flow_id)
            parts = []
            if missing_steps:
                parts.append("các bước hệ thống/người dùng thực hiện")
            if missing_outcome:
                parts.append("kết quả quan sát được cuối nhánh")
            candidates.append(
                {
                    "category": "edge_failures" if group == "exceptions" else "interaction_ux",
                    "question": f"Với nhánh {flow_id}, {(' và '.join(parts))} cụ thể là gì?",
                    "why_it_matters": "Nếu thiếu thông tin này, Developer và QA không thể triển khai và kiểm thử nhánh một cách xác định.",
                    "answer_mode": "short",
                    "options": [],
                    "recommended_option": None,
                    "suggested_answer": None,
                    "proposed_disposition": "human_required",
                    "decision_category": "observable_behavior",
                    "why_it_is_safe": None,
                    "reversible": False,
                    "impact": "high",
                    "uncertainty": "high",
                    "affects": [flow_id, *_related_acceptance_refs(data, flow_id)],
                    "evidence_refs": _string_list(item.get("evidence_refs")),
                }
            )
    coverage = []
    for category in COVERAGE_CATEGORIES:
        affected = incomplete if category in {"interaction_ux", "edge_failures", "completion_signals", "placeholders"} else []
        coverage.append(
            {
                "category": category,
                "status": "partial" if affected else "clear",
                "requirement_refs": affected,
                "evidence_refs": [],
                "rationale": (
                    "Có nhánh nghiệp vụ chưa mô tả đủ bước hoặc kết quả quan sát được."
                    if affected
                    else "Không phát hiện khoảng trống cấu trúc bằng kiểm tra tất định."
                ),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_ref": str(model.source.get("source_ref") or ""),
        "requirement_revision": model.revision,
        "coverage": coverage,
        "question_candidates": candidates,
        "deferred": [],
        "review_mode": "deterministic",
    }


def _validate_analysis(payload: dict[str, Any], model: RequirementModelV2) -> dict[str, Any]:
    issues: list[str] = []
    data = deepcopy(payload)
    if data.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version must be {SCHEMA_VERSION}")
    if data.get("source_ref") != model.source.get("source_ref"):
        issues.append("source_ref must match Requirement Model V2")
    if data.get("requirement_revision") != model.revision:
        issues.append("requirement_revision must match Requirement Model V2")
    known_requirements = _identified_ids(model.to_dict())
    known_evidence = {str(item.get("id")) for item in model.evidence if isinstance(item, dict)}
    open_question_affects = {
        str(item.get("id")): [
            ref
            for ref in _string_list(item.get("affects"))
            if ref in known_requirements
        ]
        for item in model.open_questions
        if isinstance(item, dict)
        and item.get("status") == "open"
        and str(item.get("id") or "")
    }
    coverage = data.get("coverage")
    if not isinstance(coverage, list):
        issues.append("coverage must be an array")
        coverage = []
    seen: list[str] = []
    for index, item in enumerate(coverage):
        if not isinstance(item, dict):
            issues.append(f"coverage[{index}] must be an object")
            continue
        category = str(item.get("category") or "")
        seen.append(category)
        if category not in COVERAGE_CATEGORIES:
            issues.append(f"coverage[{index}].category is invalid")
        if item.get("status") not in _COVERAGE_STATUSES:
            issues.append(f"coverage[{index}].status is invalid")
        if not _non_empty(item.get("rationale")):
            issues.append(f"coverage[{index}].rationale must not be empty")
        _validate_refs(item.get("requirement_refs"), known_requirements, f"coverage[{index}].requirement_refs", issues)
        _validate_refs(item.get("evidence_refs"), known_evidence, f"coverage[{index}].evidence_refs", issues)
    if set(seen) != set(COVERAGE_CATEGORIES) or len(seen) != len(COVERAGE_CATEGORIES):
        issues.append("coverage must contain each taxonomy category exactly once")
    candidates = data.get("question_candidates")
    if not isinstance(candidates, list):
        issues.append("question_candidates must be an array")
        candidates = []
    if len(candidates) > 5:
        issues.append("question_candidates must contain at most 5 items")
    normalizations: list[dict[str, object]] = []
    for index, item in enumerate(candidates):
        normalization = _normalize_candidate_question_refs(
            item,
            index=index,
            open_question_affects=open_question_affects,
        )
        if normalization:
            normalizations.append(normalization)
        _validate_candidate(item, index, known_requirements, known_evidence, issues)
    if not isinstance(data.get("deferred"), list):
        issues.append("deferred must be an array")
    if issues:
        raise HarnessError("requirement_coverage_invalid", "; ".join(issues), run_id=model.run_id)
    if normalizations:
        data["normalizations"] = normalizations
    data["question_candidates"] = sorted(
        candidates,
        key=lambda item: (
            _LEVEL_RANK[str(item["impact"])],
            _LEVEL_RANK[str(item["uncertainty"])],
            COVERAGE_CATEGORIES.index(str(item["category"])),
            str(item["question"]),
        ),
    )
    return data


def _normalize_candidate_question_refs(
    item: Any,
    *,
    index: int,
    open_question_affects: dict[str, list[str]],
) -> dict[str, object] | None:
    """Mở rộng Q-* đã tồn tại về stable refs; Q-* lạ vẫn để validator chặn."""

    if not isinstance(item, dict):
        return None
    original = _string_list(item.get("affects"))
    question_refs = [ref for ref in original if ref in open_question_affects]
    if not question_refs:
        return None
    normalized: list[str] = []
    for ref in original:
        replacements = open_question_affects.get(ref, [ref])
        for replacement in replacements:
            if replacement not in normalized:
                normalized.append(replacement)
    item["affects"] = normalized
    return {
        "kind": "existing_open_question_affects_expanded",
        "candidate_index": index,
        "question_refs": question_refs,
        "requirement_refs": normalized,
    }


def _validate_candidate(
    item: Any,
    index: int,
    known_requirements: set[str],
    known_evidence: set[str],
    issues: list[str],
) -> None:
    path = f"question_candidates[{index}]"
    if not isinstance(item, dict):
        issues.append(f"{path} must be an object")
        return
    item.setdefault("proposed_disposition", "human_required")
    item.setdefault("decision_category", "")
    item.setdefault("why_it_is_safe", None)
    item.setdefault("reversible", False)
    if item.get("category") not in COVERAGE_CATEGORIES:
        issues.append(f"{path}.category is invalid")
    question = str(item.get("question") or "")
    if not question.strip().endswith("?"):
        issues.append(f"{path}.question must end in ?")
    if not _non_empty(item.get("why_it_matters")):
        issues.append(f"{path}.why_it_matters must not be empty")
    if item.get("answer_mode") not in _ANSWER_MODES:
        issues.append(f"{path}.answer_mode is invalid")
    if item.get("impact") not in _LEVELS or item.get("uncertainty") not in _LEVELS:
        issues.append(f"{path} impact/uncertainty is invalid")
    disposition = item.get("proposed_disposition")
    if disposition not in {"human_required", "agent_default", "deferred_to_design"}:
        issues.append(f"{path}.proposed_disposition is invalid")
    decision_category = str(item.get("decision_category") or "")
    if disposition in {"agent_default", "deferred_to_design"} and not decision_category:
        issues.append(f"{path}.decision_category must not be empty")
    if disposition == "agent_default":
        if item.get("impact") != "low" or item.get("uncertainty") != "low":
            issues.append(f"{path}.agent_default must have low impact and uncertainty")
        if item.get("reversible") is not True:
            issues.append(f"{path}.agent_default must be reversible")
        if not _non_empty(item.get("why_it_is_safe")):
            issues.append(f"{path}.why_it_is_safe must not be empty for agent_default")
    options = item.get("options")
    if item.get("answer_mode") == "multiple_choice":
        if not isinstance(options, list) or not 2 <= len(options) <= 5:
            issues.append(f"{path}.options must contain 2-5 choices")
        else:
            option_keys: list[str] = []
            for index, option in enumerate(options):
                option_path = f"{path}.options[{index}]"
                if not isinstance(option, dict):
                    issues.append(f"{option_path} must be an object")
                    continue
                key = str(option.get("key") or "").strip()
                answer = str(option.get("answer") or option.get("label") or "").strip()
                if not key or not answer:
                    issues.append(f"{option_path} must contain non-empty key and answer")
                option_keys.append(key)
            if len(option_keys) != len(set(option_keys)):
                issues.append(f"{path}.options keys must be unique")
            recommended = item.get("recommended_option")
            if recommended is not None and str(recommended) not in option_keys:
                issues.append(f"{path}.recommended_option must reference an option key")
    elif options != []:
        issues.append(f"{path}.options must be empty for short answers")
    _validate_refs(item.get("affects"), known_requirements, f"{path}.affects", issues, required=True)
    _validate_refs(item.get("evidence_refs"), known_evidence, f"{path}.evidence_refs", issues, required=True)
    if _IMPLEMENTATION_PATTERN.search(question + " " + str(item.get("why_it_matters") or "")):
        issues.append(f"{path} contains implementation detail")


def _merge_analysis(deterministic: dict[str, Any], semantic: dict[str, Any] | None) -> dict[str, Any]:
    if semantic is None:
        return deterministic
    merged = deepcopy(semantic)
    candidates = list(deterministic["question_candidates"])
    seen = [set(_string_list(item.get("affects"))) for item in candidates]
    for item in semantic["question_candidates"]:
        affected = set(_string_list(item.get("affects")))
        if affected and not any(affected.intersection(existing) for existing in seen):
            candidates.append(deepcopy(item))
            seen.append(affected)
    candidates.sort(
        key=lambda item: (
            _LEVEL_RANK.get(str(item.get("impact")), 9),
            _LEVEL_RANK.get(str(item.get("uncertainty")), 9),
            str(item.get("question") or ""),
        )
    )
    selected = candidates[:DEFAULT_QUESTION_LIMIT]
    dropped = candidates[DEFAULT_QUESTION_LIMIT:]
    merged["question_candidates"] = selected
    deferred = merged.get("deferred")
    if not isinstance(deferred, list):
        deferred = []
    deferred.extend(
        {
            "reason": "coverage_candidate_limit",
            "category": str(item.get("category") or ""),
            "question": str(item.get("question") or ""),
            "affects": _string_list(item.get("affects")),
        }
        for item in dropped
    )
    merged["deferred"] = deferred
    merged["review_mode"] = "hybrid"
    merged["deterministic_candidate_count"] = len(deterministic["question_candidates"])
    return merged


def _adopt_candidates(
    model: RequirementModelV2,
    candidates: list[dict[str, Any]],
    *,
    max_new_questions: int | None = None,
) -> tuple[RequirementModelV2, list[str]]:
    data = model.to_dict()
    questions = data["open_questions"]
    added: list[str] = []
    for candidate in candidates:
        affects = _string_list(candidate.get("affects"))
        if not _candidate_traceable(data, affects):
            continue
        existing = next(
            (
                item
                for item in questions
                if isinstance(item, dict)
                and item.get("status") == "open"
                and set(affects).intersection(_string_list(item.get("affects")))
            ),
            None,
        )
        created = existing is None
        if created:
            if max_new_questions is not None and len(added) >= max_new_questions:
                continue
            question_id = _next_question_id(questions)
            existing = {
                "id": question_id,
                "question": str(candidate["question"]),
                "category": str(candidate.get("decision_category") or "")
                or _model_question_category(str(candidate["category"]), affects),
                "proposed_blocking": candidate.get("proposed_disposition") == "human_required",
                "effective_blocking": candidate.get("proposed_disposition") == "human_required",
                "affects": affects,
                "depends_on": [],
                "status": "open",
                "answer_guidance": None,
                "evidence_refs": _string_list(candidate.get("evidence_refs")),
            }
            questions.append(existing)
            added.append(question_id)
        else:
            existing["affects"] = list(dict.fromkeys(_string_list(existing.get("affects")) + affects))
            existing["evidence_refs"] = list(
                dict.fromkeys(
                    _string_list(existing.get("evidence_refs"))
                    + _string_list(candidate.get("evidence_refs"))
                )
            )
        proposed_disposition = str(candidate.get("proposed_disposition") or "human_required")
        if not created and existing.get("proposed_blocking") is True:
            proposed_disposition = "human_required"
        existing.update(
            {
                "coverage_category": str(candidate["category"]),
                "why_it_matters": str(candidate["why_it_matters"]),
                "answer_mode": str(candidate["answer_mode"]),
                "options": _normalize_options(candidate.get("options")),
                "recommended_option": candidate.get("recommended_option"),
                "suggested_answer": candidate.get("suggested_answer"),
                "proposed_disposition": proposed_disposition,
                "why_it_is_safe": candidate.get("why_it_is_safe"),
                "reversible": candidate.get("reversible") is True,
                "impact": str(candidate["impact"]),
                "uncertainty": str(candidate["uncertainty"]),
            }
        )
        _attach_question(data, str(existing["id"]), _string_list(existing.get("affects")))
    return RequirementModelV2.from_dict(data), added


def _candidate_traceable(data: dict[str, Any], affects: list[str]) -> bool:
    """Chỉ nhận candidate nối được vào surface hiện có, không tự đoán graph."""

    affected = set(affects)
    contract = data.get("scenario_contract")
    scenarios = contract.get("scenarios") if isinstance(contract, dict) else []
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if not isinstance(scenario, dict):
            continue
        scope = set(
            _string_list(scenario.get("requirement_refs"))
            + _string_list(scenario.get("acceptance_refs"))
        )
        related = affected.intersection(scope)
        if not related:
            continue
        surfaces = scenario.get("surfaces")
        mapped = any(
            isinstance(surface, dict)
            and surface.get("status") != "not_applicable"
            and bool(related.intersection(_string_list(surface.get("requirement_refs"))))
            for surface in (surfaces.values() if isinstance(surfaces, dict) else [])
        )
        if not mapped:
            return False
    return True


def _attach_question(data: dict[str, Any], question_id: str, affects: list[str]) -> None:
    affected = set(affects)
    _append_question_ref(data, affected, question_id)
    contract = data.get("scenario_contract")
    scenarios = contract.get("scenarios") if isinstance(contract, dict) else []
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if not isinstance(scenario, dict):
            continue
        related = affected.intersection(_string_list(scenario.get("requirement_refs")) + _string_list(scenario.get("acceptance_refs")))
        if not related:
            continue
        _append_unique(scenario, "open_question_refs", question_id)
        surfaces = scenario.get("surfaces")
        for surface in surfaces.values() if isinstance(surfaces, dict) else []:
            if not isinstance(surface, dict) or surface.get("status") == "not_applicable":
                continue
            refs = set(_string_list(surface.get("requirement_refs")))
            if related.intersection(refs):
                _append_unique(surface, "open_question_refs", question_id)


def _append_question_ref(value: Any, affected: set[str], question_id: str) -> None:
    if isinstance(value, dict):
        if str(value.get("id") or "") in affected and "open_question_refs" in value:
            _append_unique(value, "open_question_refs", question_id)
        for nested in value.values():
            _append_question_ref(nested, affected, question_id)
    elif isinstance(value, list):
        for nested in value:
            _append_question_ref(nested, affected, question_id)


def _append_unique(item: dict[str, Any], field: str, value: str) -> None:
    values = _string_list(item.get(field))
    if value not in values:
        values.append(value)
    item[field] = values


def _normalize_options(value: object) -> list[dict[str, object]]:
    """Chuẩn hóa option semantic về contract `key` + `answer` dùng chung."""

    normalized: list[dict[str, object]] = []
    for option in value if isinstance(value, list) else []:
        if not isinstance(option, dict):
            continue
        key = str(option.get("key") or "").strip()
        answer = str(option.get("answer") or option.get("label") or "").strip()
        if not key or not answer:
            continue
        item: dict[str, object] = {"key": key, "answer": answer}
        implication = str(option.get("implication") or "").strip()
        if implication:
            item["implication"] = implication
        normalized.append(item)
    return normalized


def _next_question_id(questions: list[Any]) -> str:
    numbers = []
    for item in questions:
        if isinstance(item, dict):
            match = re.fullmatch(r"Q-([1-9]\d*)", str(item.get("id") or ""))
            if match:
                numbers.append(int(match.group(1)))
    return f"Q-{max(numbers, default=0) + 1}"


def _related_acceptance_refs(data: dict[str, Any], requirement_id: str) -> list[str]:
    contract = data.get("scenario_contract")
    scenarios = contract.get("scenarios") if isinstance(contract, dict) else []
    refs: list[str] = []
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if not isinstance(scenario, dict) or requirement_id not in _string_list(scenario.get("requirement_refs")):
            continue
        for acceptance_ref in _string_list(scenario.get("acceptance_refs")):
            if acceptance_ref not in refs:
                refs.append(acceptance_ref)
    return refs


def _model_question_category(category: str, affects: list[str]) -> str:
    if category == "domain_data":
        return "data_identity"
    if category == "completion_signals":
        return "acceptance_criteria"
    if category in {"interaction_ux", "integrations", "edge_failures"}:
        return "observable_behavior"
    if any(ref.startswith("AC-") for ref in affects):
        return "acceptance_criteria"
    return "business_rule"


def _identified_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        identifier = value.get("id")
        if isinstance(identifier, str) and identifier and not identifier.startswith(("Q-", "D-", "E-", "SC-", "SRC-")):
            found.add(identifier)
        for nested in value.values():
            found.update(_identified_ids(nested))
    elif isinstance(value, list):
        for nested in value:
            found.update(_identified_ids(nested))
    return found


def _validate_refs(value: Any, allowed: set[str], path: str, issues: list[str], *, required: bool = False) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        issues.append(f"{path} must be an array of strings")
        return
    if required and not value:
        issues.append(f"{path} must not be empty")
    dangling = sorted(set(value).difference(allowed))
    if dangling:
        issues.append(f"{path} contains dangling refs: {', '.join(dangling)}")


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
