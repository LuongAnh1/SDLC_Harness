"""Deterministic task planning for sliced Design semantic review."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...core.errors import HarnessError


SEMANTIC_REVIEW_MODES = frozenset({"legacy", "sliced_shadow", "sliced_enforce"})


def build_semantic_review_plan(
    *,
    claims: list[dict[str, object]],
    vertical_design_graph: dict[str, object] | None,
) -> dict[str, object]:
    """Build stable review tasks without asking the model how to divide the work."""

    graph = _dict_value(vertical_design_graph)
    nodes = _dict_items(graph.get("nodes"))
    edges = _dict_items(graph.get("edges"))
    tasks: list[dict[str, object]] = []
    by_scenario: dict[str, list[dict[str, object]]] = {}
    global_claims: list[dict[str, object]] = []
    for claim in claims:
        scenario_id = str(claim.get("scenario_id") or "")
        if scenario_id:
            by_scenario.setdefault(scenario_id, []).append(claim)
        else:
            global_claims.append(claim)

    boundary_by_scenario: dict[str, list[dict[str, object]]] = {}
    for edge in edges:
        if not _dict_value(edge.get("boundary_contract")):
            continue
        scenario_id = str(edge.get("scenario_id") or "global")
        boundary_by_scenario.setdefault(scenario_id, []).append(edge)

    global_boundaries = boundary_by_scenario.pop("global", [])
    scenario_ids = sorted(set(by_scenario) | set(boundary_by_scenario))
    if global_claims or global_boundaries:
        primary = sorted(_claim_ids(global_claims))
        # Global claims (pre/postconditions, quality rules) may apply positively or negatively to any scenario and
        # often have no direct trace mapping, so their packet is the whole graph rather than a requirement_refs guess.
        if global_claims:
            sliced_node_ids, sliced_edge_ids = sorted(_ids(nodes)), sorted(_ids(edges))
        else:
            sliced_node_ids, sliced_edge_ids = [], []
        global_edge_ids = set(_ids(global_boundaries))
        global_node_ids = {
            endpoint
            for edge in global_boundaries
            for endpoint in (str(edge.get("from") or ""), str(edge.get("to") or ""))
            if endpoint
        }
        tasks.append(
            _task(
                task_id="cross-cutting-global",
                task_type="cross_cutting",
                primary_claim_ids=primary,
                context_claim_ids=sorted(
                    claim_id for claim_id in _claim_ids(claims) if claim_id not in set(primary)
                ),
                scenario_ids=scenario_ids,
                node_ids=sorted(set(sliced_node_ids) | global_node_ids),
                edge_ids=sorted(set(sliced_edge_ids) | global_edge_ids),
                coverage_keys=[
                    *[f"claim:{item}" for item in primary],
                    *[f"boundary:{item}" for item in sorted(global_edge_ids)],
                ],
                focus=(
                    "Review only the primary global preconditions, postconditions, authentication versus "
                    "authorization, quality and negative constraints, and any promised consistency boundary across "
                    "scenarios. Scenario-local claims are evidence only and remain owned by their scenario calls."
                    + (
                        " Also review the global boundary contracts listed in coverage_keys for transport, response "
                        "and error feasibility, decision inputs, rollback, unchanged-state, atomicity and retry "
                        "mechanisms."
                        if global_edge_ids
                        else ""
                    )
                ),
            )
        )

    for scenario_id in scenario_ids:
        scenario_claim_ids = sorted(_claim_ids(by_scenario.get(scenario_id, [])))
        selected_boundaries = boundary_by_scenario.get(scenario_id, [])
        node_ids, edge_ids = _scenario_slice(scenario_id, nodes=nodes, edges=edges)
        if edge_ids:
            tasks.append(
                _task(
                    task_id=f"runtime-{_slug(scenario_id)}",
                    task_type="scenario_runtime",
                    primary_claim_ids=[],
                    context_claim_ids=scenario_claim_ids,
                    scenario_ids=[scenario_id],
                    node_ids=node_ids,
                    edge_ids=edge_ids,
                    coverage_keys=[f"boundary:{item}" for item in sorted(_ids(selected_boundaries))],
                    focus=(
                        "Review the assigned scenario and every outcome declared by its boundary coverage keys: "
                        "transport, response and error events can occur, causal order lets each guard observe its "
                        "condition, decisions receive every input they test, and promised rollback, unchanged-state, "
                        "atomicity or retry behavior has a credible runtime mechanism."
                    ),
                )
            )
        if scenario_claim_ids:
            tasks.append(
                _task(
                    task_id=f"scenario-{_slug(scenario_id)}",
                    task_type="scenario_behavior",
                    primary_claim_ids=scenario_claim_ids,
                    context_claim_ids=[],
                    scenario_ids=[scenario_id],
                    node_ids=node_ids,
                    edge_ids=edge_ids,
                    coverage_keys=[f"claim:{item}" for item in scenario_claim_ids],
                    focus=(
                        "Review behavior order, guards, terminal outcomes, required surfaces, observable feedback, and "
                        "whether the scenario satisfies its approved Requirement claims."
                    ),
                )
            )

    shared_surfaces: list[dict[str, object]] = []
    for identity, shared_nodes in sorted(_shared_surface_groups(nodes).items()):
        shared_scenario_ids = sorted(
            {str(node.get("scenario_id") or "") for node in shared_nodes if node.get("scenario_id")}
        )
        if len(shared_scenario_ids) < 2:
            continue
        shared_surfaces.append(
            {
                "identity": identity,
                "scenario_ids": shared_scenario_ids,
                "node_ids": sorted(_ids(shared_nodes)),
            }
        )

    _validate_plan_coverage(tasks=tasks, claims=claims, edges=edges)
    payload: dict[str, object] = {
        "schema_version": "semantic_review_plan_v2",
        "graph_digest": str(graph.get("artifact_digest") or ""),
        "tasks": tasks,
        "shared_surfaces": shared_surfaces,
        "coverage": {
            "claim_count": len(claims),
            "boundary_count": sum(1 for edge in edges if _dict_value(edge.get("boundary_contract"))),
            "shared_surface_count": len(shared_surfaces),
            "task_count": len(tasks),
        },
    }
    payload["plan_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def materialize_task_context(
    task: dict[str, object],
    *,
    claims: list[dict[str, object]],
    vertical_design_graph: dict[str, object] | None,
    vertical_trace: dict[str, object] | None,
) -> dict[str, object]:
    """Resolve a planned task to a bounded, exact-ID review packet."""

    graph = _dict_value(vertical_design_graph)
    nodes = _dict_items(graph.get("nodes"))
    edges = _dict_items(graph.get("edges"))
    nodes_by_id = {str(node.get("id") or ""): node for node in nodes}
    edges_by_id = {str(edge.get("id") or ""): edge for edge in edges}
    planned_node_ids = set(_string_items(task.get("node_ids")))
    planned_edge_ids = set(_string_items(task.get("edge_ids")))
    node_ids = set(planned_node_ids)
    edge_ids = set(planned_edge_ids)
    claim_ids = set(_string_items(task.get("primary_claim_ids"))) | set(
        _string_items(task.get("context_claim_ids"))
    )
    selected_claims = [claim for claim in claims if str(claim.get("claim_id") or "") in claim_ids]
    trace_source_ids = {
        source_id
        for claim in selected_claims
        for source_id in _claim_trace_source_ids(claim)
    }
    trace = _dict_value(vertical_trace)
    ui_context = _dict_value(_dict_value(graph.get("snapshot_binding")).get("ui_repo_context"))
    all_mappings = _dict_items(trace.get("mappings"))
    scenario_ids = set(_string_items(task.get("scenario_ids")))
    for mapping in all_mappings:
        if str(mapping.get("from") or "") not in trace_source_ids:
            continue
        subject_id = str(mapping.get("to") or "")
        if subject_id in nodes_by_id:
            subject_scenario = str(nodes_by_id[subject_id].get("scenario_id") or "")
            if (
                subject_id in planned_node_ids
                or not scenario_ids
                or subject_scenario in {"", "global", *scenario_ids}
            ):
                node_ids.add(subject_id)
        if subject_id in edges_by_id:
            subject_scenario = str(edges_by_id[subject_id].get("scenario_id") or "")
            if (
                subject_id in planned_edge_ids
                or not scenario_ids
                or subject_scenario in {"", "global", *scenario_ids}
            ):
                edge_ids.add(subject_id)
    node_ids, edge_ids = _bounded_relation_closure(
        task=task,
        seed_node_ids=node_ids,
        selected_edge_ids=edge_ids,
        nodes_by_id=nodes_by_id,
        edges=edges,
    )
    for edge_id in edge_ids:
        edge = edges_by_id.get(edge_id, {})
        node_ids.update(
            endpoint
            for endpoint in (str(edge.get("from") or ""), str(edge.get("to") or ""))
            if endpoint in nodes_by_id
        )
    final_subject_ids = node_ids | edge_ids
    mappings = [
        mapping
        for mapping in all_mappings
        if (
            str(mapping.get("from") or "") in trace_source_ids
            and str(mapping.get("to") or "") in final_subject_ids
        )
        or {
            str(mapping.get("from") or ""),
            str(mapping.get("to") or ""),
        }.issubset(final_subject_ids)
    ]
    branch_entries = [
        _branch_entry(edge, nodes_by_id=nodes_by_id)
        for edge in edges
        if str(edge.get("id") or "") in edge_ids and str(edge.get("kind") or "") == "depends_on"
    ]
    task_type = str(task.get("task_type") or "")
    selected_nodes = [node for node in nodes if str(node.get("id") or "") in node_ids]
    approved_context = _approved_context(claims=claims, nodes=selected_nodes)
    return {
        "graph_digest": graph.get("artifact_digest"),
        "task": task,
        "claims": selected_claims,
        "nodes": [
            review_node_view(node, task_type=task_type, scenario_ids=scenario_ids)
            for node in selected_nodes
        ],
        "edges": [
            review_edge_view(edge, task_type=task_type)
            for edge in edges
            if str(edge.get("id") or "") in edge_ids
        ],
        # Runtime feasibility is judged from the executable graph; Requirement-to-Design mappings serve claim reviews.
        **({} if task_type == "scenario_runtime" else {"trace_mappings": mappings}),
        "unresolved_items": graph.get("unresolved_items") or [],
        **({"branch_entries": branch_entries} if branch_entries else {}),
        **({"approved_context": approved_context} if approved_context else {}),
        **(
            {
                "ui_repo_context": {
                    key: ui_context.get(key)
                    for key in ("entrypoint", "composition_chain", "data_clients", "auth", "styles", "verification")
                    if key in ui_context
                }
            }
            if ui_context and task_type != "scenario_behavior"
            else {}
        ),
    }


# Audit and provenance fields support artifact review, not semantic judgement; they only add tokens to a review call.
_AUDIT_NODE_KEYS = frozenset({"evidence_refs", "evidence_reason", "semantic_key", "source_observation_refs"})
_AUDIT_EXPERIENCE_KEYS = frozenset({"guidance_refs", "repo_evidence_refs"})


def review_node_view(
    node: dict[str, Any],
    *,
    task_type: str,
    scenario_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Project a node to the fields owned by its semantic task."""

    view = {key: value for key, value in node.items() if key not in _AUDIT_NODE_KEYS}
    binding = _dict_value(view.get("runtime_binding"))
    if binding:
        binding = {key: value for key, value in binding.items() if key != "evidence_refs"}
        attachment = _dict_value(binding.get("attachment"))
        if attachment:
            binding["attachment"] = {key: value for key, value in attachment.items() if key != "owner_ref"}
        view["runtime_binding"] = binding
    contract = _dict_value(view.get("surface_contract"))
    if contract:
        contract = dict(contract)
        runtime_surface = _dict_value(contract.get("runtime_surface"))
        if task_type == "scenario_behavior":
            contract.pop("runtime_surface", None)
        elif runtime_surface:
            contract["runtime_surface"] = {key: value for key, value in runtime_surface.items() if key != "evidence_refs"}
        experience = _dict_value(contract.get("experience_contract"))
        if task_type == "scenario_runtime":
            contract.pop("experience_contract", None)
        elif experience:
            contract["experience_contract"] = {
                key: value for key, value in experience.items() if key not in _AUDIT_EXPERIENCE_KEYS
            }
        view["surface_contract"] = contract
    if task_type == "scenario_behavior":
        view.pop("runtime_binding", None)
        node_scenario = str(view.get("scenario_id") or "")
        if scenario_ids and node_scenario not in {"", "global", *scenario_ids}:
            compact_contract = {
                key: value
                for key, value in _dict_value(view.get("surface_contract")).items()
                if key in {"states", "transitions"}
            }
            view = {
                key: value
                for key, value in view.items()
                if key in {"id", "scenario_id", "kind", "name", "surface", "component_id", "use_case_id"}
            }
            if compact_contract:
                view["surface_contract"] = compact_contract
    return view


