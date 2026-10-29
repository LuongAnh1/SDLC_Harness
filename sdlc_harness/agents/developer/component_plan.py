"""Deterministic component-aware implementation plan contract for Phase 3."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...core.errors import HarnessError
from ...profiles import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PlanningPathPolicy,
)
from ..designer.graph import GRAPH_VERSION_V2, validate_vertical_design_graph
from ..validator.vertical_trace import validate_vertical_trace_graph


COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION = (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
)
IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY = "implementation_plan_contract_version"

_ACTIONABLE_DISPOSITIONS = {"add", "modify"}
_FORWARD_DEPENDENCY_KINDS = {"calls", "reads", "writes", "depends_on", "realized_by"}
_REVERSE_DEPENDENCY_KINDS = {"returns"}
_TYPESCRIPT_SHARED_CONTRACT_BARREL = "packages/contracts/src/index.ts"
_TYPESCRIPT_DATABASE_SOURCE_ROOT = "packages/db/src/"
_TYPESCRIPT_DATABASE_BARREL = "packages/db/src/index.ts"
_IMMUTABLE_UNIT_FIELDS = {
    "unit_id",
    "order",
    "scenario_id",
    "component_id",
    "surfaces",
    "design_node_ids",
    "design_edge_ids",
    "requirement_refs",
    "acceptance_refs",
    "depends_on",
    "runtime_bindings",
    "boundary_contracts",
}


def resolve_implementation_plan_contract(state: dict[str, object]) -> str:
    """Resolve the plan schema pinned to a run; missing markers are historical V3."""

    value = str(state.get(IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY) or "").strip()
    if not value:
        return PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION
    if value not in {
        COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
        PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    }:
        raise HarnessError(
            "implementation_plan_contract_invalid",
            f"Unsupported implementation plan contract: {value}",
            run_id=str(state.get("run_id") or "") or None,
        )
    return value


def build_component_work_units(
    *,
    design_graph: dict[str, object],
    trace_graph: dict[str, object],
    repo_landscape: dict[str, object],
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    """Build stable scenario/component units from approved Vertical Design nodes."""

    run_id = str(design_graph.get("run_id") or "")
    validate_vertical_design_graph(design_graph, expected_run_id=run_id)
    trace_binding = _dict(trace_graph.get("binding"))
    validate_vertical_trace_graph(
        trace_graph,
        expected_run_id=run_id,
        expected_requirement_digest=str(trace_binding.get("requirement_digest") or ""),
        expected_design_digest=str(design_graph.get("artifact_digest") or ""),
    )

    allowed_components = {str(item[0]) for item in planning_policy.component_roots}
    nodes = {str(item.get("id") or ""): item for item in _dicts(design_graph.get("nodes"))}
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for node in nodes.values():
        if str(node.get("disposition") or "") not in _ACTIONABLE_DISPOSITIONS:
            continue
        scenario_id = str(node.get("scenario_id") or "").strip()
        component_id = str(node.get("component_id") or "").strip()
        if not scenario_id or component_id not in allowed_components:
            raise HarnessError(
                "implementation_plan_component_invalid",
                f"Actionable Design node {node.get('id')} has unsupported component `{component_id}`.",
                run_id=run_id,
            )
        grouped.setdefault((scenario_id, component_id), []).append(node)

    trace_nodes = {str(item.get("id") or ""): item for item in _dicts(trace_graph.get("nodes"))}
    acceptance_by_design: dict[str, set[str]] = {}
    for edge in _dicts(trace_graph.get("edges")):
        if str(edge.get("kind") or "") != "realizes":
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if str(_dict(trace_nodes.get(source)).get("kind") or "") == "acceptance_criterion" and target in nodes:
            acceptance_by_design.setdefault(target, set()).add(source)

    units: list[dict[str, object]] = []
    unit_by_node: dict[str, str] = {}
    for (scenario_id, component_id), group_nodes in sorted(grouped.items()):
        node_ids = sorted(str(item.get("id") or "") for item in group_nodes)
        unit_id = _unit_id(scenario_id=scenario_id, component_id=component_id, node_ids=node_ids)
        for node_id in node_ids:
            unit_by_node[node_id] = unit_id
        requirement_refs = sorted(
            {
                ref
                for node in group_nodes
                for ref in _strings(node.get("requirement_refs"))
            }
        )
        acceptance_refs = sorted(
            {
                ref
                for node_id in node_ids
                for ref in acceptance_by_design.get(node_id, set())
            }
        )
        evidence_refs = sorted(
            {
                ref
                for node in group_nodes
                for ref in [
                    *_strings(node.get("evidence_refs")),
                    *_strings(_dict(node.get("runtime_binding")).get("evidence_refs")),
                ]
            }
        )
        seeded_targets: list[dict[str, object]] = []
        seen_targets: set[tuple[str, str]] = set()
        runtime_bindings: list[dict[str, object]] = []
        for node in group_nodes:
            binding = _dict(node.get("runtime_binding"))
            if not binding:
                continue
            target = _dict(binding.get("target"))
            attachment = _dict(binding.get("attachment"))
            target_path = planning_policy.normalize_editable_path(target.get("path"))
            target_symbol = str(target.get("symbol") or "").strip()
            target_key = (target_path, target_symbol)
            if target_path and target_key not in seen_targets:
                seen_targets.add(target_key)
                seeded_targets.append(
                    {"path": target_path, "symbol": target_symbol, "action": "create_or_update"}
                )
            owner_path = str(attachment.get("owner_path") or "").strip()
            owner_symbol = str(attachment.get("owner_symbol") or "").strip()
            if owner_path:
                normalized_owner = planning_policy.normalize_editable_path(owner_path)
                owner_key = (normalized_owner, owner_symbol)
                if normalized_owner and owner_key not in seen_targets:
                    seen_targets.add(owner_key)
                    seeded_targets.append(
                        {"path": normalized_owner, "symbol": owner_symbol, "action": "create_or_update"}
                    )
            runtime_bindings.append(
                {
                    "design_node_id": str(node.get("id") or ""),
                    "runtime_kind": str(binding.get("runtime_kind") or ""),
                    "target": target,
                    "attachment": attachment,
                }
            )
        surfaces = sorted({str(item.get("surface") or "") for item in group_nodes if item.get("surface")})
        unit: dict[str, object] = {
            "unit_id": unit_id,
            "order": 0,
            "scenario_id": scenario_id,
            "component_id": component_id,
            "surfaces": surfaces,
            "title": f"Implement {component_id} changes for {scenario_id}",
            "objective": " ".join(str(item.get("contract") or "").strip() for item in group_nodes).strip(),
            "design_node_ids": node_ids,
            "design_edge_ids": [],
            "requirement_refs": requirement_refs,
            "acceptance_refs": acceptance_refs,
            "depends_on": [],
            "planned_code_targets": seeded_targets,
            "test_intents": [],
            "repo_evidence_refs": evidence_refs,
            "runtime_bindings": runtime_bindings,
            "boundary_contracts": [],
        }
        if "data" in surfaces:
            intents = sorted(
                {
                    str(_dict(item.get("surface_contract")).get("migration_intent") or "").strip()
                    for item in group_nodes
                    if str(item.get("surface") or "") == "data"
                    and str(_dict(item.get("surface_contract")).get("migration_intent") or "").strip()
                }
            )
            unit["migration_impact"] = {
                "status": "",
                "design_intent": "; ".join(intents),
                "reason": "",
                "targets": [],
            }
        units.append(unit)

    units_by_id = {str(item["unit_id"]): item for item in units}
    scenario_kinds = {
        str(item.get("id") or ""): str(item.get("scenario_kind") or "")
        for item in _dicts(trace_graph.get("nodes"))
        if str(item.get("kind") or "") == "scenario"
    }
    for edge in _dicts(design_graph.get("edges")):
        source_node = str(edge.get("from") or "")
        target_node = str(edge.get("to") or "")
        source_unit = unit_by_node.get(source_node, "")
        target_unit = unit_by_node.get(target_node, "")
        edge_id = str(edge.get("id") or "")
        for unit_id in {source_unit, target_unit} - {""}:
            edge_ids = units_by_id[unit_id]["design_edge_ids"]
            if isinstance(edge_ids, list) and edge_id and edge_id not in edge_ids:
                edge_ids.append(edge_id)
            boundary = _dict(edge.get("boundary_contract"))
            if boundary:
                contracts = units_by_id[unit_id].get("boundary_contracts")
                if isinstance(contracts, list):
                    contracts.append(
                        {
                            "design_edge_id": edge_id,
                            "from": source_node,
                            "to": target_node,
                            **boundary,
                        }
                    )
        if not source_unit or not target_unit or source_unit == target_unit:
            continue
        kind = str(edge.get("kind") or "")
        dependency = ""
        dependent = ""
        if kind in _FORWARD_DEPENDENCY_KINDS:
            dependent, dependency = source_unit, target_unit
        elif kind in _REVERSE_DEPENDENCY_KINDS:
            dependent, dependency = target_unit, source_unit
        if dependent and dependency:
            refs = units_by_id[dependent]["depends_on"]
            if isinstance(refs, list) and dependency not in refs:
                refs.append(dependency)
        source_scenario = str(_dict(nodes.get(source_node)).get("scenario_id") or "")
        target_scenario = str(_dict(nodes.get(target_node)).get("scenario_id") or "")
        if (
            kind == "depends_on"
            and source_scenario != target_scenario
            and scenario_kinds.get(source_scenario) in {"alternative", "exception"}
            and scenario_kinds.get(target_scenario) == "main"
        ):
            for branch_unit in units:
                if str(branch_unit.get("scenario_id") or "") != source_scenario:
                    continue
                refs = branch_unit.get("depends_on")
                branch_unit_id = str(branch_unit.get("unit_id") or "")
                if (
                    isinstance(refs, list)
                    and target_unit
                    and target_unit != branch_unit_id
                    and target_unit not in refs
                ):
                    refs.append(target_unit)

    ordered_ids = _topological_order(units, scenario_rank=_scenario_rank(trace_graph))
    order_by_id = {unit_id: index for index, unit_id in enumerate(ordered_ids, start=1)}
    for unit in units:
        unit["order"] = order_by_id[str(unit["unit_id"])]
        unit["depends_on"] = sorted(_strings(unit.get("depends_on")))
        unit["design_edge_ids"] = sorted(_strings(unit.get("design_edge_ids")))
        unit["boundary_contracts"] = sorted(
            _dicts(unit.get("boundary_contracts")),
            key=lambda item: str(item.get("design_edge_id") or ""),
        )
    return sorted(units, key=lambda item: int(item.get("order") or 0))


def merge_work_unit_updates(
    *,
    units: list[dict[str, object]],
    updates: object,
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    """Merge only Planner-owned enrichment fields into deterministic unit skeletons."""

    patches = {str(item.get("unit_id") or ""): item for item in _dicts(updates)}
    result: list[dict[str, object]] = []
    for base in units:
        unit = json.loads(json.dumps(base, ensure_ascii=False))
        patch = patches.get(str(unit.get("unit_id") or ""), {})
        for key in ("title", "objective"):
            value = str(patch.get(key) or "").strip()
            if value:
                unit[key] = value
        component_id = str(unit.get("component_id") or "")
        targets = _canonical_targets(
            [*_dicts(unit.get("planned_code_targets")), *_dicts(patch.get("planned_code_targets"))],
            component_id=component_id,
            surfaces=_strings(unit.get("surfaces")),
            planning_policy=planning_policy,
        )
        if (
            planning_policy.project_profile_id == "typescript_fullstack_v1"
            and any(
                planning_policy.component_for_path(str(target.get("path") or "")) == "contracts"
                for target in targets
            )
        ):
            targets = _canonical_targets(
                [*targets, {"path": _TYPESCRIPT_SHARED_CONTRACT_BARREL}],
                component_id=component_id,
                surfaces=_strings(unit.get("surfaces")),
                planning_policy=planning_policy,
            )
        unit["planned_code_targets"] = targets
        unit["test_intents"] = _canonical_test_intents(patch.get("test_intents")) or _dicts(
            unit.get("test_intents")
        )
        evidence_refs = _strings(patch.get("repo_evidence_refs"))
        if evidence_refs:
            unit["repo_evidence_refs"] = sorted(set(evidence_refs))
        if "migration_impact" in unit:
            current = _dict(unit.get("migration_impact"))
            proposed = _dict(patch.get("migration_impact"))
            status = str(proposed.get("status") or current.get("status") or "").strip()
            reason = str(proposed.get("reason") or current.get("reason") or "").strip()
            raw_targets = (
                _strings(proposed.get("targets")) or _strings(current.get("targets"))
                if status == "required"
                else []
            )
            migration_targets = [
                target
                for target in raw_targets
                if planning_policy.normalize_editable_path(target) == target
                and target.startswith("packages/db/prisma/")
            ]
            unit["migration_impact"] = {
                **current,
                "status": status,
                "reason": reason,
                "targets": sorted(set(migration_targets)),
            }
        result.append(unit)
    return _assign_typescript_database_barrel_owner(
        result,
        planning_policy=planning_policy,
    )


def _assign_typescript_database_barrel_owner(
    units: list[dict[str, object]],
    *,
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    """Keep the fixed TypeScript DB entrypoint current in every ordered provider unit."""

    if planning_policy.project_profile_id != "typescript_fullstack_v1":
        return units
    providers = [
        unit
        for unit in units
        if "data" in _strings(unit.get("surfaces"))
        and any(
            str(target.get("path") or "").startswith(_TYPESCRIPT_DATABASE_SOURCE_ROOT)
            and str(target.get("path") or "") != _TYPESCRIPT_DATABASE_BARREL
            and not planning_policy.is_test_path(str(target.get("path") or ""))
            for target in _dicts(unit.get("planned_code_targets"))
        )
    ]
    if not providers:
        return units

    for unit in units:
        unit["planned_code_targets"] = [
            target
            for target in _dicts(unit.get("planned_code_targets"))
            if str(target.get("path") or "") != _TYPESCRIPT_DATABASE_BARREL
        ]
    for provider in providers:
        provider["planned_code_targets"] = _canonical_targets(
            [
                *_dicts(provider.get("planned_code_targets")),
                {"path": _TYPESCRIPT_DATABASE_BARREL, "action": "create_or_update"},
            ],
            component_id=str(provider.get("component_id") or ""),
            surfaces=_strings(provider.get("surfaces")),
            planning_policy=planning_policy,
        )
    return units


def canonicalize_work_unit_dependencies(
    units: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Serialize units that mutate the same approved artifact, then recompute order."""

    result = json.loads(json.dumps(units, ensure_ascii=False))
    by_id = {str(item.get("unit_id") or ""): item for item in result}
    rank = {
        unit_id: (
            int(item.get("order") or 0),
            str(item.get("scenario_id") or ""),
            str(item.get("component_id") or ""),
            unit_id,
        )
        for unit_id, item in by_id.items()
    }
    owners: dict[str, list[str]] = {}
    for unit_id, unit in by_id.items():
        paths = {
            str(item.get("path") or "")
            for item in _dicts(unit.get("planned_code_targets"))
            if str(item.get("path") or "")
        }
        paths.update(_strings(_dict(unit.get("migration_impact")).get("targets")))
        paths.update(
            path
            for item in _dicts(unit.get("test_intents"))
            for path in _strings(item.get("target_paths"))
        )
        for path in paths:
            owners.setdefault(path, []).append(unit_id)

    for unit_ids in owners.values():
        ordered = sorted(set(unit_ids), key=rank.__getitem__)
        for dependency, dependent in zip(ordered, ordered[1:]):
            refs = set(_strings(by_id[dependent].get("depends_on")))
            refs.add(dependency)
            by_id[dependent]["depends_on"] = sorted(refs)

    ordered_ids = _topological_order(result)
    order_by_id = {unit_id: index for index, unit_id in enumerate(ordered_ids, start=1)}
    for unit in result:
        unit["order"] = order_by_id[str(unit.get("unit_id") or "")]
        unit["depends_on"] = sorted(_strings(unit.get("depends_on")))
    return sorted(result, key=lambda item: int(item.get("order") or 0))


