"""Canonical vertical Design graph and deterministic delta application for L3."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...core.errors import HarnessError
from .uiux import (
    RUNTIME_SURFACE_BINDING_VERSION,
    UI_EXPERIENCE_CONTRACT_V2,
)


# The only Design contracts the Harness executes (2026-11-18); runs pinned to older ones are retired.
GRAPH_VERSION = "vertical_design_graph_v2"
DELTA_VERSION = "designer_delta_v2"
DESIGNER_CONTRACT_STATE_KEY = "designer_contract_version"
DESIGN_BUNDLE_VERSION = "system_design_bundle_v2"
DESIGN_BUNDLE_STATE_KEY = "design_bundle_contract_version"
SURFACE_CONTRACT_VERSION = "design_surface_contract_v1"
RUNTIME_BINDING_VERSION = "runtime_binding_v1"
BOUNDARY_CONTRACT_VERSION = "boundary_contract_v1"
RUNTIME_INPUT_SOURCES = frozenset({"existing_runtime", "external_use_case", "new_boundary", "demo_fixture"})
RUNNABLE_STATUSES = frozenset({"bound", "externally_blocked", "fixture_required"})
# Requirement IDs that can state a dependency on another use case (precondition or flow step).
EXTERNAL_DEPENDENCY_REF_PREFIXES = ("PRE-", "MF-", "AF-", "EF-")
UI_RUNTIME_STATES = ("loading", "ready", "empty", "error", "success")
UI_VISUAL_GROUPS = ("hierarchy", "layout", "typography", "color_and_tokens", "focus_appearance", "responsive_rules")
# Required fields of ui_experience_contract_v2 (schema_version aside).
UI_EXPERIENCE_FIELDS = (
    "screen", "component_structure", "feedback", "accessibility", "responsive", "focus_flow", "focus_policies",
    "guidance_refs", "repo_evidence_refs", "runtime_states", "visual_contract",
)
UI_STYLE_TARGET_POLICIES = frozenset({"reuse_existing", "new_stylesheet"})
UI_SURFACE_ROLES = frozenset({"screen", "composition_wiring"})
# Layout items are {region, layer, content}; the run pins this contract under UI_LAYOUT_CONTRACT_KEY.
UI_LAYOUT_CONTRACT_KEY = "designer_ui_layout_contract"
UI_LAYERED_LAYOUT_V1 = "layered_layout_v1"
UI_LAYOUT_LAYERS = ("page", "overlay")


def is_composition_wiring(node: dict[str, object]) -> bool:
    """A UI node that only forwards runtime inputs to a child; it carries no screen UX of its own."""

    surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
    return str(surface.get("role") or "") == "composition_wiring"


def canonicalize_composition_wiring(graph: dict[str, object]) -> dict[str, object]:
    """Refresh Harness-owned forwarding metadata from declared screen inputs."""

    run_id = str(graph.get("run_id") or "")
    validate_vertical_design_graph(graph, expected_run_id=run_id)
    candidate = json.loads(json.dumps(graph, ensure_ascii=False))
    candidate.pop("artifact_digest", None)
    if not _materialize_composition_forwarding(candidate):
        return graph
    candidate["artifact_digest"] = graph_digest(candidate)
    return validate_vertical_design_graph(candidate, expected_run_id=run_id)


def _materialize_composition_forwarding(graph: dict[str, object]) -> bool:
    """Derive every forwarded input by screen and child; never infer business inputs."""

    run_id = str(graph.get("run_id") or "")
    nodes = _dict_items(graph.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id)
    if not any(is_composition_wiring(node) for node in nodes):
        return False  # nothing forwards inputs, so there is nothing to derive
    context = _dict_value(_dict_value(graph.get("snapshot_binding")).get("ui_repo_context"))
    chain = {
        str(item.get("ref") or ""): item
        for item in _dict_items(
            context.get("composition_chain"),
            code="vertical_design_graph_invalid",
            run_id=run_id,
        )
    }
    expected: dict[str, dict[tuple[str, str], set[str]]] = {}
    requirement_refs: dict[str, set[str]] = {}
    for node in nodes:
        if str(node.get("surface") or "") != "ui" or is_composition_wiring(node):
            continue
        surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
        screen_id = str(surface.get("screen_id") or "")
        path = _strings(surface.get("composition_path"))
        if not screen_id or len(path) < 2:
            continue
        for item in _dict_items(
            surface.get("required_inputs"),
            code="vertical_design_graph_invalid",
            run_id=run_id,
        ):
            name = str(item.get("name") or "")
            provided_by = str(item.get("provided_by") or "")
            if not name or provided_by not in path[:-1]:
                continue
            for index in range(path.index(provided_by), len(path) - 1):
                hop, child = path[index], path[index + 1]
                passed = _strings(_dict_value(_dict_value(chain.get(hop)).get("passes_props")).get(child))
                if hop not in chain or name in passed:
                    continue
                expected.setdefault(hop, {}).setdefault((screen_id, child), set()).add(name)
                requirement_refs.setdefault(hop, set()).update(_strings(item.get("requirement_refs")))

    changed = False
    for node in nodes:
        if not is_composition_wiring(node):
            continue
        target = _dict_value(_dict_value(node.get("runtime_binding")).get("target"))
        hop = f"{target.get('path') or ''}#{target.get('symbol') or ''}"
        groups = expected.get(hop, {})
        forwarded = [
            {"screen_id": screen_id, "to": child, "names": sorted(names)}
            for (screen_id, child), names in sorted(groups.items())
        ]
        contract = _dict_value(node.get("surface_contract"))
        surface = _dict_value(contract.get("runtime_surface"))
        if surface.get("forwarded_inputs") != forwarded:
            surface["forwarded_inputs"] = forwarded
            changed = True
        names = sorted({name for item in forwarded for name in _strings(item.get("names"))})
        if names:
            phrase = ", ".join(names)
            symbol = str(target.get("symbol") or "Composition hop")
            desired_contract = f"{symbol} forwards {phrase} to the next component on composition_path."
            desired_transition = {
                "from": "mounted",
                "action": f"receive {phrase}",
                "to": "forwarding",
                "outcome": f"next component receives {phrase}",
            }
            if node.get("contract") != desired_contract:
                node["contract"] = desired_contract
                changed = True
            if contract.get("states") != ["mounted", "forwarding"]:
                contract["states"] = ["mounted", "forwarding"]
                changed = True
            if contract.get("transitions") != [desired_transition]:
                contract["transitions"] = [desired_transition]
                changed = True
            if contract.get("outcomes") != [desired_transition["outcome"]]:
                contract["outcomes"] = [desired_transition["outcome"]]
                changed = True
        merged_refs = sorted({*_strings(node.get("requirement_refs")), *requirement_refs.get(hop, set())})
        if merged_refs and _strings(node.get("requirement_refs")) != merged_refs:
            node["requirement_refs"] = merged_refs
            changed = True
        contract["runtime_surface"] = surface
        node["surface_contract"] = contract
    if changed:
        graph["nodes"] = nodes
    return changed


def _composition_wiring_forwards(
    node: dict[str, object],
    *,
    screen_id: str,
    child: str,
    name: str,
) -> bool:
    if not is_composition_wiring(node):
        return False
    surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
    forwarded = surface.get("forwarded_inputs", [])
    if not isinstance(forwarded, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("screen_id") or "") == screen_id
        and str(item.get("to") or "") == child
        and name in _strings(item.get("names"))
        for item in forwarded
    )
RUNTIME_KINDS = frozenset(
    {
        "entrypoint",
        "ui_component",
        "http_client",
        "api_route",
        "service",
        "persistence_adapter",
        "data_model",
    }
)
ATTACHMENT_RELATIONS = frozenset(
    {"rendered_by", "registered_by", "called_by", "injected_by", "implemented_by", "owned_by"}
)
RUNTIME_ATTACHMENT_RELATIONS = {
    "entrypoint": frozenset({"implemented_by", "owned_by"}),
    "ui_component": frozenset({"rendered_by"}),
    "http_client": frozenset({"called_by", "injected_by"}),
    "api_route": frozenset({"registered_by"}),
    "service": frozenset({"called_by", "injected_by", "implemented_by"}),
    "persistence_adapter": frozenset({"injected_by", "implemented_by"}),
    "data_model": frozenset({"owned_by"}),
}
NODE_KINDS = frozenset(
    {
        "ui_interaction",
        "ui_state",
        "api_contract",
        "business_rule",
        "service",
        "data_entity",
        "data_operation",
        "interaction_step",
        "planned_work",
    }
)
SURFACES = frozenset({"ui", "api", "business_rule", "data", "interaction"})
DISPOSITIONS = frozenset({"reuse", "modify", "add", "no_change"})
EDGE_KINDS = frozenset({"contains", "calls", "reads", "writes", "returns", "realized_by", "depends_on"})
OPERATIONS = frozenset(
    {
        "add_node",
        "update_node",
        "remove_node",
        "add_edge",
        "remove_edge",
        "order_edges",
        "set_partition_coverage",
        "place_surface",
        "not_exercised",
    }
)
# Edge kinds that state structure; every other kind is an ordered behavior message of its scenario.
STRUCTURAL_EDGE_KINDS = frozenset({"contains", "depends_on", "realized_by"})
# A business rule may run in the UI or in the server; ui/api/data belong to fixed components.
PLACEABLE_SURFACES = frozenset({"business_rule"})
# A required backend surface a scenario never reaches (e.g. data behind a 401 refused before any access).
# shortcut: ui is excluded (Phase 1 marks an unused ui not_applicable); allow it when a real UI case needs it.
NOT_EXERCISABLE_SURFACES = frozenset({"api", "business_rule", "data"})
# Fields a delta may set on a node (the partition ids are overwritten from the delta partition).
DELTA_NODE_FIELDS = frozenset(
    {
        "temp_ref", "semantic_key", "name", "kind", "surface", "disposition", "requirement_refs", "evidence_refs",
        "evidence_reason", "contract", "surface_contract", "runtime_binding",
        "use_case_id", "scenario_id", "component_id",
    }
)
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_TYPESCRIPT_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def graph_digest(graph: dict[str, object]) -> str:
    return canonical_hash({key: value for key, value in graph.items() if key != "artifact_digest"})


def layout_regions(visual: dict[str, object]) -> list[tuple[str, str, str]]:
    """(region, layer, content) for each layout item."""

    return [
        (str(item.get("region") or "").strip(), str(item.get("layer") or ""), str(item.get("content") or "").strip())
        for item in (visual.get("layout") if isinstance(visual.get("layout"), list) else [])
        if isinstance(item, dict)
    ]


def new_vertical_design_graph(
    *,
    run_id: str,
    snapshot_binding: dict[str, object],
    contract_version: str = GRAPH_VERSION,
) -> dict[str, object]:
    if contract_version != GRAPH_VERSION:
        raise HarnessError("vertical_design_graph_invalid", "Unsupported vertical Design graph contract.", run_id=run_id)
    graph: dict[str, object] = {
        "schema_version": contract_version,
        "revision": 0,
        "run_id": run_id,
        "snapshot_binding": dict(snapshot_binding),
        "partitions": [],
        "nodes": [],
        "edges": [],
        "unresolved_items": [],
    }
    graph["artifact_digest"] = graph_digest(graph)
    return validate_vertical_design_graph(graph, expected_run_id=run_id)


def validate_vertical_design_graph(
    value: object,
    *,
    expected_run_id: str | None = None,
    expected_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != GRAPH_VERSION:
        raise HarnessError("vertical_design_graph_invalid", "Unsupported vertical Design graph contract.", run_id=expected_run_id)
    run_id = str(value.get("run_id") or "")
    if not run_id or (expected_run_id and run_id != expected_run_id):
        raise HarnessError("vertical_design_graph_invalid", "Design graph run identity is invalid.", run_id=expected_run_id)
    if not isinstance(value.get("revision"), int) or int(value.get("revision") or 0) < 0:
        raise HarnessError("vertical_design_graph_invalid", "Design graph revision is invalid.", run_id=run_id)
    binding = value.get("snapshot_binding")
    if not isinstance(binding, dict) or not binding:
        raise HarnessError("vertical_design_graph_invalid", "Design graph snapshot binding is missing.", run_id=run_id)
    if expected_binding is not None and any(binding.get(key) != item for key, item in expected_binding.items()):
        raise HarnessError("designer_input_drift", "Design graph no longer matches its frozen inputs.", run_id=run_id)

    partitions = _dict_items(value.get("partitions"), code="vertical_design_graph_invalid", run_id=run_id)
    partition_keys: set[str] = set()
    for partition in partitions:
        key = _partition_key(partition, run_id=run_id)
        if key in partition_keys:
            raise HarnessError("vertical_design_graph_invalid", "Design graph contains duplicate partitions.", run_id=run_id)
        coverage = partition.get("coverage")
        if not isinstance(coverage, dict):
            raise HarnessError("vertical_design_graph_invalid", "Design partition coverage is invalid.", run_id=run_id)
        for surface, status in coverage.items():
            if str(surface) not in SURFACES or str(status) not in {
                "covered",
                "no_change",
                "not_applicable",
                "missing",
                "not_exercised",
            }:
                raise HarnessError("vertical_design_graph_invalid", "Design partition coverage contains an invalid value.", run_id=run_id)
        source_refs = partition.get("source_observation_refs", [])
        if not isinstance(source_refs, list) or any(not isinstance(item, str) or not item for item in source_refs):
            raise HarnessError(
                "vertical_design_graph_invalid",
                "Design partition source observations are invalid.",
                run_id=run_id,
            )
        partition_keys.add(key)
    for placement in _dict_items(value.get("surface_placements", []), code="vertical_design_graph_invalid", run_id=run_id):
        if (
            str(placement.get("surface") or "") not in PLACEABLE_SURFACES
            or not str(placement.get("scenario_id") or "")
            or not str(placement.get("component_id") or "")
            or not str(placement.get("reason") or "").strip()
            or not _strings(placement.get("evidence_refs"))
        ):
            raise HarnessError("vertical_design_graph_invalid", "Design surface placement is invalid.", run_id=run_id)
    for declaration in _dict_items(value.get("surface_not_exercised", []), code="vertical_design_graph_invalid", run_id=run_id):
        if (
            str(declaration.get("surface") or "") not in NOT_EXERCISABLE_SURFACES
            or not str(declaration.get("scenario_id") or "")
            or not str(declaration.get("reason") or "").strip()
            or not _strings(declaration.get("evidence_refs"))
        ):
            raise HarnessError("vertical_design_graph_invalid", "Design not_exercised declaration is invalid.", run_id=run_id)

    nodes = _dict_items(value.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id)
    node_ids: set[str] = set()
    # One rejection names every node's surface contract problem: a large UI delta repeated the same misnesting on
    # each node and was rejected one node at a time (design_v12 designer-0008).
    contract_errors: list[str] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id.startswith("VDN-") or node_id in node_ids:
            raise HarnessError("vertical_design_graph_invalid", "Design graph node identity is invalid.", run_id=run_id)
        if str(node.get("kind") or "") not in NODE_KINDS or str(node.get("surface") or "") not in SURFACES:
            raise HarnessError("vertical_design_graph_invalid", "Design graph node kind or surface is invalid.", run_id=run_id)
        disposition = str(node.get("disposition") or "")
        if disposition not in DISPOSITIONS:
            raise HarnessError("vertical_design_graph_invalid", "Design graph node disposition is invalid.", run_id=run_id)
        if _partition_key(node, run_id=run_id) not in partition_keys:
            raise HarnessError("vertical_design_graph_invalid", "Design graph node references an unknown partition.", run_id=run_id)
        evidence_refs = _strings(node.get("evidence_refs"))
        evidence_reason = str(node.get("evidence_reason") or "").strip()
        if disposition in {"reuse", "modify", "no_change"} and not evidence_refs:
            raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} requires frozen evidence.", run_id=run_id)
        if disposition == "add" and not evidence_reason:
            raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} requires a planned-work reason.", run_id=run_id)
        if not _strings(node.get("requirement_refs")):
            raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} has no Requirement trace.", run_id=run_id)
        if not str(node.get("contract") or "").strip():
            raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} has no Design contract.", run_id=run_id)
        surface_contract = node.get("surface_contract")
        node_ids.add(node_id)
        if surface_contract is not None:
            try:
                _validate_surface_contract(
                    surface_contract,
                    surface=str(node.get("surface") or ""),
                    node_id=node_id,
                    run_id=run_id,
                )
            except HarnessError as exc:
                if exc.error_code != "vertical_design_graph_invalid":
                    raise
                contract_errors.append(exc.message)
                continue
        if node.get("runtime_binding") is not None:
            _validate_runtime_binding(node.get("runtime_binding"), node=node, run_id=run_id)
    if contract_errors:
        raise HarnessError(
            "vertical_design_graph_invalid",
            contract_errors[0]
            if len(contract_errors) == 1
            else f"{len(contract_errors)} nodes have invalid surface contracts: " + " | ".join(contract_errors),
            run_id=run_id,
        )

    edges = _dict_items(value.get("edges"), code="vertical_design_graph_invalid", run_id=run_id)
    edge_ids: set[str] = set()
    edge_orders: set[tuple[str, int]] = set()
    for edge in edges:
        edge_id = str(edge.get("id") or "")
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if not edge_id.startswith("VDE-") or edge_id in edge_ids or source not in node_ids or target not in node_ids:
            raise HarnessError("vertical_design_graph_invalid", "Design graph edge identity or endpoint is invalid.", run_id=run_id)
        if str(edge.get("kind") or "") not in EDGE_KINDS:
            raise HarnessError("vertical_design_graph_invalid", "Design graph edge kind is invalid.", run_id=run_id)
        if not str(edge.get("label") or "").strip():
            raise HarnessError("vertical_design_graph_invalid", "Design graph edge label is missing.", run_id=run_id)
        branch_group = str(edge.get("branch_group") or "").strip()
        guard = str(edge.get("guard") or "").strip()
        if bool(branch_group) != bool(guard) or (branch_group and not _SAFE_KEY.fullmatch(branch_group)):
            raise HarnessError("vertical_design_graph_invalid", "Design graph branch metadata is invalid.", run_id=run_id)
        if "terminal" in edge and not isinstance(edge.get("terminal"), bool):
            raise HarnessError("vertical_design_graph_invalid", "Design graph edge terminal marker is invalid.", run_id=run_id)
        if edge.get(BOUNDARY_CONTRACT_VERSION) is not None:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Edge {edge_id} uses {BOUNDARY_CONTRACT_VERSION} as a field name; use boundary_contract instead.",
                run_id=run_id,
            )
        if edge.get("boundary_contract") is not None:
            _validate_boundary_contract(edge.get("boundary_contract"), edge_id=edge_id, run_id=run_id)
        scenario_id = str(edge.get("scenario_id") or "")
        order = edge.get("order")
        if not scenario_id or not isinstance(order, int) or order < 1 or (scenario_id, order) in edge_orders:
            raise HarnessError("vertical_design_graph_invalid", "Design graph edge scenario/order is invalid.", run_id=run_id)
        edge_orders.add((scenario_id, order))
        edge_ids.add(edge_id)

    unresolved = value.get("unresolved_items")
    if not isinstance(unresolved, list) or any(not isinstance(item, dict) for item in unresolved):
        raise HarnessError("vertical_design_graph_invalid", "Design graph unresolved items are invalid.", run_id=run_id)
    expected_digest = graph_digest(value)
    if value.get("artifact_digest") != expected_digest:
        raise HarnessError("vertical_design_graph_invalid", "Design graph digest does not match its content.", run_id=run_id)
    return value


def design_bundle_findings(
    graph: dict[str, object],
    *,
    landscape: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Return deterministic blockers for the human-readable System Design bundle."""

    run_id = str(graph.get("run_id") or "")
    validate_vertical_design_graph(graph, expected_run_id=run_id)
    findings: list[dict[str, object]] = []
    for index, node in enumerate(_dict_items(graph.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id), start=1):
        node_id = str(node.get("id") or "")
        scenario_id = str(node.get("scenario_id") or "")
        surface = str(node.get("surface") or "")
        contract = node.get("surface_contract")
        if contract is None:
            findings.append(
                {
                    "id": f"BUNDLE-{index:03d}",
                    "category": "surface_contract_missing",
                    "severity": "blocking",
                    "message": f"Design node {node_id} has no structured {surface} contract for the review bundle.",
                    "expected_change": (
                        f"Update {node_id} with a {SURFACE_CONTRACT_VERSION} surface_contract matching the {surface} schema."
                    ),
                    "requirement_ids": _strings(node.get("requirement_refs")),
                    "diagram_ids": [scenario_id] if scenario_id else [],
                    "evidence": [{"source": "design_graph", "ref": node_id, "excerpt": str(node.get("contract") or "")}],
                    "subject_ref": f"{scenario_id}:{node_id}:surface_contract",
                    "origin": "deterministic",
                }
            )
    findings.extend(_runtime_composition_findings(graph, required_surfaces=None, landscape=landscape))
    findings.extend(
        _sequence_behavior_findings(
            nodes=_dict_items(graph.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id),
            edges=_dict_items(graph.get("edges"), code="vertical_design_graph_invalid", run_id=run_id),
        )
    )
    return findings