def review_edge_view(edge: dict[str, Any], *, task_type: str) -> dict[str, Any]:
    """Project runtime boundary detail out of a behavior review while retaining observable outcomes."""

    view = dict(edge)
    if task_type != "scenario_behavior":
        return view
    boundary = _dict_value(view.get("boundary_contract"))
    if not boundary:
        return view
    behavior_boundary = {
        key: value
        for key, value in boundary.items()
        if key in {"consumer_operation", "response_mapping", "error_mapping"} and value
    }
    if behavior_boundary:
        view["boundary_contract"] = behavior_boundary
    else:
        view.pop("boundary_contract", None)
    return view


def _approved_context(*, claims: list[dict[str, object]], nodes: list[dict[str, object]]) -> dict[str, object]:
    """Approved Requirement facts a reviewer needs to tell a declared external dependency from a missing provider."""

    text_by_requirement: dict[str, str] = {}
    for claim in claims:
        requirement_ids = _string_items(claim.get("requirement_ids"))
        statement = str(claim.get("statement") or "").strip()
        if len(requirement_ids) == 1 and statement:
            text_by_requirement.setdefault(requirement_ids[0], statement)
    preconditions = [
        {"requirement_id": requirement_id, "text": text_by_requirement.get(requirement_id, "")}
        for claim in claims
        if str(claim.get("claim_type") or "") == "precondition"
        for requirement_id in _string_items(claim.get("requirement_ids"))
    ]
    external_inputs: dict[tuple[str, str], dict[str, object]] = {}
    for node in nodes:
        runtime_surface = _dict_value(_dict_value(node.get("surface_contract")).get("runtime_surface"))
        for item in _dict_items(runtime_surface.get("required_inputs")):
            source = str(item.get("source") or "")
            name = str(item.get("name") or "")
            if not name or not source.startswith("external"):
                continue
            refs = _string_items(item.get("requirement_refs"))
            external_inputs.setdefault(
                (name, source),
                {
                    "name": name,
                    "source": source,
                    "provided_by": str(item.get("provided_by") or ""),
                    "requirement_refs": refs,
                    "requirement_text": [text_by_requirement[ref] for ref in refs if ref in text_by_requirement],
                },
            )
    context: dict[str, object] = {}
    if preconditions:
        context["preconditions"] = preconditions
    if external_inputs:
        context["external_inputs"] = [external_inputs[key] for key in sorted(external_inputs)]
    return context