def rebind_legacy_mappings_to_work_units(
    *,
    units: list[dict[str, object]],
    design_graph: dict[str, object],
    legacy_mappings: list[dict[str, object]],
    planning_policy: PlanningPathPolicy,
    signature_entries: object = None,
) -> list[dict[str, object]]:
    """Bind legacy Class/Sequence projections to canonical V4 code targets."""

    design_nodes = {
        str(item.get("id") or ""): item for item in _dicts(design_graph.get("nodes"))
    }
    targets_by_name: dict[str, tuple[str, list[dict[str, object]]]] = {}
    for unit in units:
        targets = _dicts(unit.get("planned_code_targets"))
        component_id = str(unit.get("component_id") or "")
        for node_id in _strings(unit.get("design_node_ids")):
            node = _dict(design_nodes.get(node_id))
            name = str(node.get("name") or "").strip()
            if name and targets:
                targets_by_name[name] = (component_id, targets)
    signature_symbols = {
        str(item.get("design_element_id") or ""): str(item.get("qualified_name") or "").strip()
        for item in _dicts(signature_entries)
        if str(item.get("design_element_id") or "")
        and str(item.get("qualified_name") or "").strip()
    }

    rebound: list[dict[str, object]] = []
    for mapping in legacy_mappings:
        item = dict(mapping)
        current = str(item.get("planned_code_target") or "").replace("\\", "/")
        current_path, _, current_symbol = current.partition("#")
        element_type = str(item.get("design_element_type") or "")
        design_element_id = str(item.get("design_element_id") or "")
        design_identity = design_element_id.partition(":")[2]
        design_owner = (
            design_identity.rpartition(".")[0]
            if element_type == "method" and "." in design_identity
            else design_identity
            if element_type == "class"
            else ""
        )
        owner = (
            design_owner
            if design_owner in targets_by_name
            else planning_policy.symbol_owner(current_path)
        )
        component_id, targets = targets_by_name.get(owner, ("", []))
        if not targets:
            rebound.append(item)
            continue
        primary_targets = [
            target
            for target in targets
            if planning_policy.component_for_path(
                str(target.get("path") or ""), mapping_only=True
            )
            == component_id
        ]
        targets = primary_targets or targets
        direct_symbol = element_type == "method" or (
            element_type == "sequence_message"
            and str(item.get("trace_policy") or "") == "direct_symbol"
        )
        signature_symbol = signature_symbols.get(design_element_id, "")
        target = next(
            (
                candidate
                for candidate in targets
                if signature_symbol
                and str(candidate.get("symbol") or "").strip() == signature_symbol
            ),
            None,
        )
        if target is None and direct_symbol and current_symbol:
            target = next(
                (
                    candidate
                    for candidate in targets
                    if str(candidate.get("symbol") or "").strip() == current_symbol
                    or str(candidate.get("symbol") or "").strip().endswith(
                        f".{current_symbol}"
                    )
                ),
                None,
            )
        if target is None:
            target = _best_named_target(name=owner, targets=targets)
        if not target:
            rebound.append(item)
            continue
        path = str(target.get("path") or "")
        symbol = str(target.get("symbol") or "").strip()
        path_stem = path.rsplit("/", 1)[-1].removesuffix(".ts")
        if (
            direct_symbol
            and current_symbol
            and path.endswith(".ts")
            and symbol.lower() == path_stem.lower()
        ):
            symbol = f"{symbol}.{current_symbol}"
        item["planned_code_target"] = path + (f"#{symbol}" if direct_symbol and symbol else "")
        item["component_id"] = planning_policy.component_for_path(path, mapping_only=True)
        rebound.append(item)
    return rebound


