"""Canonical Requirement-to-Design trace graph and deterministic L4 gate."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any

from ...core.errors import HarnessError
from ..designer.graph import GRAPH_VERSION, validate_vertical_design_graph
from ..requirements import RequirementModelV2
from .design_validation import DesignFinding


VERTICAL_TRACE_GRAPH_VERSION = "vertical_trace_graph_v1"
VERTICAL_TRACE_VALIDATION_VERSION = "vertical_trace_validation_v1"
VERTICAL_TRACE_STATE_KEY = "vertical_trace_contract_version"
VERTICAL_TRACE_MODE_STATE_KEY = "vertical_trace_mode"
VERTICAL_TRACE_MODES = frozenset({"shadow", "enforce"})
TRACE_LAYERS = frozenset({"requirement", "design", "implementation", "verification"})
TRACE_NODE_KINDS = frozenset(
    {
        "use_case",
        "scenario",
        "surface_requirement",
        "acceptance_criterion",
        "requirement_claim",
        "design_element",
        "planned_work",
        "code_anchor",
        "migration",
        "test_case",
        "verification_stage",
        "execution_evidence",
    }
)
TRACE_EDGE_KINDS = frozenset(
    {
        "contains",
        "requires_surface",
        "satisfies",
        "realizes",
        "calls",
        "reads",
        "writes",
        "returns",
        "realized_by",
        "depends_on",
        "planned_by",
        "implemented_by",
        "verified_by",
    }
)
REQUIREMENT_SURFACES = ("ui", "api", "business_rule", "data")


@dataclass(frozen=True)
class VerticalTraceValidationResult:
    graph: dict[str, object]
    blocking_findings: list[DesignFinding] = field(default_factory=list)
    warnings: list[DesignFinding] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)
    mode: str = "shadow"

    @property
    def ok(self) -> bool:
        return not self.blocking_findings

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": VERTICAL_TRACE_VALIDATION_VERSION,
            "contract_version": VERTICAL_TRACE_GRAPH_VERSION,
            "mode": self.mode,
            "ok": self.ok,
            "would_block": self.mode == "shadow" and bool(self.blocking_findings),
            "graph_digest": self.graph.get("artifact_digest"),
            "coverage": dict(self.coverage),
            "blocking_count": len(self.blocking_findings),
            "warning_count": len(self.warnings),
            "blocking_findings": [item.to_dict() for item in self.blocking_findings],
            "warnings": [item.to_dict() for item in self.warnings],
        }


def build_vertical_trace_graph(
    *,
    requirement_model: RequirementModelV2,
    design_graph: dict[str, object],
) -> dict[str, object]:
    """Project frozen Requirement V2 and L3 Design graph into one canonical trace graph."""

    validate_vertical_design_graph(design_graph, expected_run_id=requirement_model.run_id)
    requirement_payload = requirement_model.to_dict()
    use_case = _dict(requirement_payload.get("use_case"))
    use_case_id = str(use_case.get("id") or "")
    if not use_case_id:
        raise HarnessError("vertical_trace_graph_invalid", "Requirement use case ID is missing.", run_id=requirement_model.run_id)

    nodes: list[dict[str, object]] = [
        {
            "id": use_case_id,
            "layer": "requirement",
            "kind": "use_case",
            "label": str(use_case.get("name") or use_case_id),
            "scenario_id": "",
            "component_id": "",
            "surface": "",
            "requirement_refs": [],
            "evidence_refs": _strings(use_case.get("evidence_refs")),
        }
    ]
    claim_records = _requirement_claims(requirement_payload)
    claim_ids = {str(item["id"]) for item in claim_records}
    for claim in claim_records:
        nodes.append(claim)

    edges: list[dict[str, object]] = []
    scenarios = _dicts(_dict(requirement_payload.get("scenario_contract")).get("scenarios"))
    scenario_ids = {str(item.get("id") or "") for item in scenarios}
    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "")
        nodes.append(
            {
                "id": scenario_id,
                "layer": "requirement",
                "kind": "scenario",
                "scenario_kind": str(scenario.get("kind") or ""),
                "label": str(scenario.get("name") or scenario_id),
                "scenario_id": scenario_id,
                "component_id": "",
                "surface": "",
                "requirement_refs": _strings(scenario.get("requirement_refs")),
                "evidence_refs": _strings(scenario.get("evidence_refs")),
            }
        )
        edges.append(_edge(use_case_id, scenario_id, "contains", scenario_id=scenario_id))
        for ref in [*_strings(scenario.get("requirement_refs")), *_strings(scenario.get("acceptance_refs"))]:
            if ref in claim_ids:
                edges.append(_edge(scenario_id, ref, "contains", scenario_id=scenario_id))
        surfaces = _dict(scenario.get("surfaces"))
        for surface in REQUIREMENT_SURFACES:
            contract = _dict(surfaces.get(surface))
            surface_id = _surface_id(scenario_id, surface)
            nodes.append(
                {
                    "id": surface_id,
                    "layer": "requirement",
                    "kind": "surface_requirement",
                    "label": f"{scenario_id} {surface}",
                    "scenario_id": scenario_id,
                    "component_id": "",
                    "surface": surface,
                    "status": str(contract.get("status") or ""),
                    "reason": str(contract.get("reason") or ""),
                    "requirement_refs": _strings(contract.get("requirement_refs")),
                    "evidence_refs": _strings(contract.get("evidence_refs")),
                }
            )
            edges.append(_edge(scenario_id, surface_id, "requires_surface", scenario_id=scenario_id))
            for ref in _strings(contract.get("requirement_refs")):
                if ref in claim_ids:
                    edges.append(_edge(surface_id, ref, "satisfies", scenario_id=scenario_id))

    design_nodes = _dicts(design_graph.get("nodes"))
    for item in design_nodes:
        nodes.append(
            {
                "id": str(item.get("id") or ""),
                "layer": "design",
                "kind": "planned_work" if str(item.get("kind") or "") == "planned_work" else "design_element",
                "design_kind": str(item.get("kind") or ""),
                "label": str(item.get("name") or item.get("semantic_key") or item.get("id") or ""),
                "scenario_id": str(item.get("scenario_id") or ""),
                "component_id": str(item.get("component_id") or ""),
                "surface": str(item.get("surface") or ""),
                "disposition": str(item.get("disposition") or ""),
                "requirement_refs": _strings(item.get("requirement_refs")),
                "evidence_refs": _strings(item.get("evidence_refs")),
                "contract": str(item.get("contract") or ""),
            }
        )
        scenario_id = str(item.get("scenario_id") or "")
        surface = str(item.get("surface") or "")
        if scenario_id in scenario_ids and surface in REQUIREMENT_SURFACES:
            edges.append(_edge(_surface_id(scenario_id, surface), str(item.get("id") or ""), "realizes", scenario_id=scenario_id, component_id=str(item.get("component_id") or "")))
        for ref in _strings(item.get("requirement_refs")):
            if ref in claim_ids or ref in scenario_ids:
                edges.append(_edge(ref, str(item.get("id") or ""), "realizes", scenario_id=scenario_id, component_id=str(item.get("component_id") or "")))

    for item in _dicts(design_graph.get("edges")):
        kind = str(item.get("kind") or "")
        if kind not in TRACE_EDGE_KINDS:
            kind = "depends_on"
        trace_edge = _edge(
            str(item.get("from") or ""),
            str(item.get("to") or ""),
            kind,
            scenario_id=str(item.get("scenario_id") or ""),
        )
        trace_edge.update(
            {
                "source_edge_id": str(item.get("id") or ""),
                "label": str(item.get("label") or ""),
                "guard": str(item.get("guard") or ""),
                "branch_group": str(item.get("branch_group") or ""),
                "terminal": bool(item.get("terminal")),
                "order": int(item.get("order") or 0),
            }
        )
        edges.append(trace_edge)

    partitions = [
        {
            "use_case_id": str(item.get("use_case_id") or use_case_id),
            "scenario_id": str(item.get("scenario_id") or ""),
            "component_id": str(item.get("component_id") or ""),
            "coverage": dict(item.get("coverage") or {}) if isinstance(item.get("coverage"), dict) else {},
        }
        for item in _dicts(design_graph.get("partitions"))
    ]
    graph: dict[str, object] = {
        "schema_version": VERTICAL_TRACE_GRAPH_VERSION,
        "revision": 1,
        "run_id": requirement_model.run_id,
        "phase_gate": "design",
        "binding": {
            "requirement_revision": requirement_model.revision,
            "requirement_digest": _canonical_hash(requirement_payload),
            "design_contract_version": GRAPH_VERSION,
            "design_graph_revision": int(design_graph.get("revision") or 0),
            "design_graph_digest": str(design_graph.get("artifact_digest") or ""),
            "snapshot_binding_digest": _canonical_hash(_dict(design_graph.get("snapshot_binding"))),
        },
        "partitions": sorted(partitions, key=_partition_sort_key),
        "nodes": sorted(nodes, key=lambda item: str(item.get("id") or "")),
        "edges": sorted(_dedupe_edges(edges), key=lambda item: str(item.get("id") or "")),
    }
    graph["artifact_digest"] = _graph_digest(graph)
    return validate_vertical_trace_graph(graph, expected_run_id=requirement_model.run_id)


def validate_vertical_trace_graph(
    value: object,
    *,
    expected_run_id: str | None = None,
    expected_requirement_digest: str | None = None,
    expected_design_digest: str | None = None,
) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema_version") != VERTICAL_TRACE_GRAPH_VERSION:
        raise HarnessError("vertical_trace_graph_invalid", "Unsupported vertical trace graph contract.", run_id=expected_run_id)
    run_id = str(value.get("run_id") or "")
    if not run_id or (expected_run_id and run_id != expected_run_id):
        raise HarnessError("vertical_trace_graph_invalid", "Vertical trace run identity is invalid.", run_id=expected_run_id)
    if value.get("phase_gate") != "design" or not isinstance(value.get("revision"), int):
        raise HarnessError("vertical_trace_graph_invalid", "Vertical trace phase/revision is invalid.", run_id=run_id)
    binding = _dict(value.get("binding"))
    if expected_requirement_digest and binding.get("requirement_digest") != expected_requirement_digest:
        raise HarnessError("vertical_trace_binding_stale", "Vertical trace Requirement binding is stale.", run_id=run_id)
    if expected_design_digest and binding.get("design_graph_digest") != expected_design_digest:
        raise HarnessError("vertical_trace_binding_stale", "Vertical trace Design binding is stale.", run_id=run_id)

    nodes = _dicts(value.get("nodes"))
    node_ids: set[str] = set()
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in node_ids:
            raise HarnessError("vertical_trace_graph_invalid", "Vertical trace node identity is invalid.", run_id=run_id)
        if str(node.get("layer") or "") not in TRACE_LAYERS or str(node.get("kind") or "") not in TRACE_NODE_KINDS:
            raise HarnessError("vertical_trace_graph_invalid", f"Vertical trace node {node_id} has an invalid type.", run_id=run_id)
        node_ids.add(node_id)
    partitions = _dicts(value.get("partitions"))
    partition_keys: set[tuple[str, str, str]] = set()
    for partition in partitions:
        key = _partition_sort_key(partition)
        if not all(key) or key in partition_keys:
            raise HarnessError("vertical_trace_graph_invalid", "Vertical trace partition identity is invalid.", run_id=run_id)
        coverage = _dict(partition.get("coverage"))
        if any(surface not in REQUIREMENT_SURFACES for surface in coverage):
            raise HarnessError("vertical_trace_graph_invalid", "Vertical trace partition coverage is invalid.", run_id=run_id)
        partition_keys.add(key)
    edge_ids: set[str] = set()
    for edge in _dicts(value.get("edges")):
        edge_id = str(edge.get("id") or "")
        if not edge_id or edge_id in edge_ids or str(edge.get("kind") or "") not in TRACE_EDGE_KINDS:
            raise HarnessError("vertical_trace_graph_invalid", "Vertical trace edge identity/type is invalid.", run_id=run_id)
        if str(edge.get("from") or "") not in node_ids or str(edge.get("to") or "") not in node_ids:
            raise HarnessError("vertical_trace_graph_invalid", f"Vertical trace edge {edge_id} is dangling.", run_id=run_id)
        edge_ids.add(edge_id)
    if value.get("artifact_digest") != _graph_digest(value):
        raise HarnessError("vertical_trace_graph_invalid", "Vertical trace graph digest does not match its content.", run_id=run_id)
    return value


def validate_vertical_trace_coverage(
    graph: dict[str, object],
    *,
    mode: str,
) -> VerticalTraceValidationResult:
    if mode not in VERTICAL_TRACE_MODES:
        raise HarnessError("vertical_trace_mode_invalid", f"Unsupported vertical trace mode: {mode}")
    validate_vertical_trace_graph(graph)
    nodes = _dicts(graph.get("nodes"))
    edges = _dicts(graph.get("edges"))
    by_id = {str(item.get("id") or ""): item for item in nodes}
    design_nodes = [item for item in nodes if item.get("layer") == "design"]
    scenarios = [item for item in nodes if item.get("kind") == "scenario"]
    surfaces = [item for item in nodes if item.get("kind") == "surface_requirement"]
    blockers: list[DesignFinding] = []

    known_refs = {
        str(item.get("id") or "")
        for item in nodes
        if item.get("layer") == "requirement" and item.get("kind") != "surface_requirement"
    }
    for node in design_nodes:
        node_id = str(node.get("id") or "")
        scenario_id = str(node.get("scenario_id") or "")
        unknown = sorted(set(_strings(node.get("requirement_refs"))).difference(known_refs))
        if unknown:
            blockers.append(_finding("unknown_requirement_ref", node_id, f"Design node references unknown Requirement IDs: {', '.join(unknown)}.", requirement_ids=unknown, design_ids=[node_id]))
        if scenario_id not in by_id or by_id.get(scenario_id, {}).get("kind") != "scenario":
            blockers.append(_finding("unknown_scenario_ref", node_id, f"Design node references unknown scenario {scenario_id or '<empty>'}.", design_ids=[node_id]))
        incoming_requirement = any(
            edge.get("to") == node_id
            and edge.get("kind") == "realizes"
            and by_id.get(str(edge.get("from") or ""), {}).get("layer") == "requirement"
            for edge in edges
        )
        if not incoming_requirement:
            blockers.append(
                _finding(
                    "design_node_untraced",
                    node_id,
                    "Design node has no incoming Requirement realization edge.",
                    design_ids=[node_id],
                )
            )

    covered_surface_count = 0
    required_surface_count = 0
    for surface_node in surfaces:
        surface_id = str(surface_node.get("id") or "")
        status = str(surface_node.get("status") or "")
        realized = [edge for edge in edges if edge.get("from") == surface_id and edge.get("kind") == "realizes"]
        if status == "required":
            required_surface_count += 1
            if realized:
                covered_surface_count += 1
            else:
                blockers.append(_finding("required_surface_missing", surface_id, "Required scenario surface has no Design element.", requirement_ids=_strings(surface_node.get("requirement_refs"))))
        elif status == "not_applicable" and realized:
            blockers.append(_finding("not_applicable_surface_designed", surface_id, "A not-applicable surface has a Design element.", design_ids=[str(item.get("to") or "") for item in realized]))
        elif status not in {"required", "not_applicable"}:
            blockers.append(_finding("surface_status_unresolved", surface_id, f"Surface status {status or '<empty>'} is not valid at the Design gate."))

    for scenario in scenarios:
        scenario_id = str(scenario.get("id") or "")
        acceptance_refs = [
            str(edge.get("to") or "")
            for edge in edges
            if edge.get("from") == scenario_id and edge.get("kind") == "contains" and by_id.get(str(edge.get("to") or ""), {}).get("kind") == "acceptance_criterion"
        ]
        for ref in acceptance_refs:
            linked = any(edge.get("from") == ref and edge.get("kind") == "realizes" for edge in edges)
            if not linked:
                blockers.append(_finding("acceptance_trace_missing", ref, f"Acceptance criterion {ref} is not realized by a Design element in {scenario_id}.", requirement_ids=[ref]))

    design_edges = [edge for edge in edges if by_id.get(str(edge.get("from") or ""), {}).get("layer") == "design" and by_id.get(str(edge.get("to") or ""), {}).get("layer") == "design"]
    allowed_cross_scenario_count = 0
    for edge in design_edges:
        source = by_id[str(edge.get("from") or "")]
        target = by_id[str(edge.get("to") or "")]
        if source.get("scenario_id") != target.get("scenario_id"):
            source_scenario = by_id.get(str(source.get("scenario_id") or ""), {})
            target_scenario = by_id.get(str(target.get("scenario_id") or ""), {})
            endpoint_kinds = {
                str(source_scenario.get("scenario_kind") or ""),
                str(target_scenario.get("scenario_kind") or ""),
            }
            declared_scenario = str(edge.get("scenario_id") or "")
            declared_matches_endpoint = declared_scenario in {
                str(source.get("scenario_id") or ""),
                str(target.get("scenario_id") or ""),
            }
            main_branch_link = (
                "main" in endpoint_kinds
                and bool(endpoint_kinds.intersection({"alternative", "exception"}))
                and declared_matches_endpoint
                and str(edge.get("kind") or "") in {"depends_on", "calls", "returns"}
            )
            if main_branch_link:
                allowed_cross_scenario_count += 1
                continue
            blockers.append(_finding("cross_scenario_design_edge", str(edge.get("id") or ""), "Design interaction edge crosses unrelated scenario partitions.", design_ids=[str(source.get("id") or ""), str(target.get("id") or "")]))

    blockers = [
        replace(item, snapshot_fingerprint=str(graph.get("artifact_digest") or ""))
        for item in _dedupe_findings(blockers)
    ]
    coverage = {
        "scenario_count": len(scenarios),
        "required_surface_count": required_surface_count,
        "covered_surface_count": covered_surface_count,
        "design_node_count": len(design_nodes),
        "edge_count": len(edges),
        "allowed_cross_scenario_count": allowed_cross_scenario_count,
    }
    return VerticalTraceValidationResult(graph=graph, blocking_findings=blockers, coverage=coverage, mode=mode)


def validate_vertical_trace_report(
    value: object,
    *,
    graph: dict[str, object],
    expected_mode: str,
) -> dict[str, object]:
    """Fail closed when an enforce-mode Phase 3 handoff uses a stale report."""

    if not isinstance(value, dict) or value.get("schema_version") != VERTICAL_TRACE_VALIDATION_VERSION:
        raise HarnessError("vertical_trace_validation_invalid", "Unsupported vertical trace validation report.")
    if expected_mode not in VERTICAL_TRACE_MODES or value.get("mode") != expected_mode:
        raise HarnessError("vertical_trace_validation_invalid", "Vertical trace validation mode does not match the run.")
    if value.get("contract_version") != VERTICAL_TRACE_GRAPH_VERSION:
        raise HarnessError("vertical_trace_validation_invalid", "Vertical trace validation contract is invalid.")
    if value.get("graph_digest") != graph.get("artifact_digest"):
        raise HarnessError("vertical_trace_binding_stale", "Vertical trace validation report is stale.")
    blocking = _dicts(value.get("blocking_findings"))
    blocking_count = int(value.get("blocking_count") or 0)
    if blocking_count != len(blocking):
        raise HarnessError("vertical_trace_validation_invalid", "Vertical trace blocker count is inconsistent.")
    if expected_mode == "enforce" and (not bool(value.get("ok")) or blocking_count):
        raise HarnessError("vertical_trace_validation_failed", "Vertical trace must pass before Phase 3.")
    return value


def bounded_trace_view(graph: dict[str, object], *, scenario_ids: set[str] | None = None) -> dict[str, object]:
    """Return a sanitized use-case/scenario trace view for semantic review."""

    validate_vertical_trace_graph(graph)
    selected = set(scenario_ids or [])
    nodes = _dicts(graph.get("nodes"))
    if selected:
        kept_ids = {
            str(item.get("id") or "")
            for item in nodes
            if not str(item.get("scenario_id") or "") or str(item.get("scenario_id") or "") in selected
        }
    else:
        kept_ids = {str(item.get("id") or "") for item in nodes}
    return {
        "schema_version": "vertical_trace_semantic_view_v1",
        "phase_gate": graph.get("phase_gate"),
        "graph_digest": graph.get("artifact_digest"),
        "nodes": [item for item in nodes if str(item.get("id") or "") in kept_ids],
        "edges": [item for item in _dicts(graph.get("edges")) if str(item.get("from") or "") in kept_ids and str(item.get("to") or "") in kept_ids],
    }


def _requirement_claims(model: dict[str, object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    sections: list[tuple[str, object]] = [
        ("precondition", model.get("preconditions")),
        ("postcondition", model.get("postconditions")),
        ("business_rule", model.get("business_rules")),
        ("acceptance_criterion", model.get("acceptance_criteria")),
        ("quality_requirement", model.get("quality_requirements")),
    ]
    flows = _dict(model.get("flows"))
    sections.extend((kind, flows.get(key)) for key, kind in (("main", "main_flow"), ("alternatives", "alternative_flow"), ("exceptions", "exception_flow")))
    for claim_type, raw_items in sections:
        for item in _dicts(raw_items):
            claim_id = str(item.get("id") or "")
            if not claim_id:
                continue
            content = " ".join(str(item.get(key) or "") for key in ("text", "actor", "action", "target", "trigger", "outcome") if item.get(key)).strip()
            records.append(
                {
                    "id": claim_id,
                    "layer": "requirement",
                    "kind": "acceptance_criterion" if claim_type == "acceptance_criterion" else "requirement_claim",
                    "claim_type": claim_type,
                    "label": content or claim_id,
                    "scenario_id": "",
                    "component_id": "",
                    "surface": "",
                    "requirement_refs": [],
                    "evidence_refs": _strings(item.get("evidence_refs")),
                    "status": str(item.get("status") or ""),
                }
            )
    unique = {str(item["id"]): item for item in records}
    return [unique[key] for key in sorted(unique)]


def _edge(source: str, target: str, kind: str, *, scenario_id: str = "", component_id: str = "") -> dict[str, object]:
    canonical = f"{source}\0{kind}\0{target}\0{scenario_id}\0{component_id}"
    return {
        "id": "VTE-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "from": source,
        "to": target,
        "kind": kind,
        "scenario_id": scenario_id,
        "component_id": component_id,
    }


def _finding(category: str, subject: str, message: str, *, requirement_ids: list[str] | None = None, design_ids: list[str] | None = None) -> DesignFinding:
    canonical = f"{category}\0{subject}"
    return DesignFinding(
        finding_id="TRACE-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
        category=category,
        severity="blocking",
        message=message,
        expected_change="Update the exact Requirement/Design trace partition and regenerate the vertical trace graph.",
        requirement_ids=sorted(set(requirement_ids or [])),
        diagram_ids=sorted(set(design_ids or [])),
        evidence=[{"source": "vertical_trace_graph", "ref": subject, "excerpt": message}],
        subject_ref=subject,
        origin="deterministic",
        repair_owner="designer",
        repairability="repairable",
    )


def _dedupe_findings(items: list[DesignFinding]) -> list[DesignFinding]:
    unique = {item.canonical_key: item for item in items}
    return [unique[key] for key in sorted(unique)]


def _dedupe_edges(items: list[dict[str, object]]) -> list[dict[str, object]]:
    unique = {str(item.get("id") or ""): item for item in items}
    return list(unique.values())


def _surface_id(scenario_id: str, surface: str) -> str:
    return f"SURF:{scenario_id}:{surface}"


def _partition_sort_key(item: dict[str, object]) -> tuple[str, str, str]:
    return (str(item.get("use_case_id") or ""), str(item.get("scenario_id") or ""), str(item.get("component_id") or ""))


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _graph_digest(graph: dict[str, object]) -> str:
    return _canonical_hash({key: value for key, value in graph.items() if key != "artifact_digest"})


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []
