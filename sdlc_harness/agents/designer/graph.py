"""Canonical vertical Design graph and deterministic delta application for L3."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...core.errors import HarnessError


GRAPH_VERSION = "vertical_design_graph_v1"
DELTA_VERSION = "designer_delta_v1"
DESIGNER_CONTRACT_STATE_KEY = "designer_contract_version"
DESIGN_BUNDLE_VERSION = "system_design_bundle_v1"
DESIGN_BUNDLE_STATE_KEY = "design_bundle_contract_version"
SURFACE_CONTRACT_VERSION = "design_surface_contract_v1"
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
) -> dict[str, object]:
    graph: dict[str, object] = {
        "schema_version": GRAPH_VERSION,
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
            if str(surface) not in SURFACES or str(status) not in {"covered", "no_change", "not_applicable", "missing"}:
                raise HarnessError("vertical_design_graph_invalid", "Design partition coverage contains an invalid value.", run_id=run_id)
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


def design_bundle_findings(graph: dict[str, object]) -> list[dict[str, object]]:
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


def apply_designer_delta(
    graph: dict[str, object],
    delta: object,
    *,
    valid_evidence_refs: set[str],
) -> tuple[dict[str, object], dict[str, str]]:
    run_id = str(graph.get("run_id") or "")
    validate_vertical_design_graph(graph, expected_run_id=run_id)
    if not isinstance(delta, dict) or delta.get("schema_version") != DELTA_VERSION:
        raise HarnessError(
            "designer_delta_invalid",
            f"Unsupported Designer delta contract; expected schema_version={DELTA_VERSION} inside arguments.delta.",
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
    for operation in operations:
        op = str(operation.get("op") or "")
        if op not in OPERATIONS:
            raise HarnessError("designer_delta_invalid", f"Unsupported Designer delta operation: {op}", run_id=run_id)
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
    candidate["artifact_digest"] = graph_digest(candidate)
    validate_vertical_design_graph(candidate, expected_run_id=run_id)
    return candidate, temp_refs


def completion_findings(graph: dict[str, object], *, required_surfaces: dict[str, set[str]]) -> list[dict[str, object]]:
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
    return findings


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


def _finding(code: str, subject: str, message: str) -> dict[str, object]:
    return {
        "finding_id": "VDF-" + hashlib.sha256(f"{code}\0{subject}".encode("utf-8")).hexdigest()[:12],
        "code": code,
        "severity": "blocker",
        "subject_ref": subject,
        "message": message,
    }