def _branch_entry(edge: dict[str, object], *, nodes_by_id: dict[str, dict[str, Any]]) -> dict[str, object]:
    """Derive where a depends_on branch starts in its parent by exact state-name matching only."""

    child_id = str(edge.get("from") or "")
    parent_id = str(edge.get("to") or "")
    child_transitions = _transitions(nodes_by_id.get(child_id))
    parent_transitions = _transitions(nodes_by_id.get(parent_id))
    entry: dict[str, object] = {"edge_id": str(edge.get("id") or ""), "child_node_id": child_id, "parent_node_id": parent_id}
    parent_states = {state for item in parent_transitions for state in (item["from"], item["to"])}
    child_starts = list(dict.fromkeys(item["from"] for item in child_transitions))
    matches = [state for state in child_starts if state in parent_states]
    if len(matches) != 1:
        return {
            **entry,
            "status": "unresolved",
            "reason": (
                "No child transition starts from a state named in the parent transitions."
                if not matches
                else "Several child transitions start from parent states; the entry state is ambiguous."
            ),
            "child_start_states": child_starts,
            "parent_states": sorted(parent_states),
        }
    state = matches[0]
    forward: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for item in parent_transitions:
        forward.setdefault(item["from"], set()).add(item["to"])
        reverse.setdefault(item["to"], set()).add(item["from"])
    before = _reachable({state}, reverse)
    after = _reachable({state}, forward)
    return {
        **entry,
        "status": "resolved",
        "parent_entry_state": state,
        # Reachability, not history: in a cyclic parent a transition can both lead to and follow the entry state.
        "parent_transitions_leading_to_entry": [
            item for item in parent_transitions if item["to"] in before and item["from"] in before and item["from"] != state
        ],
        "parent_transitions_after_entry": [item for item in parent_transitions if item["from"] in after],
    }


