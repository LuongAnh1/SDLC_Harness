"""Mutation tất định để con người thu hồi một agent reasonable default."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import re
from typing import Any

from ...core.errors import HarnessError
from ...loop import canonical_json_hash
from ...storage.runtime import utc_now_iso
from .clarification_policy_v1 import HUMAN_REQUIRED
from .model_v2 import RequirementModelV2
from .reasonable_defaults_v1 import (
    AGENT_DEFAULT_ORIGIN,
    DECISION_KIND_CHALLENGE,
    EVALUATION_FIELD,
    validate_agent_reasonable_default_integrity,
)


ARTIFACT_KEY = "requirement_assumption_challenge"
ARTIFACT_PREFIX = "requirement_assumption_challenge_v1"
SCHEMA_VERSION = "requirement_assumption_challenge_v1"


@dataclass(frozen=True)
class RequirementAssumptionChallenge:
    model: RequirementModelV2
    decision: dict[str, Any]
    withdrawn_assumption: dict[str, Any]
    before_model_sha256: str

    def build_report(self, validated_model: RequirementModelV2) -> dict[str, object]:
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": validated_model.run_id,
            "requirement_revision": validated_model.revision,
            "before_requirement_model_sha256": self.before_model_sha256,
            "after_requirement_model_sha256": canonical_json_hash(validated_model.to_dict()),
            "withdrawn_assumption": deepcopy(self.withdrawn_assumption),
            "challenge_decision": deepcopy(self.decision),
        }
        report["report_digest"] = canonical_json_hash(report)
        return report


def challenge_agent_assumption(
    model: RequirementModelV2,
    *,
    assumption_id: str,
    instruction: str,
    challenged_by: str,
    challenged_at: str | None = None,
) -> RequirementAssumptionChallenge:
    target_id = assumption_id.strip()
    challenge_text = instruction.strip()
    actor = challenged_by.strip()
    if not target_id:
        raise HarnessError("requirement_assumption_stale", "Assumption id must not be empty.", run_id=model.run_id)
    if not challenge_text:
        raise HarnessError(
            "requirement_assumption_challenge_empty",
            "Assumption challenge instruction must not be empty.",
            run_id=model.run_id,
        )
    if not actor:
        raise HarnessError(
            "requirement_assumption_challenged_by_empty",
            "Assumption challenge actor must not be empty.",
            run_id=model.run_id,
        )

    validate_agent_reasonable_default_integrity(model)
    data = model.to_dict()
    assumptions = _dict_items(data.get("assumptions"))
    assumption = next((item for item in assumptions if item.get("id") == target_id), None)
    if not isinstance(assumption, dict) or assumption.get("origin") != AGENT_DEFAULT_ORIGIN:
        raise HarnessError(
            "requirement_assumption_stale",
            f"Challengeable assumption is not active: {target_id}",
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
            "requirement_agent_default_integrity_failed",
            "Assumption is not linked to its active source question.",
            run_id=model.run_id,
        )

    before_sha256 = canonical_json_hash(data)
    data["revision"] = model.revision + 1
    data["assumptions"] = [item for item in assumptions if item.get("id") != target_id]
    question["status"] = "open"
    question["proposed_disposition"] = HUMAN_REQUIRED
    question["proposed_blocking"] = True
    question["suggested_answer"] = None
    question["recommended_option"] = None
    question.pop(EVALUATION_FIELD, None)
    _reattach_open_question_refs(data, question_id, _string_items(question.get("affects")))

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
        "decision_kind": DECISION_KIND_CHALLENGE,
        "challenged_assumption_id": target_id,
        "challenged_assumption_origin": AGENT_DEFAULT_ORIGIN,
    }
    decisions.append(decision)
    return RequirementAssumptionChallenge(
        model=RequirementModelV2.from_dict(data),
        decision=deepcopy(decision),
        withdrawn_assumption=deepcopy(assumption),
        before_model_sha256=before_sha256,
    )


def assumption_challenge_quality_summary(report: dict[str, object]) -> dict[str, object]:
    decision = report.get("challenge_decision")
    assumption = report.get("withdrawn_assumption")
    return {
        "status": "challenged",
        "requirement_revision": report.get("requirement_revision"),
        "report_digest": report.get("report_digest"),
        "decision_id": decision.get("id") if isinstance(decision, dict) else None,
        "question_id": decision.get("question_id") if isinstance(decision, dict) else None,
        "assumption_id": assumption.get("id") if isinstance(assumption, dict) else None,
        "origin": assumption.get("origin") if isinstance(assumption, dict) else None,
        "challenged_by": decision.get("answered_by") if isinstance(decision, dict) else None,
    }


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


def _latest_question_decision(decisions: list[object], question_id: str) -> dict[str, Any] | None:
    for item in reversed(decisions):
        if isinstance(item, dict) and item.get("question_id") == question_id:
            return item
    return None


def _reattach_open_question_refs(data: dict[str, Any], question_id: str, affects: list[str]) -> None:
    affected = set(affects)
    _append_question_ref(data, affected, question_id)
    contract = data.get("scenario_contract")
    scenarios = contract.get("scenarios") if isinstance(contract, dict) else []
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if not isinstance(scenario, dict):
            continue
        related = affected.intersection(
            _string_items(scenario.get("requirement_refs"))
            + _string_items(scenario.get("acceptance_refs"))
        )
        if not related:
            continue
        _append_unique(scenario, "open_question_refs", question_id)
        surfaces = scenario.get("surfaces")
        for surface in surfaces.values() if isinstance(surfaces, dict) else []:
            if not isinstance(surface, dict) or surface.get("status") == "not_applicable":
                continue
            if related.intersection(_string_items(surface.get("requirement_refs"))):
                _append_unique(surface, "open_question_refs", question_id)


def _append_question_ref(value: object, affected: set[str], question_id: str) -> None:
    if isinstance(value, dict):
        if str(value.get("id") or "") in affected and "open_question_refs" in value:
            _append_unique(value, "open_question_refs", question_id)
        for nested in value.values():
            _append_question_ref(nested, affected, question_id)
    elif isinstance(value, list):
        for nested in value:
            _append_question_ref(nested, affected, question_id)


def _append_unique(item: dict[str, Any], field: str, value: str) -> None:
    values = _string_items(item.get(field))
    if value not in values:
        values.append(value)
    item[field] = values


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item] if isinstance(value, list) else []
