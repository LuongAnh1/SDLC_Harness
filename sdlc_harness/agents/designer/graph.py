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
    UIUX_EXPERIENCE_CONTRACT_VERSION,
)


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
RUNTIME_INPUT_SOURCES = frozenset({"existing_runtime", "external_use_case", "new_boundary", "demo_fixture"})
RUNNABLE_STATUSES = frozenset({"bound", "externally_blocked", "fixture_required"})
# Requirement IDs that can state a dependency on another use case (precondition or flow step).
EXTERNAL_DEPENDENCY_REF_PREFIXES = ("PRE-", "MF-", "AF-", "EF-")
UI_RUNTIME_STATES = ("loading", "ready", "empty", "error", "success")
UI_VISUAL_GROUPS = ("hierarchy", "layout", "typography", "color_and_tokens", "focus_appearance", "responsive_rules")
UI_STYLE_TARGET_POLICIES = frozenset({"reuse_existing", "new_stylesheet"})
UI_SURFACE_ROLES = frozenset({"screen", "composition_wiring"})
# Run-pinned opt-in: layout items become {region, layer, content}; graphs without the pin keep '<region>: <content>'.
UI_LAYOUT_CONTRACT_KEY = "designer_ui_layout_contract"
UI_LAYERED_LAYOUT_V1 = "layered_layout_v1"
UI_LAYOUT_LAYERS = ("page", "overlay")


def is_composition_wiring(node: dict[str, object]) -> bool:
    """A UI node that only forwards runtime inputs to a child; it carries no screen UX of its own."""

    surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
    return str(surface.get("role") or "") == "composition_wiring"
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


def ui_contract_v2(graph: dict[str, object]) -> bool:
    binding = graph.get("snapshot_binding")
    return (
        str(graph.get("schema_version") or "") == GRAPH_VERSION_V2
        and isinstance(binding, dict)
        and binding.get("designer_ui_contract_version") == UI_EXPERIENCE_CONTRACT_V2
    )


def ui_layered_layout(graph: dict[str, object]) -> bool:
    return ui_contract_v2(graph) and _dict_value(graph.get("snapshot_binding")).get(UI_LAYOUT_CONTRACT_KEY) == UI_LAYERED_LAYOUT_V1


def ui_layout_pin(ui_contract_version: str) -> str:
    """Layout contract pinned for new runs; only ui_experience_contract_v2 runs get layered layout."""

    return UI_LAYERED_LAYOUT_V1 if ui_contract_version == UI_EXPERIENCE_CONTRACT_V2 else ""


def layout_regions(visual: dict[str, object]) -> list[tuple[str, str, str]]:
    """(region, layer, content) for each layout item; layer is '' for legacy '<region>: <content>' strings."""

    regions: list[tuple[str, str, str]] = []
    for item in visual.get("layout") if isinstance(visual.get("layout"), list) else []:
        if isinstance(item, dict):
            regions.append(
                (str(item.get("region") or "").strip(), str(item.get("layer") or ""), str(item.get("content") or "").strip())
            )
        elif isinstance(item, str):
            region, _, content = item.partition(":")
            regions.append((region.strip(), "", content.strip()))
    return regions


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
    ui_v2 = ui_contract_v2(value)
    layered = ui_layered_layout(value)

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
                ui_v2=ui_v2,
                layered=layered,
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

    return findings