def _transitions(node: dict[str, Any] | None) -> list[dict[str, str]]:
    contract = _dict_value(_dict_value(node).get("surface_contract"))
    return [
        {"from": str(item.get("from") or ""), "to": str(item.get("to") or ""), "action": str(item.get("action") or "")}
        for item in _dict_items(contract.get("transitions"))
        if str(item.get("from") or "") and str(item.get("to") or "")
    ]


def _claim_trace_source_ids(claim: dict[str, object]) -> set[str]:
    source_ids = set(_string_items(claim.get("requirement_ids")))
    claim_id = str(claim.get("claim_id") or "")
    if claim_id:
        source_ids.add(claim_id)
    if str(claim.get("claim_type") or "") == "required_surface":
        scenario_id = str(claim.get("scenario_id") or "")
        surface = str(claim.get("surface") or "")
        if scenario_id and surface:
            source_ids.add(f"SURF:{scenario_id}:{surface}")
    return source_ids


def _bounded_relation_closure(
    *,
    task: dict[str, object],
    seed_node_ids: set[str],
    selected_edge_ids: set[str],
    nodes_by_id: dict[str, dict[str, object]],
    edges: list[dict[str, object]],
) -> tuple[set[str], set[str]]:
    """Keep the task-bounded connector subgraph without enumerating individual paths."""

    scenario_ids = set(_string_items(task.get("scenario_ids")))
    candidate_edges = [
        edge
        for edge in edges
        if str(edge.get("id") or "") in selected_edge_ids
        or not scenario_ids
        or str(edge.get("scenario_id") or "") in {"", "global", *scenario_ids}
    ]
    forward: dict[str, set[str]] = {}
    reverse: dict[str, set[str]] = {}
    for edge in candidate_edges:
        source_id = str(edge.get("from") or "")
        target_id = str(edge.get("to") or "")
        if source_id not in nodes_by_id or target_id not in nodes_by_id:
            continue
        forward.setdefault(source_id, set()).add(target_id)
        reverse.setdefault(target_id, set()).add(source_id)

    reachable_from_seed = _reachable(seed_node_ids, forward)
    can_reach_seed = _reachable(seed_node_ids, reverse)
    closed_edge_ids = set(selected_edge_ids)
    for edge in candidate_edges:
        edge_id = str(edge.get("id") or "")
        source_id = str(edge.get("from") or "")
        target_id = str(edge.get("to") or "")
        if edge_id and source_id in reachable_from_seed and target_id in can_reach_seed:
            closed_edge_ids.add(edge_id)

    closed_node_ids = set(seed_node_ids)
    closed_node_ids.update(
        endpoint
        for edge in candidate_edges
        if str(edge.get("id") or "") in closed_edge_ids
        for endpoint in (str(edge.get("from") or ""), str(edge.get("to") or ""))
        if endpoint in nodes_by_id
    )
    return closed_node_ids, closed_edge_ids


