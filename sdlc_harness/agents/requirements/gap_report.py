"""Phân loại deterministic các khoảng trống của Requirement trước khi routing."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from ...core.errors import HarnessError
from .model_v2 import QuestionStatus, RequirementModelV2


GAP_REPORT_VERSION = "requirement_gap_report_v1"
GAP_TYPES = frozenset(
    {"missing_evidence", "source_unavailable", "source_conflict", "business_decision"}
)
MISSING_EVIDENCE_CODES = frozenset(
    {
        "requirement_evidence_observation_invalid",
        "evidence_not_in_source",
        "explicit_without_evidence",
        "explicit_with_invalid_evidence",
        "question_without_evidence",
        "source_conflict_evidence_insufficient",
    }
)
SOURCE_UNAVAILABLE_CODES = frozenset(
    {
        "source_snapshot_corrupted",
        "requirement_source_catalog_invalid",
        "requirement_source_input_drift",
        "source_snapshot_missing",
        "source_fingerprint_mismatch",
    }
)
_TYPE_ORDER = {
    "source_unavailable": 0,
    "missing_evidence": 1,
    "source_conflict": 2,
    "business_decision": 3,
}


def build_requirement_gap_report(
    *,
    run_id: str,
    attempts: list[dict[str, object]] | None = None,
    model: RequirementModelV2 | None = None,
    error: HarnessError | None = None,
    source_manifest_input_hash: str | None = None,
    previous_report: dict[str, object] | None = None,
) -> dict[str, object]:
    """Tạo report canonical; mọi ID, owner và route đều do Python sở hữu."""

    candidates: list[dict[str, object]] = []
    if isinstance(previous_report, dict):
        for item in _dict_items(previous_report.get("gaps")):
            if item.get("type") == "missing_evidence":
                candidates.append(_without_id(item))

    for attempt in attempts or []:
        validation = attempt.get("validation_report")
        issues = validation.get("issues") if isinstance(validation, dict) else []
        for issue in _dict_items(issues):
            code = str(issue.get("code") or "")
            if code not in MISSING_EVIDENCE_CODES:
                continue
            candidates.append(
                {
                    "type": "missing_evidence",
                    "status": "resolved" if model is not None else "unresolved",
                    "owner": "requirement_agent",
                    "route": "continue_source_loop",
                    "retryable": True,
                    "question_ref": None,
                    "affects": [],
                    "evidence_refs": [],
                    "source_ids": [],
                    "error_code": code,
                    "location": str(issue.get("path") or ""),
                    "message": str(issue.get("message") or "Requirement evidence is missing."),
                }
            )

    if error is not None and error.error_code in SOURCE_UNAVAILABLE_CODES:
        candidates.append(
            {
                "type": "source_unavailable",
                "status": "unresolved",
                "owner": (
                    "harness"
                    if error.error_code == "requirement_source_catalog_invalid"
                    else "environment"
                ),
                "route": "fail",
                "retryable": False,
                "question_ref": None,
                "affects": [],
                "evidence_refs": [],
                "source_ids": [],
                "error_code": error.error_code,
                "location": "source",
                "message": error.message,
            }
        )

    if model is not None:
        data = model.to_dict()
        evidence_sources = {
            str(item.get("id") or ""): str(item.get("source_id") or "")
            for item in _dict_items(data.get("evidence"))
        }
        for question in _dict_items(data.get("open_questions")):
            question_id = str(question.get("id") or "")
            question_status = str(question.get("status") or "")
            gap_type = (
                "source_conflict"
                if str(question.get("category") or "") == "source_conflict"
                else "business_decision"
            )
            evidence_refs = _string_items(question.get("evidence_refs"))
            unresolved = question_status == QuestionStatus.OPEN.value
            blocking = question.get("effective_blocking") is True
            candidates.append(
                {
                    "type": gap_type,
                    "status": "unresolved" if unresolved else "resolved",
                    "owner": "human",
                    "route": "clarification" if unresolved and blocking else "record_only",
                    "retryable": False,
                    "question_ref": question_id,
                    "affects": _string_items(question.get("affects")),
                    "evidence_refs": evidence_refs,
                    "source_ids": sorted(
                        {
                            evidence_sources[ref]
                            for ref in evidence_refs
                            if evidence_sources.get(ref)
                        }
                    ),
                    "error_code": None,
                    "location": f"open_questions.{question_id}",
                    "message": str(question.get("question") or ""),
                }
            )

    gaps = _normalize_gaps(candidates, final_model_present=model is not None)
    unresolved_routes = {
        str(item.get("route") or "")
        for item in gaps
        if item.get("status") == "unresolved"
    }
    if error is not None or "fail" in unresolved_routes:
        status = "failed"
    elif "clarification" in unresolved_routes:
        status = "clarification_required"
    else:
        status = "clear"
    payload: dict[str, object] = {
        "schema_version": GAP_REPORT_VERSION,
        "run_id": run_id,
        "requirement_revision": model.revision if model is not None else None,
        "source_manifest_input_hash": source_manifest_input_hash or None,
        "status": status,
        "gaps": gaps,
        "summary": gap_report_summary({"status": status, "gaps": gaps}),
    }
    payload["report_digest"] = _digest(payload)
    return payload


def validate_requirement_gap_report(report: object, *, run_id: str) -> None:
    if not isinstance(report, dict):
        raise HarnessError("requirement_gap_report_invalid", "Requirement gap report must be an object.")
    if report.get("schema_version") != GAP_REPORT_VERSION or report.get("run_id") != run_id:
        raise HarnessError(
            "requirement_gap_report_invalid",
            "Requirement gap report identity is invalid.",
            run_id=run_id,
        )
    gaps = report.get("gaps")
    if not isinstance(gaps, list) or any(not isinstance(item, dict) for item in gaps):
        raise HarnessError(
            "requirement_gap_report_invalid",
            "Requirement gap report gaps must be a list of objects.",
            run_id=run_id,
        )
    expected_ids = [f"GAP-{index}" for index in range(1, len(gaps) + 1)]
    if [item.get("id") for item in gaps] != expected_ids:
        raise HarnessError(
            "requirement_gap_report_invalid",
            "Requirement gap report IDs are not stable and contiguous.",
            run_id=run_id,
        )
    if any(item.get("type") not in GAP_TYPES for item in gaps):
        raise HarnessError(
            "requirement_gap_report_invalid",
            "Requirement gap report contains an unknown gap type.",
            run_id=run_id,
        )
    expected = deepcopy(report)
    digest = expected.pop("report_digest", None)
    if digest != _digest(expected):
        raise HarnessError(
            "requirement_gap_report_invalid",
            "Requirement gap report digest does not match its content.",
            run_id=run_id,
        )


def gap_report_summary(report: dict[str, object]) -> dict[str, object]:
    gaps = _dict_items(report.get("gaps"))
    by_type = {
        gap_type: sum(1 for item in gaps if item.get("type") == gap_type)
        for gap_type in sorted(GAP_TYPES)
    }
    return {
        "status": str(report.get("status") or "clear"),
        "total": len(gaps),
        "unresolved": sum(1 for item in gaps if item.get("status") == "unresolved"),
        "by_type": by_type,
    }


def _normalize_gaps(
    candidates: list[dict[str, object]],
    *,
    final_model_present: bool,
) -> list[dict[str, object]]:
    deduplicated: dict[str, dict[str, object]] = {}
    for raw in candidates:
        item = deepcopy(raw)
        if final_model_present and item.get("type") == "missing_evidence":
            item["status"] = "resolved"
        identity = _gap_identity(item)
        deduplicated[identity] = item
    ordered = sorted(
        deduplicated.values(),
        key=lambda item: (
            _TYPE_ORDER.get(str(item.get("type") or ""), 99),
            str(item.get("question_ref") or ""),
            str(item.get("error_code") or ""),
            str(item.get("location") or ""),
            str(item.get("message") or ""),
        ),
    )
    return [{"id": f"GAP-{index}", **item} for index, item in enumerate(ordered, start=1)]


def _gap_identity(item: dict[str, object]) -> str:
    if item.get("question_ref"):
        return f"question:{item.get('question_ref')}"
    return "|".join(
        str(item.get(key) or "")
        for key in ("type", "error_code", "location", "message")
    )


def _without_id(item: dict[str, object]) -> dict[str, object]:
    return {key: deepcopy(value) for key, value in item.items() if key != "id"}


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
