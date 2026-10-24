"""Áp dụng Requirement repair delta theo policy do Python sở hữu."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from ...core.errors import HarnessError
from .model_v2 import RequirementModelV2
from .source_tools import observation_text_contains_quote
from .validator_v2 import (
    RequirementModelV2ValidationReport,
    normalize_and_validate_requirement_model_v2,
)


DELTA_SCHEMA_VERSION = "requirement_delta_v1"
MAX_DELTA_OPERATIONS = 64
_OP_NAMES = {"entity.add", "entity.update", "entity.remove", "surface.update"}
_SURFACE_NAMES = {"ui", "api", "business_rule", "data"}
_PROTECTED_FIELDS = {
    "id", "schema_version", "revision", "run_id", "source", "source_id",
    "provenance_ledger", "decisions", "decision_ref", "validation", "effective_blocking",
}
_REFERENCE_FIELDS = {
    "actor", "actor_refs", "requirement_refs", "acceptance_refs", "evidence_refs",
    "open_question_refs", "verifies", "affects", "depends_on",
}
_COLLECTIONS: dict[str, tuple[str, str]] = {
    "use_case.actors": ("ACT-", "ACT"),
    "preconditions": ("PRE-", "PRE"),
    "flows.main": ("MF-", "MF"),
    "flows.alternatives": ("AF-", "AF"),
    "flows.exceptions": ("EF-", "EF"),
    "postconditions": ("POST-", "POST"),
    "business_rules": ("BR-", "BR"),
    "acceptance_criteria": ("AC-", "AC"),
    "quality_requirements": ("QR-", "QR"),
    "assumptions": ("ASM-", "ASM"),
    "open_questions": ("Q-", "Q"),
    "evidence": ("E-", "E"),
    "scenario_contract.scenarios": ("SC-", "SC"),
}
_UPDATE_FIELDS: dict[str, frozenset[str]] = {
    "UC": frozenset({"name"}),
    "GOAL": frozenset({"text", "status", "evidence_refs", "open_question_refs"}),
    "ACT": frozenset({"name", "status", "evidence_refs", "open_question_refs"}),
    "PRE": frozenset({"text", "status", "evidence_refs", "open_question_refs"}),
    "MF": frozenset({"actor", "action", "target", "evidence_refs", "open_question_refs"}),
    "AF": frozenset({"trigger", "steps", "outcome", "evidence_refs", "open_question_refs"}),
    "EF": frozenset({"trigger", "steps", "outcome", "evidence_refs", "open_question_refs"}),
    "POST": frozenset({"text", "status", "evidence_refs", "open_question_refs"}),
    "BR": frozenset({"text", "status", "evidence_refs", "open_question_refs"}),
    "AC": frozenset({"text", "status", "evidence_refs", "open_question_refs", "verifies"}),
    "QR": frozenset({"text", "status", "evidence_refs", "open_question_refs"}),
    "ASM": frozenset(
        {"text", "status", "evidence_refs", "open_question_refs", "reason", "affects"}
    ),
    "Q": frozenset(
        {
            "question", "category", "proposed_blocking", "affects", "depends_on",
            "answer_guidance", "evidence_refs",
        }
    ),
    "E": frozenset({"location", "quote", "observation_ref"}),
    "SC": frozenset(
        {
            "name", "kind", "actor_refs", "requirement_refs", "acceptance_refs",
            "evidence_refs", "open_question_refs",
        }
    ),
}
_ADD_FIELDS = {
    kind: fields | ({"surfaces"} if kind == "SC" else set())
    for kind, fields in _UPDATE_FIELDS.items()
}
_SURFACE_FIELDS = frozenset(
    {"status", "reason", "requirement_refs", "evidence_refs", "open_question_refs"}
)


@dataclass(frozen=True)
class RequirementDeltaApplication:
    """Kết quả delta đã merge và qua full validation."""

    model: RequirementModelV2
    report: RequirementModelV2ValidationReport
    base_candidate_sha256: str
    result_candidate_sha256: str
    operations: list[dict[str, object]]


def requirement_candidate_hash(value: RequirementModelV2 | object) -> str:
    payload = value.to_dict() if isinstance(value, RequirementModelV2) else value
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def apply_requirement_delta(
    *,
    base_model: RequirementModelV2,
    raw_delta: object,
    observations: dict[str, dict[str, object]],
    source_text_by_id: dict[str, str],
) -> RequirementDeltaApplication:
    """Kiểm policy, merge atomic và validate một repair delta."""

    delta = _parse_delta(raw_delta, run_id=base_model.run_id)
    expected_hash = requirement_candidate_hash(base_model)
    if delta["base_candidate_sha256"] != expected_hash:
        raise HarnessError(
            "requirement_delta_stale",
            "Requirement delta base hash does not match the current candidate.",
            run_id=base_model.run_id,
        )

    operations = deepcopy(delta["operations"])
    data = base_model.to_dict()
    temp_ids = _allocate_temp_ids(data, operations, run_id=base_model.run_id)
    _rewrite_temp_references(operations, temp_ids)
    _apply_operations(
        data,
        operations,
        observations=observations,
        run_id=base_model.run_id,
    )
    data["validation"] = {"status": "invalid", "errors": []}
    try:
        validation = normalize_and_validate_requirement_model_v2(data, source_text_by_id)
    except HarnessError as exc:
        raise HarnessError(
            "requirement_delta_validation_failed",
            "Requirement delta produced an invalid model shape: " + exc.message,
            run_id=base_model.run_id,
        ) from exc
    if not validation.report.ok:
        details = "; ".join(
            f"{issue.code}@{issue.path}" for issue in validation.report.issues
        )
        raise HarnessError(
            "requirement_delta_validation_failed",
            "Requirement delta failed validation: " + details,
            run_id=base_model.run_id,
        )
    return RequirementDeltaApplication(
        model=validation.model,
        report=validation.report,
        base_candidate_sha256=expected_hash,
        result_candidate_sha256=requirement_candidate_hash(validation.model),
        operations=operations,
    )


def _parse_delta(value: object, *, run_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError("requirement_delta_invalid", "Requirement delta must be an object.", run_id=run_id)
    expected = {"schema_version", "base_candidate_sha256", "operations"}
    if set(value) != expected or value.get("schema_version") != DELTA_SCHEMA_VERSION:
        raise HarnessError(
            "requirement_delta_invalid",
            "Requirement delta root contract is invalid.",
            run_id=run_id,
        )
    base_hash = value.get("base_candidate_sha256")
    operations = value.get("operations")
    if not isinstance(base_hash, str) or not base_hash.startswith("sha256:"):
        raise HarnessError("requirement_delta_invalid", "Requirement delta base hash is invalid.", run_id=run_id)
    if (
        not isinstance(operations, list)
        or not operations
        or len(operations) > MAX_DELTA_OPERATIONS
        or any(not isinstance(item, dict) for item in operations)
    ):
        raise HarnessError(
            "requirement_delta_invalid",
            f"Requirement delta must contain 1..{MAX_DELTA_OPERATIONS} operation objects.",
            run_id=run_id,
        )
    return {
        "schema_version": DELTA_SCHEMA_VERSION,
        "base_candidate_sha256": base_hash,
        "operations": operations,
    }


def _allocate_temp_ids(
    data: dict[str, Any],
    operations: list[dict[str, object]],
    *,
    run_id: str,
) -> dict[str, str]:
    existing_ids = set(_entity_index(data))
    next_numbers: dict[str, int] = {}
    for collection, (prefix, _) in _COLLECTIONS.items():
        del collection
        suffixes = [int(item[len(prefix):]) for item in existing_ids if re.fullmatch(re.escape(prefix) + r"\d+", item)]
        next_numbers[prefix] = max(suffixes, default=0) + 1

    temp_ids: dict[str, str] = {}
    mutated_targets: set[str] = set()
    mutated_surfaces: set[tuple[str, str]] = set()
    for operation in operations:
        name = str(operation.get("op") or "")
        if name not in _OP_NAMES:
            raise HarnessError("requirement_delta_invalid", f"Unknown delta operation: {name}", run_id=run_id)
        if name == "entity.add":
            _require_keys(operation, {"op", "collection", "temp_ref", "value"}, run_id=run_id)
            collection = str(operation.get("collection") or "")
            temp_ref = str(operation.get("temp_ref") or "")
            value = operation.get("value")
            if collection not in _COLLECTIONS or not re.fullmatch(r"tmp:[A-Za-z][A-Za-z0-9_-]{0,63}", temp_ref):
                raise HarnessError("requirement_delta_invalid", "Entity add target or temp_ref is invalid.", run_id=run_id)
            if temp_ref in temp_ids or not isinstance(value, dict):
                raise HarnessError("requirement_delta_invalid", "Entity add temp_ref must be unique and value must be an object.", run_id=run_id)
            if set(value).intersection(_PROTECTED_FIELDS):
                raise HarnessError("requirement_delta_policy_violation", "Entity add contains Python-owned fields.", run_id=run_id)
            prefix, _ = _COLLECTIONS[collection]
            stable_id = f"{prefix}{next_numbers[prefix]}"
            next_numbers[prefix] += 1
            temp_ids[temp_ref] = stable_id
        elif name in {"entity.update", "entity.remove"}:
            required = {"op", "target_id", "fields"} if name == "entity.update" else {"op", "target_id"}
            _require_keys(operation, required, run_id=run_id)
            target_id = str(operation.get("target_id") or "")
            if target_id in mutated_targets:
                raise HarnessError("requirement_delta_invalid", f"Delta mutates {target_id} more than once.", run_id=run_id)
            mutated_targets.add(target_id)
        else:
            _require_keys(operation, {"op", "scenario_id", "surface", "fields"}, run_id=run_id)
            key = (str(operation.get("scenario_id") or ""), str(operation.get("surface") or ""))
            if key in mutated_surfaces:
                raise HarnessError("requirement_delta_invalid", "Delta mutates a surface more than once.", run_id=run_id)
            mutated_surfaces.add(key)
    return temp_ids


def _rewrite_temp_references(operations: list[dict[str, object]], temp_ids: dict[str, str]) -> None:
    for operation in operations:
        if operation.get("op") == "entity.add":
            operation["assigned_id"] = temp_ids[str(operation["temp_ref"])]
            _rewrite_reference_fields(operation.get("value"), temp_ids)
        elif operation.get("op") in {"entity.update", "surface.update"}:
            _rewrite_reference_fields(operation.get("fields"), temp_ids)


def _rewrite_reference_fields(value: object, temp_ids: dict[str, str]) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in _REFERENCE_FIELDS:
            if isinstance(item, str) and item in temp_ids:
                value[key] = temp_ids[item]
            elif isinstance(item, list):
                value[key] = [temp_ids.get(entry, entry) if isinstance(entry, str) else entry for entry in item]
        elif isinstance(item, dict):
            _rewrite_reference_fields(item, temp_ids)


def _apply_operations(
    data: dict[str, Any],
    operations: list[dict[str, object]],
    *,
    observations: dict[str, dict[str, object]],
    run_id: str,
) -> None:
    for operation in operations:
        name = str(operation["op"])
        if name == "entity.add":
            collection = str(operation["collection"])
            value = deepcopy(operation["value"])
            value["id"] = str(operation["assigned_id"])
            _, kind = _COLLECTIONS[collection]
            _normalize_entity_fields(value, kind=kind, observations=observations, is_add=True, run_id=run_id)
            _collection(data, collection).append(value)
        elif name == "entity.update":
            target_id = str(operation["target_id"])
            index = _entity_index(data)
            target = index.get(target_id)
            if target is None:
                raise HarnessError("requirement_delta_invalid", f"Unknown update target: {target_id}", run_id=run_id)
            item, kind, _ = target
            fields = deepcopy(operation["fields"])
            if not isinstance(fields, dict) or not fields:
                raise HarnessError("requirement_delta_invalid", "Entity update fields must be a non-empty object.", run_id=run_id)
            _normalize_entity_fields(fields, kind=kind, observations=observations, is_add=False, run_id=run_id)
            item.update(fields)
        elif name == "entity.remove":
            target_id = str(operation["target_id"])
            index = _entity_index(data)
            target = index.get(target_id)
            if target is None:
                raise HarnessError("requirement_delta_invalid", f"Unknown remove target: {target_id}", run_id=run_id)
            _, kind, collection = target
            if kind in {"UC", "GOAL"}:
                raise HarnessError("requirement_delta_policy_violation", f"Cannot remove protected entity {target_id}.", run_id=run_id)
            items = _collection(data, collection)
            items[:] = [item for item in items if str(item.get("id") or "") != target_id]
        else:
            scenario_id = str(operation["scenario_id"])
            surface_name = str(operation["surface"])
            fields = operation["fields"]
            if surface_name not in _SURFACE_NAMES or not isinstance(fields, dict) or not fields:
                raise HarnessError("requirement_delta_invalid", "Surface update target or fields are invalid.", run_id=run_id)
            unknown = set(fields).difference(_SURFACE_FIELDS)
            if unknown:
                raise HarnessError("requirement_delta_policy_violation", "Surface update contains forbidden fields.", run_id=run_id)
            scenario_target = _entity_index(data).get(scenario_id)
            if scenario_target is None or scenario_target[1] != "SC":
                raise HarnessError("requirement_delta_invalid", f"Unknown scenario target: {scenario_id}", run_id=run_id)
            surfaces = scenario_target[0].get("surfaces")
            if not isinstance(surfaces, dict) or not isinstance(surfaces.get(surface_name), dict):
                raise HarnessError("requirement_delta_invalid", "Scenario surface does not exist.", run_id=run_id)
            surfaces[surface_name].update(deepcopy(fields))


def _normalize_entity_fields(
    fields: dict[str, Any],
    *,
    kind: str,
    observations: dict[str, dict[str, object]],
    is_add: bool,
    run_id: str,
) -> None:
    allowed = _ADD_FIELDS[kind] if is_add else _UPDATE_FIELDS[kind]
    candidate_fields = set(fields) - ({"id"} if is_add else set())
    unknown = candidate_fields.difference(allowed)
    if unknown:
        raise HarnessError(
            "requirement_delta_policy_violation",
            f"{kind} mutation contains forbidden fields: {', '.join(sorted(unknown))}.",
            run_id=run_id,
        )
    if kind == "Q" and is_add:
        fields["status"] = "open"
        fields["effective_blocking"] = False
    if kind != "E":
        return
    observation_ref = fields.pop("observation_ref", None)
    quote_changed = "quote" in fields
    if is_add or quote_changed:
        observation = observations.get(str(observation_ref or ""))
        if observation is None or not observation_text_contains_quote(observation, fields.get("quote")):
            raise HarnessError(
                "requirement_delta_policy_violation",
                "Evidence delta is not grounded in the selected observation.",
                run_id=run_id,
            )
        fields["source_id"] = str(observation.get("source_id") or "")
    elif observation_ref is not None:
        raise HarnessError(
            "requirement_delta_policy_violation",
            "Evidence observation_ref may only accompany a quote update.",
            run_id=run_id,
        )


def _entity_index(data: dict[str, Any]) -> dict[str, tuple[dict[str, Any], str, str]]:
    result: dict[str, tuple[dict[str, Any], str, str]] = {}
    use_case = data.get("use_case")
    if isinstance(use_case, dict):
        _index_item(result, use_case, "UC", "use_case")
        goal = use_case.get("goal")
        if isinstance(goal, dict):
            _index_item(result, goal, "GOAL", "use_case.goal")
    for collection, (_, kind) in _COLLECTIONS.items():
        for item in _collection(data, collection, required=False):
            _index_item(result, item, kind, collection)
    return result


def _index_item(
    result: dict[str, tuple[dict[str, Any], str, str]],
    item: dict[str, Any],
    kind: str,
    collection: str,
) -> None:
    item_id = str(item.get("id") or "")
    if item_id:
        if item_id in result:
            raise HarnessError(
                "requirement_delta_invalid",
                f"Requirement delta base contains duplicate ID: {item_id}",
            )
        result[item_id] = (item, kind, collection)


def _collection(
    data: dict[str, Any],
    name: str,
    *,
    required: bool = True,
) -> list[dict[str, Any]]:
    if name == "use_case.actors":
        parent = data.get("use_case")
        value = parent.get("actors") if isinstance(parent, dict) else None
    elif name.startswith("flows."):
        parent = data.get("flows")
        value = parent.get(name.split(".", 1)[1]) if isinstance(parent, dict) else None
    elif name == "scenario_contract.scenarios":
        parent = data.get("scenario_contract")
        value = parent.get("scenarios") if isinstance(parent, dict) else None
    else:
        value = data.get(name)
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    if required:
        raise HarnessError("requirement_delta_invalid", f"Candidate collection is invalid: {name}")
    return []


def _require_keys(value: dict[str, object], expected: set[str], *, run_id: str) -> None:
    if set(value) != expected:
        raise HarnessError(
            "requirement_delta_invalid",
            "Requirement delta operation fields are invalid.",
            run_id=run_id,
        )