def _reachable(seed_ids: set[str], adjacency: dict[str, set[str]]) -> set[str]:
    reached = set(seed_ids)
    pending = list(seed_ids)
    while pending:
        source_id = pending.pop()
        for target_id in adjacency.get(source_id, set()):
            if target_id in reached:
                continue
            reached.add(target_id)
            pending.append(target_id)
    return reached


def relation_index(vertical_design_graph: dict[str, object] | None) -> dict[str, object]:
    graph = _dict_value(vertical_design_graph)
    return {
        "graph_digest": graph.get("artifact_digest"),
        "nodes": [
            {
                key: node.get(key)
                for key in ("id", "scenario_id", "component_id", "surface", "kind", "name", "semantic_key")
                if node.get(key) not in (None, "")
            }
            for node in _dict_items(graph.get("nodes"))
        ],
        "edges": [
            {
                key: edge.get(key)
                for key in ("id", "scenario_id", "from", "to", "kind", "label", "terminal", "branch_group")
                if edge.get(key) not in (None, "")
            }
            for edge in _dict_items(graph.get("edges"))
        ],
    }


def _validate_plan_coverage(
    *, tasks: list[dict[str, object]], claims: list[dict[str, object]], edges: list[dict[str, object]]
) -> None:
    expected_claims = set(_claim_ids(claims))
    assignments = [claim_id for task in tasks for claim_id in _string_items(task.get("primary_claim_ids"))]
    if set(assignments) != expected_claims or len(assignments) != len(set(assignments)):
        raise HarnessError(
            "design_semantic_plan_invalid",
            "Every semantic claim must have exactly one primary review task.",
        )
    expected_boundaries = {
        f"boundary:{edge_id}"
        for edge in edges
        if _dict_value(edge.get("boundary_contract"))
        for edge_id in [str(edge.get("id") or "")]
        if edge_id
    }
    covered = {key for task in tasks for key in _string_items(task.get("coverage_keys"))}
    if not expected_boundaries.issubset(covered):
        raise HarnessError(
            "design_semantic_plan_invalid",
            "Every boundary contract must have a runtime review task.",
        )