def _sequence_behavior_findings(
    *,
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Reject UI scenarios whose ordered behavior cannot form one observable path."""

    findings: list[dict[str, object]] = []
    ui_nodes_by_scenario: dict[str, list[dict[str, object]]] = {}
    for node in nodes:
        if str(node.get("surface") or "") == "ui":
            ui_nodes_by_scenario.setdefault(str(node.get("scenario_id") or ""), []).append(node)

    for scenario_id, ui_nodes in sorted(ui_nodes_by_scenario.items()):
        behavior = sorted(
            (
                edge
                for edge in edges
                if str(edge.get("scenario_id") or "") == scenario_id
                and str(edge.get("kind") or "") not in STRUCTURAL_EDGE_KINDS
            ),
            key=lambda item: int(item.get("order") or 0),
        )
        owner = ui_nodes[0]
        if not behavior:
            findings.append(
                _composition_finding(
                    "sequence_behavior_missing",
                    scenario_id,
                    f"Scenario {scenario_id} has a UI surface but no ordered behavior beyond structural dependencies.",
                    "Add the user action and observable outcome interactions for this scenario; depends_on alone is not behavior.",
                    node=owner,
                )
            )
            continue

        terminals = [edge for edge in behavior if bool(edge.get("terminal"))]
        if len(terminals) != 1 or terminals[0] is not behavior[-1]:
            findings.append(
                _composition_finding(
                    "sequence_terminal_invalid",
                    scenario_id,
                    f"Scenario {scenario_id} must have exactly one terminal interaction and it must be last.",
                    "Keep one terminal=true edge on the final observable outcome and clear terminal on earlier interactions.",
                    node=owner,
                )
            )

        for previous, current in zip(behavior, behavior[1:]):
            if previous.get("branch_group") or current.get("branch_group"):
                continue
            if str(previous.get("to") or "") == str(current.get("from") or ""):
                continue
            findings.append(
                _composition_finding(
                    "sequence_order_disconnected",
                    scenario_id,
                    f"Scenario {scenario_id} jumps between unrelated participants at orders "
                    f"{previous.get('order')} and {current.get('order')}.",
                    "Reorder or replace the interactions so each message continues from the participant reached by the previous message.",
                    node=owner,
                )
            )
            break

    return findings


def _validate_surface_contract(
    value: object,
    *,
    surface: str,
    node_id: str,
    run_id: str,
) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SURFACE_CONTRACT_VERSION:
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} has an unsupported surface contract{_version_mismatch(value, SURFACE_CONTRACT_VERSION)}.",
            run_id=run_id,
        )
    if str(value.get("surface") or "") != surface:
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} surface contract does not match its graph surface.",
            run_id=run_id,
        )

    def require_text(key: str) -> None:
        if not str(value.get(key) or "").strip():
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} surface contract is missing {key}.",
                run_id=run_id,
            )

    def require_strings(key: str, *, allow_empty: bool = False) -> None:
        raw = value.get(key)
        if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} surface contract has invalid {key}: it must be an array of non-empty strings, "
                "never objects.",
                run_id=run_id,
            )
        if not allow_empty and not raw:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} surface contract requires at least one {key} item.",
                run_id=run_id,
            )

    def require_objects(key: str, required_keys: set[str], *, allow_empty: bool = False) -> None:
        raw = value.get(key)
        if not isinstance(raw, list) or any(
            not isinstance(item, dict)
            or any(not str(item.get(required) or "").strip() for required in required_keys)
            for item in raw
        ):
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} surface contract has invalid {key}: it must be an array of objects, each with "
                f"non-empty {', '.join(sorted(required_keys))}.",
                run_id=run_id,
            )
        if not allow_empty and not raw:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} surface contract requires at least one {key} item.",
                run_id=run_id,
            )

    if surface == "ui":
        require_text("entrypoint")
        require_strings("states")
        require_objects("transitions", {"from", "action", "to", "outcome"})
        declared_states = set(value["states"])
        undeclared_states = list(
            dict.fromkeys(
                str(transition[endpoint])
                for transition in value["transitions"]
                for endpoint in ("from", "to")
                if str(transition[endpoint]) not in declared_states
            )
        )
        # Keep pre-gate Harness wiring resumable; new templates declare both states.
        legacy_harness_wiring = (
            str(_dict_value(value.get("runtime_surface")).get("role") or "") == "composition_wiring"
            and declared_states == {"forwarding"}
            and undeclared_states == ["mounted"]
        )
        if undeclared_states and not legacy_harness_wiring:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} UI transitions reference undeclared states: "
                f"{', '.join(undeclared_states)}. Declare them in states or change the transition endpoints "
                "to declared states.",
                run_id=run_id,
            )
        require_strings("validations", allow_empty=True)
        require_strings("outcomes")
        experience = value.get("experience_contract")
        # Every experience field is required, so this only names a misnesting the validator would reject anyway.
        misplaced = [key for key in UI_EXPERIENCE_FIELDS if key in value] if isinstance(experience, dict) else []
        misplaced = [key for key in misplaced if key not in experience]
        if misplaced:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} surface_contract holds {', '.join(misplaced)}, which belong inside "
                "surface_contract.experience_contract; move them into experience_contract (it was closed too early).",
                run_id=run_id,
            )
        if experience is not None:
            _validate_ui_experience_contract(experience, node_id=node_id, run_id=run_id)
        _validate_runtime_surface(value.get("runtime_surface"), node_id=node_id, run_id=run_id)
        screen_id = str(_dict_value(_dict_value(experience).get("screen")).get("screen_id") or "")
        if experience is not None and screen_id != str(_dict_value(value.get("runtime_surface")).get("screen_id") or ""):
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} experience_contract.screen.screen_id must equal runtime_surface.screen_id.",
                run_id=run_id,
            )
    elif surface == "api":
        require_text("operation_id")
        require_text("method")
        if str(value.get("method") or "").upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} API method is invalid.", run_id=run_id)
        require_text("path")
        if not str(value.get("path") or "").startswith("/"):
            raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} API path must start with /.", run_id=run_id)
        require_objects("request_fields", {"name", "type", "location"}, allow_empty=True)
        require_objects("responses", {"status", "outcome"})
        require_text("authorization")
        if "authorization_required" in value and not isinstance(value.get("authorization_required"), bool):
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} API authorization_required must be boolean.",
                run_id=run_id,
            )
        access_fields = {
            "authentication",
            "authentication_required",
            "authentication_source",
            "authentication_requirement_refs",
            "authentication_evidence_refs",
        }
        if access_fields.intersection(value):
            require_text("authentication")
            if not isinstance(value.get("authentication_required"), bool):
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    f"Node {node_id} API authentication_required must be boolean.",
                    run_id=run_id,
                )
            if not isinstance(value.get("authorization_required"), bool):
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    f"Node {node_id} API authorization_required must be boolean when using the typed access contract.",
                    run_id=run_id,
                )
            source = str(value.get("authentication_source") or "")
            if source not in {"existing_runtime", "external_dependency", "new_boundary", "none"}:
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    f"Node {node_id} API authentication_source is invalid.",
                    run_id=run_id,
                )
            require_strings("authentication_requirement_refs", allow_empty=True)
            require_strings("authentication_evidence_refs", allow_empty=True)
            if value.get("authentication_required") is True and source == "none":
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    f"Node {node_id} requires authentication but declares no authentication source.",
                    run_id=run_id,
                )
            # authentication_required=False with a source is optional authentication: callers without a credential are
            # served and a present credential identifies the caller (Conduit UC4: a public read that only the author may
            # see as a draft). Rejecting it forced "none" and dropped the identity the endpoint needs.
            if source == "existing_runtime" and not value.get("authentication_evidence_refs"):
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    f"Node {node_id} existing authentication has no repository evidence.",
                    run_id=run_id,
                )
    elif surface == "business_rule":
        require_strings("inputs")
        require_text("decision")
        require_strings("outcomes")
    elif surface == "data":
        require_text("entity")
        require_text("operation")
        require_objects("fields", {"name", "type"})
        require_strings("constraints", allow_empty=True)
        require_strings("relations", allow_empty=True)
        require_text("migration_intent")
    elif surface == "interaction":
        require_strings("steps")
        require_strings("outcomes")


def _validate_runtime_surface(value: object, *, node_id: str, run_id: str) -> None:
    def invalid(detail: str) -> HarnessError:
        return HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} runtime_surface {detail}.",
            run_id=run_id,
        )

    if not isinstance(value, dict) or value.get("schema_version") != RUNTIME_SURFACE_BINDING_VERSION:
        raise invalid(f"must use {RUNTIME_SURFACE_BINDING_VERSION}")
    if not _SAFE_KEY.fullmatch(str(value.get("screen_id") or "")):
        raise invalid("screen_id is missing or unsafe")
    entrypoint = str(value.get("entrypoint") or "")
    if not _is_symbol_ref(entrypoint):
        raise invalid("entrypoint must be <repo path>#<symbol>")
    path = value.get("composition_path")
    if not isinstance(path, list) or not path or any(not _is_symbol_ref(str(item)) for item in path):
        raise invalid("composition_path must list entrypoint through target as <repo path>#<symbol>")
    if str(path[0]) != entrypoint:
        raise invalid("composition_path must start at entrypoint")
    inputs = value.get("required_inputs")
    if not isinstance(inputs, list):
        raise invalid("required_inputs must be a list")
    for item in inputs:
        if (
            not isinstance(item, dict)
            or not str(item.get("name") or "").strip()
            or str(item.get("source") or "") not in RUNTIME_INPUT_SOURCES
            or not str(item.get("binding") or "").strip()
            or not _is_symbol_ref(str(item.get("provided_by") or ""))
            or not isinstance(item.get("requirement_refs", []), list)
            or not isinstance(item.get("states", []), list)
        ):
            raise invalid(
                "required_inputs items need name, source ("
                + "|".join(sorted(RUNTIME_INPUT_SOURCES))
                + "), binding and provided_by <repo path>#<symbol>"
            )
    if str(value.get("runnable_status") or "") not in RUNNABLE_STATUSES:
        raise invalid("runnable_status must be " + "|".join(sorted(RUNNABLE_STATUSES)))
    if str(value.get("role") or "screen") not in UI_SURFACE_ROLES:
        raise invalid("role must be " + "|".join(sorted(UI_SURFACE_ROLES)))
    forwarded = value.get("forwarded_inputs", [])
    if not isinstance(forwarded, list) or any(
        not isinstance(item, dict)
        or not _SAFE_KEY.fullmatch(str(item.get("screen_id") or ""))
        or not _is_symbol_ref(str(item.get("to") or ""))
        or not isinstance(item.get("names"), list)
        or not item.get("names")
        or any(not isinstance(name, str) or not name.strip() for name in item.get("names", []))
        for item in forwarded
    ):
        raise invalid("forwarded_inputs must contain screen_id, to <repo path>#<symbol>, and non-empty names")
    if str(value.get("role") or "screen") != "composition_wiring" and forwarded:
        raise invalid("forwarded_inputs is reserved for composition_wiring nodes")
    if not isinstance(value.get("evidence_refs", []), list):
        raise invalid("evidence_refs must be a list")


def _is_symbol_ref(value: str) -> bool:
    path, _, symbol = value.partition("#")
    return _is_repo_path(path) and bool(_TYPESCRIPT_IDENTIFIER.fullmatch(symbol))


def _validate_ui_experience_contract(value: object, *, node_id: str, run_id: str) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != UI_EXPERIENCE_CONTRACT_V2:
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} has an unsupported UI experience contract; expected {UI_EXPERIENCE_CONTRACT_V2}.",
            run_id=run_id,
        )
    for key in ("component_structure", "accessibility", "responsive", "focus_flow", "guidance_refs"):
        items = value.get(key)
        if not isinstance(items, list) or not items or any(not isinstance(item, str) or not item.strip() for item in items):
            # design_v20 designer-0038/0039: "has invalid guidance_refs" alone made the Designer empty valid refs it
            # had nested under visual_contract; say what is required and where the field was found.
            misplaced = [
                name for name, nested in value.items() if isinstance(nested, dict) and key in nested
            ]
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} UI experience contract has invalid {key}: experience_contract.{key} must be a "
                "non-empty array of non-empty strings."
                + "".join(
                    f" It was found under experience_contract.{name}; move it up to experience_contract."
                    for name in misplaced
                ),
                run_id=run_id,
            )
    feedback = value.get("feedback")
    if not isinstance(feedback, list) or not feedback or any(
        not isinstance(item, dict)
        or not str(item.get("trigger") or "").strip()
        or not str(item.get("response") or "").strip()
        for item in feedback
    ):
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} UI experience contract has invalid feedback.",
            run_id=run_id,
        )
    _validate_holistic_experience(value, node_id=node_id, run_id=run_id)


def _layout_item_valid(item: object) -> bool:
    return (
        isinstance(item, dict)
        and bool(str(item.get("region") or "").strip())
        and item.get("layer") in UI_LAYOUT_LAYERS
        and bool(str(item.get("content") or "").strip())
    )


def _validate_holistic_experience(value: dict[str, object], *, node_id: str, run_id: str) -> None:
    """Structural checks only; whether the UX decisions are good stays with semantic review."""

    def invalid(detail: str) -> HarnessError:
        return HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} {UI_EXPERIENCE_CONTRACT_V2} {detail}.",
            run_id=run_id,
        )

    def texts(raw: object) -> bool:
        return isinstance(raw, list) and bool(raw) and all(isinstance(item, str) and item.strip() for item in raw)

    screen = value.get("screen")
    if (
        not isinstance(screen, dict)
        or not _SAFE_KEY.fullmatch(str(screen.get("screen_id") or ""))
        or not str(screen.get("purpose") or "").strip()
        or not str(screen.get("entry_condition") or "").strip()
    ):
        raise invalid("screen needs screen_id, purpose and entry_condition")
    states = value.get("runtime_states")
    if not isinstance(states, dict):
        raise invalid("runtime_states must be an object")
    for name in UI_RUNTIME_STATES:
        state = states.get(name)
        applicable = (
            isinstance(state, dict)
            and str(state.get("presentation") or "").strip()
            and str(state.get("trigger") or "").strip()
        )
        skipped = (
            isinstance(state, dict)
            and state.get("not_applicable") is True
            and str(state.get("reason") or "").strip()
        )
        if not applicable and not skipped:
            raise invalid(
                f"runtime_states.{name} needs presentation and trigger, or not_applicable=true with a reason"
            )
    visual = value.get("visual_contract")
    if not isinstance(visual, dict):
        raise invalid("visual_contract must be an object")
    for key in UI_VISUAL_GROUPS:
        if key != "layout" and not texts(visual.get(key)):
            raise invalid(f"visual_contract.{key} needs at least one decision")
    layout = visual.get("layout")
    if not isinstance(layout, list) or not layout or not all(_layout_item_valid(item) for item in layout):
        raise invalid("visual_contract.layout needs {region,layer,content} items with layer " + "|".join(UI_LAYOUT_LAYERS))
    if str(visual.get("style_target_policy") or "") not in UI_STYLE_TARGET_POLICIES:
        raise invalid("visual_contract.style_target_policy must be " + "|".join(sorted(UI_STYLE_TARGET_POLICIES)))
    if not str(visual.get("style_target_reason") or "").strip():
        raise invalid("visual_contract.style_target_reason is required")
    policies = value.get("focus_policies")
    if not isinstance(policies, list):
        raise invalid("focus_policies must be a list")
    for policy in policies:
        announcement = policy.get("announcement") if isinstance(policy, dict) else None
        if (
            not isinstance(policy, dict)
            or not str(policy.get("event") or "").strip()
            or not isinstance(policy.get("source_removed"), bool)
            or not str(policy.get("primary_target") or "").strip()
            or not str(policy.get("selection_strategy") or "").strip()
            or not isinstance(announcement, dict)
            or not str(announcement.get("target") or "").strip()
            or not isinstance(announcement.get("moves_focus"), bool)
        ):
            raise invalid(
                "focus_policies items need event, source_removed, primary_target, selection_strategy "
                "and announcement{target, moves_focus}"
            )
        if policy["source_removed"]:
            removed = str(policy.get("removed_element") or "").strip()
            fallback = str(policy.get("fallback_target") or "").strip()
            if not removed or not fallback:
                raise invalid("focus policy with source_removed=true needs removed_element and fallback_target")
            if removed in {str(policy["primary_target"]).strip(), fallback}:
                raise invalid(f"focus policy for {policy['event']} must not move focus to the removed element")
    if not texts(value.get("repo_evidence_refs")):
        raise invalid("repo_evidence_refs needs the ui_repo_context evidence used for these decisions")


def _validate_delta_guidance_refs(
    surface_contract: object,
    *,
    valid_guidance_refs: set[str],
    run_id: str,
) -> None:
    contract = _dict_value(surface_contract)
    experience = _dict_value(contract.get("experience_contract"))
    refs = _strings(experience.get("guidance_refs"))
    if any(ref not in valid_guidance_refs for ref in refs):
        raise HarnessError(
            "designer_delta_invalid",
            "UI experience contract references guidance that was not observed in this Designer session.",
            run_id=run_id,
        )


def _validate_runtime_binding(value: object, *, node: dict[str, object], run_id: str) -> None:
    node_id = str(node.get("id") or "")
    if not isinstance(value, dict) or value.get("schema_version") != RUNTIME_BINDING_VERSION:
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} has an unsupported runtime binding{_version_mismatch(value, RUNTIME_BINDING_VERSION)}.",
            run_id=run_id,
        )
    runtime_kind = str(value.get("runtime_kind") or "")
    if runtime_kind not in RUNTIME_KINDS:
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime kind is invalid.", run_id=run_id)
    target = value.get("target")
    if not isinstance(target, dict):
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime target is missing.", run_id=run_id)
    component_id = str(target.get("component_id") or "")
    path = str(target.get("path") or "")
    symbol = str(target.get("symbol") or "")
    if component_id != str(node.get("component_id") or "") or not _is_repo_path(path) or not symbol.strip():
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime target is invalid.", run_id=run_id)
    attachment = value.get("attachment")
    if not isinstance(attachment, dict):
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime attachment is missing.", run_id=run_id)
    if str(attachment.get("relation") or "") not in ATTACHMENT_RELATIONS:
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime attachment relation is invalid.", run_id=run_id)
    owner_ref = str(attachment.get("owner_ref") or "")
    owner_path = str(attachment.get("owner_path") or "")
    owner_symbol = str(attachment.get("owner_symbol") or "")
    # A file anchor (RLA-* source_file, e.g. the router module that registers routes) is a whole-file owner with no
    # symbol; the composition check compares owner_path/owner_symbol with that anchor.
    if (
        not owner_ref
        or (owner_path and not _is_repo_path(owner_path))
        or (owner_path and not owner_symbol and not owner_ref.startswith("RLA-"))
    ):
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime attachment owner is invalid.", run_id=run_id)
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list) or any(not isinstance(item, str) or not item for item in evidence_refs):
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime evidence is invalid.", run_id=run_id)


def _validate_boundary_contract(value: object, *, edge_id: str, run_id: str) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != BOUNDARY_CONTRACT_VERSION:
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Edge {edge_id} has an unsupported boundary contract{_version_mismatch(value, BOUNDARY_CONTRACT_VERSION)}.",
            run_id=run_id,
        )


def _version_mismatch(value: object, expected: str) -> str:
    # Saying what was written and what is expected lets the Designer fix a version it copied from a nested contract.
    if not isinstance(value, dict):
        return f": it must be an object with schema_version '{expected}'"
    return f": schema_version is {value.get('schema_version')!r}, expected '{expected}'"
    for key in ("consumer_operation", "provider_operation"):
        if not str(value.get(key) or "").strip():
            raise HarnessError("vertical_design_graph_invalid", f"Edge {edge_id} boundary contract is missing {key}.", run_id=run_id)
    for key in ("input_mapping", "auth_mapping", "response_mapping", "error_mapping"):
        mappings = value.get(key)
        if not isinstance(mappings, list) or any(
            not isinstance(item, dict)
            or not str(item.get("from") or "").strip()
            or not str(item.get("to") or "").strip()
            or not isinstance(item.get("required"), bool)
            for item in mappings
        ):
            raise HarnessError("vertical_design_graph_invalid", f"Edge {edge_id} boundary contract has invalid {key}.", run_id=run_id)


def apply_designer_delta(
    graph: dict[str, object],
    delta: object,
    *,
    valid_evidence_refs: set[str],
    valid_guidance_refs: set[str] | None = None,
) -> tuple[dict[str, object], dict[str, str]]:
    written: dict[str, Any] = {}
    try:
        return _apply_designer_delta(
            graph,
            delta,
            valid_evidence_refs=valid_evidence_refs,
            valid_guidance_refs=valid_guidance_refs,
            written=written,
        )
    except HarnessError as exc:
        # The Designer never saw the ids allocated for this delta: name its nodes and edges as it wrote them.
        message = exc.message
        for allocated, name in _delta_written_names(
            graph, written.get("candidate") or {}, written.get("temp_refs") or {}, written.get("edge_refs") or {}
        ).items():
            message = message.replace(allocated, name)
        exc.message = message
        exc.args = (message,)
        raise


def _apply_designer_delta(
    graph: dict[str, object],
    delta: object,
    *,
    valid_evidence_refs: set[str],
    valid_guidance_refs: set[str] | None,
    written: dict[str, Any],
) -> tuple[dict[str, object], dict[str, str]]:
    run_id = str(graph.get("run_id") or "")
    validate_vertical_design_graph(graph, expected_run_id=run_id)
    expected_delta_version = DELTA_VERSION
    if not isinstance(delta, dict) or delta.get("schema_version") != expected_delta_version:
        raise HarnessError(
            "designer_delta_invalid",
            f"Unsupported Designer delta contract; expected schema_version={expected_delta_version} inside arguments.delta.",
            run_id=run_id,
        )
    if str(delta.get("base_graph_digest") or "") != str(graph.get("artifact_digest") or ""):
        raise HarnessError("designer_delta_precondition_failed", "Designer delta targets a stale graph revision.", run_id=run_id)
    partition = delta.get("partition")
    if not isinstance(partition, dict):
        raise HarnessError("designer_delta_invalid", "Designer delta partition is missing.", run_id=run_id)
    partition_key = _partition_key(partition, run_id=run_id)
    candidate = json.loads(json.dumps(graph, ensure_ascii=False))
    candidate.pop("artifact_digest", None)
    temp_refs: dict[str, str] = {}
    edge_refs: dict[str, str] = {}
    written.update(candidate=candidate, temp_refs=temp_refs, edge_refs=edge_refs)
    operations = _dict_items(delta.get("operations"), code="designer_delta_invalid", run_id=run_id)
    if not operations:
        raise HarnessError("designer_delta_invalid", "Designer delta must contain at least one operation.", run_id=run_id)
    order_ops = [item for item in operations if str(item.get("op") or "") == "order_edges"]
    if len(order_ops) > 1:
        raise HarnessError("designer_delta_invalid", "A delta may contain at most one order_edges operation.", run_id=run_id)
    remove_ops = [item for item in operations if str(item.get("op") or "") == "remove_edge"]
    add_ops = [item for item in operations if str(item.get("op") or "") == "add_edge"]
    replacement_order: int | None = None
    if len(remove_ops) == len(add_ops) == 1:
        removed = next(
            (
                item
                for item in _dict_items(candidate.get("edges"), code="designer_delta_invalid", run_id=run_id)
                if str(item.get("id") or "") == str(remove_ops[0].get("edge_id") or "")
            ),
            None,
        )
        added = _dict_value(add_ops[0].get("edge"))
        if removed and (
            str(removed.get("scenario_id") or ""),
            str(removed.get("from") or ""),
            str(removed.get("to") or ""),
            str(removed.get("kind") or ""),
            str(removed.get("label") or ""),
            str(removed.get("branch_group") or ""),
            str(removed.get("guard") or ""),
        ) == (
            str(added.get("scenario_id") or partition.get("scenario_id") or ""),
            str(added.get("from") or ""),
            str(added.get("to") or ""),
            str(added.get("kind") or ""),
            str(added.get("label") or ""),
            str(added.get("branch_group") or ""),
            str(added.get("guard") or ""),
        ):
            replacement_order = int(removed.get("order") or 0) or None
    for operation in operations:
        op = str(operation.get("op") or "")
        if op not in OPERATIONS:
            raise HarnessError("designer_delta_invalid", f"Unsupported Designer delta operation: {op}", run_id=run_id)
        if op == "add_edge" and replacement_order is not None:
            edge = _dict_value(operation.get("edge"))
            if "order" not in edge:
                operation = {**operation, "edge": {**edge, "order": replacement_order}}
        if op == "order_edges":
            continue  # applied below, once every add_edge and remove_edge of the delta exists
        _apply_operation(
            candidate,
            operation,
            partition=partition,
            partition_key=partition_key,
            temp_refs=temp_refs,
            valid_evidence_refs=valid_evidence_refs,
            valid_guidance_refs=valid_guidance_refs or set(),
            run_id=run_id,
            edge_refs=edge_refs,
        )
    if order_ops:
        _apply_edge_order(candidate, order_ops[0], partition=partition, edge_refs=edge_refs, run_id=run_id)
    _resolve_attachment_temp_refs(candidate, temp_refs, before=graph, run_id=run_id)
    _validate_new_placements(candidate, before=graph, partition=partition, run_id=run_id)
    _validate_not_exercised(candidate, run_id=run_id)
    candidate["unresolved_items"] = _dict_items(
        delta.get("unresolved_items", []),
        code="designer_delta_invalid",
        run_id=run_id,
    )
    _materialize_composition_forwarding(candidate)
    _copy_paired_input_mappings(candidate, before=graph)
    candidate["revision"] = int(graph.get("revision") or 0) + 1
    candidate["nodes"] = sorted(_dict_items(candidate.get("nodes"), code="designer_delta_invalid", run_id=run_id), key=lambda item: str(item.get("id") or ""))
    candidate["edges"] = sorted(
        _dict_items(candidate.get("edges"), code="designer_delta_invalid", run_id=run_id),
        key=lambda item: (
            str(item.get("scenario_id") or ""),
            int(item.get("order") or 0),
            str(item.get("id") or ""),
        ),
    )
    candidate["partitions"] = sorted(_dict_items(candidate.get("partitions"), code="designer_delta_invalid", run_id=run_id), key=lambda item: _partition_key(item, run_id=run_id))
    _validate_unique_cross_scenario_parent(candidate, run_id=run_id)
    semantic_candidate = dict(candidate)
    semantic_candidate.pop("artifact_digest", None)
    semantic_candidate.pop("revision", None)
    semantic_current = dict(graph)
    semantic_current.pop("artifact_digest", None)
    semantic_current.pop("revision", None)
    if semantic_candidate == semantic_current:
        raise HarnessError(
            "designer_delta_no_effect",
            "Designer delta made no semantic graph change. Address the active finding instead of removing and re-adding equivalent nodes or edges.",
            run_id=run_id,
        )
    candidate["artifact_digest"] = graph_digest(candidate)
    validate_vertical_design_graph(candidate, expected_run_id=run_id)
    new_findings = _new_boundary_contract_findings(graph, candidate)
    contract_findings = [item for item in new_findings if item.get("category") == "boundary_contract_mismatch"]
    if contract_findings:
        details = "; ".join(
            f"{item.get('subject_ref')}: {item.get('message')}"
            for item in contract_findings
        )
        raise HarnessError(
            "designer_delta_invalid",
            f"Designer delta failed boundary contract validation: {details}",
            run_id=run_id,
        )
    # Required props come from the frozen AS-IS source facts, so a missing declaration is known when the delta
    # arrives; reporting it only at completion cost a later repair round (design_v10 designer-0006).
    input_findings = [item for item in new_findings if item.get("category") == "ui_required_input_undeclared"]
    if input_findings:
        details = "; ".join(f"{item.get('subject_ref')}: {item.get('message')}" for item in input_findings)
        raise HarnessError(
            "designer_delta_invalid",
            f"Designer delta leaves required props undeclared: {details} Declare each required prop in "
            "runtime_surface.required_inputs with its source, binding and the provided_by hop that passes it.",
            run_id=run_id,
        )
    return candidate, temp_refs


def _delta_written_names(
    before: dict[str, object],
    candidate: dict[str, object],
    temp_refs: dict[str, str],
    edge_refs: dict[str, str] | None = None,
) -> dict[str, str]:
    """Ids the Harness allocated for this delta, named as the Designer wrote them: edges by their temp_ref or as
    from -> to (kind), then nodes by temp_ref (edges first, so an edge name keeps the temp_refs it is written with)."""

    written = {node_id: temp_ref for temp_ref, node_id in temp_refs.items()}
    written_edges = {edge_id: edge_ref for edge_ref, edge_id in (edge_refs or {}).items()}
    existing = {str(edge.get("id") or "") for edge in before.get("edges") or [] if isinstance(edge, dict)}
    return {
        **{
            str(edge["id"]): written_edges.get(str(edge["id"])) or (
                f"{written.get(str(edge.get('from')), edge.get('from'))} -> "
                f"{written.get(str(edge.get('to')), edge.get('to'))} ({edge.get('kind')})"
            )
            for edge in candidate.get("edges") or []
            if isinstance(edge, dict) and edge.get("id") and str(edge["id"]) not in existing
        },
        **written,
    }


def _apply_edge_order(
    graph: dict[str, object],
    operation: dict[str, object],
    *,
    partition: dict[str, object],
    edge_refs: dict[str, str],
    run_id: str,
) -> None:
    """Renumber the scenario's behavior edges in the listed order (design_v19 designer-0009: with append-only order
    the only way to interleave a later partition's edges was to remove and re-add every edge with its contract)."""

    unknown_keys = sorted(set(operation) - {"op", "scenario_id", "edges", "terminal"})
    if unknown_keys:
        raise HarnessError(
            "designer_delta_invalid",
            f"order_edges accepts only scenario_id, edges and terminal; remove {unknown_keys}.",
            run_id=run_id,
        )
    scenario_id = str(operation.get("scenario_id") or partition.get("scenario_id") or "")
    if scenario_id != str(partition.get("scenario_id") or ""):
        raise HarnessError(
            "designer_delta_invalid", "order_edges can only order the scenario of the delta partition.", run_id=run_id
        )
    refs = operation.get("edges")
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise HarnessError(
            "designer_delta_invalid",
            "order_edges.edges must be a non-empty array of VDE-* edge ids and temp_refs of edges this delta adds.",
            run_id=run_id,
        )
    ordered = [edge_refs.get(ref.strip(), ref.strip()) for ref in refs]
    edges = _dict_items(graph.get("edges"), code="designer_delta_invalid", run_id=run_id)
    behavior = [
        item
        for item in edges
        if str(item.get("scenario_id") or "") == scenario_id and str(item.get("kind") or "") not in STRUCTURAL_EDGE_KINDS
    ]
    behavior_ids = {str(item.get("id") or "") for item in behavior}
    problems: list[str] = []
    repeated = sorted({ref for ref, resolved in zip(refs, ordered) if ordered.count(resolved) > 1})
    if repeated:
        problems.append(f"listed more than once: {repeated}")
    unknown = [ref for ref, resolved in zip(refs, ordered) if resolved not in behavior_ids]
    if unknown:
        problems.append(f"not a calls/returns/reads/writes edge of {scenario_id}: {unknown}")
    missing = [item for item in behavior if str(item.get("id") or "") not in ordered]
    if missing:
        problems.append(
            "missing: "
            + ", ".join(f"{item.get('id')} ({item.get('from')} -> {item.get('to')} {item.get('kind')})" for item in missing)
        )
    if problems:
        raise HarnessError(
            "designer_delta_invalid",
            f"order_edges must list every behavior edge of {scenario_id} exactly once; " + "; ".join(problems) + ".",
            run_id=run_id,
        )
    terminal = operation.get("terminal")
    if terminal is not None and (
        not isinstance(terminal, str) or edge_refs.get(terminal.strip(), terminal.strip()) != ordered[-1]
    ):
        raise HarnessError(
            "designer_delta_invalid", "order_edges.terminal must name the last edge of order_edges.edges.", run_id=run_id
        )
    position = {edge_id: index for index, edge_id in enumerate(ordered, start=1)}
    structural = sorted(
        (
            item
            for item in edges
            if str(item.get("scenario_id") or "") == scenario_id and str(item.get("id") or "") not in position
        ),
        key=lambda item: int(item.get("order") or 0),
    )
    for index, item in enumerate(structural, start=len(ordered) + 1):
        position[str(item.get("id") or "")] = index
    graph["edges"] = [
        {
            **item,
            "order": position[str(item.get("id") or "")],
            **({"terminal": str(item.get("id") or "") == ordered[-1]} if terminal is not None else {}),
        }
        if str(item.get("scenario_id") or "") == scenario_id
        else item
        for item in edges
    ]


DELTA_TIME_COMPOSITION_CATEGORIES = frozenset({"boundary_contract_mismatch", "ui_required_input_undeclared"})


def _new_boundary_contract_findings(
    current: dict[str, object],
    candidate: dict[str, object],
) -> list[dict[str, object]]:
    """Delta-time composition findings the candidate adds (DELTA_TIME_COMPOSITION_CATEGORIES)."""

    def boundary_findings(graph: dict[str, object]) -> list[dict[str, object]]:
        return [
            item
            for item in _runtime_composition_findings(
                graph,
                required_surfaces=None,
                landscape=None,
            )
            if str(item.get("category") or "") in DELTA_TIME_COMPOSITION_CATEGORIES
        ]

    existing = {
        (
            str(item.get("id") or ""),
            str(item.get("message") or ""),
            str(item.get("expected_change") or ""),
        )
        for item in boundary_findings(current)
    }
    return [
        item
        for item in boundary_findings(candidate)
        if (
            str(item.get("id") or ""),
            str(item.get("message") or ""),
            str(item.get("expected_change") or ""),
        )
        not in existing
    ]


def _validate_unique_cross_scenario_parent(graph: dict[str, object], *, run_id: str) -> None:
    nodes_by_id = {
        str(item.get("id") or ""): item
        for item in _dict_items(graph.get("nodes"), code="designer_delta_invalid", run_id=run_id)
    }
    parent_edges: dict[str, list[str]] = {}
    for edge in _dict_items(graph.get("edges"), code="designer_delta_invalid", run_id=run_id):
        if str(edge.get("kind") or "") != "depends_on":
            continue
        source = nodes_by_id.get(str(edge.get("from") or ""), {})
        target = nodes_by_id.get(str(edge.get("to") or ""), {})
        scenario_id = str(edge.get("scenario_id") or "")
        source_scenario = str(source.get("scenario_id") or "")
        target_scenario = str(target.get("scenario_id") or "")
        if scenario_id and source_scenario == scenario_id and target_scenario and target_scenario != scenario_id:
            parent_edges.setdefault(scenario_id, []).append(str(edge.get("id") or "<new edge>"))
    duplicate = next(
        ((scenario_id, edge_ids) for scenario_id, edge_ids in sorted(parent_edges.items()) if len(edge_ids) > 1),
        None,
    )
    if duplicate is not None:
        scenario_id, edge_ids = duplicate
        raise HarnessError(
            "designer_delta_invalid",
            (
                f"Branch scenario {scenario_id} already has one cross-scenario parent dependency; "
                f"remove the existing parent before adding another ({', '.join(edge_ids)})."
            ),
            run_id=run_id,
        )


def completion_findings(
    graph: dict[str, object],
    *,
    required_surfaces: dict[str, set[str]],
    landscape: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    run_id = str(graph.get("run_id") or "")
    validate_vertical_design_graph(graph, expected_run_id=run_id)
    findings: list[dict[str, object]] = []
    partitions = {_partition_key(item, run_id=run_id): item for item in _dict_items(graph.get("partitions"), code="vertical_design_graph_invalid", run_id=run_id)}
    nodes = _dict_items(graph.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id)
    declarations = _dict_items(graph.get("surface_not_exercised", []), code="vertical_design_graph_invalid", run_id=run_id)
    declared = {(str(item.get("scenario_id") or ""), str(item.get("surface") or "")) for item in declarations}
    carried = {ref for node in nodes for ref in _strings(node.get("requirement_refs"))}
    for item in declarations:
        # Nothing runs on the declared surface, so its Requirement ids must be carried by other Design nodes.
        unowned = [ref for ref in _strings(item.get("requirement_refs")) if ref not in carried]
        if unowned:
            findings.append(
                _finding(
                    "not_exercised_ref_unowned",
                    f"{item.get('scenario_id')}:{item.get('surface')}",
                    f"Requirement ids of a surface declared not exercised are carried by no Design node: {', '.join(unowned)}.",
                )
            )
    for scenario_id, surfaces in sorted(required_surfaces.items()):
        matching = [item for key, item in partitions.items() if key.split("|", 1)[0] == scenario_id]
        if not matching:
            findings.append(_finding("missing_partition", scenario_id, "Scenario has no Design partition."))
            continue
        for surface in sorted(surfaces):
            if (scenario_id, surface) in declared:
                continue
            covered = any(str(_dict_value(item.get("coverage")).get(surface) or "") in {"covered", "no_change"} for item in matching)
            has_node = any(str(item.get("scenario_id") or "") == scenario_id and str(item.get("surface") or "") == surface for item in nodes)
            if not covered or not has_node:
                findings.append(_finding("required_surface_missing", f"{scenario_id}:{surface}", "Required surface has no covered Design node."))
    for item in _dict_items(graph.get("unresolved_items"), code="vertical_design_graph_invalid", run_id=run_id):
        if str(item.get("owner") or "") in {"requirement", "human"}:
            findings.append(_finding("human_decision_required", str(item.get("id") or "unresolved"), str(item.get("message") or "Design requires a human decision.")))
    for node in nodes:
        if str(node.get("surface") or "") != "ui":
            continue
        surface_contract = _dict_value(node.get("surface_contract"))
        if not _dict_value(surface_contract.get("experience_contract")) and not is_composition_wiring(node):
            findings.append(
                _finding(
                    "ui_experience_contract_missing",
                    str(node.get("id") or "ui"),
                    f"UI node has no {UI_EXPERIENCE_CONTRACT_V2} backed by observed UI/UX guidance.",
                )
            )
        if not _dict_value(surface_contract.get("runtime_surface")):
            findings.append(
                _finding(
                    "ui_runtime_surface_missing",
                    str(node.get("id") or "ui"),
                    f"UI node has no {RUNTIME_SURFACE_BINDING_VERSION} proving entrypoint composition and required inputs.",
                )
            )
    findings.extend(
        _runtime_composition_findings(
            graph,
            required_surfaces=required_surfaces,
            landscape=landscape,
        )
    )
    return findings


def _runtime_composition_findings(
    graph: dict[str, object],
    *,
    required_surfaces: dict[str, set[str]] | None,
    landscape: dict[str, object] | None,
) -> list[dict[str, object]]:
    run_id = str(graph.get("run_id") or "")
    nodes = _dict_items(graph.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id)
    edges = _dict_items(graph.get("edges"), code="vertical_design_graph_invalid", run_id=run_id)
    partitions = _dict_items(graph.get("partitions"), code="vertical_design_graph_invalid", run_id=run_id)
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    anchors = {
        str(item.get("anchor_id") or ""): item
        for item in _dict_items((landscape or {}).get("anchors", []), code="vertical_design_graph_invalid", run_id=run_id)
        if str(item.get("anchor_id") or "")
    }
    component_roots = {
        str(item.get("component_id") or ""): _strings(item.get("roots"))
        for item in _dict_items((landscape or {}).get("components", []), code="vertical_design_graph_invalid", run_id=run_id)
    }
    findings: list[dict[str, object]] = []
    ui_components = _dict_value(
        _dict_value(_dict_value(graph.get("snapshot_binding")).get("ui_repo_context")).get("components")
    )

    for partition in partitions:
        scenario_id = str(partition.get("scenario_id") or "")
        component_id = str(partition.get("component_id") or "")
        materialized = any(
            str(value or "") in {"covered", "no_change"}
            for value in _dict_value(partition.get("coverage")).values()
        ) or any(
            str(node.get("scenario_id") or "") == scenario_id
            and str(node.get("component_id") or "") == component_id
            for node in nodes
        )
        if not materialized:
            continue
        if not _strings(partition.get("source_observation_refs")):
            findings.append(
                _composition_finding(
                    "design_investigation_missing",
                    f"{scenario_id}@{component_id}",
                    "Component has no successful repo.read or available CodeGraph source observation.",
                    "Read the composition owner source or its CodeGraph call path before proposing this component delta.",
                    scenario_id=scenario_id,
                    component_id=component_id,
                )
            )

    realized_targets = {
        str(edge.get("from") or ""): str(edge.get("to") or "")
        for edge in edges
        if str(edge.get("kind") or "") == "realized_by"
    }
    for node in nodes:
        node_id = str(node.get("id") or "")
        scenario_id = str(node.get("scenario_id") or "")
        binding = _dict_value(node.get("runtime_binding"))
        if not binding:
            target_id = realized_targets.get(node_id, "")
            target_binding = _dict_value(nodes_by_id.get(target_id, {}).get("runtime_binding"))
            if str(node.get("kind") or "") != "interaction_step" or not target_binding:
                findings.append(
                    _composition_finding(
                        "runtime_binding_missing",
                        node_id,
                        f"Design node {node_id} is not bound to a runtime target.",
                        "Add runtime_binding_v1 or realize a pure interaction_step through a runtime-bound node.",
                        node=node,
                    )
                )
            continue

        target = _dict_value(binding.get("target"))
        attachment = _dict_value(binding.get("attachment"))
        component_id = str(node.get("component_id") or "")
        target_path = str(target.get("path") or "")
        runtime_kind = str(binding.get("runtime_kind") or "")
        relation = str(attachment.get("relation") or "")
        roots = component_roots.get(component_id, [])
        if roots and not any(
            root == "." or target_path == root or target_path.startswith(f"{root}/")
            for root in roots
        ):
            findings.append(
                _composition_finding(
                    "component_ownership_invalid",
                    node_id,
                    f"Runtime target {target_path} is outside component {component_id} roots.",
                    "Choose a target path owned by the frozen component profile.",
                    node=node,
                )
            )
        if relation not in RUNTIME_ATTACHMENT_RELATIONS.get(runtime_kind, frozenset()):
            findings.append(
                _composition_finding(
                    "runtime_attachment_missing",
                    node_id,
                    f"Runtime attachment relation {relation or '<missing>'} is incompatible with {runtime_kind or '<missing>'}.",
                    "Use the attachment relation allowed for the selected runtime kind and name its composition owner.",
                    node=node,
                )
            )
        owner_ref = str(attachment.get("owner_ref") or "")
        owner_valid = False
        if owner_ref in nodes_by_id:
            owner_node = nodes_by_id[owner_ref]
            owner_valid = (
                bool(_dict_value(owner_node.get("runtime_binding")))
                and str(owner_node.get("component_id") or "") == component_id
            )
        elif owner_ref in anchors:
            owner = anchors[owner_ref]
            owner_valid = str(owner.get("component_id") or "") == component_id
            owner_path = str(attachment.get("owner_path") or "")
            owner_symbol = str(attachment.get("owner_symbol") or "")
            owner_valid = owner_valid and (not owner_path or owner_path == str(owner.get("path") or ""))
            owner_valid = owner_valid and (not owner_symbol or owner_symbol == str(owner.get("symbol") or ""))
        elif owner_ref.startswith("RLA-") and not anchors:
            owner_valid = True
        elif ui_components:
            # Non-exported components (e.g. an app Root) have no landscape anchor but are frozen in ui_repo_context;
            # the Designer prompt offers their chain ref as owner_ref, whatever the UI component is called.
            owner_key = f"{attachment.get('owner_path') or ''}#{attachment.get('owner_symbol') or ''}"
            ui_component = _dict_value(ui_components.get(owner_key))
            owner_valid = bool(ui_component) and owner_ref in {owner_key, str(ui_component.get("evidence_ref") or "")}
        if not owner_valid:
            findings.append(
                _composition_finding(
                    "runtime_attachment_missing",
                    node_id,
                    f"Runtime target for {node_id} has no valid composition owner.",
                    "Attach it to a frozen repo anchor or another runtime-bound Design node.",
                    node=node,
                )
            )
        elif relation == "called_by":
            off_caller = _called_by_owner_off_caller(node, attachment, nodes_by_id=nodes_by_id, edges=edges, anchors=anchors)
            if off_caller:
                findings.append(
                    _composition_finding(
                        "runtime_attachment_off_caller",
                        node_id,
                        f"runtime_binding.attachment of {node_id} says it is called_by {off_caller['owner']}, but the "
                        f"calls edge(s) {', '.join(off_caller['edges'])} into it come from {', '.join(off_caller['callers'])}.",
                        "A called_by owner is the caller: set attachment owner_ref/owner_path/owner_symbol to the runtime "
                        "target of the node that calls it, or correct the calls edge.",
                        node=node,
                    )
                )
        if str(node.get("disposition") or "") == "add":
            target_symbol = str(target.get("symbol") or "")
            matches_existing_target = any(
                str(anchor.get("component_id") or "") == component_id
                and str(anchor.get("path") or "") == target_path
                and str(anchor.get("symbol") or "") == target_symbol
                for anchor in anchors.values()
            )
            duplicates_owner = (
                bool(str(attachment.get("owner_path") or ""))
                and str(attachment.get("owner_path") or "") == target_path
                and str(attachment.get("owner_symbol") or "") == target_symbol
            )
            if matches_existing_target or duplicates_owner:
                findings.append(
                    _composition_finding(
                        "planned_target_conflicts_as_is",
                        node_id,
                        f"Add node {node_id} points at an existing AS-IS target or duplicates its composition owner.",
                        "Choose a concrete new target path/symbol and attach it to the existing owner, or change the disposition to modify/reuse.",
                        node=node,
                    )
                )

    api_operations: dict[tuple[str, str], list[dict[str, object]]] = {}
    for node in nodes:
        if (
            str(node.get("surface") or "") != "api"
            or str(node.get("disposition") or "") not in {"add", "modify"}
        ):
            continue
        surface_contract = _dict_value(node.get("surface_contract"))
        method = str(surface_contract.get("method") or "").strip().upper()
        path = str(surface_contract.get("path") or "").strip()
        binding = _dict_value(node.get("runtime_binding"))
        target = _dict_value(binding.get("target"))
        target_path = str(target.get("path") or "").strip()
        target_symbol = str(target.get("symbol") or "").strip()
        if method and path and target_path:
            api_operations.setdefault((method, path), []).append(node)

    for (method, path), members in sorted(api_operations.items()):
        ordered = sorted(
            members,
            key=lambda item: (
                str(item.get("scenario_id") or ""),
                str(item.get("id") or ""),
            ),
        )
        canonical = ordered[0]
        canonical_target = _dict_value(_dict_value(canonical.get("runtime_binding")).get("target"))
        canonical_key = (
            str(canonical_target.get("path") or "").strip(),
            str(canonical_target.get("symbol") or "").strip(),
        )
        for conflict in ordered[1:]:
            conflict_target = _dict_value(_dict_value(conflict.get("runtime_binding")).get("target"))
            conflict_key = (
                str(conflict_target.get("path") or "").strip(),
                str(conflict_target.get("symbol") or "").strip(),
            )
            if conflict_key == canonical_key:
                continue
            operation = f"{method} {path}"
            findings.append(
                _composition_finding(
                    "duplicate_runtime_api_operation",
                    str(conflict.get("id") or operation),
                    (
                        f"API operation {operation} is assigned to conflicting runtime targets "
                        f"{canonical_key[0]}#{canonical_key[1]} and "
                        f"{conflict_key[0]}#{conflict_key[1]}."
                    ),
                    (
                        "Reuse the approved runtime target for this operation or change the "
                        "method/path so each registered route is unique."
                    ),
                    node=conflict,
                )
            )

    checked_operation_bindings: set[tuple[str, str, str]] = set()
    checked_runtime_boundaries: set[tuple[str, str, str]] = set()
    for edge in edges:
        source = nodes_by_id.get(str(edge.get("from") or ""), {})
        target = nodes_by_id.get(str(edge.get("to") or ""), {})
        if not source or not target or str(edge.get("kind") or "") in {"contains", "depends_on", "realized_by"}:
            continue
        crosses_boundary = str(source.get("component_id") or "") != str(target.get("component_id") or "")
        contract = _dict_value(edge.get("boundary_contract"))
        # Same-component calls are checked only when they declare a contract; older graphs omit it there.
        if crosses_boundary or contract:
            provider = source if str(edge.get("kind") or "") == "returns" else target
            consumer = target if str(edge.get("kind") or "") == "returns" else source
            provider_target = _dict_value(_dict_value(provider.get("runtime_binding")).get("target"))
            consumer_target = _dict_value(_dict_value(consumer.get("runtime_binding")).get("target"))
            provider_path = str(provider_target.get("path") or "")
            consumer_path = str(consumer_target.get("path") or "")
            provider_app = provider_path.split("/", 2)[:2] if provider_path.startswith("apps/") else []
            consumer_app = consumer_path.split("/", 2)[:2] if consumer_path.startswith("apps/") else []
            boundary_key = (
                str(edge.get("scenario_id") or ""),
                str(provider.get("id") or ""),
                str(consumer.get("component_id") or ""),
            )
            if (
                crosses_boundary
                and provider_app
                and consumer_app
                and provider_app != consumer_app
                and str(provider.get("surface") or "") != "api"
                and boundary_key not in checked_runtime_boundaries
            ):
                checked_runtime_boundaries.add(boundary_key)
                findings.append(
                    _composition_finding(
                        "runtime_boundary_unreachable",
                        str(provider.get("id") or edge.get("id") or ""),
                        (
                            f"Runtime path {consumer_path} cannot directly call non-API provider "
                            f"{provider_path} across application components."
                        ),
                        (
                            "Make the call executable without inventing transport: keep caller-local logic in the "
                            "caller application or an importable shared package, or use an explicit API/transport "
                            "surface only when the approved scenario requires one."
                        ),
                        node=provider,
                    )
                )
            diagnostics = _boundary_contract_diagnostics(contract, provider)
            if str(edge.get("kind") or "") == "returns" and contract:
                paired = _paired_request_edge(edge, edges)
                paired_inputs = _dict_value(paired.get("boundary_contract")).get("input_mapping")
                if isinstance(paired_inputs, list) and _mapping_pairs(paired_inputs) != _mapping_pairs(
                    contract.get("input_mapping")
                ):
                    diagnostics["paired_edge_id"] = str(paired.get("id") or "")
                    diagnostics["paired_input_mapping"] = paired_inputs
                    diagnostics["mismatches"].append(
                        f"copy input_mapping (consumer field -> provider field) from paired "
                        f"{paired.get('kind')} edge {paired.get('id')}; only response_mapping and "
                        "error_mapping flow provider -> consumer"
                    )
            if diagnostics["mismatches"]:
                expected_change = "; ".join(str(item) for item in diagnostics["mismatches"])
                finding = _composition_finding(
                    "boundary_contract_mismatch",
                    str(edge.get("id") or ""),
                    f"Boundary contract mismatch: {expected_change}.",
                    expected_change + ".",
                    scenario_id=str(edge.get("scenario_id") or ""),
                    component_id=str(source.get("component_id") or ""),
                )
                finding["contract_diagnostics"] = diagnostics
                findings.append(finding)
            operation_diagnostics = _runtime_operation_binding_diagnostics(contract, provider)
            if operation_diagnostics:
                operation_key = (
                    str(provider.get("id") or ""),
                    str(operation_diagnostics["expected_provider_operation"]),
                    str(operation_diagnostics["actual_target_symbol"]),
                )
                if operation_key not in checked_operation_bindings:
                    checked_operation_bindings.add(operation_key)
                    expected_operation = str(operation_diagnostics["expected_provider_operation"])
                    actual_symbol = str(operation_diagnostics["actual_target_symbol"])
                    target_path = str(operation_diagnostics["target_path"])
                    finding = _composition_finding(
                        "runtime_operation_binding_mismatch",
                        str(provider.get("id") or ""),
                        (
                            f"Boundary provider operation {expected_operation} is bound to "
                            f"{target_path}#{actual_symbol}."
                        ),
                        (
                            f"Keep the target path when appropriate, but bind this provider node "
                            f"to the callable symbol {expected_operation}."
                        ),
                        node=provider,
                    )
                    finding["contract_diagnostics"] = operation_diagnostics
                    findings.append(finding)

    ui_findings, ui_reachable_from_entry = _ui_runtime_surface_findings(graph, nodes)
    findings.extend(ui_findings)

    inferred = required_surfaces or _required_surfaces_from_partitions(partitions)
    # A surface declared not exercised has no node to reach (the scenario ends before it).
    not_exercised = {
        (str(item.get("scenario_id") or ""), str(item.get("surface") or ""))
        for item in _dict_items(graph.get("surface_not_exercised", []), code="vertical_design_graph_invalid", run_id=run_id)
    }
    for scenario_id, all_surfaces in sorted(inferred.items()):
        surfaces = {surface for surface in all_surfaces if (scenario_id, surface) not in not_exercised}
        scenario_nodes = [item for item in nodes if str(item.get("scenario_id") or "") == scenario_id]
        if not scenario_nodes:
            continue
        starts = [
            item
            for item in scenario_nodes
            if str(_dict_value(item.get("runtime_binding")).get("runtime_kind") or "")
            in (
                {"entrypoint", "ui_component"}
                if "ui" in surfaces
                else {"entrypoint", "api_route"}
                if "api" in surfaces
                else {"entrypoint", "service", "persistence_adapter", "data_model"}
            )
            # A UI component only starts a path once its composition proves app reachability.
            and not (
                str(item.get("surface") or "") == "ui"
                and str(item.get("id") or "") not in ui_reachable_from_entry
            )
        ]
        if not starts:
            findings.append(
                _composition_finding(
                    "runtime_path_disconnected",
                    scenario_id,
                    "Scenario has no runtime entrypoint for its required surfaces.",
                    "Bind a reachable UI or backend entrypoint and connect required surfaces to an observable outcome.",
                    scenario_id=scenario_id,
                    component_id=str(scenario_nodes[0].get("component_id") or ""),
                )
            )
            continue
        reachable = _reachable_node_ids(starts=starts, edges=edges, scenario_id=scenario_id)
        missing = [
            surface
            for surface in sorted(surfaces)
            if not any(str(item.get("surface") or "") == surface and str(item.get("id") or "") in reachable for item in scenario_nodes)
        ]
        if missing or (len(surfaces) > 1 and not any(bool(item.get("terminal")) for item in edges if str(item.get("scenario_id") or "") == scenario_id)):
            findings.append(
                _composition_finding(
                    "runtime_path_disconnected",
                    scenario_id,
                    f"Required runtime path is disconnected; unreachable surfaces: {missing or ['observable outcome']}.",
                    "Connect the scenario entrypoint through every required surface and mark its observable terminal edge.",
                    scenario_id=scenario_id,
                    component_id=str(starts[0].get("component_id") or ""),
                )
            )
    return findings


def _ui_runtime_surface_findings(
    graph: dict[str, object],
    nodes: list[dict[str, object]],
) -> tuple[list[dict[str, object]], set[str]]:
    """Prove each v2 UI surface is reachable from the app entrypoint with every required input owned."""

    binding = _dict_value(graph.get("snapshot_binding"))
    context = _dict_value(binding.get("ui_repo_context"))
    requirement_ids = set(_strings(binding.get("requirement_reference_ids")))
    approved_decision_ids = set(_strings(binding.get("approved_decision_ids")))
    entry = str(_dict_value(context.get("entrypoint")).get("ref") or "")
    chain = {
        str(hop.get("ref") or ""): hop
        for hop in context.get("composition_chain", [])
        if isinstance(hop, dict)
    }
    components = _dict_value(context.get("components"))
    providers = {
        str(item.get("ref") or "")
        for item in context.get("data_clients", [])
        if isinstance(item, dict)
    } | set(chain) | set(components)
    node_ids = {str(node.get("id") or "") for node in nodes}
    planned_targets: dict[str, dict[str, object]] = {}
    for node in nodes:
        target = _dict_value(_dict_value(node.get("runtime_binding")).get("target"))
        ref = f"{target.get('path') or ''}#{target.get('symbol') or ''}"
        if ref != "#":
            planned_targets.setdefault(ref, node)
    findings: list[dict[str, object]] = []
    reachable: set[str] = set()
    if not entry:
        if not any(str(node.get("surface") or "") == "ui" for node in nodes):
            return findings, reachable
        findings.append(
            _composition_finding(
                "ui_entrypoint_unresolved",
                "ui_repo_context",
                "Frozen web app has no resolvable entrypoint root component.",
                "Escalate to the project owner; the fixed profile requires a web entrypoint that renders a root component.",
            )
        )
        return findings, reachable

    for node in nodes:
        if str(node.get("surface") or "") != "ui":
            continue
        node_id = str(node.get("id") or "")
        surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
        if not surface:
            continue
        runtime = _dict_value(node.get("runtime_binding"))
        target = _dict_value(runtime.get("target"))
        attachment = _dict_value(runtime.get("attachment"))
        target_ref = f"{target.get('path') or ''}#{target.get('symbol') or ''}"
        path = _strings(surface.get("composition_path"))
        entry_ok = bool(path) and path[0] == entry and path[-1] == target_ref
        if not entry_ok:
            findings.append(
                _composition_finding(
                    "ui_entrypoint_unreachable",
                    node_id,
                    f"UI surface {node_id} composition_path must run from {entry} to its runtime target {target_ref}.",
                    f"Set composition_path to start at {entry} and end at {target_ref}, listing every mounting component between them.",
                    node=node,
                )
            )
        invalid_hops = [hop for hop in path[1:-1] if hop not in chain and hop not in planned_targets]
        for parent, child in zip(path, path[1:]):
            parent_node = planned_targets.get(parent)
            parent_modified = parent_node is not None and str(parent_node.get("disposition") or "") in {"modify", "add"}
            if parent in chain and chain[parent].get("renders_status") == "unknown" and not parent_modified:
                invalid_hops.append(f"{parent}:render evidence unavailable")
            # Two AS-IS hops must already be a real render edge unless the Design modifies the parent to mount it.
            if parent in chain and child in chain and child not in _strings(chain[parent].get("renders")) and not parent_modified:
                invalid_hops.append(f"{parent}->{child}")
        for hop in invalid_hops:
            findings.append(
                _composition_finding(
                    "ui_composition_hop_invalid",
                    f"{node_id}:{hop}",
                    f"Composition hop {hop} is neither in the frozen web composition chain nor a runtime target in this Design.",
                    "Use an AS-IS component from ui_repo_context.composition_chain or add a runtime-bound UI node for the new mounting component.",
                    node=node,
                )
            )
        owner_ref = f"{attachment.get('owner_path') or ''}#{attachment.get('owner_symbol') or ''}"
        if len(path) >= 2 and owner_ref != "#" and owner_ref != path[-2]:
            findings.append(
                _composition_finding(
                    "ui_attachment_off_path",
                    node_id,
                    f"runtime_binding owner {owner_ref} is not the component that renders {target_ref} on composition_path ({path[-2]}).",
                    f"Attach the UI target to {path[-2]} or correct composition_path.",
                    node=node,
                )
            )
        if entry_ok and not invalid_hops:
            reachable.add(node_id)

        inputs = [item for item in surface.get("required_inputs", []) if isinstance(item, dict)]
        declared = {str(item.get("name") or "") for item in inputs}
        if str(node.get("disposition") or "") in {"modify", "reuse", "no_change"}:
            component = _dict_value(components.get(target_ref))
            unknown = component.get("props_status") == "unknown" or (
                context.get("extraction") and not component
            )
            if unknown:
                findings.append(_composition_finding(
                    "ui_source_facts_unresolved", node_id,
                    f"Required props for {target_ref} could not be extracted from the frozen source.",
                    "Investigate the target source or CodeGraph; report an evidence blocker if its contract remains unresolved.",
                    node=node,
                ))
            missing = [
                str(prop.get("name") or "")
                for prop in (component.get("props") or [])
                if isinstance(prop, dict)
                and not prop.get("optional")
                and not prop.get("has_default")
                and str(prop.get("name") or "") not in declared
            ]
            if missing:
                findings.append(
                    _composition_finding(
                        "ui_required_input_undeclared",
                        node_id,
                        f"AS-IS component {target_ref} requires props {missing} that runtime_surface.required_inputs does not declare.",
                        "Declare each required prop with its source, binding and provided_by hop.",
                        node=node,
                    )
                )
        has_external = False
        has_fixture = False
        for item in inputs:
            name = str(item.get("name") or "")
            source = str(item.get("source") or "")
            bound_to = str(item.get("binding") or "")
            refs = {*_strings(item.get("requirement_refs")), bound_to}
            provided_by = str(item.get("provided_by") or "")
            problem = ""
            if source == "existing_runtime" and bound_to not in providers:
                problem = f"existing_runtime binding {bound_to} is not a provider in the frozen web app"
            elif source == "new_boundary" and bound_to not in node_ids:
                problem = f"new_boundary binding {bound_to} is not a node in this Design graph"
            elif source == "external_use_case" and not any(
                ref.startswith(EXTERNAL_DEPENDENCY_REF_PREFIXES) and ref in requirement_ids for ref in refs
            ):
                problem = (
                    "external_use_case must cite the approved Requirement precondition (PRE-*) or flow step "
                    "(MF-*/AF-*/EF-*) that states the dependency on the other use case"
                )
            elif source == "demo_fixture" and not any(
                ref.startswith("D-") and ref in requirement_ids and ref in approved_decision_ids for ref in refs
            ):
                problem = "demo_fixture must cite an accepted Requirement decision (D-*)"
            elif path and provided_by == path[-1]:
                problem = (
                    "it is state owned by the target component itself, not a runtime input; remove it from "
                    "required_inputs and model it in runtime_states/transitions instead"
                )
            elif provided_by not in path[:-1]:
                problem = f"provided_by {provided_by} is not a hop before the target on composition_path"
            if problem:
                findings.append(
                    _composition_finding(
                        "ui_required_input_unbound",
                        f"{node_id}:{name}",
                        f"Required input {name}: {problem}.",
                        "Bind the input to an existing provider, a Design node, an approved Requirement PRE-*/D-* reference, and a provided_by hop on composition_path.",
                        node=node,
                    )
                )
                continue
            has_external = has_external or source == "external_use_case"
            has_fixture = has_fixture or source == "demo_fixture"
            # shortcut: matches the prop name per hop; renamed props across hops need an explicit modify node.
            start = path.index(provided_by)
            for index in range(start, len(path) - 1):
                hop, child = path[index], path[index + 1]
                passed = _strings(_dict_value(_dict_value(chain.get(hop)).get("passes_props")).get(child))
                hop_node = planned_targets.get(hop)
                modeled = (
                    hop_node is not None
                    and str(hop_node.get("disposition") or "") in {"modify", "add"}
                    and _composition_wiring_forwards(
                        hop_node,
                        screen_id=str(surface.get("screen_id") or ""),
                        child=child,
                        name=name,
                    )
                )
                if hop in chain and name not in passed and not modeled:
                    findings.append(
                        _composition_finding(
                            "composition_hop_unmodeled",
                            f"{node_id}:{name}:{hop}",
                            f"AS-IS {hop} does not pass {name} to {child}, and its Harness-owned wiring does not declare that input for screen {surface.get('screen_id')}.",
                            f"Materialize {name} in the composition_wiring forwarded_inputs for {hop} -> {child} and screen {surface.get('screen_id')}.",
                            node=node,
                        )
                    )
        status = str(surface.get("runnable_status") or "")
        expected = "externally_blocked" if has_external else "fixture_required" if has_fixture else "bound"
        if status != expected:
            findings.append(
                _composition_finding(
                    "ui_runnable_status_inconsistent",
                    node_id,
                    f"runnable_status {status} does not match its required inputs; expected {expected}.",
                    f"Set runnable_status to {expected}.",
                    node=node,
                )
            )
        experience = _dict_value(_dict_value(node.get("surface_contract")).get("experience_contract"))
        visual = _dict_value(experience.get("visual_contract"))
        styles = _dict_value(context.get("styles"))
        if visual.get("style_target_policy") == "reuse_existing" and not _strings(styles.get("stylesheet_files")):
            findings.append(
                _composition_finding(
                    "ui_style_policy_unsupported",
                    node_id,
                    "visual_contract.style_target_policy=reuse_existing but the frozen web app has no stylesheet to reuse.",
                    "Choose new_stylesheet and explain the visual convention it introduces.",
                    node=node,
                )
            )
        context_refs = set(_strings(context.get("evidence_refs")))
        if experience and not context_refs.intersection(_strings(experience.get("repo_evidence_refs"))):
            findings.append(
                _composition_finding(
                    "ui_visual_evidence_missing",
                    node_id,
                    "UI experience decisions cite no evidence from the frozen ui_repo_context.",
                    "Add the ui_repo_context evidence refs (entrypoint, shell, style or data conventions) the decisions rely on.",
                    node=node,
                )
            )

    screens: dict[str, list[tuple[dict[str, object], list[str]]]] = {}
    for node in nodes:
        surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
        if str(node.get("surface") or "") == "ui" and surface and not is_composition_wiring(node):
            screens.setdefault(str(surface.get("screen_id") or ""), []).append(
                (node, _strings(surface.get("composition_path")))
            )
    for screen_id, members in sorted(screens.items()):
        root_path = min((path for _, path in members), key=len)
        for node, path in members:
            if path[: len(root_path)] != root_path:
                findings.append(
                    _composition_finding(
                        "ui_screen_inconsistent",
                        f"{screen_id}:{node.get('id')}",
                        f"UI node {node.get('id')} shares screen {screen_id} but does not mount under {' -> '.join(root_path)}.",
                        "Give nodes on one screen a shared composition root, or use a different screen_id for a separate screen.",
                        node=node,
                    )
                )
        findings.extend(_screen_visual_findings(screen_id, [node for node, _ in members]))
    return findings, reachable


def _screen_visual_findings(screen_id: str, members: list[dict[str, object]]) -> list[dict[str, object]]:
    """Scenario nodes of one screen describe the same page: style target and each region's layer must agree."""

    findings: list[dict[str, object]] = []
    ordered = sorted(members, key=lambda item: (str(item.get("scenario_id") or ""), str(item.get("id") or "")))
    first_policy: tuple[str, str] | None = None
    first_layer: dict[str, tuple[str, str, str]] = {}
    for node in ordered:
        node_id = str(node.get("id") or "")
        visual = _dict_value(_dict_value(_dict_value(node.get("surface_contract")).get("experience_contract")).get("visual_contract"))
        policy = str(visual.get("style_target_policy") or "")
        if policy and first_policy is None:
            first_policy = (policy, node_id)
        elif policy and first_policy is not None and policy != first_policy[0]:
            findings.append(
                _composition_finding(
                    "ui_screen_style_inconsistent",
                    f"{screen_id}:{node_id}:style_target_policy",
                    f"UI node {node_id} sets style_target_policy={policy} but {first_policy[1]} on the same screen "
                    f"{screen_id} sets {first_policy[0]}.",
                    f"Use style_target_policy={first_policy[0]} like the other nodes of screen {screen_id}.",
                    node=node,
                )
            )
        for region, layer, _ in layout_regions(visual):
            key = region.lower()
            owner = first_layer.setdefault(key, (layer, node_id, region))
            if owner[0] != layer:
                findings.append(
                    _composition_finding(
                        "ui_screen_layout_inconsistent",
                        f"{screen_id}:{node_id}:{key}",
                        f"UI node {node_id} places region '{region}' on layer {layer} but {owner[1]} on the same "
                        f"screen {screen_id} places it on layer {owner[0]}.",
                        f"Put region '{owner[2]}' on layer {owner[0]}, or rename it if it is a different region.",
                        node=node,
                    )
                )
    return findings


def _mapping_pairs(value: object) -> set[tuple[str, str]]:
    items = value if isinstance(value, list) else []
    return {
        (str(item.get("from") or "").strip(), str(item.get("to") or "").strip())
        for item in items
        if isinstance(item, dict)
    }


def _called_by_owner_off_caller(
    node: dict[str, object],
    attachment: dict[str, object],
    *,
    nodes_by_id: dict[str, dict[str, object]],
    edges: list[dict[str, object]],
    anchors: dict[str, dict[str, object]],
) -> dict[str, list[str] | str]:
    """A called_by owner that is none of the nodes calling this node (r7 SC-1 and every eTour rule: owner createApp)."""

    node_id = str(node.get("id") or "")
    callers = [
        (str(edge.get("id") or ""), nodes_by_id.get(str(edge.get("from") or ""), {}))
        for edge in edges
        if str(edge.get("kind") or "") == "calls" and str(edge.get("to") or "") == node_id
    ]
    if not callers:
        return {}
    owner_ref = str(attachment.get("owner_ref") or "")
    if owner_ref in nodes_by_id:
        target = _dict_value(_dict_value(nodes_by_id[owner_ref].get("runtime_binding")).get("target"))
        owner = (str(target.get("path") or ""), str(target.get("symbol") or ""))
    elif owner_ref in anchors:
        owner = (str(anchors[owner_ref].get("path") or ""), str(anchors[owner_ref].get("symbol") or ""))
    else:
        owner = (str(attachment.get("owner_path") or ""), str(attachment.get("owner_symbol") or ""))

    def target_of(caller: dict[str, object]) -> tuple[str, str]:
        target = _dict_value(_dict_value(caller.get("runtime_binding")).get("target"))
        return (str(target.get("path") or ""), str(target.get("symbol") or ""))

    # One matching caller is enough: a shared rule may be called by several nodes but names one owner.
    if any(str(caller.get("id") or "") == owner_ref or target_of(caller) == owner for _, caller in callers):
        return {}
    return {
        "owner": f"{owner[0]}#{owner[1]}",
        "edges": [edge_id for edge_id, _ in callers],
        "callers": [f"{caller.get('id')} ({'#'.join(target_of(caller))})" for _, caller in callers],
    }


ESTABLISHED_NOT_PROVEN = (
    "whether each mapped value exists on this path at the moment it is used",
    "whether node prose and edge prose state the same contract (for example the same Authorization scheme)",
    "whether each node's runtime_binding (target, attachment owner and relation) fits the edges that reach it",
    "whether a decision's logic and outcomes satisfy the claims",
    "whether a promised mechanism (rollback, duplicate guard, atomicity, unchanged state) has a runtime mechanism",
)


def established_facts(
    graph: dict[str, object],
    *,
    scenario_ids: list[str],
    edge_ids: list[str],
) -> dict[str, object]:
    """What the deterministic checks prove for one semantic review packet (r7: reviewers re-derived them at length).

    Each fact is computed here with the same checks the Harness gates use and is stated only when that check passes
    for that exact edge or scenario; nothing is asserted from the pipeline order alone.
    """

    run_id = str(graph.get("run_id") or "")
    nodes = [item for item in graph.get("nodes") or [] if isinstance(item, dict)]
    edges = [item for item in graph.get("edges") or [] if isinstance(item, dict)]
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    design: list[str] = []
    try:
        validate_vertical_design_graph(graph, expected_run_id=run_id)
        design.append(
            "graph schema, stable ids, node kinds and surfaces, dispositions with their evidence and partition "
            "coverage are valid"
        )
    except HarnessError:
        pass

    boundaries: dict[str, list[str]] = {}
    wanted = set(edge_ids)
    for edge in edges:
        edge_id = str(edge.get("id") or "")
        contract = _dict_value(edge.get("boundary_contract"))
        if edge_id not in wanted or not contract:
            continue
        source = nodes_by_id.get(str(edge.get("from") or ""), {})
        target = nodes_by_id.get(str(edge.get("to") or ""), {})
        if not source or not target:
            continue
        returns = str(edge.get("kind") or "") == "returns"
        provider = source if returns else target
        diagnostics = _boundary_contract_diagnostics(contract, provider)
        if diagnostics["mismatches"] or _runtime_operation_binding_diagnostics(contract, provider):
            continue
        facts: list[str] = []
        if str(provider.get("surface") or "") == "api":
            facts.append(
                f"provider_operation {diagnostics['actual_provider_operation']} is the provider's declared method and path"
            )
            if diagnostics["required_input_targets"]:
                facts.append(
                    "every request field is mapped as a required target by name: "
                    + ", ".join(diagnostics["required_input_targets"])
                )
            if diagnostics["authorization_required"]:
                facts.append("auth_mapping is declared (the provider requires authentication or authorization)")
            if _dict_items(contract.get("response_mapping", []), code="vertical_design_graph_invalid", run_id=run_id):
                facts.append("response_mapping is declared")
            if diagnostics["error_response_statuses"]:
                facts.append(
                    "error_mapping is declared for the provider's error statuses "
                    + ", ".join(diagnostics["error_response_statuses"])
                )
        elif str(provider.get("surface") or "") == "business_rule" and diagnostics.get("required_input_targets"):
            facts.append(
                "every declared rule input is mapped as a required target by name: "
                + ", ".join(_strings(diagnostics.get("required_input_targets")))
            )
        binding = _dict_value(provider.get("runtime_binding"))
        operation = str(contract.get("provider_operation") or "").strip()
        if str(binding.get("runtime_kind") or "") in {"service", "persistence_adapter"} and _TYPESCRIPT_IDENTIFIER.fullmatch(
            operation
        ):
            # The same condition under which _runtime_operation_binding_diagnostics checked it (and it passed above).
            facts.append(f"provider_operation {operation} is the callable the provider is bound to")
        if returns:
            paired = _paired_request_edge(edge, edges)
            if paired and _mapping_pairs(_dict_value(paired.get("boundary_contract")).get("input_mapping")) == _mapping_pairs(
                contract.get("input_mapping")
            ):
                facts.append(f"input_mapping repeats paired {paired.get('kind')} edge {paired.get('id')}")
        if facts:
            boundaries[edge_id] = facts

    scenario: list[str] = []
    sequence = _sequence_behavior_findings(nodes=nodes, edges=edges)
    for scenario_id in scenario_ids:
        scoped = [item for item in nodes if str(item.get("scenario_id") or "") == scenario_id]
        if not scoped:
            continue
        if any(str(item.get("surface") or "") == "ui" for item in scoped) and not any(
            str(item.get("subject_ref") or "") == scenario_id for item in sequence
        ):
            scenario.append(
                f"{scenario_id}: the ordered behavior edges form one continuous path with exactly one terminal edge, last"
            )
    return {
        "design": design,
        "boundaries": boundaries,
        "scenario": scenario,
        "not_established": list(ESTABLISHED_NOT_PROVEN),
    }


def _copy_paired_input_mappings(candidate: dict[str, object], *, before: dict[str, object]) -> None:
    """Give each new returns edge the input_mapping of its paired request edge.

    The boundary gate requires the two to be identical, so the copy is mechanical; Conduit UC4 H-r1 lost two
    Designer deltas to "copy input_mapping from paired calls edge". Existing edges are never changed.
    """

    existing = {str(item.get("id") or "") for item in before.get("edges") or [] if isinstance(item, dict)}
    edges = [item for item in candidate.get("edges") or [] if isinstance(item, dict)]
    for edge in edges:
        contract = edge.get("boundary_contract")
        if str(edge.get("kind") or "") != "returns" or str(edge.get("id") or "") in existing or not isinstance(contract, dict):
            continue
        paired_inputs = _dict_value(_paired_request_edge(edge, edges).get("boundary_contract")).get("input_mapping")
        if isinstance(paired_inputs, list):
            contract["input_mapping"] = json.loads(json.dumps(paired_inputs))


def _paired_request_edge(edge: dict[str, object], edges: list[dict[str, object]]) -> dict[str, object]:
    """Latest calls/reads/writes edge before this returns edge, from its consumer to its provider."""
    contract = _dict_value(edge.get("boundary_contract"))
    order = edge.get("order") if isinstance(edge.get("order"), int) else 0
    candidates = [
        item
        for item in edges
        if str(item.get("kind") or "") in {"calls", "reads", "writes"}
        and str(item.get("from") or "") == str(edge.get("to") or "")
        and str(item.get("to") or "") == str(edge.get("from") or "")
        and str(item.get("scenario_id") or "") == str(edge.get("scenario_id") or "")
        and _dict_value(item.get("boundary_contract"))
        and (not order or not isinstance(item.get("order"), int) or item["order"] < order)
    ]
    same_operation = [
        item
        for item in candidates
        if str(_dict_value(item.get("boundary_contract")).get("provider_operation") or "")
        == str(contract.get("provider_operation") or "")
    ]
    pool = same_operation or candidates
    return max(pool, key=lambda item: item.get("order") if isinstance(item.get("order"), int) else 0, default={})


def _boundary_contract_diagnostics(
    contract: dict[str, object], provider: dict[str, object]
) -> dict[str, object]:
    surface = _dict_value(provider.get("surface_contract"))
    mismatches: list[str] = []
    if not contract:
        mismatches.append("add boundary_contract")
    if str(provider.get("surface") or "") == "business_rule" and contract:
        mapped_all = {
            str(item.get("to") or "")
            for item in contract.get("input_mapping", [])
            if isinstance(item, dict) and str(item.get("to") or "")
        }
        mapped_required = {
            str(item.get("to") or "")
            for item in contract.get("input_mapping", [])
            if isinstance(item, dict) and bool(item.get("required"))
        }
        declared = [name for name in _strings(surface.get("inputs")) if name]
        missing = [name for name in declared if name not in mapped_required]
        undeclared = sorted(mapped_all - set(declared))
        if missing:
            mismatches.append(f"map required input targets {missing}")
        if undeclared:
            mismatches.append(f"remove undeclared input targets {undeclared}")
        return {
            "provider_surface": "business_rule",
            "required_input_targets": declared,
            "mapped_input_targets": sorted(mapped_all),
            "missing_input_targets": missing,
            "undeclared_input_targets": undeclared,
            "mismatches": mismatches,
        }
    if str(provider.get("surface") or "") != "api":
        return {"provider_surface": str(provider.get("surface") or ""), "mismatches": mismatches}
    expected = f"{str(surface.get('method') or '').upper()} {str(surface.get('path') or '')}".strip()
    actual = str(contract.get("provider_operation") or "").strip()
    if expected and actual.upper() != expected.upper():
        mismatches.append(f"set provider_operation to {expected}")
    fields = _dict_items(surface.get("request_fields", []), code="vertical_design_graph_invalid", run_id="")
    input_mappings = _dict_items(contract.get("input_mapping", []), code="vertical_design_graph_invalid", run_id="")
    # Every request field must be mapped; the mapping's own `required` says whether the consumer may omit it
    # (design_v19 designer-0007: Conduit's optional tagList mapped with required=false was rejected).
    mapped_targets = {str(item.get("to") or "") for item in input_mappings}
    required_targets = [str(item.get("name") or "") for item in fields if str(item.get("name") or "")]
    missing_targets = [name for name in required_targets if name not in mapped_targets]
    if missing_targets:
        # A 'to' must equal a request field name exactly; naming the near misses (e.g. 'slug (path)') shows what to fix.
        unmatched = sorted(
            {str(item.get("to") or "") for item in input_mappings if str(item.get("to") or "")} - set(required_targets)
        )
        mismatches.append(
            f"map required input targets {missing_targets}"
            + (f" (mapped targets that match no request field name: {unmatched})" if unmatched else "")
        )
    auth_required = (
        surface.get("authentication_required") is True
        or surface.get("authorization_required") is True
    )
    auth_missing = auth_required and not _dict_items(
        contract.get("auth_mapping", []), code="vertical_design_graph_invalid", run_id=""
    )
    if auth_missing:
        access = str(surface.get("authentication") or surface.get("authorization") or "").strip()
        mismatches.append(f"add auth_mapping for {access}")
    responses = _dict_items(surface.get("responses", []), code="vertical_design_graph_invalid", run_id="")
    error_statuses = [
        str(item.get("status") or "")
        for item in responses
        if str(item.get("status") or "").startswith(("4", "5"))
    ]
    # An error-only API (for example a pure 503 path) has no success response to map.
    has_success_response = any(
        not str(item.get("status") or "").startswith(("4", "5")) for item in responses
    )
    response_missing = has_success_response and not _dict_items(
        contract.get("response_mapping", []), code="vertical_design_graph_invalid", run_id=""
    )
    error_missing = bool(error_statuses) and not _dict_items(
        contract.get("error_mapping", []), code="vertical_design_graph_invalid", run_id=""
    )
    if response_missing:
        mismatches.append("add response_mapping")
    if error_missing:
        mismatches.append(f"add error_mapping for statuses {error_statuses}")
    return {
        "provider_surface": "api",
        "expected_provider_operation": expected,
        "actual_provider_operation": actual,
        "required_input_targets": required_targets,
        "mapped_input_targets": sorted(mapped_targets),
        "missing_input_targets": missing_targets,
        "authorization_required": auth_required,
        "auth_mapping_missing": auth_missing,
        "error_response_statuses": error_statuses,
        "response_mapping_missing": response_missing,
        "error_mapping_missing": error_missing,
        "mismatches": mismatches,
    }


def _runtime_operation_binding_diagnostics(
    contract: dict[str, object], provider: dict[str, object]
) -> dict[str, object] | None:
    """Match concrete service/data operations to their bound TypeScript callable."""

    binding = _dict_value(provider.get("runtime_binding"))
    runtime_kind = str(binding.get("runtime_kind") or "")
    if runtime_kind not in {"service", "persistence_adapter"}:
        return None
    expected = str(contract.get("provider_operation") or "").strip()
    if not _TYPESCRIPT_IDENTIFIER.fullmatch(expected):
        return None
    target = _dict_value(binding.get("target"))
    actual_symbol = str(target.get("symbol") or "").strip()
    actual_callable = actual_symbol.rsplit(".", 1)[-1]
    if actual_callable == expected:
        return None
    return {
        "provider_runtime_kind": runtime_kind,
        "expected_provider_operation": expected,
        "actual_target_symbol": actual_symbol,
        "actual_callable_symbol": actual_callable,
        "target_path": str(target.get("path") or ""),
    }


def _required_surfaces_from_partitions(partitions: list[dict[str, object]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for partition in partitions:
        scenario_id = str(partition.get("scenario_id") or "")
        for surface, status in _dict_value(partition.get("coverage")).items():
            if str(status) in {"covered", "no_change"}:
                result.setdefault(scenario_id, set()).add(str(surface))
    return result


def _reachable_node_ids(
    *,
    starts: list[dict[str, object]],
    edges: list[dict[str, object]],
    scenario_id: str,
) -> set[str]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        if str(edge.get("scenario_id") or "") != scenario_id:
            continue
        adjacency.setdefault(str(edge.get("from") or ""), set()).add(str(edge.get("to") or ""))
    reachable = {str(item.get("id") or "") for item in starts}
    pending = list(reachable)
    while pending:
        source = pending.pop()
        for target in adjacency.get(source, set()):
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    return reachable


def _composition_finding(
    category: str,
    subject: str,
    message: str,
    expected_change: str,
    *,
    node: dict[str, object] | None = None,
    scenario_id: str = "",
    component_id: str = "",
) -> dict[str, object]:
    finding_id = "COMP-" + hashlib.sha256(f"{category}\0{subject}".encode("utf-8")).hexdigest()[:12]
    item = node or {}
    scenario = scenario_id or str(item.get("scenario_id") or "")
    component = component_id or str(item.get("component_id") or "")
    return {
        "id": finding_id,
        "finding_id": finding_id,
        "code": category,
        "category": category,
        "severity": "blocking",
        "message": message,
        "expected_change": expected_change,
        "requirement_ids": _strings(item.get("requirement_refs")),
        "diagram_ids": [scenario] if scenario else [],
        "evidence": [{"source": "design_graph", "ref": subject, "excerpt": message}],
        "subject_ref": subject,
        "origin": "deterministic",
        "partition": {"scenario_id": scenario, "component_id": component},
    }


def _reject_unknown_node_fields(raw: dict[str, object], *, where: str, run_id: str) -> None:
    """A node field outside the contract means a malformed delta, e.g. the next operation nested inside the node."""

    unknown = sorted(str(key) for key in raw if key not in DELTA_NODE_FIELDS)
    if not unknown:
        return
    hint = (
        " The next operation was written inside this node: close the node object before starting the next "
        "{\"op\": ...} item of operations."
        if {"op", "edge", "node"} & set(unknown)
        else ""
    )
    raise HarnessError(
        "designer_delta_invalid",
        f"{where} has fields outside the Design node contract: {', '.join(unknown)}.{hint}",
        run_id=run_id,
    )


def _apply_operation(
    graph: dict[str, object],
    operation: dict[str, object],
    *,
    partition: dict[str, object],
    partition_key: str,
    temp_refs: dict[str, str],
    valid_evidence_refs: set[str],
    valid_guidance_refs: set[str],
    run_id: str,
    edge_refs: dict[str, str] | None = None,
) -> None:
    op = str(operation.get("op") or "")
    if op == "set_partition_coverage":
        coverage = operation.get("coverage")
        if not isinstance(coverage, dict):
            raise HarnessError("designer_delta_invalid", "Partition coverage must be an object.", run_id=run_id)
        partitions = _dict_items(graph.get("partitions"), code="designer_delta_invalid", run_id=run_id)
        entry = {**partition, "coverage": dict(coverage)}
        graph["partitions"] = [item for item in partitions if _partition_key(item, run_id=run_id) != partition_key] + [entry]
        return
    if op == "add_node":
        raw = operation.get("node")
        if not isinstance(raw, dict):
            raise HarnessError("designer_delta_invalid", "add_node requires a node object.", run_id=run_id)
        _reject_unknown_node_fields(raw, where="add_node.node", run_id=run_id)
        semantic_key = str(raw.get("semantic_key") or "").strip()
        if not _SAFE_KEY.fullmatch(semantic_key):
            raise HarnessError("designer_delta_invalid", "Design node semantic_key is invalid.", run_id=run_id)
        node_id = "VDN-" + hashlib.sha256(f"{partition_key}\0{raw.get('kind')}\0{semantic_key}".encode("utf-8")).hexdigest()[:16]
        temp_ref = str(raw.get("temp_ref") or "")
        if temp_ref:
            if not temp_ref.startswith("tmp:") or temp_ref in temp_refs:
                raise HarnessError("designer_delta_invalid", "Design node temp_ref is invalid.", run_id=run_id)
            temp_refs[temp_ref] = node_id
        evidence_refs = _strings(raw.get("evidence_refs"))
        _reject_unknown_evidence(evidence_refs, valid_evidence_refs, where="Design node evidence_refs", run_id=run_id)
        _validate_delta_runtime_evidence(
            raw.get("runtime_binding"),
            valid_evidence_refs=valid_evidence_refs,
            run_id=run_id,
        )
        _validate_delta_access_evidence(
            raw.get("surface_contract"),
            valid_evidence_refs=valid_evidence_refs,
            run_id=run_id,
        )
        _validate_delta_guidance_refs(
            raw.get("surface_contract"),
            valid_guidance_refs=valid_guidance_refs,
            run_id=run_id,
        )
        node = {
            **raw,
            **partition,
            "id": node_id,
            "evidence_refs": evidence_refs,
        }
        node.pop("temp_ref", None)
        nodes = _dict_items(graph.get("nodes"), code="designer_delta_invalid", run_id=run_id)
        if any(str(item.get("id") or "") == node_id for item in nodes):
            raise HarnessError("designer_delta_invalid", f"Design node already exists: {node_id}", run_id=run_id)
        target = _dict_value(_dict_value(node.get("runtime_binding")).get("target"))
        planned_owner = next(
            (
                item
                for item in nodes
                if target
                and str(item.get("disposition") or "") == "add"
                and _dict_value(_dict_value(item.get("runtime_binding")).get("target")) == target
            ),
            None,
        )
        if str(node.get("disposition") or "") in {"reuse", "modify", "no_change"} and planned_owner is not None:
            raise HarnessError(
                "designer_delta_invalid",
                "Design node shares a runtime target that is only planned by "
                f"{str(planned_owner.get('id') or 'another node')}. Sharing the target does not make it repository "
                "reuse; keep disposition=add, leave node.evidence_refs empty, and explain the planned GAP in "
                "evidence_reason.",
                run_id=run_id,
            )
        graph["nodes"] = [*nodes, node]
        return
    if op == "update_node":
        node_id = _resolve_ref(str(operation.get("node_id") or ""), temp_refs)
        changes = operation.get("changes")
        if not isinstance(changes, dict) or any(key in changes for key in {"id", "scenario_id", "component_id", "use_case_id"}):
            raise HarnessError("designer_delta_invalid", "update_node changes are invalid.", run_id=run_id)
        _reject_unknown_node_fields(changes, where="update_node.changes", run_id=run_id)
        if "evidence_refs" in changes:
            _reject_unknown_evidence(
                _strings(changes.get("evidence_refs")), valid_evidence_refs, where="Updated node evidence_refs", run_id=run_id
            )
        if "runtime_binding" in changes:
            _validate_delta_runtime_evidence(
                changes.get("runtime_binding"),
                valid_evidence_refs=valid_evidence_refs,
                run_id=run_id,
            )
        if "surface_contract" in changes:
            _validate_delta_access_evidence(
                changes.get("surface_contract"),
                valid_evidence_refs=valid_evidence_refs,
                run_id=run_id,
            )
            _validate_delta_guidance_refs(
                changes.get("surface_contract"),
                valid_guidance_refs=valid_guidance_refs,
                run_id=run_id,
            )
        nodes = _dict_items(graph.get("nodes"), code="designer_delta_invalid", run_id=run_id)
        target_node = next((item for item in nodes if str(item.get("id") or "") == node_id), None)
        if target_node is None:
            raise HarnessError("designer_delta_invalid", f"Design node does not exist: {node_id}", run_id=run_id)
        if _partition_key(target_node, run_id=run_id) != partition_key:
            raise HarnessError("designer_delta_invalid", "Designer delta cannot update a node outside its partition.", run_id=run_id)
        graph["nodes"] = [{**item, **changes} if str(item.get("id") or "") == node_id else item for item in nodes]
        return
    if op == "remove_node":
        node_id = _resolve_ref(str(operation.get("node_id") or ""), temp_refs)
        nodes = _dict_items(graph.get("nodes"), code="designer_delta_invalid", run_id=run_id)
        target_node = next((item for item in nodes if str(item.get("id") or "") == node_id), None)
        if target_node is None or _partition_key(target_node, run_id=run_id) != partition_key:
            raise HarnessError("designer_delta_invalid", "Designer delta cannot remove a node outside its partition.", run_id=run_id)
        graph["nodes"] = [item for item in nodes if str(item.get("id") or "") != node_id]
        graph["edges"] = [item for item in _dict_items(graph.get("edges"), code="designer_delta_invalid", run_id=run_id) if node_id not in {str(item.get("from") or ""), str(item.get("to") or "")}]
        return
    if op == "add_edge":
        raw = operation.get("edge")
        if not isinstance(raw, dict):
            raise HarnessError("designer_delta_invalid", "add_edge requires an edge object.", run_id=run_id)
        source = _resolve_ref(str(raw.get("from") or ""), temp_refs)
        target = _resolve_ref(str(raw.get("to") or ""), temp_refs)
        kind = str(raw.get("kind") or "")
        label = str(raw.get("label") or "").strip()
        branch_group = str(raw.get("branch_group") or "").strip()
        guard = str(raw.get("guard") or "").strip()
        scenario_id = str(raw.get("scenario_id") or partition.get("scenario_id") or "").strip()
        if kind not in EDGE_KINDS:
            raise HarnessError(
                "designer_delta_invalid",
                f"Design edge kind is invalid; expected one of {sorted(EDGE_KINDS)}.",
                run_id=run_id,
            )
        if not label:
            raise HarnessError("designer_delta_invalid", "Design edge label is required.", run_id=run_id)
        if bool(branch_group) != bool(guard) or (branch_group and not _SAFE_KEY.fullmatch(branch_group)):
            raise HarnessError(
                "designer_delta_invalid",
                "Design edge branch_group and guard must either both be omitted or both be valid non-empty strings.",
                run_id=run_id,
            )
        if "terminal" in raw and not isinstance(raw.get("terminal"), bool):
            raise HarnessError("designer_delta_invalid", "Design edge terminal must be boolean.", run_id=run_id)
        if not scenario_id or not _SAFE_KEY.fullmatch(scenario_id):
            raise HarnessError("designer_delta_invalid", "Design edge scenario_id is invalid.", run_id=run_id)
        # A new edge may carry a temp_ref so order_edges of the same delta can place it among existing edges.
        edge_ref = str(raw.get("temp_ref") or "").strip()
        if edge_ref and (
            edge_refs is None or not edge_ref.startswith("tmp:") or edge_ref in edge_refs or edge_ref in temp_refs
        ):
            raise HarnessError(
                "designer_delta_invalid",
                f"Design edge temp_ref {edge_ref} is invalid or repeated: give each new edge its own 'tmp:' name.",
                run_id=run_id,
            )
        edges = _dict_items(graph.get("edges"), code="designer_delta_invalid", run_id=run_id)
        requested_order = raw.get("order")
        if requested_order is None:
            order = 1 + max(
                [int(item.get("order") or 0) for item in edges if str(item.get("scenario_id") or "") == scenario_id],
                default=0,
            )
        elif isinstance(requested_order, int) and requested_order > 0:
            order = requested_order
        else:
            raise HarnessError("designer_delta_invalid", "Design edge order must be a positive integer.", run_id=run_id)
        if any(
            str(item.get("scenario_id") or "") == scenario_id and int(item.get("order") or 0) == order
            for item in edges
        ):
            raise HarnessError("designer_delta_invalid", f"Design edge order already exists for {scenario_id}: {order}", run_id=run_id)
        edge_id = "VDE-" + hashlib.sha256(
            f"{scenario_id}\0{source}\0{kind}\0{target}\0{label}\0{branch_group}\0{guard}".encode("utf-8")
        ).hexdigest()[:16]
        if any(str(item.get("id") or "") == edge_id for item in edges):
            raise HarnessError(
                "designer_delta_invalid",
                f"Design edge already exists: {edge_id}. Remove this exact edge_id before re-adding it, or omit the duplicate add_edge.",
                run_id=run_id,
            )
        if edge_ref and edge_refs is not None:
            edge_refs[edge_ref] = edge_id
        graph["edges"] = [
            *edges,
            {
                **{key: value for key, value in raw.items() if key != "temp_ref"},
                "id": edge_id,
                "from": source,
                "to": target,
                "kind": kind,
                "label": label,
                "scenario_id": scenario_id,
                "order": order,
                "branch_group": branch_group,
                "guard": guard,
                "terminal": bool(raw.get("terminal", False)),
            },
        ]
        return
    if op == "remove_edge":
        edge_id = str(operation.get("edge_id") or "")
        edges = _dict_items(graph.get("edges"), code="designer_delta_invalid", run_id=run_id)
        target_edge = next((item for item in edges if str(item.get("id") or "") == edge_id), None)
        if target_edge is None:
            raise HarnessError(
                "designer_delta_invalid",
                "remove_edge requires edge_id for an existing Design edge.",
                run_id=run_id,
            )
        if str(target_edge.get("scenario_id") or "") != str(partition.get("scenario_id") or ""):
            raise HarnessError(
                "designer_delta_invalid",
                "Designer delta cannot remove an edge outside its scenario partition.",
                run_id=run_id,
            )
        graph["edges"] = [item for item in edges if str(item.get("id") or "") != edge_id]
        return
    if op == "place_surface":
        _apply_surface_placement(
            graph,
            operation,
            partition=partition,
            valid_evidence_refs=valid_evidence_refs,
            run_id=run_id,
        )
        return
    if op == "not_exercised":
        _apply_not_exercised(
            graph,
            operation,
            partition=partition,
            valid_evidence_refs=valid_evidence_refs,
            run_id=run_id,
        )


def _apply_not_exercised(
    graph: dict[str, object],
    operation: dict[str, object],
    *,
    partition: dict[str, object],
    valid_evidence_refs: set[str],
    run_id: str,
) -> None:
    """Record that the scenario runs nothing on a required surface; whether that is true is a semantic question."""

    scenario_id = str(partition.get("scenario_id") or "")
    surface = str(operation.get("surface") or "")
    reason = str(operation.get("reason") or "").strip()
    evidence_refs = _strings(operation.get("evidence_refs"))

    def invalid(message: str) -> HarnessError:
        return HarnessError("designer_delta_invalid", f"not_exercised {message}", run_id=run_id)

    # requirement_refs is filled by the Harness from the approved Requirement before the delta is applied.
    unknown = sorted(set(operation) - {"op", "surface", "reason", "evidence_refs", "requirement_refs"})
    if unknown:
        raise invalid(f"has fields outside its contract: {', '.join(unknown)}.")
    if surface not in NOT_EXERCISABLE_SURFACES:
        raise invalid(f"can only declare {', '.join(sorted(NOT_EXERCISABLE_SURFACES))}, not {surface or '<missing>'}.")
    if not reason or not evidence_refs:
        raise invalid("needs a reason and the evidence_refs of the code that ends the scenario before this surface.")
    _reject_unknown_evidence(evidence_refs, valid_evidence_refs, where="not_exercised evidence_refs", run_id=run_id)
    declarations = _dict_items(graph.get("surface_not_exercised", []), code="designer_delta_invalid", run_id=run_id)
    if any(item.get("scenario_id") == scenario_id and item.get("surface") == surface for item in declarations):
        raise invalid(f"{scenario_id} {surface} is already declared not exercised.")
    graph["surface_not_exercised"] = sorted(
        [
            *declarations,
            {
                "scenario_id": scenario_id,
                "surface": surface,
                "reason": reason,
                "evidence_refs": evidence_refs,
                "requirement_refs": _strings(operation.get("requirement_refs")),
                "declared_in_partition": f"{scenario_id}|{partition.get('component_id')}",
            },
        ],
        key=lambda item: (str(item.get("scenario_id") or ""), str(item.get("surface") or "")),
    )


def _validate_not_exercised(candidate: dict[str, object], *, run_id: str) -> None:
    """A declared surface has no node and no covered coverage; coverage not_exercised needs a declaration."""

    declared = {(item.get("scenario_id"), item.get("surface")) for item in candidate.get("surface_not_exercised") or []}
    nodes = [node for node in candidate.get("nodes") or [] if isinstance(node, dict)]
    for scenario_id, surface in sorted(declared, key=str):
        if any(node.get("scenario_id") == scenario_id and node.get("surface") == surface for node in nodes):
            raise HarnessError(
                "designer_delta_invalid",
                f"not_exercised {scenario_id} {surface} conflicts with a {surface} node of {scenario_id}; a surface the "
                "scenario runs is designed as a node, not declared not exercised.",
                run_id=run_id,
            )
        if not any(node.get("scenario_id") == scenario_id for node in nodes):
            raise HarnessError(
                "designer_delta_invalid",
                f"not_exercised: {scenario_id} has no Design node left; a scenario keeps at least one designed surface.",
                run_id=run_id,
            )
    for item in candidate.get("partitions") or []:
        for surface, status in _dict_value(item.get("coverage")).items():
            key = (item.get("scenario_id"), surface)
            if status == "not_exercised" and key not in declared:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"coverage {surface}=not_exercised needs a {{\"op\":\"not_exercised\"}} operation with its reason.",
                    run_id=run_id,
                )
            if status in {"covered", "no_change"} and key in declared:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"{item.get('scenario_id')} {surface} is declared not exercised, so its coverage must be not_exercised.",
                    run_id=run_id,
                )


def _validate_new_placements(
    candidate: dict[str, object], *, before: dict[str, object], partition: dict[str, object], run_id: str
) -> None:
    """Placed here: this delta designs the surface. Placed elsewhere: this delta leaves it to that partition."""

    old = {(item.get("scenario_id"), item.get("surface")) for item in before.get("surface_placements") or []}
    component_id = str(partition.get("component_id") or "")
    for placement in candidate.get("surface_placements") or []:
        scenario_id, surface = placement.get("scenario_id"), placement.get("surface")
        if (scenario_id, surface) in old:
            continue
        designed_here = any(
            node.get("scenario_id") == scenario_id
            and node.get("surface") == surface
            and node.get("component_id") == component_id
            for node in candidate.get("nodes") or []
        )
        covered_here = any(
            item.get("scenario_id") == scenario_id
            and item.get("component_id") == component_id
            and str(_dict_value(item.get("coverage")).get(str(surface)) or "") in {"covered", "no_change"}
            for item in candidate.get("partitions") or []
        )
        placed_here = placement.get("component_id") == component_id
        if placed_here and not designed_here:
            raise HarnessError(
                "designer_delta_invalid",
                f"place_surface puts {scenario_id} {surface} in {component_id}, the component of this delta, so this "
                f"delta must also add the {surface} node (and cover {surface}).",
                run_id=run_id,
            )
        if (designed_here or covered_here) and not placed_here:
            raise HarnessError(
                "designer_delta_invalid",
                f"place_surface sends {scenario_id} {surface} to {placement.get('component_id')}, so this {component_id} "
                f"delta must not add a {surface} node or cover {surface}; that partition designs it.",
                run_id=run_id,
            )


def _apply_surface_placement(
    graph: dict[str, object],
    operation: dict[str, object],
    *,
    partition: dict[str, object],
    valid_evidence_refs: set[str],
    run_id: str,
) -> None:
    """Record where the scenario's business rule runs; whether that place is right is a semantic review question."""

    scenario_id = str(partition.get("scenario_id") or "")
    surface = str(operation.get("surface") or "")
    component_id = str(operation.get("component_id") or "").strip()
    reason = str(operation.get("reason") or "").strip()
    evidence_refs = _strings(operation.get("evidence_refs"))

    def invalid(message: str) -> HarnessError:
        return HarnessError("designer_delta_invalid", f"place_surface {message}", run_id=run_id)

    unknown = sorted(set(operation) - {"op", "surface", "component_id", "reason", "evidence_refs"})
    if unknown:
        raise invalid(f"has fields outside its contract: {', '.join(unknown)}.")
    if surface not in PLACEABLE_SURFACES:
        raise invalid(f"can only place {', '.join(sorted(PLACEABLE_SURFACES))}, not {surface or '<missing>'}.")
    if not component_id or not reason or not evidence_refs:
        raise invalid("needs component_id, a reason and the evidence_refs of the code that makes the decision.")
    _reject_unknown_evidence(evidence_refs, valid_evidence_refs, where="place_surface evidence_refs", run_id=run_id)
    if any(
        str(node.get("scenario_id") or "") == scenario_id and str(node.get("surface") or "") == surface
        for node in _dict_items(graph.get("nodes"), code="designer_delta_invalid", run_id=run_id)
        if str(node.get("component_id") or "") != str(partition.get("component_id") or "")
    ):
        raise invalid(f"cannot move {scenario_id} {surface}: another partition already designed it.")
    placements = _dict_items(graph.get("surface_placements", []), code="designer_delta_invalid", run_id=run_id)
    existing = next(
        (item for item in placements if item.get("scenario_id") == scenario_id and item.get("surface") == surface),
        None,
    )
    if existing is not None:
        # shortcut: a placement is final for construction, so a Validator repair cannot move it yet; add a repair path
        # when a real run needs one.
        raise invalid(
            f"{scenario_id} {surface} is already placed in {existing.get('component_id')} by partition "
            f"{existing.get('placed_in_partition')}; a placement is made once."
        )
    graph["surface_placements"] = sorted(
        [
            *placements,
            {
                "scenario_id": scenario_id,
                "surface": surface,
                "component_id": component_id,
                "reason": reason,
                "evidence_refs": evidence_refs,
                "placed_in_partition": f"{scenario_id}|{partition.get('component_id')}",
            },
        ],
        key=lambda item: (str(item.get("scenario_id") or ""), str(item.get("surface") or "")),
    )


def _validate_delta_runtime_evidence(
    value: object,
    *,
    valid_evidence_refs: set[str],
    run_id: str,
) -> None:
    if value is None:
        return
    binding = _dict_value(value)
    refs = _strings(binding.get("evidence_refs"))
    _reject_unknown_evidence(refs, valid_evidence_refs, where="Runtime binding evidence_refs", run_id=run_id)


def _validate_delta_access_evidence(
    value: object,
    *,
    valid_evidence_refs: set[str],
    run_id: str,
) -> None:
    contract = _dict_value(value)
    refs = _strings(contract.get("authentication_evidence_refs"))
    _reject_unknown_evidence(refs, valid_evidence_refs, where="API authentication_evidence_refs", run_id=run_id)


def _reject_unknown_evidence(refs: list[str], valid_evidence_refs: set[str], *, where: str, run_id: str) -> None:
    # Naming the refs lets the Designer fix them; a vague message made it guess and repeat the mistake.
    unknown = [ref for ref in refs if ref not in valid_evidence_refs]
    if unknown:
        raise HarnessError(
            "designer_delta_invalid",
            f"{where} contain refs no Designer tool returned: {', '.join(unknown)}. Copy each evidence ref exactly "
            "as a Designer tool or the prompt returned it; the #L line is part of the ref, never change it or write "
            "a new one.",
            run_id=run_id,
        )


def _partition_key(value: dict[str, object], *, run_id: str) -> str:
    parts = [str(value.get(key) or "").strip() for key in ("scenario_id", "component_id")]
    if any(not part or not _SAFE_KEY.fullmatch(part) for part in parts):
        raise HarnessError("vertical_design_graph_invalid", "Design partition identity is invalid.", run_id=run_id)
    return "|".join(parts)


def _resolve_ref(value: str, temp_refs: dict[str, str]) -> str:
    return temp_refs.get(value, value)


def _resolve_attachment_temp_refs(
    graph: dict[str, object], temp_refs: dict[str, str], *, before: dict[str, object], run_id: str
) -> None:
    """A runtime owner added in the same delta is named by its temp_ref; store its node id, as edges do."""

    def owner_of(node: object) -> object:
        binding = node.get("runtime_binding") if isinstance(node, dict) else None
        attachment = binding.get("attachment") if isinstance(binding, dict) else None
        return attachment if isinstance(attachment, dict) else None

    unchanged = {
        str(node.get("id") or ""): (owner_of(node) or {}).get("owner_ref")
        for node in before.get("nodes") or []
        if isinstance(node, dict)
    }
    nodes = _dict_items(graph.get("nodes"), code="designer_delta_invalid", run_id=run_id)
    for node in nodes:
        attachment = owner_of(node)
        owner_ref = str((attachment or {}).get("owner_ref") or "")
        if not owner_ref.startswith("tmp:") or unchanged.get(str(node.get("id") or "")) == owner_ref:
            continue
        if owner_ref not in temp_refs:
            raise HarnessError(
                "designer_delta_invalid",
                f"Node {node.get('id')} runtime_binding.attachment.owner_ref {owner_ref} is the temp_ref of no node "
                "added in this delta; use the temp_ref of a node this delta adds, a runtime-bound VDN-* id or an "
                "RLA-* anchor.",
                run_id=run_id,
            )
        # New dicts so the Designer's own delta (saved as its artifact) keeps the temp_ref it wrote.
        node["runtime_binding"] = {
            **node["runtime_binding"],
            "attachment": {**attachment, "owner_ref": temp_refs[owner_ref]},
        }
    graph["nodes"] = nodes


def _dict_items(value: object, *, code: str, run_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HarnessError(code, "Expected an array of objects.", run_id=run_id)
    return [dict(item) for item in value]


def _dict_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []


def _is_repo_path(value: str) -> bool:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _finding(code: str, subject: str, message: str) -> dict[str, object]:
    return {
        "finding_id": "VDF-" + hashlib.sha256(f"{code}\0{subject}".encode("utf-8")).hexdigest()[:12],
        "code": code,
        "severity": "blocker",
        "subject_ref": subject,
        "message": message,
    }
