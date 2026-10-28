"""Canonical vertical Design graph and deterministic delta application for L3."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...core.errors import HarnessError


GRAPH_VERSION_V1 = "vertical_design_graph_v1"
GRAPH_VERSION_V2 = "vertical_design_graph_v2"
GRAPH_VERSION = GRAPH_VERSION_V2
SUPPORTED_GRAPH_VERSIONS = frozenset({GRAPH_VERSION_V1, GRAPH_VERSION_V2})
DELTA_VERSION_V1 = "designer_delta_v1"
DELTA_VERSION_V2 = "designer_delta_v2"
DELTA_VERSION = DELTA_VERSION_V2
DESIGNER_CONTRACT_STATE_KEY = "designer_contract_version"
DESIGN_BUNDLE_VERSION_V1 = "system_design_bundle_v1"
DESIGN_BUNDLE_VERSION_V2 = "system_design_bundle_v2"
DESIGN_BUNDLE_VERSION = DESIGN_BUNDLE_VERSION_V2
DESIGN_BUNDLE_STATE_KEY = "design_bundle_contract_version"
SURFACE_CONTRACT_VERSION = "design_surface_contract_v1"
RUNTIME_BINDING_VERSION = "runtime_binding_v1"
BOUNDARY_CONTRACT_VERSION = "boundary_contract_v1"
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
OPERATIONS = frozenset({"add_node", "update_node", "remove_node", "add_edge", "remove_edge", "set_partition_coverage"})
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


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


def new_vertical_design_graph(
    *,
    run_id: str,
    snapshot_binding: dict[str, object],
    contract_version: str = GRAPH_VERSION,
) -> dict[str, object]:
    if contract_version not in SUPPORTED_GRAPH_VERSIONS:
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
    if not isinstance(value, dict) or value.get("schema_version") not in SUPPORTED_GRAPH_VERSIONS:
        raise HarnessError("vertical_design_graph_invalid", "Unsupported vertical Design graph contract.", run_id=expected_run_id)
    graph_version = str(value.get("schema_version") or "")
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
            if str(surface) not in SURFACES or str(status) not in {"covered", "no_change", "not_applicable", "missing"}:
                raise HarnessError("vertical_design_graph_invalid", "Design partition coverage contains an invalid value.", run_id=run_id)
        if graph_version == GRAPH_VERSION_V2:
            source_refs = partition.get("source_observation_refs", [])
            if not isinstance(source_refs, list) or any(not isinstance(item, str) or not item for item in source_refs):
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    "Design partition source observations are invalid.",
                    run_id=run_id,
                )
        partition_keys.add(key)

    nodes = _dict_items(value.get("nodes"), code="vertical_design_graph_invalid", run_id=run_id)
    node_ids: set[str] = set()
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
        if surface_contract is not None:
            _validate_surface_contract(
                surface_contract,
                surface=str(node.get("surface") or ""),
                node_id=node_id,
                run_id=run_id,
            )
        if graph_version == GRAPH_VERSION_V2 and node.get("runtime_binding") is not None:
            _validate_runtime_binding(node.get("runtime_binding"), node=node, run_id=run_id)
        node_ids.add(node_id)

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
        if graph_version == GRAPH_VERSION_V2:
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
    if str(graph.get("schema_version") or "") == GRAPH_VERSION_V2:
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
                and str(edge.get("kind") or "") not in {"contains", "depends_on", "realized_by"}
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

        confirmation_required = any(
            "confirm" in " ".join(str(transition.get(key) or "") for key in ("from", "action", "to", "outcome")).casefold()
            for node in ui_nodes
            for transition in _dict_items(
                _dict_value(node.get("surface_contract")).get("transitions") or [],
                code="vertical_design_graph_invalid",
                run_id="",
            )
        )
        writes = [edge for edge in behavior if str(edge.get("kind") or "") == "writes"]
        if confirmation_required and writes:
            ui_node_ids = {str(node.get("id") or "") for node in ui_nodes}
            first_write_order = min(int(edge.get("order") or 0) for edge in writes)
            returned_to_ui = [
                int(edge.get("order") or 0)
                for edge in behavior
                if str(edge.get("to") or "") in ui_node_ids and int(edge.get("order") or 0) < first_write_order
            ]
            confirmation_calls = [
                int(edge.get("order") or 0)
                for edge in behavior
                if str(edge.get("from") or "") in ui_node_ids
                and any(return_order < int(edge.get("order") or 0) < first_write_order for return_order in returned_to_ui)
            ]
            if not confirmation_calls:
                findings.append(
                    _composition_finding(
                        "confirmation_interaction_missing",
                        scenario_id,
                        f"Scenario {scenario_id} writes data without a separate UI confirmation interaction.",
                        "Return the verification result to the UI, then add a distinct confirmation call before the first writes edge.",
                        node=owner,
                    )
                )
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
            f"Node {node_id} has an unsupported surface contract.",
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
                f"Node {node_id} surface contract has invalid {key}.",
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
                f"Node {node_id} surface contract has invalid {key}.",
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
        require_strings("validations", allow_empty=True)
        require_strings("outcomes")
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


def _validate_runtime_binding(value: object, *, node: dict[str, object], run_id: str) -> None:
    node_id = str(node.get("id") or "")
    if not isinstance(value, dict) or value.get("schema_version") != RUNTIME_BINDING_VERSION:
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} has an unsupported runtime binding.", run_id=run_id)
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
    if not owner_ref or (owner_path and not _is_repo_path(owner_path)) or (owner_path and not owner_symbol):
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime attachment owner is invalid.", run_id=run_id)
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list) or any(not isinstance(item, str) or not item for item in evidence_refs):
        raise HarnessError("vertical_design_graph_invalid", f"Node {node_id} runtime evidence is invalid.", run_id=run_id)


def _validate_boundary_contract(value: object, *, edge_id: str, run_id: str) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != BOUNDARY_CONTRACT_VERSION:
        raise HarnessError("vertical_design_graph_invalid", f"Edge {edge_id} has an unsupported boundary contract.", run_id=run_id)
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


def delta_version_for(graph_version: str) -> str:
    return DELTA_VERSION_V2 if graph_version == GRAPH_VERSION_V2 else DELTA_VERSION_V1


def design_bundle_version_for(graph_version: str) -> str:
    return DESIGN_BUNDLE_VERSION_V2 if graph_version == GRAPH_VERSION_V2 else DESIGN_BUNDLE_VERSION_V1


def apply_designer_delta(
    graph: dict[str, object],
    delta: object,
    *,
    valid_evidence_refs: set[str],
) -> tuple[dict[str, object], dict[str, str]]:
    run_id = str(graph.get("run_id") or "")
    validate_vertical_design_graph(graph, expected_run_id=run_id)
    expected_delta_version = delta_version_for(str(graph.get("schema_version") or ""))
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
    operations = _dict_items(delta.get("operations"), code="designer_delta_invalid", run_id=run_id)
    if not operations:
        raise HarnessError("designer_delta_invalid", "Designer delta must contain at least one operation.", run_id=run_id)
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
        _apply_operation(
            candidate,
            operation,
            partition=partition,
            partition_key=partition_key,
            temp_refs=temp_refs,
            valid_evidence_refs=valid_evidence_refs,
            run_id=run_id,
        )
    candidate["unresolved_items"] = _dict_items(
        delta.get("unresolved_items", []),
        code="designer_delta_invalid",
        run_id=run_id,
    )
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
    if str(candidate.get("schema_version") or "") == GRAPH_VERSION_V2:
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
    return candidate, temp_refs


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
    for scenario_id, surfaces in sorted(required_surfaces.items()):
        matching = [item for key, item in partitions.items() if key.split("|", 1)[0] == scenario_id]
        if not matching:
            findings.append(_finding("missing_partition", scenario_id, "Scenario has no Design partition."))
            continue
        for surface in sorted(surfaces):
            covered = any(str(_dict_value(item.get("coverage")).get(surface) or "") in {"covered", "no_change"} for item in matching)
            has_node = any(str(item.get("scenario_id") or "") == scenario_id and str(item.get("surface") or "") == surface for item in nodes)
            if not covered or not has_node:
                findings.append(_finding("required_surface_missing", f"{scenario_id}:{surface}", "Required surface has no covered Design node."))
    for item in _dict_items(graph.get("unresolved_items"), code="vertical_design_graph_invalid", run_id=run_id):
        if str(item.get("owner") or "") in {"requirement", "human"}:
            findings.append(_finding("human_decision_required", str(item.get("id") or "unresolved"), str(item.get("message") or "Design requires a human decision.")))
    if str(graph.get("schema_version") or "") == GRAPH_VERSION_V2:
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
    component_descriptions = {
        component_id: " ".join([component_id, *roots]).casefold()
        for component_id, roots in component_roots.items()
    }
    findings: list[dict[str, object]] = []

    for partition in partitions:
        scenario_id = str(partition.get("scenario_id") or "")
        component_id = str(partition.get("component_id") or "")
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
        surface = str(node.get("surface") or "")
        relation = str(attachment.get("relation") or "")
        roots = component_roots.get(component_id, [])
        compatible_components = _compatible_surface_components(
            surface=surface,
            component_descriptions=component_descriptions,
        )
        if compatible_components and component_id not in compatible_components:
            findings.append(
                _composition_finding(
                    "surface_component_mismatch",
                    node_id,
                    f"{surface or 'Design'} surface is assigned to component {component_id}, but the frozen landscape has a more appropriate owner.",
                    f"Move the node to one of these profile-owned components: {sorted(compatible_components)}.",
                    node=node,
                )
            )
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

    for edge in edges:
        source = nodes_by_id.get(str(edge.get("from") or ""), {})
        target = nodes_by_id.get(str(edge.get("to") or ""), {})
        if not source or not target or str(edge.get("kind") or "") in {"contains", "depends_on", "realized_by"}:
            continue
        crosses_boundary = str(source.get("component_id") or "") != str(target.get("component_id") or "")
        if crosses_boundary:
            contract = _dict_value(edge.get("boundary_contract"))
            provider = source if str(edge.get("kind") or "") == "returns" else target
            diagnostics = _boundary_contract_diagnostics(contract, provider)
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

    inferred = required_surfaces or _required_surfaces_from_partitions(partitions)
    for scenario_id, surfaces in sorted(inferred.items()):
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


def _compatible_surface_components(
    *,
    surface: str,
    component_descriptions: dict[str, str],
) -> set[str]:
    aliases = {
        "ui": ("web", "frontend", " ui", "client"),
        "api": ("api", "backend", "server"),
        "business_rule": ("api", "backend", "service", "domain"),
        "data": ("database", " db", "persistence", "prisma"),
        "interaction": ("api", "backend", "web", "frontend"),
    }.get(surface, ())
    return {
        component_id
        for component_id, description in component_descriptions.items()
        if any(alias in f" {description}" for alias in aliases)
    }


def _boundary_contract_diagnostics(
    contract: dict[str, object], provider: dict[str, object]
) -> dict[str, object]:
    surface = _dict_value(provider.get("surface_contract"))
    mismatches: list[str] = []
    if not contract:
        mismatches.append("add boundary_contract")
    if str(provider.get("surface") or "") != "api":
        return {"provider_surface": str(provider.get("surface") or ""), "mismatches": mismatches}
    expected = f"{str(surface.get('method') or '').upper()} {str(surface.get('path') or '')}".strip()
    actual = str(contract.get("provider_operation") or "").strip()
    if expected and actual.upper() != expected.upper():
        mismatches.append(f"set provider_operation to {expected}")
    fields = _dict_items(surface.get("request_fields", []), code="vertical_design_graph_invalid", run_id="")
    input_mappings = _dict_items(contract.get("input_mapping", []), code="vertical_design_graph_invalid", run_id="")
    mapped_targets = {str(item.get("to") or "") for item in input_mappings if bool(item.get("required"))}
    required_targets = [str(item.get("name") or "") for item in fields if str(item.get("name") or "")]
    missing_targets = [name for name in required_targets if name not in mapped_targets]
    if missing_targets:
        mismatches.append(f"map required input targets {missing_targets}")
    authorization = str(surface.get("authorization") or "").strip().casefold()
    auth_required = bool(authorization and authorization not in {"none", "not required", "public"})
    auth_missing = auth_required and not _dict_items(
        contract.get("auth_mapping", []), code="vertical_design_graph_invalid", run_id=""
    )
    if auth_missing:
        mismatches.append(f"add auth_mapping for {str(surface.get('authorization') or '').strip()}")
    responses = _dict_items(surface.get("responses", []), code="vertical_design_graph_invalid", run_id="")
    error_statuses = [
        str(item.get("status") or "")
        for item in responses
        if str(item.get("status") or "").startswith(("4", "5"))
    ]
    response_missing = not _dict_items(
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


def _apply_operation(
    graph: dict[str, object],
    operation: dict[str, object],
    *,
    partition: dict[str, object],
    partition_key: str,
    temp_refs: dict[str, str],
    valid_evidence_refs: set[str],
    run_id: str,
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
        if any(ref not in valid_evidence_refs for ref in evidence_refs):
            raise HarnessError("designer_delta_invalid", "Design node references evidence outside the frozen landscape.", run_id=run_id)
        _validate_delta_runtime_evidence(
            raw.get("runtime_binding"),
            valid_evidence_refs=valid_evidence_refs,
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
        graph["nodes"] = [*nodes, node]
        return
    if op == "update_node":
        node_id = _resolve_ref(str(operation.get("node_id") or ""), temp_refs)
        changes = operation.get("changes")
        if not isinstance(changes, dict) or any(key in changes for key in {"id", "scenario_id", "component_id", "use_case_id"}):
            raise HarnessError("designer_delta_invalid", "update_node changes are invalid.", run_id=run_id)
        if "evidence_refs" in changes and any(ref not in valid_evidence_refs for ref in _strings(changes.get("evidence_refs"))):
            raise HarnessError("designer_delta_invalid", "Updated node references unknown evidence.", run_id=run_id)
        if "runtime_binding" in changes:
            _validate_delta_runtime_evidence(
                changes.get("runtime_binding"),
                valid_evidence_refs=valid_evidence_refs,
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
        graph["edges"] = [
            *edges,
            {
                **raw,
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
    if any(ref not in valid_evidence_refs for ref in refs):
        raise HarnessError(
            "designer_delta_invalid",
            "Runtime binding references evidence outside the frozen or observed repository evidence.",
            run_id=run_id,
        )


def _partition_key(value: dict[str, object], *, run_id: str) -> str:
    parts = [str(value.get(key) or "").strip() for key in ("scenario_id", "component_id")]
    if any(not part or not _SAFE_KEY.fullmatch(part) for part in parts):
        raise HarnessError("vertical_design_graph_invalid", "Design partition identity is invalid.", run_id=run_id)
    return "|".join(parts)


def _resolve_ref(value: str, temp_refs: dict[str, str]) -> str:
    return temp_refs.get(value, value)


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