def _task(**values: object) -> dict[str, object]:
    return dict(values)


def _scenario_slice(
    scenario_id: str, *, nodes: list[dict[str, object]], edges: list[dict[str, object]]
) -> tuple[list[str], list[str]]:
    selected_nodes = {str(node.get("id") or "") for node in nodes if str(node.get("scenario_id") or "") == scenario_id}
    selected_edges = [
        edge
        for edge in edges
        if str(edge.get("scenario_id") or "") == scenario_id
    ]
    selected_nodes.update(
        endpoint
        for edge in selected_edges
        for endpoint in (str(edge.get("from") or ""), str(edge.get("to") or ""))
        if endpoint
    )
    return sorted(item for item in selected_nodes if item), sorted(_ids(selected_edges))


def _shared_surface_groups(nodes: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for node in nodes:
        contract = _dict_value(node.get("surface_contract"))
        runtime_surface = _dict_value(contract.get("runtime_surface"))
        if str(node.get("kind") or "") == "composition_wiring" or str(runtime_surface.get("role") or "") == "composition_wiring":
            continue
        surface = str(node.get("surface") or "")
        identity = ""
        if surface == "ui":
            experience = _dict_value(contract.get("experience_contract"))
            identity = str(_dict_value(experience.get("screen")).get("screen_id") or runtime_surface.get("screen_id") or "")
        elif surface == "api":
            method = str(contract.get("method") or contract.get("http_method") or "")
            route = str(contract.get("route") or contract.get("path") or "")
            identity = f"{method}:{route}" if method and route else ""
        else:
            target = _dict_value(_dict_value(node.get("runtime_binding")).get("target"))
            target_path = str(target.get("path") or "")
            target_symbol = str(target.get("symbol") or "")
            identity = f"{target_path}#{target_symbol}" if target_path or target_symbol else str(node.get("name") or "")
        if not identity:
            continue
        key = f"{surface}:{str(node.get('component_id') or '')}:{identity}"
        groups.setdefault(key, []).append(node)
    return groups


def _claim_ids(claims: list[dict[str, object]]) -> list[str]:
    return [str(claim.get("claim_id") or "") for claim in claims if str(claim.get("claim_id") or "")]


def _ids(items: list[dict[str, object]]) -> set[str]:
    return {str(item.get("id") or "") for item in items if str(item.get("id") or "")}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "global"


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
