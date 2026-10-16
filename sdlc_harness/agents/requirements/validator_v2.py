"""Python-owned validation and blocking policy for Requirement Model V2."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Iterable

from .model_v2 import (
    DecisionStatus,
    QuestionStatus,
    RequirementModelV2,
    RequirementStatus,
    SCHEMA_VERSION,
    ValidationStatus,
)


HARD_BLOCKING_CATEGORIES = frozenset(
    {"business_rule", "acceptance_criteria", "data_identity", "api_contract", "observable_behavior"}
)
BEHAVIOR_PREFIXES = ("MF-", "AF-", "EF-", "POST-", "BR-", "AC-", "QR-")
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

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


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
    source_text: str,
) -> RequirementModelV2ValidationResult:
    """Return a normalized copy and a fail-closed validation report."""

    model = payload if isinstance(payload, RequirementModelV2) else RequirementModelV2.from_dict(payload)
    data = model.to_dict()
    _normalize_python_owned_fields(data)

    issues: list[RequirementModelV2Issue] = []
    _validate_schema(data, issues)
    ids = _validate_ids(data, issues)
    _validate_references(data, ids, issues)
    _validate_grounding(data, source_text, ids, issues)
    _validate_decisions(data, ids, issues)
    _validate_question_dependencies(data, ids, issues)
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

    data["validation"] = {
        "status": status,
        "errors": [issue.to_dict() for issue in issues],
    }
    normalized = RequirementModelV2.from_dict(data)
    report = RequirementModelV2ValidationReport(
        ok=not issues,
        status=status,
        issues=issues,
        blocking_question_ids=blocking_ids,
    )
    return RequirementModelV2ValidationResult(model=normalized, report=report)


def _normalize_python_owned_fields(data: dict[str, Any]) -> None:
    for question in _dict_list(data.get("open_questions")):
        affects = _string_list(question.get("affects"))
        category = str(question.get("category") or "")
        proposed = question.get("proposed_blocking") is True
        affects_behavior = any(ref.startswith(BEHAVIOR_PREFIXES) for ref in affects)
        question["effective_blocking"] = proposed or category in HARD_BLOCKING_CATEGORIES or affects_behavior


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
        for name in ("source_ref", "artifact_ref", "fingerprint"):
            if not _non_empty(source.get(name)):
                _add(issues, "required_field", f"source.{name}", f"{name} must not be empty.")

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
        _require_fields(item, ("id", "source_ref", "location", "quote"), f"evidence[{index}]", issues)


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
    question_ids = {item.get("id") for item in _dict_list(data.get("open_questions"))}
    decision_ids = {item.get("id") for item in _dict_list(data.get("decisions"))}
    requirement_ids = {item_id for item_id in ids if not item_id.startswith(("Q-", "D-", "E-"))}

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
    source_text: str,
    ids: dict[str, str],
    issues: list[RequirementModelV2Issue],
) -> None:
    normalized_source = _normalize_whitespace(source_text)
    evidence_by_id = {str(item.get("id")): item for item in _dict_list(data.get("evidence"))}
    source_ref = str(data.get("source", {}).get("source_ref") or "") if isinstance(data.get("source"), dict) else ""

    for index, item in enumerate(_dict_list(data.get("evidence"))):
        path = f"evidence[{index}]"
        quote = _normalize_whitespace(str(item.get("quote") or ""))
        if not quote or quote not in normalized_source:
            _add(issues, "evidence_not_in_source", f"{path}.quote", "Evidence quote is not present in the source snapshot.")
        if item.get("source_ref") != source_ref:
            _add(issues, "evidence_source_mismatch", f"{path}.source_ref", "Evidence source_ref must match model source_ref.")

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


def _validate_decisions(data: dict[str, Any], ids: dict[str, str], issues: list[RequirementModelV2Issue]) -> None:
    question_ids = {str(item.get("id")) for item in _dict_list(data.get("open_questions"))}
    requirement_ids = {item_id for item_id in ids if not item_id.startswith(("Q-", "D-", "E-", "UC-"))}
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


def _validate_semantics(data: dict[str, Any], issues: list[RequirementModelV2Issue]) -> None:
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    for index, item in enumerate(_dict_list(flows.get("main"))):
        text = " ".join(str(item.get(field) or "") for field in ("actor", "action", "target"))
        if _contains_exception_term(text):
            _add(issues, "exception_in_main_flow", f"flows.main[{index}]", "Main flow contains exception or cancellation behavior.")
    for index, item in enumerate(_dict_list(data.get("postconditions"))):
        if _contains_exception_term(str(item.get("text") or "")):
            _add(issues, "exception_in_postcondition", f"postconditions[{index}]", "Postcondition contains exception or cancellation behavior.")
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


def _add(issues: list[RequirementModelV2Issue], code: str, path: str, message: str) -> None:
    issues.append(RequirementModelV2Issue(code=code, path=path, message=message))