def _best_named_target(
    *,
    name: str,
    targets: list[dict[str, object]],
) -> dict[str, object] | None:
    if not targets:
        return None
    name_tokens = set(_identifier_tokens(name))

    def rank(target: dict[str, object]) -> tuple[int, int, int, str, str]:
        path = str(target.get("path") or "")
        symbol = str(target.get("symbol") or "")
        path_stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
        target_tokens = set(_identifier_tokens(path_stem)) | set(_identifier_tokens(symbol))
        generic_penalty = 1 if path_stem.lower() in {"app", "index", "main", "server"} else 0
        fuzzy_overlap = sum(
            1
            for name_token in name_tokens
            if any(
                min(len(name_token), len(target_token)) >= 5
                and (
                    name_token.startswith(target_token)
                    or target_token.startswith(name_token)
                )
                for target_token in target_tokens
            )
        )
        return (
            len(name_tokens & target_tokens),
            fuzzy_overlap,
            -generic_penalty,
            path,
            symbol,
        )

    return max(targets, key=rank)


def _identifier_tokens(value: object) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    return [item.lower() for item in re.findall(r"[A-Za-z0-9]+", text)]


def component_plan_projections(
    *,
    units: list[dict[str, object]],
    design_graph: dict[str, object],
    requirement_graph: dict[str, object],
    legacy_mappings: list[dict[str, object]],
    planning_policy: PlanningPathPolicy | None = None,
) -> dict[str, list[dict[str, object]]]:
    """Render legacy Phase 4 fields from V4 units; never create a second plan authority."""

    design_nodes = {str(item.get("id") or ""): item for item in _dicts(design_graph.get("nodes"))}
    steps: list[dict[str, object]] = []
    files: dict[str, dict[str, object]] = {}
    vdn_mappings: list[dict[str, object]] = []
    all_test_intents: list[dict[str, object]] = []
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        targets = _dicts(unit.get("planned_code_targets"))
        paths = [str(item.get("path") or "") for item in targets if str(item.get("path") or "")]
        test_paths = [
            path
            for item in _dicts(unit.get("test_intents"))
            for path in _strings(item.get("target_paths"))
        ]
        paths.extend(path for path in test_paths if path not in paths)
        migration = _dict(unit.get("migration_impact"))
        paths.extend(path for path in _strings(migration.get("targets")) if path not in paths)
        steps.append(
            {
                "order": int(unit.get("order") or 0),
                "unit_id": unit_id,
                "component_id": str(unit.get("component_id") or ""),
                "title": str(unit.get("title") or ""),
                "description": str(unit.get("objective") or ""),
                "files": paths,
                "design_refs": _strings(unit.get("design_node_ids")),
                "depends_on": _strings(unit.get("depends_on")),
            }
        )
        for path in paths:
            entry = files.setdefault(
                path,
                {
                    "path": path,
                    "action": "create_or_update",
                    "reason": f"Required by component-aware work unit {unit_id}.",
                    "component_id": (
                        planning_policy.component_for_path(path)
                        if planning_policy
                        else str(unit.get("component_id") or "")
                    ),
                    "related_design_elements": [],
                    "work_unit_ids": [],
                },
            )
            entry["related_design_elements"] = sorted(
                set(_strings(entry.get("related_design_elements"))) | set(_strings(unit.get("design_node_ids")))
            )
            entry["work_unit_ids"] = sorted(set(_strings(entry.get("work_unit_ids"))) | {unit_id})
        primary = next((item for item in targets if str(item.get("path") or "")), None)
        if primary:
            for node_id in _strings(unit.get("design_node_ids")):
                node = _dict(design_nodes.get(node_id))
                runtime_target = _dict(_dict(node.get("runtime_binding")).get("target"))
                target = str(runtime_target.get("path") or primary.get("path") or "")
                symbol = str(runtime_target.get("symbol") or primary.get("symbol") or "").strip()
                if symbol:
                    target += f"#{symbol}"
                vdn_mappings.append(
                    {
                        "design_element_id": node_id,
                        "design_element_type": str(node.get("kind") or "design_node"),
                        "design_element_name": str(node.get("name") or node_id),
                        "planned_code_target": target,
                        "component_id": str(unit.get("component_id") or ""),
                        "reason": f"Implemented by component-aware work unit {unit_id}.",
                        "test_intent": "; ".join(
                            str(item.get("intent") or "") for item in _dicts(unit.get("test_intents"))
                        ),
                        "work_unit_id": unit_id,
                    }
                )
        all_test_intents.extend(_dicts(unit.get("test_intents")))

    mappings = _dedupe_mappings([*legacy_mappings, *vdn_mappings])
    requirements = {str(item.get("id") or ""): item for item in _dicts(requirement_graph.get("requirements"))}
    coverage: list[dict[str, object]] = []
    refs = sorted({ref for unit in units for ref in _strings(unit.get("requirement_refs"))})
    for ref in refs:
        related = [unit for unit in units if ref in _strings(unit.get("requirement_refs"))]
        coverage.append(
            {
                "requirement_id": ref,
                "content": str(_dict(requirements.get(ref)).get("content") or ""),
                "design_targets": sorted({node for unit in related for node in _strings(unit.get("design_node_ids"))}),
                "design_element_ids": sorted({node for unit in related for node in _strings(unit.get("design_node_ids"))}),
                "work_unit_ids": [str(unit.get("unit_id") or "") for unit in related],
                "planned_code_targets": sorted(
                    {
                        str(target.get("path") or "")
                        + (f"#{target.get('symbol')}" if str(target.get("symbol") or "") else "")
                        for unit in related
                        for target in _dicts(unit.get("planned_code_targets"))
                        if str(target.get("path") or "")
                    }
                ),
                "test_intents": sorted(
                    {
                        str(item.get("intent") or "")
                        for unit in related
                        for item in _dicts(unit.get("test_intents"))
                        if str(item.get("intent") or "")
                    }
                ),
            }
        )
    return {
        "implementation_steps": steps,
        "proposed_files": list(files.values()),
        "design_mappings": mappings,
        "requirement_coverage": coverage,
        "test_intents": _dedupe_test_intents(all_test_intents),
    }


