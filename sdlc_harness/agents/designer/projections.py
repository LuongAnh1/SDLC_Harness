"""Deterministic human and compatibility projections for the L3 Design graph."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .graph import DESIGN_BUNDLE_VERSION


def render_design_projections(
    graph: dict[str, object],
    *,
    requirement_model: dict[str, object] | None = None,
) -> dict[str, object]:
    nodes = _dicts(graph.get("nodes"))
    edges = _dicts(graph.get("edges"))
    class_nodes = [
        _class_node(
            item,
            incoming_edges=[edge for edge in edges if str(edge.get("to") or "") == str(item.get("id") or "")],
        )
        for item in nodes
        if str(item.get("kind") or "") != "interaction_step"
    ]
    class_ids = {str(item["id"]) for item in class_nodes}
    relationships = [
        {
            "from": str(edge.get("from") or ""),
            "to": str(edge.get("to") or ""),
            "relation": "-->",
            "kind": str(edge.get("kind") or "depends_on"),
        }
        for edge in edges
        if str(edge.get("from") or "") in class_ids and str(edge.get("to") or "") in class_ids
    ]
    class_graph = {
        "version": "class_diagram_graph_v1",
        "nodes": class_nodes,
        "relationships": relationships,
        "others": [],
    }
    sequence_diagrams = []
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    scenario_ids = sorted({str(item.get("scenario_id") or "") for item in nodes if str(item.get("scenario_id") or "")})
    for scenario_id in scenario_ids:
        scenario_edges = [
            item
            for item in edges
            if str(item.get("scenario_id") or "") == scenario_id
            and str(item.get("kind") or "") in {"calls", "reads", "writes", "returns"}
        ]
        scenario_edges.sort(key=lambda item: (int(item.get("order") or 0), str(item.get("id") or "")))
        scenario_ids_set = {
            str(item.get("id") or "")
            for item in nodes
            if str(item.get("scenario_id") or "") == scenario_id
        }
        scenario_ids_set.update(
            str(edge.get(endpoint) or "")
            for edge in scenario_edges
            for endpoint in ("from", "to")
        )
        scenario_nodes = [nodes_by_id[node_id] for node_id in sorted(scenario_ids_set) if node_id in nodes_by_id]
        participants = [
            {
                "id": str(item.get("id") or ""),
                "type": _participant_type(str(item.get("surface") or "")),
                "label": _display_name(item),
            }
            for item in scenario_nodes
        ]
        messages, control_flows, events = _sequence_events(scenario_id=scenario_id, edges=scenario_edges)
        sequence_diagrams.append(
            {
                "id": f"sequence_{_slug(scenario_id)}",
                "nodes": participants,
                "messages": messages,
                "controlFlows": control_flows,
                "events": events,
                "parseIssues": [],
            }
        )
    sequence_graph = {"version": "sequence_diagram_graph_v2", "sequenceDiagrams": sequence_diagrams}
    component_view = _component_view(nodes, edges)
    surface_view = _surface_view(nodes, graph=graph, requirement_model=requirement_model or {})
    markdown = _render_markdown(
        graph=graph,
        class_nodes=class_nodes,
        relationships=relationships,
        sequence_diagrams=sequence_diagrams,
        component_view=component_view,
        surface_view=surface_view,
    )
    return {
        "design_markdown": markdown,
        "class_graph": class_graph,
        "sequence_graph": sequence_graph,
        "component_view": component_view,
        "surface_view": surface_view,
    }


def build_deterministic_trace(
    *,
    requirement_graph: dict[str, object],
    graph: dict[str, object],
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    nodes = _dicts(graph.get("nodes"))
    class_targets = {
        str(item.get("id") or ""): str(item.get("className") or item.get("id") or "")
        for item in _dicts(class_graph.get("nodes"))
    }
    sequence_targets = {str(item.get("id") or "") for item in _dicts(sequence_graph.get("sequenceDiagrams"))}
    class_trace: list[dict[str, object]] = []
    sequence_trace: list[dict[str, object]] = []
    for requirement in _dicts(requirement_graph.get("requirements")):
        requirement_id = str(requirement.get("id") or "")
        content = str(requirement.get("content") or "")
        matching = [
            item
            for item in nodes
            if requirement_id in _strings(item.get("requirement_refs"))
        ]
        if not matching:
            # L1's scenario contract does not attach use-case-wide preconditions
            # to one surface. Keep the legacy Phase 3 projection complete while
            # the canonical graph retains explicit node-level Requirement refs.
            matching = nodes
        class_links = [
            {"target": class_targets[str(item.get("id") or "")], "reason": "Canonical L3 graph node realizes this requirement."}
            for item in matching
            if str(item.get("id") or "") in class_targets
        ]
        scenario_ids = {str(item.get("scenario_id") or "") for item in matching}
        sequence_links = [
            {"target": target, "reason": "Canonical L3 scenario partition realizes this requirement."}
            for target in sorted(sequence_targets)
            if target.removeprefix("sequence_").replace("_", "-") in {value.replace("_", "-") for value in scenario_ids}
        ]
        if not sequence_links:
            sequence_links = [
                {"target": target, "reason": "Canonical L3 interaction projection realizes this requirement."}
                for target in sorted(sequence_targets)
            ]
        class_trace.append({"requirement": requirement_id, "content": content, "links": class_links})
        sequence_trace.append({"requirement": requirement_id, "content": content, "links": sequence_links})
    return class_trace, sequence_trace


def projection_manifest(*, graph: dict[str, object], artifacts: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": "design_projection_manifest_v1",
        "design_bundle_contract_version": DESIGN_BUNDLE_VERSION,
        "graph_revision": int(graph.get("revision") or 0),
        "graph_digest": str(graph.get("artifact_digest") or ""),
        "projections": [
            {"artifact_key": key, "artifact_ref": artifacts[key]}
            for key in sorted(artifacts)
            if key in {
                "design_artifact",
                "class_diagram_graph",
                "sequence_diagram_graph",
                "requirement_design_trace",
                "design_component_view",
                "design_surface_view",
            }
        ],
    }


def _render_markdown(
    *,
    graph: dict[str, object],
    class_nodes: list[dict[str, object]],
    relationships: list[dict[str, object]],
    sequence_diagrams: list[dict[str, object]],
    component_view: dict[str, object],
    surface_view: dict[str, object],
) -> str:
    lines = [
        "# Vertical Design",
        "",
        f"- Contract: `{graph.get('schema_version', '')}`",
        f"- Revision: `{graph.get('revision', 0)}`",
        f"- Graph digest: `{graph.get('artifact_digest', '')}`",
        "",
        "## Class Diagram",
        "",
        "@startuml",
    ]
    for node in class_nodes:
        identifier = _uml_id(str(node.get("id") or "node"))
        label = str(node.get("className") or identifier).replace('"', "'")
        stereotype = str(node.get("stereotype") or "component")
        lines.extend([f'class "{label}" as {identifier} <<{stereotype}>> {{', "  +execute() : void", "}"])
    for edge in relationships:
        lines.append(f"{_uml_id(str(edge['from']))} --> {_uml_id(str(edge['to']))} : {edge['kind']}")
    lines.extend(["@enduml", "", "## Sequence Diagrams", ""])
    for diagram in sequence_diagrams:
        lines.extend([f"### {diagram['id']}", "", "@startuml"])
        for node in _dicts(diagram.get("nodes")):
            lines.append(f'participant "{str(node.get("label") or "Component").replace(chr(34), chr(39))}" as {_uml_id(str(node.get("id") or "node"))}')
        for event in _dicts(diagram.get("events")):
            if str(event.get("eventType") or "") == "control_start":
                lines.append(f"opt {str(event.get('condition') or 'guarded branch')}")
            elif str(event.get("eventType") or "") == "control_end":
                lines.append("end")
            elif str(event.get("eventType") or "") == "message":
                lines.append(
                    f"{_uml_id(str(event.get('from') or 'source'))} -> {_uml_id(str(event.get('to') or 'target'))}: "
                    f"{str(event.get('message') or 'calls')}"
                )
        lines.extend(["@enduml", ""])
    lines.extend(["## AS-IS / GAP / TO-BE", ""])
    for partition in _dicts(graph.get("partitions")):
        lines.append(
            f"- `{partition.get('scenario_id', '')}` / `{partition.get('component_id', '')}`: "
            f"{partition.get('coverage', {})}"
        )
    lines.extend(["", "## Component Architecture", "", "| Component | Design elements |", "| --- | ---: |"])
    for component in _dicts(component_view.get("components")):
        lines.append(f"| {component.get('component_id', '')} | {len(_dicts(component.get('nodes')))} |")
    lines.extend(["", "## Surface Contracts", ""])
    for title, key in (
        ("UI", "ui"),
        ("API", "api"),
        ("Business Rules", "business_rules"),
        ("Data", "data"),
    ):
        lines.extend([f"### {title}", "", "| Scenario | Element | Disposition | Contract |", "| --- | --- | --- | --- |"])
        for item in _dicts(surface_view.get(key)):
            lines.append(
                f"| {item.get('scenario_id', '')} | {item.get('name', '')} | "
                f"{item.get('disposition', '')} | {str(item.get('contract') or '').replace('|', '/')} |"
            )
        if not _dicts(surface_view.get(key)):
            lines.append("| — | Not applicable | — | No required surface in this use case. |")
        lines.append("")
    lines.extend(["## Change Impact", "", "| Scenario | Surface | Element | Decision | Evidence |", "| --- | --- | --- | --- | --- |"]) 
    for item in _dicts(surface_view.get("change_impact")):
        evidence = ", ".join(_strings(item.get("evidence_refs"))) or str(item.get("evidence_reason") or "")
        lines.append(
            f"| {item.get('scenario_id', '')} | {item.get('surface', '')} | {item.get('name', '')} | "
            f"{item.get('disposition', '')} | {evidence.replace('|', '/')} |"
        )
    return "\n".join(lines).strip()


def _class_node(node: dict[str, object], *, incoming_edges: list[dict[str, object]]) -> dict[str, object]:
    operations = list(
        dict.fromkeys(
            _operation_name(str(edge.get("label") or edge.get("kind") or "execute"))
            for edge in incoming_edges
            if str(edge.get("kind") or "") in {"calls", "reads", "writes"}
        )
    )
    if not operations:
        operations = ["execute"]
    return {
        "id": str(node.get("id") or ""),
        "type": "class",
        "className": _display_name(node),
        "stereotype": str(node.get("component_id") or node.get("surface") or "component"),
        "attributes": [],
        "methods": [
            {"visibility": "+", "name": operation, "parameters": [], "returnType": "void"}
            for operation in operations
        ],
        "designKind": str(node.get("kind") or ""),
        "disposition": str(node.get("disposition") or ""),
        "evidenceRefs": _strings(node.get("evidence_refs")),
    }


def _display_name(node: dict[str, object]) -> str:
    return str(node.get("name") or node.get("semantic_key") or node.get("kind") or "DesignElement")


def _sequence_events(
    *,
    scenario_id: str,
    edges: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    messages: list[dict[str, object]] = []
    control_flows: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    event_order = 0
    index = 0
    prefix = f"sequence_{_slug(scenario_id)}"
    while index < len(edges):
        branch_group = str(edges[index].get("branch_group") or "")
        guard = str(edges[index].get("guard") or "")
        fragment_id = ""
        if branch_group:
            event_order += 1
            fragment_id = f"{prefix}:fragment:{event_order:04d}"
            start = {
                "id": fragment_id,
                "order": event_order,
                "eventType": "control_start",
                "type": "opt",
                "condition": guard,
                "branchGroup": branch_group,
                "depth": 0,
                "parentId": "",
            }
            control_flows.append(dict(start))
            events.append(dict(start))
        while index < len(edges) and str(edges[index].get("branch_group") or "") == branch_group:
            edge = edges[index]
            event_order += 1
            message = {
                "id": f"{prefix}:message:{event_order:04d}",
                "order": event_order,
                "eventType": "message",
                "from": str(edge.get("from") or ""),
                "to": str(edge.get("to") or ""),
                "arrow": "-->",
                "type": "return" if str(edge.get("kind") or "") == "returns" else "synchronous",
                "message": f"{_operation_name(str(edge.get('label') or edge.get('kind') or 'execute'))}()",
                "controlPath": [fragment_id] if fragment_id else [],
                "terminal": bool(edge.get("terminal", False)),
            }
            messages.append(dict(message))
            events.append(dict(message))
            index += 1
        if fragment_id:
            event_order += 1
            end = {
                "id": f"{prefix}:control:{event_order:04d}",
                "order": event_order,
                "eventType": "control_end",
                "type": "end",
                "condition": "",
                "branchGroup": branch_group,
                "depth": 0,
                "parentId": fragment_id,
            }
            control_flows.append(dict(end))
            events.append(dict(end))
    return messages, control_flows, events


def _operation_name(label: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", label)
    if not words:
        return "execute"
    first, *rest = words
    value = first.casefold() + "".join(word[:1].upper() + word[1:] for word in rest)
    return f"op{value[:1].upper()}{value[1:]}" if value[:1].isdigit() else value


def _component_view(nodes: list[dict[str, object]], edges: list[dict[str, object]]) -> dict[str, object]:
    components: dict[str, list[dict[str, object]]] = {}
    node_components: dict[str, str] = {}
    for node in nodes:
        component_id = str(node.get("component_id") or "unknown")
        node_id = str(node.get("id") or "")
        node_components[node_id] = component_id
        components.setdefault(component_id, []).append(_projection_node(node))
    dependency_groups: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        source = node_components.get(str(edge.get("from") or ""), "")
        target = node_components.get(str(edge.get("to") or ""), "")
        if not source or not target or source == target:
            continue
        dependency_groups.setdefault((source, target), []).append(str(edge.get("label") or edge.get("kind") or "depends"))
    dependencies = [
        {"from": source, "to": target, "labels": sorted(dict.fromkeys(labels))}
        for (source, target), labels in sorted(dependency_groups.items())
    ]
    component_rows = [
        {
            "component_id": key,
            "node_refs": sorted(str(item.get("id") or "") for item in value),
            "nodes": sorted(value, key=lambda item: (str(item.get("scenario_id") or ""), str(item.get("id") or ""))),
        }
        for key, value in sorted(components.items())
    ]
    return {
        "schema_version": "design_component_view_v2",
        "components": component_rows,
        "dependencies": dependencies,
        "plantuml": _component_plantuml(component_rows, dependencies),
    }


def _surface_view(
    nodes: list[dict[str, object]],
    *,
    graph: dict[str, object],
    requirement_model: dict[str, object],
) -> dict[str, object]:
    projected = [_projection_node(node) for node in nodes]
    grouped = {
        "ui": [item for item in projected if item["surface"] == "ui"],
        "api": [item for item in projected if item["surface"] == "api"],
        "business_rules": [item for item in projected if item["surface"] == "business_rule"],
        "data": [item for item in projected if item["surface"] == "data"],
        "interaction": [item for item in projected if item["surface"] == "interaction"],
    }
    for values in grouped.values():
        values.sort(key=lambda item: (str(item.get("scenario_id") or ""), str(item.get("id") or "")))
    coverage = [
        {
            "scenario_id": str(item.get("scenario_id") or ""),
            "component_id": str(item.get("component_id") or ""),
            "surfaces": dict(item.get("coverage") or {}) if isinstance(item.get("coverage"), dict) else {},
        }
        for item in _dicts(graph.get("partitions"))
    ]
    not_applicable: list[dict[str, str]] = []
    scenario_contract = _dict(requirement_model.get("scenario_contract"))
    for scenario in _dicts(scenario_contract.get("scenarios")):
        scenario_id = str(scenario.get("id") or "")
        for surface, raw in _dict(scenario.get("surfaces")).items():
            contract = _dict(raw)
            if str(contract.get("status") or "") == "not_applicable":
                not_applicable.append(
                    {
                        "scenario_id": scenario_id,
                        "surface": str(surface),
                        "reason": str(contract.get("reason") or "Not required by the approved scenario."),
                    }
                )
    return {
        "schema_version": "design_surface_view_v2",
        **grouped,
        "coverage": coverage,
        "not_applicable": not_applicable,
        "ui_state_diagrams": _ui_state_diagrams(grouped["ui"]),
        "data_model_plantuml": _data_model_plantuml(grouped["data"]),
        "change_impact": projected,
    }


def _projection_node(node: dict[str, object]) -> dict[str, object]:
    surface_contract = node.get("surface_contract")
    return {
        "id": str(node.get("id") or ""),
        "name": _display_name(node),
        "kind": str(node.get("kind") or ""),
        "surface": str(node.get("surface") or ""),
        "scenario_id": str(node.get("scenario_id") or ""),
        "component_id": str(node.get("component_id") or ""),
        "disposition": str(node.get("disposition") or ""),
        "contract": str(node.get("contract") or ""),
        "surface_contract": dict(surface_contract) if isinstance(surface_contract, dict) else {},
        "requirement_refs": _strings(node.get("requirement_refs")),
        "evidence_refs": _strings(node.get("evidence_refs")),
        "evidence_reason": str(node.get("evidence_reason") or ""),
    }


def _component_plantuml(components: list[dict[str, object]], dependencies: list[dict[str, object]]) -> str:
    lines = ["@startuml", "skinparam componentStyle rectangle", "left to right direction"]
    aliases: dict[str, str] = {}
    for component in components:
        component_id = str(component.get("component_id") or "component")
        alias = "C_" + _slug(component_id)
        aliases[component_id] = alias
        lines.append(f'component "{component_id.replace(chr(34), chr(39))}" as {alias}')
    for dependency in dependencies:
        source = str(dependency.get("from") or "")
        target = str(dependency.get("to") or "")
        labels = ", ".join(_strings(dependency.get("labels")))
        if source in aliases and target in aliases:
            lines.append(f"{aliases[source]} --> {aliases[target]} : {labels[:120]}")
    lines.append("@enduml")
    return "\n".join(lines)


def _ui_state_diagrams(nodes: list[dict[str, object]]) -> list[dict[str, str]]:
    diagrams: list[dict[str, str]] = []
    for node in nodes:
        contract = node.get("surface_contract") if isinstance(node.get("surface_contract"), dict) else {}
        transitions = _dicts(contract.get("transitions"))
        if not transitions:
            continue
        lines = ["@startuml", "hide empty description"]
        aliases: dict[str, str] = {}
        for transition in transitions:
            for key in ("from", "to"):
                state = str(transition.get(key) or "state")
                aliases.setdefault(state, "S_" + hashlib.sha256(state.encode("utf-8")).hexdigest()[:8])
        for state, alias in aliases.items():
            lines.append(f'state "{state.replace(chr(34), chr(39))}" as {alias}')
        for transition in transitions:
            source = aliases[str(transition.get("from") or "state")]
            target = aliases[str(transition.get("to") or "state")]
            label = f"{transition.get('action', '')} / {transition.get('outcome', '')}".replace(chr(34), chr(39))
            lines.append(f"{source} --> {target} : {label}")
        lines.append("@enduml")
        diagrams.append({"scenario_id": str(node.get("scenario_id") or ""), "node_id": str(node.get("id") or ""), "plantuml": "\n".join(lines)})
    return diagrams


def _data_model_plantuml(nodes: list[dict[str, object]]) -> str:
    entities: dict[str, dict[str, object]] = {}
    for node in nodes:
        contract = node.get("surface_contract") if isinstance(node.get("surface_contract"), dict) else {}
        entity = str(contract.get("entity") or "").strip()
        if entity:
            entities.setdefault(entity, dict(contract))
    if not entities:
        return ""
    lines = ["@startuml", "hide methods", "hide stereotypes"]
    for entity, contract in sorted(entities.items()):
        alias = "D_" + hashlib.sha256(entity.encode("utf-8")).hexdigest()[:8]
        lines.append(f'entity "{entity.replace(chr(34), chr(39))}" as {alias} {{')
        for field in _dicts(contract.get("fields")):
            lines.append(f"  {field.get('name', '')} : {field.get('type', '')}")
        lines.append("}")
        notes = [*_strings(contract.get("constraints")), *_strings(contract.get("relations"))]
        if notes:
            lines.append(f"note right of {alias} : {'; '.join(notes).replace(chr(34), chr(39))}")
    lines.append("@enduml")
    return "\n".join(lines)


def _participant_type(surface: str) -> str:
    return "actor" if surface == "ui" else "database" if surface == "data" else "participant"


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower() or hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _uml_id(value: str) -> str:
    return "N_" + re.sub(r"[^A-Za-z0-9_]", "_", value)


def _dicts(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []
