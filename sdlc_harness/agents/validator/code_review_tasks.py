"""Deterministic Scenario + Boundary task planning for Phase 4 code review."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any


CODE_VALIDATION_MODE = "scenario_boundary_code_validation_v1"
CODE_VALIDATION_REVIEW_PLAN_VERSION = "code_validation_review_plan_v2"
CODE_VALIDATION_TASK_CACHE_VERSION = "code_validation_task_cache_v1"
DEFERRED_UI_EXPERIENCE_FIELDS = (
    "accessibility",
    "focus_flow",
    "focus_policies",
    "guidance_refs",
    "repo_evidence_refs",
    "responsive",
    "visual_contract",
)


def build_code_validation_review_plan(
    *,
    work_packet: dict[str, object],
    approved_context: dict[str, object],
    validation_claims: list[dict[str, object]],
) -> dict[str, object]:
    """Assign every semantic claim to one scenario or the cross-scenario boundary."""

    structured = _dict(approved_context.get("implementation_plan_structured_data"))
    design_graph = _dict(approved_context.get("vertical_design_graph_data"))
    design_catalog = {
        str(item.get("id") or "").strip(): item
        for item in [*_dicts(design_graph.get("nodes")), *_dicts(design_graph.get("edges"))]
        if str(item.get("id") or "").strip()
    }
    units = _dicts(structured.get("work_units")) or _dicts(work_packet.get("execution_units"))
    if not units:
        units = [_synthetic_unit(work_packet=work_packet, validation_claims=validation_claims)]

    unit_by_id = {
        str(unit.get("unit_id") or "").strip(): unit
        for unit in units
        if str(unit.get("unit_id") or "").strip()
    }
    scenario_units: dict[str, list[dict[str, object]]] = defaultdict(list)
    for unit in units:
        scenario_id = str(unit.get("scenario_id") or "GLOBAL").strip() or "GLOBAL"
        scenario_units[scenario_id].append(unit)

    local_claims: dict[str, list[dict[str, object]]] = defaultdict(list)
    boundary_claims: list[dict[str, object]] = []
    assignments: dict[str, str] = {}
    for claim in validation_claims:
        claim_id = str(claim.get("claim_id") or "").strip()
        matching = _matching_claim_scenarios(units=units, claim=claim)
        if len(matching) == 1:
            scenario_id = next(iter(matching))
            local_claims[scenario_id].append(claim)
            assignments[claim_id] = f"scenario:{scenario_id}"
        elif not matching and len(scenario_units) == 1:
            scenario_id = next(iter(scenario_units))
            local_claims[scenario_id].append(claim)
            assignments[claim_id] = f"scenario:{scenario_id}"
        else:
            boundary_claims.append(claim)
            assignments[claim_id] = "boundary"

    derived_boundary_claims = _boundary_contract_claims(units=units, unit_by_id=unit_by_id)
    for claim in derived_boundary_claims:
        claim_id = str(claim["claim_id"])
        boundary_claims.append(claim)
        assignments[claim_id] = "boundary"

    tasks: list[dict[str, object]] = []
    for scenario_id in sorted(scenario_units, key=_scenario_sort_key):
        task_units = sorted(scenario_units[scenario_id], key=_unit_sort_key)
        tasks.append(
            _task(
                task_id=f"scenario:{scenario_id}",
                task_kind="scenario",
                scenario_ids=[scenario_id],
                units=task_units,
                claims=local_claims.get(scenario_id, []),
                design_catalog=design_catalog,
            )
        )

    if boundary_claims or _has_cross_scenario_structure(units=units, unit_by_id=unit_by_id):
        tasks.append(
            _task(
                task_id="boundary:cross-scenario",
                task_kind="boundary",
                scenario_ids=sorted(scenario_units, key=_scenario_sort_key),
                units=sorted(units, key=_unit_sort_key),
                claims=boundary_claims,
                design_catalog=design_catalog,
            )
        )

    reviewed = [
        claim_id
        for task in tasks
        for claim_id in _strings(task.get("authority_claim_ids"))
    ]
    expected = [
        str(claim.get("claim_id") or "").strip()
        for claim in [*validation_claims, *derived_boundary_claims]
        if str(claim.get("claim_id") or "").strip()
    ]
    if len(reviewed) != len(set(reviewed)) or set(reviewed) != set(expected):
        raise ValueError("Code validation task plan does not assign every claim exactly once.")

    return {
        "version": CODE_VALIDATION_REVIEW_PLAN_VERSION,
        "mode": CODE_VALIDATION_MODE,
        "tasks": tasks,
        "claim_assignments": assignments,
        "coverage": {
            "expected_claim_ids": sorted(expected),
            "assigned_claim_ids": sorted(reviewed),
            "task_count": len(tasks),
        },
    }


class CodeValidationTaskCache:
    """Cache only contract-valid task outputs for byte-identical prompts."""

    def __init__(self, path: Path | None):
        self.path = path

    @staticmethod
    def key(prompt: str) -> str:
        return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def get(self, prompt: str) -> dict[str, object] | None:
        key = self.key(prompt)
        entry = _dict(self._read().get("entries")).get(key)
        if not isinstance(entry, dict):
            return None
        if entry.get("prompt_sha256") != key and entry.get("prompt") != prompt:
            return None
        output = entry.get("output")
        return dict(output) if isinstance(output, dict) else None

    def put(self, prompt: str, output: dict[str, object]) -> None:
        if self.path is None:
            return
        payload = self._read()
        entries = _dict(payload.get("entries"))
        prompt_sha256 = self.key(prompt)
        entries[prompt_sha256] = {"prompt_sha256": prompt_sha256, "output": output}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": CODE_VALIDATION_TASK_CACHE_VERSION,
                    "entries": entries,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _read(self) -> dict[str, object]:
        if self.path is None or not self.path.is_file():
            return {"schema_version": CODE_VALIDATION_TASK_CACHE_VERSION, "entries": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": CODE_VALIDATION_TASK_CACHE_VERSION, "entries": {}}
        if not isinstance(payload, dict) or payload.get("schema_version") != CODE_VALIDATION_TASK_CACHE_VERSION:
            return {"schema_version": CODE_VALIDATION_TASK_CACHE_VERSION, "entries": {}}
        return payload


def _task(
    *,
    task_id: str,
    task_kind: str,
    scenario_ids: list[str],
    units: list[dict[str, object]],
    claims: list[dict[str, object]],
    design_catalog: dict[str, dict[str, object]],
) -> dict[str, object]:
    design_ids = _task_design_ids(
        task_kind=task_kind,
        units=units,
        design_catalog=design_catalog,
    )
    context_design_ids = _context_design_ids(
        task_kind=task_kind,
        scenario_ids=set(scenario_ids),
        design_ids=set(design_ids),
        design_catalog=design_catalog,
    )
    owned_boundary_contract_ids = sorted(
        design_id
        for design_id in design_ids
        if _owns_boundary_contract(
            task_kind=task_kind,
            contract=design_catalog.get(design_id),
            design_catalog=design_catalog,
        )
    )
    allowed_design_ids = set(design_ids)
    allowed_unit_ids = {
        str(unit.get("unit_id") or "").strip()
        for unit in units
        if str(unit.get("unit_id") or "").strip()
    }
    projected_claims = [
        _project_claim(claim, allowed_design_ids=allowed_design_ids) for claim in claims
    ]
    task_requirement_refs = sorted(
        {
            value
            for claim in claims
            for value in [str(claim.get("requirement_ref") or "").strip()]
            if value
        }
    )
    code_targets = _unique_dicts(
        target
        for unit in units
        for target in _dicts(unit.get("planned_code_targets"))
        if str(target.get("path") or "").strip()
    )
    test_paths = sorted(
        {
            path
            for unit in units
            for path in [
                *_strings(unit.get("test_target_paths")),
                *[
                    target_path
                    for intent in [
                        *_dicts(unit.get("test_intents")),
                        *_dicts(unit.get("qa_test_intents")),
                    ]
                    if str(intent.get("stage_id") or "").strip() != "typecheck"
                    for target_path in _strings(intent.get("target_paths"))
                ],
            ]
            if path
        }
    )
    approved_design_contracts: list[dict[str, object]] = []
    deferred_design_sections: list[dict[str, object]] = []
    scenario_owned_design_ids: dict[str, list[str]] = defaultdict(list)
    for design_id in design_ids:
        contract = design_catalog.get(design_id)
        if contract is None:
            continue
        projected, deferred_paths = _project_active_design_contract(contract)
        if (
            task_kind == "boundary"
            and isinstance(projected.get("boundary_contract"), dict)
            and not _is_cross_scenario_edge(contract, design_catalog=design_catalog)
        ):
            projected.pop("boundary_contract", None)
            scenario_id = str(contract.get("scenario_id") or "GLOBAL").strip() or "GLOBAL"
            scenario_owned_design_ids[f"scenario:{scenario_id}"].append(design_id)
        approved_design_contracts.append(projected)
        if deferred_paths:
            deferred_design_sections.append(
                {
                    "design_id": design_id,
                    "stage": "browser_ui_qa",
                    "field_paths": deferred_paths,
                }
            )
    return {
        "task_id": task_id,
        "task_kind": task_kind,
        "scenario_ids": scenario_ids,
        "unit_ids": [str(unit.get("unit_id") or "").strip() for unit in units],
        "authority_claim_ids": [str(claim.get("claim_id") or "").strip() for claim in claims],
        "claims": projected_claims,
        "requirement_refs": task_requirement_refs,
        "design_ids": design_ids,
        "context_only_design_ids": context_design_ids,
        "owned_boundary_contract_ids": owned_boundary_contract_ids,
        "approved_design_contracts": approved_design_contracts,
        "deferred_design_sections": deferred_design_sections,
        "scenario_owned_design_sections": [
            {
                "owner_task_id": owner_task_id,
                "field_path": "boundary_contract",
                "design_ids": sorted(owner_design_ids),
            }
            for owner_task_id, owner_design_ids in sorted(scenario_owned_design_ids.items())
        ],
        "context_only_design_contracts": [
            _project_context_design_contract(design_catalog[value])
            for value in context_design_ids
            if value in design_catalog
        ],
        "code_targets": code_targets,
        "test_paths": test_paths,
        "units": [
            _project_unit(
                unit,
                allowed_design_ids=allowed_design_ids,
                allowed_unit_ids=allowed_unit_ids,
            )
            for unit in units
        ],
    }


def _owns_boundary_contract(
    *,
    task_kind: str,
    contract: dict[str, object] | None,
    design_catalog: dict[str, dict[str, object]],
) -> bool:
    if not contract or not isinstance(contract.get("boundary_contract"), dict):
        return False
    is_cross_scenario = _is_cross_scenario_edge(contract, design_catalog=design_catalog)
    return is_cross_scenario if task_kind == "boundary" else not is_cross_scenario


def _task_design_ids(
    *,
    task_kind: str,
    units: list[dict[str, object]],
    design_catalog: dict[str, dict[str, object]],
) -> list[str]:
    selected = {
        value
        for unit in units
        for value in [*_strings(unit.get("design_node_ids")), *_strings(unit.get("design_edge_ids"))]
    }
    if task_kind == "scenario":
        selected = {
            value
            for value in selected
            if not _is_cross_scenario_edge(design_catalog.get(value), design_catalog=design_catalog)
        }
    pending = list(selected)
    while pending:
        contract = design_catalog.get(pending.pop())
        if not contract:
            continue
        for endpoint in (str(contract.get("from") or ""), str(contract.get("to") or "")):
            if endpoint and endpoint in design_catalog and endpoint not in selected:
                selected.add(endpoint)
                pending.append(endpoint)
    return sorted(selected)


def _context_design_ids(
    *,
    task_kind: str,
    scenario_ids: set[str],
    design_ids: set[str],
    design_catalog: dict[str, dict[str, object]],
) -> list[str]:
    """Select compact explanatory contracts for exact code entities shared by other scenarios."""

    if task_kind != "scenario":
        return []
    owned_targets = {
        target
        for design_id in design_ids
        for target in [_runtime_target_key(design_catalog.get(design_id))]
        if target
    }
    if not owned_targets:
        return []
    context_nodes = {
        design_id
        for design_id, contract in design_catalog.items()
        if design_id not in design_ids
        and str(contract.get("scenario_id") or "").strip() not in scenario_ids
        and _runtime_target_key(contract) in owned_targets
    }
    visible_nodes = design_ids | context_nodes
    context_edges = {
        design_id
        for design_id, contract in design_catalog.items()
        if design_id not in design_ids
        and str(contract.get("scenario_id") or "").strip() not in scenario_ids
        and str(contract.get("from") or "") in visible_nodes
        and str(contract.get("to") or "") in visible_nodes
    }
    return sorted(context_nodes | context_edges)


def _runtime_target_key(contract: dict[str, object] | None) -> str:
    if not contract:
        return ""
    target = _dict(_dict(contract.get("runtime_binding")).get("target"))
    path = str(target.get("path") or "").strip().replace("\\", "/")
    if not path:
        return ""
    return json.dumps(
        {
            "component_id": str(target.get("component_id") or "").strip(),
            "path": path,
            "symbol": str(target.get("symbol") or "").strip(),
        },
        sort_keys=True,
    )


def _project_context_design_contract(contract: dict[str, object]) -> dict[str, object]:
    """Keep behavior needed to interpret shared code without importing another task wholesale."""

    keys = (
        "id",
        "scenario_id",
        "kind",
        "surface",
        "name",
        "label",
        "from",
        "to",
        "guard",
        "terminal",
        "requirement_refs",
        "contract",
        "boundary_contract",
    )
    projected = {key: contract.get(key) for key in keys if contract.get(key) not in (None, [], {}, "")}
    binding = _dict(contract.get("runtime_binding"))
    target = _dict(binding.get("target"))
    if target:
        projected["runtime_binding"] = {
            **({"runtime_kind": binding.get("runtime_kind")} if binding.get("runtime_kind") else {}),
            "target": target,
        }
    surface_contract = _dict(contract.get("surface_contract"))
    if surface_contract:
        semantic_keys = (
            "schema_version",
            "surface",
            "method",
            "path",
            "operation_id",
            "authentication_required",
            "authentication",
            "request_fields",
            "responses",
            "inputs",
            "outcomes",
            "decision",
            "operation",
            "constraints",
            "states",
            "transitions",
            "validations",
        )
        projected["surface_contract"] = {
            key: surface_contract.get(key)
            for key in semantic_keys
            if surface_contract.get(key) not in (None, [], {}, "")
        }
        # Shared-code behavior can live only in the experience contract (e.g. error-state feedback).
        experience = {
            key: value
            for key, value in _dict(surface_contract.get("experience_contract")).items()
            if key not in DEFERRED_UI_EXPERIENCE_FIELDS and value not in (None, [], {}, "")
        }
        if experience:
            projected["surface_contract"]["experience_contract"] = experience
    return projected


def _project_active_design_contract(
    contract: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    """Separate browser/UI-only fields while preserving current-stage Design authority."""

    projected = dict(contract)
    for field in (
        "evidence_reason",
        "evidence_refs",
        "source_observation_refs",
        "semantic_key",
        "disposition",
    ):
        projected.pop(field, None)
    surface_contract = _dict(projected.get("surface_contract"))
    experience = _dict(surface_contract.get("experience_contract"))
    if not experience:
        return projected, []
    active_experience = dict(experience)
    deferred_paths: list[str] = []
    for field in DEFERRED_UI_EXPERIENCE_FIELDS:
        if field not in active_experience:
            continue
        active_experience.pop(field, None)
        deferred_paths.append(f"surface_contract.experience_contract.{field}")
    surface_contract["experience_contract"] = active_experience
    projected["surface_contract"] = surface_contract
    return projected, deferred_paths


def _is_cross_scenario_edge(
    contract: dict[str, object] | None,
    *,
    design_catalog: dict[str, dict[str, object]],
) -> bool:
    if not contract:
        return False
    source = design_catalog.get(str(contract.get("from") or ""))
    target = design_catalog.get(str(contract.get("to") or ""))
    if not source or not target:
        return False
    source_scenario = str(source.get("scenario_id") or "").strip()
    target_scenario = str(target.get("scenario_id") or "").strip()
    return bool(source_scenario and target_scenario and source_scenario != target_scenario)


def _project_claim(
    claim: dict[str, object],
    *,
    allowed_design_ids: set[str],
) -> dict[str, object]:
    projected = dict(claim)
    projected.pop("design_targets", None)
    projected.pop("test_intents", None)
    projected["design_element_ids"] = [
        value for value in _strings(claim.get("design_element_ids")) if value in allowed_design_ids
    ]
    return projected


def _boundary_contract_claims(
    *,
    units: list[dict[str, object]],
    unit_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    seen: set[str] = set()
    for unit in units:
        unit_id = str(unit.get("unit_id") or "").strip()
        scenario_id = str(unit.get("scenario_id") or "GLOBAL").strip() or "GLOBAL"
        for dependency_id in _strings(unit.get("depends_on")):
            dependency = unit_by_id.get(dependency_id)
            dependency_scenario = str((dependency or {}).get("scenario_id") or "GLOBAL").strip() or "GLOBAL"
            if dependency is None or dependency_scenario == scenario_id:
                continue
            identity = f"dependency|{dependency_id}|{unit_id}"
            claim_id = _boundary_claim_id(identity)
            if claim_id in seen:
                continue
            seen.add(claim_id)
            claims.append(
                {
                    "claim_id": claim_id,
                    "claim_kind": "cross_scenario_dependency",
                    "requirement_ref": "",
                    "requirement_content": (
                        f"Work unit {unit_id} in {scenario_id} must integrate with dependency "
                        f"{dependency_id} in {dependency_scenario}."
                    ),
                    "design_element_ids": sorted(
                        set(_strings(unit.get("design_edge_ids")))
                        | set(_strings((dependency or {}).get("design_edge_ids")))
                    ),
                    "code_targets": sorted(
                        set(_unit_target_refs(unit)) | set(_unit_target_refs(dependency or {}))
                    ),
                    "unit_ids": [dependency_id, unit_id],
                }
            )

    owners: dict[str, list[dict[str, object]]] = defaultdict(list)
    for unit in units:
        for target in _dicts(unit.get("planned_code_targets")):
            path = str(target.get("path") or "").strip()
            if path:
                owners[path].append(unit)
    for path, path_units in owners.items():
        scenarios = {
            str(unit.get("scenario_id") or "GLOBAL").strip() or "GLOBAL"
            for unit in path_units
        }
        if len(scenarios) < 2:
            continue
        unit_ids = sorted({str(unit.get("unit_id") or "").strip() for unit in path_units})
        identity = f"shared_path|{path}|{'|'.join(unit_ids)}"
        claim_id = _boundary_claim_id(identity)
        if claim_id in seen:
            continue
        seen.add(claim_id)
        claims.append(
            {
                "claim_id": claim_id,
                "claim_kind": "shared_code_target",
                "requirement_ref": "",
                "requirement_content": (
                    f"Shared code target {path} must preserve the contracts of scenarios "
                    f"{', '.join(sorted(scenarios, key=_scenario_sort_key))}."
                ),
                "design_element_ids": sorted(
                    {
                        design_id
                        for unit in path_units
                        for design_id in [
                            *_strings(unit.get("design_node_ids")),
                            *_strings(unit.get("design_edge_ids")),
                        ]
                    }
                ),
                "code_targets": [path],
                "unit_ids": unit_ids,
            }
        )
    return claims


def _matching_claim_scenarios(
    *,
    units: list[dict[str, object]],
    claim: dict[str, object],
) -> set[str]:
    """Use the strongest exact projection before falling back to shared code paths."""

    requirement_ref = str(claim.get("requirement_ref") or "").strip()
    if requirement_ref:
        requirement_matches = {
            _unit_scenario(unit)
            for unit in units
            if requirement_ref
            in (set(_strings(unit.get("requirement_refs"))) | set(_strings(unit.get("acceptance_refs"))))
        }
        if requirement_matches:
            return requirement_matches
    claim_design_ids = set(_strings(claim.get("design_element_ids")))
    if claim_design_ids:
        design_matches = {
            _unit_scenario(unit)
            for unit in units
            if claim_design_ids
            & (set(_strings(unit.get("design_node_ids"))) | set(_strings(unit.get("design_edge_ids"))))
        }
        if design_matches:
            return design_matches
    claim_paths = {_target_path(value) for value in _strings(claim.get("code_targets"))}
    return {
        _unit_scenario(unit)
        for unit in units
        if claim_paths & {_target_path(value) for value in _unit_target_refs(unit)}
    }


def _unit_scenario(unit: dict[str, object]) -> str:
    return str(unit.get("scenario_id") or "GLOBAL").strip() or "GLOBAL"


def _has_cross_scenario_structure(
    *,
    units: list[dict[str, object]],
    unit_by_id: dict[str, dict[str, object]],
) -> bool:
    scenarios_by_path: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        scenario_id = str(unit.get("scenario_id") or "GLOBAL").strip() or "GLOBAL"
        for target in _dicts(unit.get("planned_code_targets")):
            path = str(target.get("path") or "").strip()
            if path:
                scenarios_by_path[path].add(scenario_id)
        for dependency_id in _strings(unit.get("depends_on")):
            dependency = unit_by_id.get(dependency_id)
            if dependency and str(dependency.get("scenario_id") or "GLOBAL").strip() != scenario_id:
                return True
    return any(len(scenarios) > 1 for scenarios in scenarios_by_path.values())


def _synthetic_unit(
    *,
    work_packet: dict[str, object],
    validation_claims: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "unit_id": "WU-VALIDATION-BASELINE",
        "scenario_id": "GLOBAL",
        "depends_on": [],
        "requirement_refs": [
            str(claim.get("requirement_ref") or "").strip()
            for claim in validation_claims
            if str(claim.get("requirement_ref") or "").strip()
        ],
        "design_node_ids": [
            value for claim in validation_claims for value in _strings(claim.get("design_element_ids"))
        ],
        "design_edge_ids": [],
        "planned_code_targets": [
            {"path": _target_path(value), "symbol": _target_symbol(value)}
            for claim in validation_claims
            for value in _strings(claim.get("code_targets"))
            if _target_path(value)
        ],
        "test_target_paths": [
            str(item.get("path") or "").strip()
            for item in _dicts(work_packet.get("proposed_files"))
            if str(item.get("path") or "").strip()
        ],
    }


def _project_unit(
    unit: dict[str, object],
    *,
    allowed_design_ids: set[str],
    allowed_unit_ids: set[str],
) -> dict[str, object]:
    keys = (
        "unit_id",
        "scenario_id",
        "title",
        "depends_on",
        "design_node_ids",
        "design_edge_ids",
        "runtime_bindings",
        "planned_code_targets",
    )
    projected = {key: unit.get(key) for key in keys if unit.get(key) not in (None, [], {}, "")}
    projected["test_paths"] = sorted(
        {
            path
            for path in [
                *_strings(unit.get("test_target_paths")),
                *[
                    target_path
                    for intent in [
                        *_dicts(unit.get("test_intents")),
                        *_dicts(unit.get("qa_test_intents")),
                    ]
                    if str(intent.get("stage_id") or "").strip() != "typecheck"
                    for target_path in _strings(intent.get("target_paths"))
                ],
            ]
            if path
        }
    )
    dependencies = [value for value in _strings(projected.get("depends_on")) if value in allowed_unit_ids]
    if dependencies:
        projected["depends_on"] = dependencies
    else:
        projected.pop("depends_on", None)
    for key in ("design_node_ids", "design_edge_ids"):
        values = [value for value in _strings(projected.get(key)) if value in allowed_design_ids]
        if values:
            projected[key] = values
        else:
            projected.pop(key, None)
    if "runtime_bindings" in projected:
        bindings: list[dict[str, object]] = []
        for raw_binding in _dicts(projected["runtime_bindings"]):
            binding = dict(raw_binding)
            wired_by = str(binding.get("attachment_wired_by") or "").strip()
            if wired_by and wired_by not in allowed_unit_ids:
                binding.pop("attachment_wired_by", None)
            bindings.append(binding)
        projected["runtime_bindings"] = bindings
    return projected


def _unit_target_refs(unit: dict[str, object]) -> list[str]:
    values: list[str] = []
    for target in _dicts(unit.get("planned_code_targets")):
        path = str(target.get("path") or "").strip()
        symbol = str(target.get("symbol") or "").strip()
        if path:
            values.append(path + (f"#{symbol}" if symbol else ""))
    values.extend(_strings(unit.get("write_paths")))
    values.extend(_strings(unit.get("test_target_paths")))
    return values


def _boundary_claim_id(identity: str) -> str:
    return "BOUNDARY-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12].upper()


def _target_path(value: str) -> str:
    return str(value or "").split("#", 1)[0].strip().replace("\\", "/")


def _target_symbol(value: str) -> str:
    parts = str(value or "").split("#", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def _scenario_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", value)
    return (int(match.group(1)) if match else 10**9, value)


def _unit_sort_key(unit: dict[str, object]) -> tuple[int, str]:
    try:
        order = int(unit.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    return order, str(unit.get("unit_id") or "")


def _unique_dicts(values: Any) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