def component_plan_issues(
    *,
    structured_plan: dict[str, object],
    expected_units: list[dict[str, object]],
    expected_inputs: dict[str, object],
    planning_policy: PlanningPathPolicy,
    verification_stage_ids: list[str],
    design_graph: dict[str, object] | None = None,
    trace_graph: dict[str, object] | None = None,
    repo_landscape: dict[str, object] | None = None,
) -> list[str]:
    """Return deterministic V4 contract violations."""

    issues: list[str] = []
    if structured_plan.get("version") != COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION:
        issues.append(f"Plan version must be `{COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION}`.")
    if _dict(structured_plan.get("inputs")) != expected_inputs:
        issues.append("Component-aware plan input binding is stale or incomplete.")
    units = _dicts(structured_plan.get("work_units"))
    actual_by_id = {str(item.get("unit_id") or ""): item for item in units}
    expected_with_targets: list[dict[str, object]] = []
    for base in expected_units:
        item = json.loads(json.dumps(base, ensure_ascii=False))
        actual = _dict(actual_by_id.get(str(item.get("unit_id") or "")))
        item["planned_code_targets"] = _dicts(actual.get("planned_code_targets"))
        item["test_intents"] = _dicts(actual.get("test_intents"))
        if "migration_impact" in item:
            item["migration_impact"] = _dict(actual.get("migration_impact"))
        expected_with_targets.append(item)
    expected_by_id = {
        str(item.get("unit_id") or ""): item
        for item in canonicalize_work_unit_dependencies(expected_with_targets)
    }
    if not units:
        issues.append("Component-aware plan has no work units.")
    if set(actual_by_id) != set(expected_by_id):
        issues.append("Component-aware plan work-unit identity does not match the approved Design graph.")
    allowed_stages = set(verification_stage_ids)
    strict_runtime_targets = str(_dict(design_graph).get("schema_version") or "") == GRAPH_VERSION_V2
    for unit_id, unit in actual_by_id.items():
        expected = expected_by_id.get(unit_id)
        if expected:
            for field in _IMMUTABLE_UNIT_FIELDS:
                if unit.get(field) != expected.get(field):
                    issues.append(f"Work unit {unit_id} changed immutable field `{field}`.")
        targets = _dicts(unit.get("planned_code_targets"))
        if not targets:
            issues.append(f"Work unit {unit_id} has no planned code target.")
        component_id = str(unit.get("component_id") or "")
        target_paths = {
            str(target.get("path") or "")
            for target in targets
            if str(target.get("path") or "")
        }
        for target in targets:
            path = str(target.get("path") or "")
            normalized = planning_policy.normalize_editable_path(path)
            target_component = planning_policy.component_for_path(path)
            if normalized != path or not _target_allowed_for_unit(
                path=path,
                target_component=target_component,
                component_id=component_id,
                surfaces=_strings(unit.get("surfaces")),
            ):
                issues.append(f"Work unit {unit_id} has invalid target path `{path}`.")
        if strict_runtime_targets and expected:
            allowed_targets = {
                (
                    str(_dict(binding.get("target")).get("path") or ""),
                    str(_dict(binding.get("target")).get("symbol") or ""),
                )
                for binding in _dicts(expected.get("runtime_bindings"))
            } | {
                (
                    str(_dict(binding.get("attachment")).get("owner_path") or ""),
                    str(_dict(binding.get("attachment")).get("owner_symbol") or ""),
                )
                for binding in _dicts(expected.get("runtime_bindings"))
            }
            allowed_targets.discard(("", ""))
            has_boundary_contract = bool(_dicts(expected.get("boundary_contracts")))
            for target in targets:
                target_key = (
                    str(target.get("path") or ""),
                    str(target.get("symbol") or ""),
                )
                is_shared_contract = (
                    has_boundary_contract
                    and planning_policy.component_for_path(target_key[0]) == "contracts"
                )
                is_data_schema_artifact = "data" in _strings(unit.get("surfaces")) and (
                    target_key[0] == "packages/db/prisma/schema.prisma"
                    or (
                        target_key[0].startswith("packages/db/prisma/migrations/")
                        and target_key[0].endswith(".sql")
                    )
                )
                is_database_barrel = (
                    "data" in _strings(unit.get("surfaces"))
                    and target_key[0] == _TYPESCRIPT_DATABASE_BARREL
                    and not target_key[1]
                )
                if (
                    target_key not in allowed_targets
                    and not is_shared_contract
                    and not is_data_schema_artifact
                    and not is_database_barrel
                ):
                    issues.append(
                        f"Work unit {unit_id} has unapproved support target "
                        f"`{target_key[0]}#{target_key[1]}` outside runtime wiring."
                    )
        for binding in _dicts(unit.get("runtime_bindings")):
            target_path = str(_dict(binding.get("target")).get("path") or "")
            owner_path = str(_dict(binding.get("attachment")).get("owner_path") or "")
            for required_path in {target_path, owner_path} - {""}:
                if required_path not in target_paths:
                    issues.append(
                        f"Work unit {unit_id} omits runtime wiring target `{required_path}`."
                    )
        known_edge_ids = set(_strings(unit.get("design_edge_ids")))
        for boundary in _dicts(unit.get("boundary_contracts")):
            if str(boundary.get("design_edge_id") or "") not in known_edge_ids:
                issues.append(f"Work unit {unit_id} has an unbound runtime boundary contract.")
        tests = _dicts(unit.get("test_intents"))
        if not tests:
            issues.append(f"Work unit {unit_id} has no test intent.")
        covered_acceptance = {
            ref for item in tests for ref in _strings(item.get("acceptance_refs"))
        }
        missing_acceptance = set(_strings(unit.get("acceptance_refs"))) - covered_acceptance
        if missing_acceptance:
            issues.append(f"Work unit {unit_id} omits acceptance test coverage: {sorted(missing_acceptance)}")
        for item in tests:
            stage_id = str(item.get("stage_id") or "")
            if stage_id not in allowed_stages:
                issues.append(f"Work unit {unit_id} references unsupported verification stage `{stage_id}`.")
            test_paths = _strings(item.get("target_paths"))
            if not test_paths:
                issues.append(f"Work unit {unit_id} test intent has no exact target path.")
            for path in test_paths:
                valid_path = planning_policy.normalize_editable_path(path) == path
                valid_stage_target = (
                    planning_policy.is_test_path(path)
                    if stage_id == "unit"
                    else path in target_paths and not planning_policy.is_test_path(path)
                    if stage_id == "typecheck"
                    else False
                )
                if not valid_path or not valid_stage_target:
                    issues.append(f"Work unit {unit_id} has invalid test target path `{path}`.")
                if stage_id == "unit":
                    source_path = planning_policy.source_path_for_test(path)
                    test_component = planning_policy.component_for_path(path)
                    additional_source_suffixes = (
                        tuple(
                            source_suffix
                            for source_suffix, _ in planning_policy.rule(
                                test_component
                            ).additional_suffix_pairs
                        )
                        if test_component and test_component != "e2e"
                        else ()
                    )
                    if (
                        source_path
                        and additional_source_suffixes
                        and source_path.endswith(additional_source_suffixes)
                        and source_path not in target_paths
                    ):
                        issues.append(
                            f"Work unit {unit_id} plans unit test `{path}` without matching "
                            f"source target `{source_path}`."
                        )
        if "data" in _strings(unit.get("surfaces")):
            migration = _dict(unit.get("migration_impact"))
            status = str(migration.get("status") or "")
            if status not in {"required", "not_required"}:
                issues.append(f"Work unit {unit_id} has no valid migration decision.")
            if not str(migration.get("reason") or "").strip():
                issues.append(f"Work unit {unit_id} has no migration decision reason.")
            if status == "required" and not _strings(migration.get("targets")):
                issues.append(f"Work unit {unit_id} requires migration but has no migration target.")
            if status == "required" and not any(
                path.startswith("packages/db/prisma/migrations/") and path.endswith(".sql")
                for path in _strings(migration.get("targets"))
            ):
                issues.append(
                    f"Work unit {unit_id} requires migration but has no concrete Prisma migration SQL target."
                )
    unit_ids = set(actual_by_id)
    for unit_id, unit in actual_by_id.items():
        for dependency in _strings(unit.get("depends_on")):
            if dependency not in unit_ids or dependency == unit_id:
                issues.append(f"Work unit {unit_id} has invalid dependency `{dependency}`.")
            elif int(_dict(actual_by_id.get(dependency)).get("order") or 0) >= int(unit.get("order") or 0):
                issues.append(f"Work unit {unit_id} is ordered before dependency `{dependency}`.")

    has_web_surface = any(
        str(unit.get("component_id") or "") == "web"
        and "ui" in _strings(unit.get("surfaces"))
        for unit in units
    )
    has_api_surface = any(
        str(unit.get("component_id") or "") == "api"
        and "api" in _strings(unit.get("surfaces"))
        for unit in units
    )
    profile_has_contracts = any(
        rule.component_id == "contracts" for rule in planning_policy.component_rules
    )
    has_contract_target = any(
        planning_policy.component_for_path(str(target.get("path") or "")) == "contracts"
        for unit in units
        for target in _dicts(unit.get("planned_code_targets"))
    )
    if has_web_surface and has_api_surface and profile_has_contracts and not has_contract_target:
        issues.append(
            "Cross-boundary Web/API work requires an approved shared contract target from the repository profile."
        )
    contract_paths = {
        str(target.get("path") or "")
        for unit in units
        for target in _dicts(unit.get("planned_code_targets"))
        if planning_policy.component_for_path(str(target.get("path") or "")) == "contracts"
    }
    if (
        planning_policy.project_profile_id == "typescript_fullstack_v1"
        and contract_paths - {_TYPESCRIPT_SHARED_CONTRACT_BARREL}
        and _TYPESCRIPT_SHARED_CONTRACT_BARREL not in contract_paths
    ):
        issues.append(
            "TypeScript full-stack shared contract modules must update "
            f"`{_TYPESCRIPT_SHARED_CONTRACT_BARREL}`."
        )
    if planning_policy.project_profile_id == "typescript_fullstack_v1":
        database_providers = [
            unit
            for unit in units
            if "data" in _strings(unit.get("surfaces"))
            and any(
                str(target.get("path") or "").startswith(_TYPESCRIPT_DATABASE_SOURCE_ROOT)
                and str(target.get("path") or "") != _TYPESCRIPT_DATABASE_BARREL
                and not planning_policy.is_test_path(str(target.get("path") or ""))
                for target in _dicts(unit.get("planned_code_targets"))
            )
        ]
        database_barrel_owners = [
            unit
            for unit in units
            if any(
                str(target.get("path") or "") == _TYPESCRIPT_DATABASE_BARREL
                for target in _dicts(unit.get("planned_code_targets"))
            )
        ]
        provider_ids = {str(unit.get("unit_id") or "") for unit in database_providers}
        owner_ids = {str(unit.get("unit_id") or "") for unit in database_barrel_owners}
        if provider_ids - owner_ids:
            issues.append(
                "Every TypeScript database provider unit must update "
                f"`{_TYPESCRIPT_DATABASE_BARREL}`."
            )

    if strict_runtime_targets:
        issues.extend(
            _structural_plan_issues(
                units=units,
                design_graph=_dict(design_graph),
                trace_graph=_dict(trace_graph),
            )
        )

    step_ids = {str(item.get("unit_id") or "") for item in _dicts(structured_plan.get("implementation_steps"))}
    if step_ids != unit_ids:
        issues.append("Compatibility implementation steps do not match work units.")
    expected_paths = {
        str(target.get("path") or "")
        for unit in units
        for target in _dicts(unit.get("planned_code_targets"))
        if str(target.get("path") or "")
    } | {
        path
        for unit in units
        for path in _strings(_dict(unit.get("migration_impact")).get("targets"))
    } | {
        path
        for unit in units
        for item in _dicts(unit.get("test_intents"))
        for path in _strings(item.get("target_paths"))
    }
    proposed_paths = {str(item.get("path") or "") for item in _dicts(structured_plan.get("proposed_files"))}
    if expected_paths != proposed_paths:
        issues.append("Compatibility proposed files do not match work-unit targets.")
    mapped_vdns = {
        str(item.get("design_element_id") or "")
        for item in _dicts(structured_plan.get("design_mappings"))
        if str(item.get("design_element_id") or "").startswith("VDN-")
    }
    expected_vdns = {node for unit in units for node in _strings(unit.get("design_node_ids"))}
    if mapped_vdns != expected_vdns:
        issues.append("Compatibility design mappings do not cover the work-unit Design nodes.")
    planned_paths = {
        str(target.get("path") or "")
        for unit in units
        for target in _dicts(unit.get("planned_code_targets"))
        if str(target.get("path") or "")
    }
    for mapping in _dicts(structured_plan.get("design_mappings")):
        mapped_path = str(mapping.get("planned_code_target") or "").split("#", 1)[0]
        if mapped_path and mapped_path not in planned_paths:
            issues.append(
                "Compatibility design mapping points outside work-unit code targets: "
                f"{mapping.get('design_element_id')} -> {mapped_path}"
            )
    return issues