def _validate_surface_contract(
    value: object,
    *,
    surface: str,
    node_id: str,
    run_id: str,
    ui_v2: bool = False,
    layered: bool = False,
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
        experience = value.get("experience_contract")
        if experience is not None:
            _validate_ui_experience_contract(
                experience,
                node_id=node_id,
                run_id=run_id,
                expected_version=UI_EXPERIENCE_CONTRACT_V2 if ui_v2 else UIUX_EXPERIENCE_CONTRACT_VERSION,
                layered=layered,
            )
        if ui_v2:
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
            if value.get("authentication_required") is False and source != "none":
                raise HarnessError(
                    "vertical_design_graph_invalid",
                    f"Node {node_id} does not require authentication but declares an authentication source.",
                    run_id=run_id,
                )
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
    if not isinstance(value.get("evidence_refs", []), list):
        raise invalid("evidence_refs must be a list")


def _is_symbol_ref(value: str) -> bool:
    path, _, symbol = value.partition("#")
    return _is_repo_path(path) and bool(_TYPESCRIPT_IDENTIFIER.fullmatch(symbol))


def _validate_ui_experience_contract(
    value: object,
    *,
    node_id: str,
    run_id: str,
    expected_version: str = UIUX_EXPERIENCE_CONTRACT_VERSION,
    layered: bool = False,
) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != expected_version:
        raise HarnessError(
            "vertical_design_graph_invalid",
            f"Node {node_id} has an unsupported UI experience contract; expected {expected_version}.",
            run_id=run_id,
        )
    for key in ("component_structure", "accessibility", "responsive", "focus_flow", "guidance_refs"):
        items = value.get(key)
        if not isinstance(items, list) or not items or any(not isinstance(item, str) or not item.strip() for item in items):
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Node {node_id} UI experience contract has invalid {key}.",
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
    if expected_version == UI_EXPERIENCE_CONTRACT_V2:
        _validate_holistic_experience(value, node_id=node_id, run_id=run_id, layered=layered)


def _layout_item_valid(item: object, layered: bool) -> bool:
    if isinstance(item, dict):
        return (
            bool(str(item.get("region") or "").strip())
            and item.get("layer") in UI_LAYOUT_LAYERS
            and bool(str(item.get("content") or "").strip())
        )
    return not layered and isinstance(item, str) and bool(item.strip())


def _validate_holistic_experience(
    value: dict[str, object], *, node_id: str, run_id: str, layered: bool = False
) -> None:
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
    if not isinstance(layout, list) or not layout or not all(_layout_item_valid(item, layered) for item in layout):
        raise invalid(
            "visual_contract.layout needs {region,layer,content} items with layer " + "|".join(UI_LAYOUT_LAYERS)
            if layered
            else "visual_contract.layout needs at least one decision"
        )
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
    valid_guidance_refs: set[str] | None = None,
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
            valid_guidance_refs=valid_guidance_refs or set(),
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
    if str(_dict_value(graph.get("snapshot_binding")).get("designer_uiux_mode") or "off") == "enforce":
        for node in nodes:
            if str(node.get("surface") or "") != "ui":
                continue
            surface_contract = _dict_value(node.get("surface_contract"))
            expected_experience = (
                UI_EXPERIENCE_CONTRACT_V2 if ui_contract_v2(graph) else UIUX_EXPERIENCE_CONTRACT_VERSION
            )
            if not _dict_value(surface_contract.get("experience_contract")) and not is_composition_wiring(node):
                findings.append(
                    _finding(
                        "ui_experience_contract_missing",
                        str(node.get("id") or "ui"),
                        f"UI node has no {expected_experience} backed by observed UI/UX guidance.",
                    )
                )
            if ui_contract_v2(graph) and not _dict_value(surface_contract.get("runtime_surface")):
                findings.append(
                    _finding(
                        "ui_runtime_surface_missing",
                        str(node.get("id") or "ui"),
                        f"UI node has no {RUNTIME_SURFACE_BINDING_VERSION} proving entrypoint composition and required inputs.",
                    )
                )
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
    findings: list[dict[str, object]] = []
    ui_v2_graph = ui_contract_v2(graph)
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
        elif ui_v2_graph and component_id == "web":
            # Non-exported components (e.g. an app Root) have no landscape anchor but are frozen in ui_repo_context.
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

    ui_v2 = ui_contract_v2(graph)
    ui_reachable_from_entry: set[str] = set()
    if ui_v2:
        ui_findings, ui_reachable_from_entry = _ui_runtime_surface_findings(graph, nodes)
        findings.extend(ui_findings)

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
            # Under UI contract v2 a UI component only starts a path once its composition proves app reachability.
            and not (
                ui_v2
                and str(item.get("surface") or "") == "ui"
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
            missing = [
                str(prop.get("name") or "")
                for prop in component.get("props", [])
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
                ref.startswith("DEC-") and ref in requirement_ids for ref in refs
            ):
                problem = "demo_fixture must cite an approved Requirement decision (DEC-*)"
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
                        "Bind the input to an existing provider, a Design node, an approved Requirement PRE-*/DEC-* reference, and a provided_by hop on composition_path.",
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
                modeled = hop_node is not None and str(hop_node.get("disposition") or "") in {"modify", "add"}
                if hop in chain and name not in passed and not modeled:
                    findings.append(
                        _composition_finding(
                            "composition_hop_unmodeled",
                            f"{node_id}:{name}:{hop}",
                            f"AS-IS {hop} does not pass {name} to {child}, and no Design node modifies it.",
                            f"Add a modify node with runtime_binding target {hop} that supplies {name} to {child}.",
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
        if ui_layered_layout(graph):
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
        mapped = {
            str(item.get("to") or "")
            for item in contract.get("input_mapping", [])
            if isinstance(item, dict) and bool(item.get("required"))
        }
        declared = [name for name in _strings(surface.get("inputs")) if name]
        missing = [name for name in declared if name not in mapped]
        if missing:
            mismatches.append(f"map required input targets {missing}")
        return {
            "provider_surface": "business_rule",
            "required_input_targets": declared,
            "mapped_input_targets": sorted(mapped),
            "missing_input_targets": missing,
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
    mapped_targets = {str(item.get("to") or "") for item in input_mappings if bool(item.get("required"))}
    required_targets = [str(item.get("name") or "") for item in fields if str(item.get("name") or "")]
    missing_targets = [name for name in required_targets if name not in mapped_targets]
    if missing_targets:
        mismatches.append(f"map required input targets {missing_targets}")
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


def _validate_delta_access_evidence(
    value: object,
    *,
    valid_evidence_refs: set[str],
    run_id: str,
) -> None:
    contract = _dict_value(value)
    refs = _strings(contract.get("authentication_evidence_refs"))
    if any(ref not in valid_evidence_refs for ref in refs):
        raise HarnessError(
            "designer_delta_invalid",
            "API authentication references evidence outside the frozen or observed repository evidence.",
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
