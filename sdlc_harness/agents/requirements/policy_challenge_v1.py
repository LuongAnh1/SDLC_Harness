"""Mutation tất định để con người thu hồi một reviewed policy default."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from ...core.errors import HarnessError
from ...loop import canonical_json_hash
from ...storage.runtime import utc_now_iso
from .model_v2 import RequirementModelV2
from .safe_defaults_v1 import (
    EVALUATION_FIELD,
    POLICY_ID_QUALITY_NONE,
    POLICY_ORIGIN,
    REGISTRY_VERSION,
    validate_reviewed_safe_default_integrity,
)


ARTIFACT_KEY = "requirement_policy_default_challenge"
ARTIFACT_PREFIX = "requirement_policy_default_challenge_v1"
SCHEMA_VERSION = "requirement_policy_default_challenge_v1"
DECISION_KIND = "policy_default_challenge"


@dataclass(frozen=True)
class RequirementPolicyDefaultChallenge:
    """Kết quả draft trước khi workflow chạy normalizer/validator lần cuối."""

    model: RequirementModelV2
    decision: dict[str, Any]
    withdrawn_assumption: dict[str, Any]
    override_target_ids: tuple[str, ...]
    before_model_sha256: str

    def build_report(self, validated_model: RequirementModelV2) -> dict[str, object]:
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": validated_model.run_id,
            "requirement_revision": validated_model.revision,
            "before_requirement_model_sha256": self.before_model_sha256,
            "after_requirement_model_sha256": canonical_json_hash(validated_model.to_dict()),
            "withdrawn_assumption": deepcopy(self.withdrawn_assumption),
            "override_target_ids": list(self.override_target_ids),
            "challenge_decision": deepcopy(self.decision),
        }
        report["report_digest"] = canonical_json_hash(report)
        return report


def challenge_reviewed_safe_default(
    model: RequirementModelV2,
    *,
    assumption_id: str,
    instruction: str,
    challenged_by: str,
    challenged_at: str | None = None,
) -> RequirementPolicyDefaultChallenge:
    """Thu hồi đúng một active policy assumption và mở lại source question."""

    target_id = assumption_id.strip()
    challenge_text = instruction.strip()
    actor = challenged_by.strip()
    if not target_id:
        raise HarnessError(
            "requirement_policy_default_stale",
            "Policy-default assumption id must not be empty.",
            run_id=model.run_id,
        )
    if not challenge_text:
        raise HarnessError(
            "requirement_policy_challenge_empty",
            "Policy-default challenge instruction must not be empty.",
            run_id=model.run_id,
        )
    if not actor:
        raise HarnessError(
            "requirement_policy_challenged_by_empty",
            "Policy-default challenge actor must not be empty.",
            run_id=model.run_id,
        )

    validate_reviewed_safe_default_integrity(model)
    data = model.to_dict()
    assumptions = _dict_items(data.get("assumptions"))
    assumption = next((item for item in assumptions if item.get("id") == target_id), None)
    if not isinstance(assumption, dict) or assumption.get("origin") != POLICY_ORIGIN:
        raise HarnessError(
            "requirement_policy_default_stale",
            f"Policy-default assumption is not active: {target_id}",
            run_id=model.run_id,
        )
    if assumption.get("policy_registry_version") != REGISTRY_VERSION:
        raise HarnessError(
            "requirement_policy_default_integrity_failed",
            "Policy-default assumption uses an unsupported registry version.",
            run_id=model.run_id,
        )

    question_id = str(assumption.get("source_question_id") or "")
    question = next(
        (item for item in _dict_items(data.get("open_questions")) if item.get("id") == question_id),
        None,
    )
    evaluation = question.get(EVALUATION_FIELD) if isinstance(question, dict) else None
    if (
        not isinstance(question, dict)
        or question.get("status") != "superseded"
        or not isinstance(evaluation, dict)
        or evaluation.get("status") != "applied"
        or evaluation.get("assumption_id") != target_id
    ):
        raise HarnessError(
            "requirement_policy_default_integrity_failed",
            "Policy-default assumption is not linked to its active source question.",
            run_id=model.run_id,
        )

    before_sha256 = canonical_json_hash(data)
    data["revision"] = model.revision + 1
    data["assumptions"] = [item for item in assumptions if item.get("id") != target_id]
    question["status"] = "open"
    question["suggested_policy_id"] = None
    question.pop(EVALUATION_FIELD, None)
    override_target_ids = _create_override_targets(
        data,
        assumption=assumption,
        question=question,
    )

    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
        data["decisions"] = decisions
    previous = _latest_question_decision(decisions, question_id)
    if previous is not None:
        previous["status"] = "superseded"
    decision = {
        "id": _next_decision_id(decisions),
        "question_id": question_id,
        "answer": challenge_text,
        "answered_by": actor,
        "answered_at": challenged_at or utc_now_iso(),
        "affected_requirement_ids": [],
        "status": "insufficient",
        "supersedes_decision_id": previous.get("id") if previous is not None else None,
        "submission_hash": _challenge_submission_hash(target_id, challenge_text),
        "decision_kind": DECISION_KIND,
        "challenged_assumption_id": target_id,
        "challenged_policy_id": str(assumption.get("policy_id") or ""),
        "policy_registry_version": REGISTRY_VERSION,
    }
    decisions.append(decision)
    return RequirementPolicyDefaultChallenge(
        model=RequirementModelV2.from_dict(data),
        decision=deepcopy(decision),
        withdrawn_assumption=deepcopy(assumption),
        override_target_ids=tuple(override_target_ids),
        before_model_sha256=before_sha256,
    )


def challenge_quality_summary(report: dict[str, object]) -> dict[str, object]:
    decision = report.get("challenge_decision")
    assumption = report.get("withdrawn_assumption")
    return {
        "status": "challenged",
        "requirement_revision": report.get("requirement_revision"),
        "report_digest": report.get("report_digest"),
        "decision_id": decision.get("id") if isinstance(decision, dict) else None,
        "question_id": decision.get("question_id") if isinstance(decision, dict) else None,
        "assumption_id": assumption.get("id") if isinstance(assumption, dict) else None,
        "policy_id": assumption.get("policy_id") if isinstance(assumption, dict) else None,
        "challenged_by": decision.get("answered_by") if isinstance(decision, dict) else None,
        "override_target_ids": list(report.get("override_target_ids") or []),
    }


def _create_override_targets(
    data: dict[str, Any],
    *,
    assumption: dict[str, Any],
    question: dict[str, Any],
) -> list[str]:
    """Tạo requirement placeholder mà Human Decision hiện hành có thể cập nhật."""

    if assumption.get("policy_id") != POLICY_ID_QUALITY_NONE:
        raise HarnessError(
            "requirement_policy_challenge_unsupported",
            "Reviewed policy does not define a human-override target factory.",
            run_id=str(data.get("run_id") or "") or None,
        )
    quality_requirements = data.get("quality_requirements")
    if not isinstance(quality_requirements, list):
        raise HarnessError(
            "requirement_policy_default_integrity_failed",
            "Requirement model has no quality_requirements collection.",
            run_id=str(data.get("run_id") or "") or None,
        )
    target_id = _next_requirement_id(quality_requirements, prefix="QR")
    question_id = str(question.get("id") or "")
    quality_requirements.append(
        {
            "id": target_id,
            "text": "UNKNOWN_FROM_SOURCE: quality target requires an explicit human decision.",
            "status": "unknown",
            "evidence_refs": list(assumption.get("evidence_refs") or []),
            "open_question_refs": [question_id],
        }
    )
    question["affects"] = [target_id]
    return [target_id]


def _challenge_submission_hash(assumption_id: str, instruction: str) -> str:
    normalized = " ".join(instruction.strip().split()).casefold()
    raw = f"{assumption_id}\0{normalized}".encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _next_decision_id(decisions: list[object]) -> str:
    numbers: list[int] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"D-([1-9]\d*)", str(item.get("id") or ""))
        if match:
            numbers.append(int(match.group(1)))
    return f"D-{max(numbers, default=0) + 1}"


def _next_requirement_id(items: list[object], *, prefix: str) -> str:
    numbers: list[int] = []
    pattern = re.compile(rf"{re.escape(prefix)}-([1-9]\d*)")
    for item in items:
        if not isinstance(item, dict):
            continue
        match = pattern.fullmatch(str(item.get("id") or ""))
        if match:
            numbers.append(int(match.group(1)))
    return f"{prefix}-{max(numbers, default=0) + 1}"


def _latest_question_decision(decisions: list[object], question_id: str) -> dict[str, Any] | None:
    for item in reversed(decisions):
        if isinstance(item, dict) and item.get("question_id") == question_id:
            return item
    return None


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