def _structural_plan_issues(
    *,
    units: list[dict[str, object]],
    design_graph: dict[str, object],
    trace_graph: dict[str, object],
) -> list[str]:
    issues: list[str] = []
    units_by_id = {str(item.get("unit_id") or ""): item for item in units}
    node_unit_counts: dict[str, int] = {}
    unit_by_node = {
        node_id: str(unit.get("unit_id") or "")
        for unit in units
        for node_id in _strings(unit.get("design_node_ids"))
    }
    for unit in units:
        for node_id in _strings(unit.get("design_node_ids")):
            node_unit_counts[node_id] = node_unit_counts.get(node_id, 0) + 1
    design_nodes = {
        str(item.get("id") or ""): item for item in _dicts(design_graph.get("nodes"))
    }
    actionable_nodes = {
        node_id
        for node_id, node in design_nodes.items()
        if str(node.get("disposition") or "") in _ACTIONABLE_DISPOSITIONS
    }
    if set(unit_by_node) != actionable_nodes or any(
        node_unit_counts.get(node_id) != 1 for node_id in actionable_nodes
    ):
        issues.append("Work units do not cover every actionable Vertical Design node exactly once.")

    scenario_kinds = {
        str(item.get("id") or ""): str(item.get("scenario_kind") or "")
        for item in _dicts(trace_graph.get("nodes"))
        if str(item.get("kind") or "") == "scenario"
    }
    dependencies = {
        unit_id: set(_strings(unit.get("depends_on")))
        for unit_id, unit in units_by_id.items()
    }

    def dependency_closure(unit_id: str) -> set[str]:
        found: set[str] = set()
        pending = list(dependencies.get(unit_id, set()))
        while pending:
            dependency = pending.pop()
            if dependency in found:
                continue
            found.add(dependency)
            pending.extend(dependencies.get(dependency, set()) - found)
        return found

    parent_scenarios: dict[str, set[str]] = {}
    scenario_edges: dict[str, list[dict[str, object]]] = {}
    for edge in _dicts(design_graph.get("edges")):
        source_node = str(edge.get("from") or "")
        target_node = str(edge.get("to") or "")
        source = _dict(design_nodes.get(source_node))
        target = _dict(design_nodes.get(target_node))
        source_scenario = str(source.get("scenario_id") or "")
        target_scenario = str(target.get("scenario_id") or "")
        if source_scenario and source_scenario == target_scenario:
            scenario_edges.setdefault(source_scenario, []).append(edge)
        source_unit = unit_by_node.get(source_node, "")
        target_unit = unit_by_node.get(target_node, "")
        if source_unit and target_unit and source_unit != target_unit:
            kind = str(edge.get("kind") or "")
            dependent = source_unit if kind in _FORWARD_DEPENDENCY_KINDS else target_unit if kind in _REVERSE_DEPENDENCY_KINDS else ""
            dependency = target_unit if kind in _FORWARD_DEPENDENCY_KINDS else source_unit if kind in _REVERSE_DEPENDENCY_KINDS else ""
            if dependent and dependency and dependency not in dependency_closure(dependent):
                issues.append(
                    f"Work unit {dependent} does not preserve Design dependency on {dependency}."
                )
        if (
            str(edge.get("kind") or "") == "depends_on"
            and scenario_kinds.get(source_scenario) in {"alternative", "exception"}
            and scenario_kinds.get(target_scenario) == "main"
        ):
            parent_scenarios.setdefault(source_scenario, set()).add(target_scenario)
        if not edge.get("boundary_contract"):
            continue
        edge_id = str(edge.get("id") or "")
        for endpoint_unit in {source_unit, target_unit} - {""}:
            covered_edges = {
                str(item.get("design_edge_id") or "")
                for item in _dicts(_dict(units_by_id.get(endpoint_unit)).get("boundary_contracts"))
            }
            if edge_id not in covered_edges:
                issues.append(
                    f"Runtime boundary {edge_id} is not bound to work unit {endpoint_unit}."
                )

    for unit_id, unit in units_by_id.items():
        scenario_id = str(unit.get("scenario_id") or "")
        if scenario_kinds.get(scenario_id) not in {"alternative", "exception"}:
            continue
        parents = parent_scenarios.get(scenario_id, set())
        parent_units = {
            candidate_id
            for candidate_id, candidate in units_by_id.items()
            if str(candidate.get("scenario_id") or "") in parents
        }
        if not parents or not dependency_closure(unit_id).intersection(parent_units):
            issues.append(
                f"Branch work unit {unit_id} has no dependency path to its parent main scenario."
            )

    actionable_surfaces = {
        (str(node.get("scenario_id") or ""), str(node.get("surface") or ""))
        for node in design_nodes.values()
        if str(node.get("disposition") or "") in _ACTIONABLE_DISPOSITIONS
    }
    planned_surfaces = {
        (str(unit.get("scenario_id") or ""), surface)
        for unit in units
        for surface in _strings(unit.get("surfaces"))
    }
    missing_surfaces = sorted(actionable_surfaces - planned_surfaces)
    if missing_surfaces:
        issues.append(f"Work units omit actionable scenario surfaces: {missing_surfaces}")

    required_surfaces: dict[str, set[str]] = {}
    for node in _dicts(trace_graph.get("nodes")):
        if str(node.get("kind") or "") != "surface_requirement" or str(node.get("status") or "") != "required":
            continue
        required_surfaces.setdefault(str(node.get("scenario_id") or ""), set()).add(
            str(node.get("surface") or "")
        )
    for scenario_id, surfaces in required_surfaces.items():
        scenario_nodes = [
            node for node in design_nodes.values() if str(node.get("scenario_id") or "") == scenario_id
        ]
        if not scenario_nodes:
            continue
        starts = [
            node
            for node in scenario_nodes
            if str(_dict(node.get("runtime_binding")).get("runtime_kind") or "")
            in (
                {"entrypoint", "ui_component"}
                if "ui" in surfaces
                else {"entrypoint", "api_route"}
                if "api" in surfaces
                else {"entrypoint", "service", "persistence_adapter", "data_model"}
            )
        ]
        adjacency: dict[str, set[str]] = {}
        for edge in scenario_edges.get(scenario_id, []):
            adjacency.setdefault(str(edge.get("from") or ""), set()).add(str(edge.get("to") or ""))
        reachable = {str(node.get("id") or "") for node in starts}
        pending = list(reachable)
        while pending:
            source = pending.pop()
            for target in adjacency.get(source, set()):
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        uncovered = sorted(
            surface
            for surface in surfaces
            if not any(
                str(node.get("surface") or "") == surface
                and str(node.get("id") or "") in reachable
                for node in scenario_nodes
            )
        )
        has_terminal = any(
            bool(edge.get("terminal")) and str(edge.get("to") or "") in reachable
            for edge in scenario_edges.get(scenario_id, [])
        )
        if not starts or uncovered or (len(surfaces) > 1 and not has_terminal):
            issues.append(
                f"Scenario {scenario_id} has no connected runtime work-unit path; "
                f"uncovered surfaces: {uncovered or ['observable outcome']}."
            )
    return issues


