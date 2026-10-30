"""Policy xác thực và blocking do Python sở hữu cho Requirement Model V2."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from typing import Any, Iterable

from ...core.errors import HarnessError
from .clarification_policy_v1 import (
    AGENT_DEFAULT,
    BEHAVIOR_PREFIXES,
    CLARIFICATION_DISPOSITION_FIELD,
    DEFERRED_TO_DESIGN,
    HARD_BLOCKING_CATEGORIES,
    LEGACY_BLOCKING_POLICY_VERSION,
    REASONABLE_DEFAULT_POLICY_VERSION,
    SHADOW_POLICY_VERSION,
    classify_clarification_question,
    surface_blocking_question_ids,
)
from .reasonable_defaults_v1 import (
    EVALUATION_FIELD as REASONABLE_DEFAULT_EVALUATION_FIELD,
    apply_agent_reasonable_defaults,
)
from .model_v2 import (
    DecisionStatus,
    QuestionStatus,
    RequirementModelV2,
    RequirementStatus,
    SCHEMA_VERSION,
    ValidationStatus,
)


SCENARIO_CONTRACT_VERSION = "requirement_scenario_surface_v1"
SCENARIO_KINDS = frozenset({"main", "alternative", "exception"})
SURFACE_NAMES = frozenset({"ui", "api", "business_rule", "data"})
SURFACE_STATUSES = frozenset({"required", "not_applicable", "clarification_required"})
PROVENANCE_SCHEMA_VERSION = "requirement_provenance_v1"
SOURCE_TYPES = frozenset({"dataset", "jira", "document", "approved_evidence"})
EXPECTED_PREFIXES = {
    "use_case": "UC-",
    "goal": "GOAL-",
    "actors": "ACT-",
    "preconditions": "PRE-",
    "main": "MF-",
    "alternatives": "AF-",
    "exceptions": "EF-",
    "postconditions": "POST-",
    "business_rules": "BR-",
    "acceptance_criteria": "AC-",
    "quality_requirements": "QR-",
    "assumptions": "ASM-",
    "open_questions": "Q-",
    "decisions": "D-",
    "evidence": "E-",
    "scenarios": "SC-",
    "sources": "SRC-",
}
EXCEPTION_TERMS = (
    "cancel",
    "cancellation",
    "hủy",
    "interruption",
    "connection failure",
    "connection error",
    "invalid",
    "insufficient",
    "errored",
    "exception",
)


@dataclass(frozen=True)
class RequirementModelV2Issue:
    code: str
    path: str
    message: str
    subject_ref: str = ""
    missing_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
        if self.subject_ref:
            payload["subject_ref"] = self.subject_ref
        if self.missing_fields:
            payload["missing_fields"] = list(self.missing_fields)
        return payload


@dataclass(frozen=True)
class RequirementModelV2ValidationReport:
    ok: bool
    status: str
    issues: list[RequirementModelV2Issue]
    blocking_question_ids: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
            "blocking_question_ids": list(self.blocking_question_ids),
        }


@dataclass(frozen=True)
class RequirementModelV2ValidationResult:
    model: RequirementModelV2
    report: RequirementModelV2ValidationReport


def normalize_and_validate_requirement_model_v2(
    payload: RequirementModelV2 | object,
    source_text_by_id: dict[str, str],
    *,
    clarification_policy_version: str | None = None,
) -> RequirementModelV2ValidationResult:
    """Return a normalized copy and a fail-closed validation report."""

    model = payload if isinstance(payload, RequirementModelV2) else RequirementModelV2.from_dict(payload)
    data = model.to_dict()
    policy_version = _resolve_clarification_policy_version(
        data,
        requested=clarification_policy_version,
    )
    _normalize_python_owned_fields(
        data,
        clarification_policy_version=policy_version,
    )

    issues: list[RequirementModelV2Issue] = []
    _validate_schema(data, issues)
    ids = _validate_ids(data, issues)
    _validate_references(data, ids, issues)
    _validate_grounding(data, source_text_by_id, ids, issues)
    _validate_question_gap_semantics(data, issues)
    _validate_decisions(data, ids, issues)
    _validate_question_dependencies(data, ids, issues)
    _validate_scenario_contract(data, ids, issues)
    _validate_semantics(data, issues)

    blocking_ids = [
        str(item.get("id"))
        for item in _dict_list(data.get("open_questions"))
        if item.get("status") == QuestionStatus.OPEN.value and item.get("effective_blocking") is True
    ]
    if issues:
        status = ValidationStatus.INVALID.value
    elif blocking_ids:
        status = ValidationStatus.CLARIFICATION_REQUIRED.value
    else:
        status = ValidationStatus.VALID.value

    normalized_validation: dict[str, object] = {
        "status": status,
        "errors": [issue.to_dict() for issue in issues],
    }
    if policy_version in {
        SHADOW_POLICY_VERSION,
        REASONABLE_DEFAULT_POLICY_VERSION,
    }:
        normalized_validation["clarification_policy_version"] = policy_version
    data["validation"] = normalized_validation
    normalized = RequirementModelV2.from_dict(data)
    report = RequirementModelV2ValidationReport(
        ok=not issues,
        status=status,
        issues=issues,
        blocking_question_ids=blocking_ids,
    )
    return RequirementModelV2ValidationResult(model=normalized, report=report)


def _resolve_clarification_policy_version(
    data: dict[str, Any],
    *,
    requested: str | None,
) -> str:
    validation = data.get("validation")
    embedded = (
        str(validation.get("clarification_policy_version") or "")
        if isinstance(validation, dict)
        else ""
    )
    allowed = {
        LEGACY_BLOCKING_POLICY_VERSION,
        SHADOW_POLICY_VERSION,
        REASONABLE_DEFAULT_POLICY_VERSION,
    }
    if requested is not None and requested not in allowed:
        raise HarnessError(
            "requirement_clarification_policy_invalid",
            f"Unsupported Requirement clarification policy: {requested!r}.",
            run_id=str(data.get("run_id") or "") or None,
        )
    if embedded and embedded not in allowed:
        raise HarnessError(
            "requirement_clarification_policy_invalid",
            f"Canonical Requirement has unsupported clarification policy: {embedded!r}.",
            run_id=str(data.get("run_id") or "") or None,
        )
    if requested is not None and embedded and requested != embedded:
        raise HarnessError(
            "requirement_clarification_policy_mismatch",
            "Run and canonical Requirement use different clarification policies.",
            run_id=str(data.get("run_id") or "") or None,
        )
    return requested or embedded or LEGACY_BLOCKING_POLICY_VERSION


def _normalize_python_owned_fields(
    data: dict[str, Any],
    *,
    clarification_policy_version: str,
) -> None:
    surface_blockers = surface_blocking_question_ids(data)
    if clarification_policy_version == REASONABLE_DEFAULT_POLICY_VERSION:
        apply_agent_reasonable_defaults(
            data,
            surface_blockers=surface_blockers,
        )
    for question in _dict_list(data.get("open_questions")):
        question.pop(CLARIFICATION_DISPOSITION_FIELD, None)
        affects = _string_list(question.get("affects"))
        category = str(question.get("category") or "")
        proposed = question.get("proposed_blocking") is True
        affects_behavior = any(ref.startswith(BEHAVIOR_PREFIXES) for ref in affects)
        legacy_effective = proposed or category in HARD_BLOCKING_CATEGORIES or affects_behavior
        question["effective_blocking"] = legacy_effective
        reasonable_evaluation = question.get(REASONABLE_DEFAULT_EVALUATION_FIELD)
        if (
            clarification_policy_version == REASONABLE_DEFAULT_POLICY_VERSION
            and isinstance(reasonable_evaluation, dict)
            and reasonable_evaluation.get("status") == "applied"
        ):
            question["effective_blocking"] = False
            question[CLARIFICATION_DISPOSITION_FIELD] = {
                "value": AGENT_DEFAULT,
                "policy_version": REASONABLE_DEFAULT_POLICY_VERSION,
                "assumption_id": reasonable_evaluation.get("assumption_id"),
                "reason_codes": list(reasonable_evaluation.get("reason_codes") or []),
            }
        elif (
            clarification_policy_version in {
                SHADOW_POLICY_VERSION,
                REASONABLE_DEFAULT_POLICY_VERSION,
            }
            and question.get("status") == QuestionStatus.OPEN.value
        ):
            classification = classify_clarification_question(
                question,
                surface_blockers=surface_blockers,
            )
            if classification["disposition"] == DEFERRED_TO_DESIGN:
                question["effective_blocking"] = False
                question[CLARIFICATION_DISPOSITION_FIELD] = {
                    "value": DEFERRED_TO_DESIGN,
                    "policy_version": clarification_policy_version,
                    "reason_codes": list(classification["reason_codes"]),
                }
            elif clarification_policy_version == REASONABLE_DEFAULT_POLICY_VERSION:
                question["effective_blocking"] = True
                question[CLARIFICATION_DISPOSITION_FIELD] = {
                    "value": "human_required",
                    "policy_version": REASONABLE_DEFAULT_POLICY_VERSION,
                    "reason_codes": list(classification["reason_codes"]),
                }


def _validate_schema(data: dict[str, Any], issues: list[RequirementModelV2Issue]) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        _add(issues, "invalid_schema_version", "schema_version", f"Expected {SCHEMA_VERSION!r}.")
    if not isinstance(data.get("revision"), int) or data.get("revision", 0) < 1:
        _add(issues, "invalid_revision", "revision", "Revision must be an integer starting at 1.")
    if not _non_empty(data.get("run_id")):
        _add(issues, "required_field", "run_id", "run_id must not be empty.")

    source = data.get("source")
    if not isinstance(source, dict):
        _add(issues, "invalid_type", "source", "source must be an object.")
    else:
        for name in ("source_id", "source_ref", "artifact_ref", "fingerprint"):
            if not _non_empty(source.get(name)):
                _add(issues, "required_field", f"source.{name}", f"{name} must not be empty.")

    provenance = data.get("provenance_ledger")
    if not isinstance(provenance, dict):
        _add(issues, "invalid_type", "provenance_ledger", "provenance_ledger must be an object.")
    else:
        _require_fields(provenance, ("schema_version", "sources"), "provenance_ledger", issues)
        if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
            _add(
                issues,
                "invalid_provenance_schema_version",
                "provenance_ledger.schema_version",
                f"Expected {PROVENANCE_SCHEMA_VERSION!r}.",
            )
        sources = provenance.get("sources")
        if not isinstance(sources, list):
            _add(issues, "invalid_type", "provenance_ledger.sources", "sources must be a list.")
        elif not sources:
            _add(issues, "provenance_sources_empty", "provenance_ledger.sources", "At least one source is required.")
        else:
            for index, item in enumerate(sources):
                path = f"provenance_ledger.sources[{index}]"
                if not isinstance(item, dict):
                    _add(issues, "invalid_type", path, "Source entry must be an object.")
                    continue
                _require_fields(
                    item,
                    ("id", "type", "source_ref", "locator", "revision", "artifact_ref", "fingerprint"),
                    path,
                    issues,
                )
                if item.get("type") not in SOURCE_TYPES:
                    _add(issues, "invalid_source_type", f"{path}.type", "Unknown provenance source type.")
                for name in ("source_ref", "locator", "artifact_ref", "fingerprint"):
                    if not _non_empty(item.get(name)):
                        _add(issues, "required_field", f"{path}.{name}", f"{name} must not be empty.")
                revision = item.get("revision")
                if revision is not None and not _non_empty(revision):
                    _add(
                        issues,
                        "invalid_source_revision",
                        f"{path}.revision",
                        "Source revision must be null or a non-empty string.",
                    )

    use_case = data.get("use_case")
    if not isinstance(use_case, dict):
        _add(issues, "invalid_type", "use_case", "use_case must be an object.")
    else:
        _require_fields(use_case, ("id", "name", "goal", "actors"), "use_case", issues)
        if not isinstance(use_case.get("goal"), dict):
            _add(issues, "invalid_type", "use_case.goal", "goal must be an object.")
        if not isinstance(use_case.get("actors"), list):
            _add(issues, "invalid_type", "use_case.actors", "actors must be a list.")

    flows = data.get("flows")
    if not isinstance(flows, dict):
        _add(issues, "invalid_type", "flows", "flows must be an object.")
    else:
        for name in ("main", "alternatives", "exceptions"):
            if not isinstance(flows.get(name), list):
                _add(issues, "invalid_type", f"flows.{name}", f"{name} must be a list.")

    scenario_contract = data.get("scenario_contract")
    if not isinstance(scenario_contract, dict):
        _add(issues, "invalid_type", "scenario_contract", "scenario_contract must be an object.")
    else:
        _require_fields(scenario_contract, ("schema_version", "scenarios"), "scenario_contract", issues)
        if scenario_contract.get("schema_version") != SCENARIO_CONTRACT_VERSION:
            _add(
                issues,
                "invalid_scenario_contract_version",
                "scenario_contract.schema_version",
                f"Expected {SCENARIO_CONTRACT_VERSION!r}.",
            )
        scenarios = scenario_contract.get("scenarios")
        if not isinstance(scenarios, list):
            _add(issues, "invalid_type", "scenario_contract.scenarios", "scenarios must be a list.")
        elif not scenarios:
            _add(issues, "scenario_contract_empty", "scenario_contract.scenarios", "At least one scenario is required.")
        else:
            for index, scenario in enumerate(scenarios):
                path = f"scenario_contract.scenarios[{index}]"
                if not isinstance(scenario, dict):
                    _add(issues, "invalid_type", path, "Scenario must be an object.")
                    continue
                _require_fields(
                    scenario,
                    (
                        "id",
                        "name",
                        "kind",
                        "actor_refs",
                        "requirement_refs",
                        "acceptance_refs",
                        "evidence_refs",
                        "open_question_refs",
                        "surfaces",
                    ),
                    path,
                    issues,
                )
                for field in (
                    "actor_refs",
                    "requirement_refs",
                    "acceptance_refs",
                    "evidence_refs",
                    "open_question_refs",
                ):
                    _require_string_list(scenario, field, path, issues)
                if not _non_empty(scenario.get("name")):
                    _add(issues, "required_field", f"{path}.name", "Scenario name must not be empty.")
                if scenario.get("kind") not in SCENARIO_KINDS:
                    _add(issues, "invalid_scenario_kind", f"{path}.kind", "Unknown scenario kind.")
                surfaces = scenario.get("surfaces")
                if not isinstance(surfaces, dict):
                    _add(issues, "invalid_type", f"{path}.surfaces", "surfaces must be an object.")
                    continue
                actual_surfaces = set(surfaces)
                if actual_surfaces != SURFACE_NAMES:
                    _add(
                        issues,
                        "invalid_surface_set",
                        f"{path}.surfaces",
                        "surfaces must contain exactly ui, api, business_rule, and data.",
                    )
                for surface_name in sorted(SURFACE_NAMES.intersection(actual_surfaces)):
                    surface = surfaces.get(surface_name)
                    surface_path = f"{path}.surfaces.{surface_name}"
                    if not isinstance(surface, dict):
                        _add(issues, "invalid_type", surface_path, "Surface must be an object.")
                        continue
                    _require_fields(
                        surface,
                        ("status", "reason", "requirement_refs", "evidence_refs", "open_question_refs"),
                        surface_path,
                        issues,
                    )
                    for field in ("requirement_refs", "evidence_refs", "open_question_refs"):
                        _require_string_list(surface, field, surface_path, issues)
                    if surface.get("status") not in SURFACE_STATUSES:
                        _add(issues, "invalid_surface_status", f"{surface_path}.status", "Unknown surface status.")
                    if not _non_empty(surface.get("reason")):
                        _add(issues, "surface_reason_required", f"{surface_path}.reason", "Surface reason must not be empty.")

    for collection, items in _statement_collections(data):
        for index, item in enumerate(items):
            path = f"{collection}[{index}]"
            if not isinstance(item, dict):
                _add(issues, "invalid_type", path, "Requirement item must be an object.")
                continue
            _require_fields(item, ("id", "text", "status", "evidence_refs", "open_question_refs"), path, issues)
            _validate_requirement_status(item, path, issues)
            _require_string_list(item, "evidence_refs", path, issues)
            _require_string_list(item, "open_question_refs", path, issues)

    if isinstance(use_case, dict):
        goal = use_case.get("goal")
        if isinstance(goal, dict):
            _require_fields(goal, ("id", "text", "status", "evidence_refs", "open_question_refs"), "use_case.goal", issues)
            _validate_requirement_status(goal, "use_case.goal", issues)
            _require_string_list(goal, "evidence_refs", "use_case.goal", issues)
            _require_string_list(goal, "open_question_refs", "use_case.goal", issues)
        for index, actor in enumerate(_dict_list(use_case.get("actors"))):
            path = f"use_case.actors[{index}]"
            _require_fields(actor, ("id", "name", "status", "evidence_refs", "open_question_refs"), path, issues)
            _validate_requirement_status(actor, path, issues)
            _require_string_list(actor, "evidence_refs", path, issues)
            _require_string_list(actor, "open_question_refs", path, issues)

    if isinstance(flows, dict):
        for index, item in enumerate(_dict_list(flows.get("main"))):
            path = f"flows.main[{index}]"
            _require_fields(item, ("id", "actor", "action", "evidence_refs", "open_question_refs"), path, issues)
            _require_string_list(item, "evidence_refs", path, issues)
            _require_string_list(item, "open_question_refs", path, issues)
        for group in ("alternatives", "exceptions"):
            for index, item in enumerate(_dict_list(flows.get(group))):
                path = f"flows.{group}[{index}]"
                _require_fields(item, ("id", "trigger", "steps", "outcome", "evidence_refs", "open_question_refs"), path, issues)
                _require_string_list(item, "steps", path, issues)
                _require_string_list(item, "evidence_refs", path, issues)
                _require_string_list(item, "open_question_refs", path, issues)

    for index, item in enumerate(_dict_list(data.get("acceptance_criteria"))):
        _require_string_list(item, "verifies", f"acceptance_criteria[{index}]", issues)
    for index, item in enumerate(_dict_list(data.get("assumptions"))):
        path = f"assumptions[{index}]"
        _require_fields(item, ("reason", "affects"), path, issues)
        _require_string_list(item, "affects", path, issues)

    for index, item in enumerate(_dict_list(data.get("open_questions"))):
        path = f"open_questions[{index}]"
        _require_fields(
            item,
            ("id", "question", "category", "proposed_blocking", "effective_blocking", "affects", "depends_on", "status", "evidence_refs"),
            path,
            issues,
        )
        if item.get("status") not in {status.value for status in QuestionStatus}:
            _add(issues, "invalid_question_status", f"{path}.status", "Unknown question status.")
        if not isinstance(item.get("proposed_blocking"), bool):
            _add(issues, "invalid_type", f"{path}.proposed_blocking", "proposed_blocking must be boolean.")
        for field in ("affects", "depends_on", "evidence_refs"):
            _require_string_list(item, field, path, issues)
        _validate_question_answer_contract(item, path, issues)

    for index, item in enumerate(_dict_list(data.get("decisions"))):
        path = f"decisions[{index}]"
        _require_fields(
            item,
            ("id", "question_id", "answer", "answered_by", "answered_at", "affected_requirement_ids", "status", "supersedes_decision_id"),
            path,
            issues,
        )
        if item.get("status") not in {status.value for status in DecisionStatus}:
            _add(issues, "invalid_decision_status", f"{path}.status", "Unknown decision status.")
        _require_string_list(item, "affected_requirement_ids", path, issues)

    for index, item in enumerate(_dict_list(data.get("evidence"))):
        path = f"evidence[{index}]"
        _require_fields(item, ("id", "source_id", "location", "quote"), path, issues)
        for name in ("source_id", "location", "quote"):
            if not _non_empty(item.get(name)):
                _add(issues, "required_field", f"{path}.{name}", f"{name} must not be empty.")


def _validate_ids(data: dict[str, Any], issues: list[RequirementModelV2Issue]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for kind, path, item in _all_identified_items(data):
        item_id = item.get("id")
        if not _non_empty(item_id):
            _add(issues, "required_id", f"{path}.id", "Stable ID must not be empty.")
            continue
        item_id = str(item_id)
        prefix = EXPECTED_PREFIXES[kind]
        if not re.fullmatch(re.escape(prefix) + r"[1-9]\d*", item_id):
            _add(issues, "invalid_id_prefix", f"{path}.id", f"Expected ID in the form {prefix}<number>.")
        if item_id in ids:
            _add(issues, "duplicate_id", f"{path}.id", f"ID {item_id} is already used at {ids[item_id]}.")
        else:
            ids[item_id] = path
    return ids


def _validate_references(data: dict[str, Any], ids: dict[str, str], issues: list[RequirementModelV2Issue]) -> None:
    evidence_ids = {item.get("id") for item in _dict_list(data.get("evidence"))}
    provenance = data.get("provenance_ledger") if isinstance(data.get("provenance_ledger"), dict) else {}
    source_ids = {item.get("id") for item in _dict_list(provenance.get("sources"))}
    question_ids = {item.get("id") for item in _dict_list(data.get("open_questions"))}
    decision_ids = {item.get("id") for item in _dict_list(data.get("decisions"))}
    requirement_ids = {item_id for item_id in ids if not item_id.startswith(("Q-", "D-", "E-", "SC-", "SRC-"))}

    for index, item in enumerate(_dict_list(data.get("evidence"))):
        _refs_exist([item.get("source_id")], source_ids, f"evidence[{index}].source_id", "source", issues)

    for path, item in _requirement_like_items(data):
        _refs_exist(item.get("evidence_refs"), evidence_ids, f"{path}.evidence_refs", "evidence", issues)
        _refs_exist(item.get("open_question_refs"), question_ids, f"{path}.open_question_refs", "question", issues)
        decision_ref = item.get("decision_ref")
        if decision_ref is not None and decision_ref not in decision_ids:
            _add(issues, "dangling_reference", f"{path}.decision_ref", f"Unknown decision reference: {decision_ref}.")

    for index, item in enumerate(_dict_list(data.get("acceptance_criteria"))):
        _refs_exist(item.get("verifies"), requirement_ids, f"acceptance_criteria[{index}].verifies", "requirement", issues)
    for index, item in enumerate(_dict_list(data.get("assumptions"))):
        _refs_exist(item.get("affects"), requirement_ids, f"assumptions[{index}].affects", "requirement", issues)
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    actor_ids = {
        str(item.get("id"))
        for item in _dict_list(data.get("use_case", {}).get("actors"))
        if isinstance(data.get("use_case"), dict)
    }
    for index, item in enumerate(_dict_list(flows.get("main"))):
        path = f"flows.main[{index}]"
        actor_ref = item.get("actor")
        if actor_ref not in actor_ids and actor_ref != "System":
            _add(issues, "dangling_reference", f"{path}.actor", f"Unknown actor reference: {actor_ref}.")
        _refs_exist(item.get("evidence_refs"), evidence_ids, f"{path}.evidence_refs", "evidence", issues)
        _refs_exist(item.get("open_question_refs"), question_ids, f"{path}.open_question_refs", "question", issues)
    for group in ("alternatives", "exceptions"):
        for index, item in enumerate(_dict_list(flows.get(group))):
            path = f"flows.{group}[{index}]"
            _refs_exist(item.get("evidence_refs"), evidence_ids, f"{path}.evidence_refs", "evidence", issues)
            _refs_exist(item.get("open_question_refs"), question_ids, f"{path}.open_question_refs", "question", issues)

    for index, item in enumerate(_dict_list(data.get("open_questions"))):
        path = f"open_questions[{index}]"
        _refs_exist(item.get("affects"), requirement_ids, f"{path}.affects", "requirement", issues)
        _refs_exist(item.get("depends_on"), question_ids, f"{path}.depends_on", "question", issues)
        _refs_exist(item.get("evidence_refs"), evidence_ids, f"{path}.evidence_refs", "evidence", issues)


def _validate_grounding(
    data: dict[str, Any],
    source_text_by_id: dict[str, str],
    ids: dict[str, str],
    issues: list[RequirementModelV2Issue],
) -> None:
    evidence_by_id = {str(item.get("id")): item for item in _dict_list(data.get("evidence"))}
    provenance = data.get("provenance_ledger") if isinstance(data.get("provenance_ledger"), dict) else {}
    sources = _dict_list(provenance.get("sources"))
    source_by_id = {str(item.get("id")): item for item in sources if _non_empty(item.get("id"))}
    root_source = data.get("source") if isinstance(data.get("source"), dict) else {}
    primary = source_by_id.get(str(root_source.get("source_id") or ""))
    if primary is None or any(
        root_source.get(field) != primary.get(field)
        for field in ("source_ref", "artifact_ref", "fingerprint")
    ):
        _add(
            issues,
            "source_root_mismatch",
            "source",
            "Primary source pointer must match exactly one provenance ledger entry.",
        )

    normalized_sources: dict[str, str] = {}
    for index, source in enumerate(sources):
        source_id = str(source.get("id") or "")
        text = source_text_by_id.get(source_id)
        if not isinstance(text, str):
            _add(
                issues,
                "source_snapshot_missing",
                f"provenance_ledger.sources[{index}]",
                f"Verified snapshot text is unavailable for {source_id}.",
            )
            continue
        normalized_sources[source_id] = _normalize_whitespace(text)
        fingerprint = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        if source.get("fingerprint") != fingerprint:
            _add(
                issues,
                "source_fingerprint_mismatch",
                f"provenance_ledger.sources[{index}].fingerprint",
                f"Source fingerprint does not match verified snapshot text for {source_id}.",
            )

    for index, item in enumerate(_dict_list(data.get("evidence"))):
        path = f"evidence[{index}]"
        source_id = str(item.get("source_id") or "")
        normalized_source = normalized_sources.get(source_id, "")
        quote = _normalize_whitespace(str(item.get("quote") or ""))
        if not quote or quote not in normalized_source:
            _add(issues, "evidence_not_in_source", f"{path}.quote", "Evidence quote is not present in the source snapshot.")

    accepted_decisions = {
        str(item.get("id"))
        for item in _dict_list(data.get("decisions"))
        if item.get("status") == DecisionStatus.ACCEPTED.value
    }
    for path, item in _requirement_like_items(data):
        status = item.get("status")
        evidence_refs = _string_list(item.get("evidence_refs"))
        question_refs = _string_list(item.get("open_question_refs"))
        if status == RequirementStatus.EXPLICIT.value:
            if not evidence_refs:
                _add(issues, "explicit_without_evidence", path, "Explicit requirement must reference source evidence.")
            elif any(ref not in evidence_by_id for ref in evidence_refs):
                _add(issues, "explicit_with_invalid_evidence", path, "Explicit requirement references invalid evidence.")
        elif status == RequirementStatus.HUMAN_DECISION.value:
            decision_ref = item.get("decision_ref")
            if decision_ref not in accepted_decisions:
                _add(issues, "human_decision_without_accepted_decision", path, "Human decision requirement needs an accepted decision_ref.")
        elif status == RequirementStatus.UNKNOWN.value:
            if not question_refs:
                _add(issues, "unknown_without_question", path, "Unknown requirement must reference an open question.")
            content = str(item.get("text") or item.get("name") or item.get("action") or "").strip()
            if content and not content.startswith("UNKNOWN_FROM_SOURCE"):
                _add(issues, "unknown_contains_inferred_content", path, "Unknown requirement must not contain inferred content.")


def _validate_question_answer_contract(
    question: dict[str, Any],
    path: str,
    issues: list[RequirementModelV2Issue],
) -> None:
    """Kiểm tra shape lựa chọn khi Requirement Agent đã phát hành contract mới."""

    answer_mode = question.get("answer_mode")
    if answer_mode is None:
        return
    if answer_mode not in {"short", "multiple_choice"}:
        _add(issues, "invalid_answer_mode", f"{path}.answer_mode", "Unknown answer mode.")
        return
    options = question.get("options")
    if answer_mode == "short":
        if options != []:
            _add(issues, "invalid_question_options", f"{path}.options", "Short answers must have no options.")
        return
    if not isinstance(options, list) or not 2 <= len(options) <= 5:
        _add(
            issues,
            "invalid_question_options",
            f"{path}.options",
            "Multiple-choice questions must have 2-5 options.",
        )
        return
    keys: list[str] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            _add(
                issues,
                "invalid_question_option",
                f"{path}.options[{index}]",
                "Question option must contain key and answer.",
            )
            continue
        key = str(option.get("key") or "").strip()
        answer = str(option.get("answer") or "").strip()
        if not key or not answer:
            _add(
                issues,
                "invalid_question_option",
                f"{path}.options[{index}]",
                "Question option must contain key and answer.",
            )
        keys.append(key)
    if len(keys) != len(set(keys)):
        _add(issues, "duplicate_question_option", f"{path}.options", "Question option keys must be unique.")
    recommended = question.get("recommended_option")
    if recommended is not None and str(recommended) not in keys:
        _add(
            issues,
            "invalid_recommended_option",
            f"{path}.recommended_option",
            "Recommended option must reference an option key.",
        )


def _validate_question_gap_semantics(
    data: dict[str, Any],
    issues: list[RequirementModelV2Issue],
) -> None:
    """Giữ clarification grounded và tách source conflict khỏi business ambiguity."""

    for index, question in enumerate(_dict_list(data.get("open_questions"))):
        path = f"open_questions[{index}]"
        evidence_refs = _string_list(question.get("evidence_refs"))
        if not evidence_refs:
            _add(
                issues,
                "question_without_evidence",
                f"{path}.evidence_refs",
                "An open question must cite evidence explaining why clarification is needed.",
            )
        if str(question.get("category") or "") == "source_conflict" and len(set(evidence_refs)) < 2:
            _add(
                issues,
                "source_conflict_evidence_insufficient",
                f"{path}.evidence_refs",
                "A source conflict must cite at least two distinct evidence items.",
            )


def _validate_decisions(data: dict[str, Any], ids: dict[str, str], issues: list[RequirementModelV2Issue]) -> None:
    question_ids = {str(item.get("id")) for item in _dict_list(data.get("open_questions"))}
    requirement_ids = {item_id for item_id in ids if not item_id.startswith(("Q-", "D-", "E-", "UC-", "SC-", "SRC-"))}
    decision_ids = {str(item.get("id")) for item in _dict_list(data.get("decisions"))}
    accepted_by_question: dict[str, list[str]] = {}
    for index, decision in enumerate(_dict_list(data.get("decisions"))):
        path = f"decisions[{index}]"
        question_id = str(decision.get("question_id") or "")
        if question_id not in question_ids:
            _add(issues, "dangling_reference", f"{path}.question_id", f"Unknown question reference: {question_id}.")
        _refs_exist(decision.get("affected_requirement_ids"), requirement_ids, f"{path}.affected_requirement_ids", "requirement", issues)
        supersedes = decision.get("supersedes_decision_id")
        if supersedes is not None and supersedes not in decision_ids:
            _add(issues, "dangling_reference", f"{path}.supersedes_decision_id", f"Unknown decision reference: {supersedes}.")
        if decision.get("status") == DecisionStatus.ACCEPTED.value:
            accepted_by_question.setdefault(question_id, []).append(str(decision.get("id")))

    for index, question in enumerate(_dict_list(data.get("open_questions"))):
        if question.get("status") == QuestionStatus.RESOLVED.value and not accepted_by_question.get(str(question.get("id"))):
            _add(
                issues,
                "resolved_question_without_decision",
                f"open_questions[{index}].status",
                "Resolved question must have an accepted decision.",
            )


def _validate_question_dependencies(data: dict[str, Any], ids: dict[str, str], issues: list[RequirementModelV2Issue]) -> None:
    graph = {
        str(item.get("id")): _string_list(item.get("depends_on"))
        for item in _dict_list(data.get("open_questions"))
        if _non_empty(item.get("id"))
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for dependency in graph.get(node, []):
            if dependency in graph and visit(dependency):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for node in graph:
        if visit(node):
            _add(issues, "question_dependency_cycle", "open_questions", f"Question dependency cycle includes {node}.")
            break


def _validate_scenario_contract(
    data: dict[str, Any],
    ids: dict[str, str],
    issues: list[RequirementModelV2Issue],
) -> None:
    contract = data.get("scenario_contract")
    if not isinstance(contract, dict):
        return
    scenarios = _dict_list(contract.get("scenarios"))
    if not scenarios:
        return

    actor_ids = {item_id for item_id in ids if item_id.startswith("ACT-")}
    evidence_ids = {item_id for item_id in ids if item_id.startswith("E-")}
    question_by_id = {
        str(item.get("id")): item for item in _dict_list(data.get("open_questions")) if _non_empty(item.get("id"))
    }
    question_ids = set(question_by_id)
    acceptance_ids = {item_id for item_id in ids if item_id.startswith("AC-")}
    scenario_requirement_ids = {
        item_id
        for item_id in ids
        if item_id.startswith(("MF-", "AF-", "EF-", "POST-", "BR-", "QR-"))
    }
    required_coverage_ids = {
        item_id for item_id in ids if item_id.startswith(("MF-", "AF-", "EF-", "BR-", "AC-"))
    }
    covered_ids: set[str] = set()

    for index, scenario in enumerate(scenarios):
        path = f"scenario_contract.scenarios[{index}]"
        actor_refs = _string_list(scenario.get("actor_refs"))
        requirement_refs = _string_list(scenario.get("requirement_refs"))
        acceptance_refs = _string_list(scenario.get("acceptance_refs"))
        evidence_refs = _string_list(scenario.get("evidence_refs"))
        question_refs = _string_list(scenario.get("open_question_refs"))
        _refs_exist(actor_refs, actor_ids, f"{path}.actor_refs", "actor", issues)
        _refs_exist(requirement_refs, scenario_requirement_ids, f"{path}.requirement_refs", "scenario requirement", issues)
        _refs_exist(acceptance_refs, acceptance_ids, f"{path}.acceptance_refs", "acceptance criterion", issues)
        _refs_exist(evidence_refs, evidence_ids, f"{path}.evidence_refs", "evidence", issues)
        _refs_exist(question_refs, question_ids, f"{path}.open_question_refs", "question", issues)
        if not requirement_refs:
            _add(issues, "scenario_requirement_required", f"{path}.requirement_refs", "Scenario must reference behavior.")
        if not acceptance_refs:
            _add(issues, "scenario_acceptance_required", f"{path}.acceptance_refs", "Scenario must reference acceptance criteria.")
        if not evidence_refs:
            _add(issues, "scenario_evidence_required", f"{path}.evidence_refs", "Scenario must reference source evidence.")
        covered_ids.update(requirement_refs)
        covered_ids.update(acceptance_refs)

        scenario_scope = set(requirement_refs + acceptance_refs)
        expected_questions = {
            question_id
            for question_id, question in question_by_id.items()
            if question.get("status") == QuestionStatus.OPEN.value
            and set(_string_list(question.get("affects"))).intersection(scenario_scope)
        }
        missing_questions = sorted(expected_questions.difference(question_refs))
        if missing_questions:
            _add(
                issues,
                "scenario_question_coverage_missing",
                f"{path}.open_question_refs",
                "Scenario is missing related questions: " + ", ".join(missing_questions) + ".",
            )
        unrelated_questions = sorted(
            question_id
            for question_id in question_refs
            if question_id in question_by_id
            and not set(_string_list(question_by_id[question_id].get("affects"))).intersection(scenario_scope)
        )
        if unrelated_questions:
            _add(
                issues,
                "scenario_question_unrelated",
                f"{path}.open_question_refs",
                "Questions do not affect this scenario: " + ", ".join(unrelated_questions) + ".",
            )

        mapped_questions: set[str] = set()
        surfaces = scenario.get("surfaces") if isinstance(scenario.get("surfaces"), dict) else {}
        for surface_name in sorted(SURFACE_NAMES):
            surface = surfaces.get(surface_name)
            if not isinstance(surface, dict):
                continue
            surface_path = f"{path}.surfaces.{surface_name}"
            status = surface.get("status")
            surface_requirements = _string_list(surface.get("requirement_refs"))
            surface_evidence = _string_list(surface.get("evidence_refs"))
            surface_questions = _string_list(surface.get("open_question_refs"))
            _refs_exist(surface_requirements, scenario_scope, f"{surface_path}.requirement_refs", "scenario-local requirement", issues)
            _refs_exist(surface_evidence, evidence_ids, f"{surface_path}.evidence_refs", "evidence", issues)
            _refs_exist(surface_questions, set(question_refs), f"{surface_path}.open_question_refs", "scenario-local question", issues)
            mapped_questions.update(surface_questions)
            if status == "required" and not surface_requirements:
                _add(
                    issues,
                    "surface_requirement_required",
                    f"{surface_path}.requirement_refs",
                    "A required surface must reference scenario behavior or acceptance.",
                )
            elif status == "not_applicable" and (surface_requirements or surface_questions):
                _add(
                    issues,
                    "surface_not_applicable_conflict",
                    surface_path,
                    "A not_applicable surface cannot reference requirements or open questions.",
                )
            elif status == "clarification_required":
                if not surface_requirements:
                    _add(
                        issues,
                        "surface_requirement_required",
                        f"{surface_path}.requirement_refs",
                        "A clarification_required surface must identify affected behavior.",
                    )
                if not surface_questions:
                    _add(
                        issues,
                        "surface_clarification_question_required",
                        f"{surface_path}.open_question_refs",
                        "A clarification_required surface must reference an open question.",
                    )
            for question_id in surface_questions:
                question = question_by_id.get(question_id)
                if question is not None and question.get("status") != QuestionStatus.OPEN.value:
                    _add(
                        issues,
                        "surface_question_not_open",
                        f"{surface_path}.open_question_refs",
                        f"Surface question must be open: {question_id}.",
                    )
                if question is not None and not set(_string_list(question.get("affects"))).intersection(surface_requirements):
                    _add(
                        issues,
                        "surface_question_unrelated",
                        f"{surface_path}.open_question_refs",
                        f"Question does not affect this surface's requirements: {question_id}.",
                    )
        unmapped_questions = sorted(set(question_refs).difference(mapped_questions))
        if unmapped_questions:
            _add(
                issues,
                "scenario_question_not_mapped",
                f"{path}.surfaces",
                "Scenario questions are not assigned to a surface: " + ", ".join(unmapped_questions) + ".",
            )

    missing_coverage = sorted(required_coverage_ids.difference(covered_ids))
    if missing_coverage:
        _add(
            issues,
            "scenario_coverage_missing",
            "scenario_contract.scenarios",
            "Requirement behavior is not covered by a scenario: " + ", ".join(missing_coverage) + ".",
        )


def _validate_semantics(data: dict[str, Any], issues: list[RequirementModelV2Issue]) -> None:
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    question_by_id = {
        str(item.get("id") or ""): item
        for item in _dict_list(data.get("open_questions"))
    }
    for index, item in enumerate(_dict_list(flows.get("main"))):
        text = " ".join(str(item.get(field) or "") for field in ("actor", "action", "target"))
        if _contains_exception_term(text):
            _add(issues, "exception_in_main_flow", f"flows.main[{index}]", "Main flow contains exception or cancellation behavior.")
    for index, item in enumerate(_dict_list(data.get("postconditions"))):
        if _contains_exception_term(str(item.get("text") or "")):
            _add(issues, "exception_in_postcondition", f"postconditions[{index}]", "Postcondition contains exception or cancellation behavior.")
    for group in ("alternatives", "exceptions"):
        for index, item in enumerate(_dict_list(flows.get(group))):
            missing: list[str] = []
            if not _string_list(item.get("steps")):
                missing.append("steps")
            if not _non_empty(item.get("outcome")):
                missing.append("outcome")
            if not missing:
                continue
            item_id = str(item.get("id") or "")
            has_blocking_question = any(
                question is not None
                and question.get("status") == QuestionStatus.OPEN.value
                and question.get("effective_blocking") is True
                and item_id in _string_list(question.get("affects"))
                for question_id in _string_list(item.get("open_question_refs"))
                for question in [question_by_id.get(question_id)]
            )
            if not has_blocking_question:
                _add(
                    issues,
                    "branch_behavior_without_clarification",
                    f"flows.{group}[{index}]",
                    (
                        f"Branch {item_id} is missing {', '.join(missing)} and must link "
                        "an open blocking clarification question."
                    ),
                    subject_ref=item_id,
                    missing_fields=tuple(missing),
                )
    for index, item in enumerate(_dict_list(data.get("acceptance_criteria"))):
        text = str(item.get("text") or "")
        if not _string_list(item.get("verifies")):
            _add(
                issues,
                "acceptance_without_verifies",
                f"acceptance_criteria[{index}].verifies",
                "Acceptance criterion must identify the behavior it verifies.",
            )
        if re.search(r"\b(?:unknown_from_source|todo|tbd|to be defined)\b", text, re.IGNORECASE):
            _add(
                issues,
                "acceptance_placeholder",
                f"acceptance_criteria[{index}].text",
                "Acceptance criterion contains an unresolved placeholder.",
            )
    for index, item in enumerate(_dict_list(data.get("assumptions"))):
        if item.get("status") != RequirementStatus.ASSUMPTION.value:
            _add(issues, "assumption_marked_confirmed", f"assumptions[{index}].status", "Assumption collection items must use status=assumption.")


def _statement_collections(data: dict[str, Any]) -> Iterable[tuple[str, list[Any]]]:
    for name in ("preconditions", "postconditions", "business_rules", "acceptance_criteria", "quality_requirements", "assumptions"):
        value = data.get(name)
        yield name, value if isinstance(value, list) else []


def _requirement_like_items(data: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    use_case = data.get("use_case") if isinstance(data.get("use_case"), dict) else {}
    if isinstance(use_case.get("goal"), dict):
        yield "use_case.goal", use_case["goal"]
    for index, item in enumerate(_dict_list(use_case.get("actors"))):
        yield f"use_case.actors[{index}]", item
    for collection, items in _statement_collections(data):
        for index, item in enumerate(_dict_list(items)):
            yield f"{collection}[{index}]", item


def _all_identified_items(data: dict[str, Any]) -> Iterable[tuple[str, str, dict[str, Any]]]:
    use_case = data.get("use_case") if isinstance(data.get("use_case"), dict) else {}
    if isinstance(use_case, dict):
        yield "use_case", "use_case", use_case
        if isinstance(use_case.get("goal"), dict):
            yield "goal", "use_case.goal", use_case["goal"]
        for index, item in enumerate(_dict_list(use_case.get("actors"))):
            yield "actors", f"use_case.actors[{index}]", item
    for collection, items in _statement_collections(data):
        for index, item in enumerate(_dict_list(items)):
            yield collection, f"{collection}[{index}]", item
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    for group in ("main", "alternatives", "exceptions"):
        for index, item in enumerate(_dict_list(flows.get(group))):
            yield group, f"flows.{group}[{index}]", item
    for collection in ("open_questions", "decisions", "evidence"):
        for index, item in enumerate(_dict_list(data.get(collection))):
            yield collection, f"{collection}[{index}]", item
    provenance = data.get("provenance_ledger") if isinstance(data.get("provenance_ledger"), dict) else {}
    for index, item in enumerate(_dict_list(provenance.get("sources"))):
        yield "sources", f"provenance_ledger.sources[{index}]", item
    scenario_contract = data.get("scenario_contract") if isinstance(data.get("scenario_contract"), dict) else {}
    for index, item in enumerate(_dict_list(scenario_contract.get("scenarios"))):
        yield "scenarios", f"scenario_contract.scenarios[{index}]", item


def _require_fields(item: dict[str, Any], fields: Iterable[str], path: str, issues: list[RequirementModelV2Issue]) -> None:
    for field in fields:
        if field not in item:
            _add(issues, "required_field", f"{path}.{field}", f"Missing required field: {field}.")


def _validate_requirement_status(
    item: dict[str, Any],
    path: str,
    issues: list[RequirementModelV2Issue],
) -> None:
    if item.get("status") not in {status.value for status in RequirementStatus}:
        _add(issues, "invalid_requirement_status", f"{path}.status", "Unknown requirement status.")


def _require_string_list(item: dict[str, Any], field: str, path: str, issues: list[RequirementModelV2Issue]) -> None:
    value = item.get(field)
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        _add(issues, "invalid_type", f"{path}.{field}", f"{field} must be a list of strings.")


def _refs_exist(value: Any, valid: set[Any], path: str, kind: str, issues: list[RequirementModelV2Issue]) -> None:
    for ref in _string_list(value):
        if ref not in valid:
            _add(issues, "dangling_reference", path, f"Unknown {kind} reference: {ref}.")


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _non_empty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def _contains_exception_term(value: str) -> bool:
    normalized = _normalize_whitespace(value).casefold()
    return any(term in normalized for term in EXCEPTION_TERMS)


def _add(
    issues: list[RequirementModelV2Issue],
    code: str,
    path: str,
    message: str,
    *,
    subject_ref: str = "",
    missing_fields: tuple[str, ...] = (),
) -> None:
    issues.append(
        RequirementModelV2Issue(
            code=code,
            path=path,
            message=message,
            subject_ref=subject_ref,
            missing_fields=missing_fields,
        )
    )