def _unit_id(*, scenario_id: str, component_id: str, node_ids: list[str]) -> str:
    payload = json.dumps([scenario_id, component_id, node_ids], ensure_ascii=False, separators=(",", ":"))
    return "WU-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12].upper()


def _scenario_rank(trace_graph: dict[str, object]) -> dict[str, tuple[int, int]]:
    scenarios = [
        item
        for item in _dicts(trace_graph.get("nodes"))
        if str(item.get("kind") or "") == "scenario"
    ]
    primary_main = next(
        (
            str(item.get("id") or "")
            for item in scenarios
            if str(item.get("scenario_kind") or "") == "main"
        ),
        "",
    )
    kind_rank = {"main": 1, "alternative": 2, "exception": 3}
    return {
        str(item.get("id") or ""): (
            0
            if str(item.get("id") or "") == primary_main
            else kind_rank.get(str(item.get("scenario_kind") or ""), 4),
            index,
        )
        for index, item in enumerate(scenarios)
        if str(item.get("id") or "")
    }


def _topological_order(
    units: list[dict[str, object]],
    *,
    scenario_rank: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    by_id = {str(item.get("unit_id") or ""): item for item in units}
    remaining = {unit_id: set(_strings(item.get("depends_on"))) for unit_id, item in by_id.items()}
    ranks = scenario_rank or {}
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            (unit_id for unit_id, dependencies in remaining.items() if not dependencies),
            key=lambda unit_id: (
                *ranks.get(str(by_id[unit_id].get("scenario_id") or ""), (4, 10**9)),
                int(by_id[unit_id].get("order") or 10**9),
                str(by_id[unit_id].get("scenario_id") or ""),
                str(by_id[unit_id].get("component_id") or ""),
                unit_id,
            ),
        )
        if not ready:
            raise HarnessError(
                "implementation_plan_dependency_cycle",
                f"Component-aware work units contain a dependency cycle: {sorted(remaining)}",
            )
        for unit_id in ready:
            ordered.append(unit_id)
            remaining.pop(unit_id)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return ordered


def _canonical_targets(
    targets: list[dict[str, object]],
    *,
    component_id: str,
    surfaces: list[str],
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    result: dict[tuple[str, str], dict[str, object]] = {}
    for target in targets:
        path = planning_policy.normalize_editable_path(target.get("path"))
        if not path:
            continue
        owner = planning_policy.component_for_path(path)
        if not _target_allowed_for_unit(
            path=path,
            target_component=owner,
            component_id=component_id,
            surfaces=surfaces,
        ):
            continue
        symbol = str(target.get("symbol") or "").strip()
        result[(path, symbol)] = {
            "path": path,
            "symbol": symbol,
            "action": "create_or_update",
        }
    return [result[key] for key in sorted(result)]


def _target_allowed_for_unit(
    *,
    path: str,
    target_component: str,
    component_id: str,
    surfaces: list[str],
) -> bool:
    if target_component == component_id or path.startswith("packages/db/prisma/"):
        return True
    return target_component == "contracts" and component_id == "api" and "api" in surfaces


def _canonical_test_intents(value: object) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    for item in _dicts(value):
        stage_id = str(item.get("stage_id") or "").strip()
        intent = str(item.get("intent") or "").strip()
        acceptance_refs = tuple(sorted(set(_strings(item.get("acceptance_refs")))))
        target_paths = tuple(sorted(set(_safe_relative_paths(item.get("target_paths")))))
        key = (stage_id, intent, acceptance_refs, target_paths)
        if stage_id and intent and key not in seen:
            seen.add(key)
            result.append(
                {
                    "stage_id": stage_id,
                    "intent": intent,
                    "acceptance_refs": list(acceptance_refs),
                    "target_paths": list(target_paths),
                }
            )
    return result


def _dedupe_test_intents(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return _canonical_test_intents(items)


def _dedupe_mappings(items: list[dict[str, object]]) -> list[dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in items:
        design_id = str(item.get("design_element_id") or "")
        if design_id:
            result[design_id] = item
    return list(result.values())


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _safe_relative_paths(value: object) -> list[str]:
    result: list[str] = []
    for raw in _strings(value):
        path = raw.replace("\\", "/")
        if (
            path.startswith("/")
            or path == ".."
            or path.startswith("../")
            or "/../" in path
            or ":" in path.split("/", 1)[0]
        ):
            continue
        result.append(path)
    return result
