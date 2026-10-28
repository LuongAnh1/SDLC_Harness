"""Durable, bounded read-only tool loop for the L3 Designer role."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object
from ...loop import RoleToolResultV1, canonical_json_hash
from ...storage.runtime import RuntimeStore, utc_now_iso
from .graph import (
    BOUNDARY_CONTRACT_VERSION,
    DELTA_VERSION,
    GRAPH_VERSION,
    GRAPH_VERSION_V2,
    RUNTIME_BINDING_VERSION,
    SURFACE_CONTRACT_VERSION,
    apply_designer_delta,
    completion_findings,
    delta_version_for,
    new_vertical_design_graph,
    validate_vertical_design_graph,
)
from .tools import DesignerToolRegistry


SESSION_VERSION = "designer_loop_session_v1"
MANIFEST_VERSION = "designer_loop_manifest_v1"
JOURNAL_VERSION = "designer_loop_journal_v1"
INVALID_ACTION_LIMIT = 3
OBSERVATION_ACTION_LIMIT = 3
OBSERVATION_TOOL_IDS = frozenset(
    {
        "landscape.search",
        "landscape.get_anchor",
        "repo.search",
        "repo.read",
        "codegraph.query",
        "design.inspect_partition",
    }
)


class DesignerExecutionError(HarnessError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        run_id: str,
        retriable: bool,
        session_path: str = "",
        manifest_path: str = "",
        journal_path: str = "",
        progress: dict[str, object] | None = None,
    ):
        super().__init__(error_code, message, run_id=run_id)
        self.retriable = retriable
        self.session_path = session_path
        self.manifest_path = manifest_path
        self.journal_path = journal_path
        self.progress = progress or {}


@dataclass(frozen=True)
class DesignerExecutionResult:
    graph: dict[str, object]
    session_path: str
    manifest_path: str
    journal_path: str
    delta_paths: list[str]
    progress: dict[str, object]


class DesignerToolLoopExecutor:
    def __init__(self, *, settings: Any, store: RuntimeStore, llm_client: Any):
        self.settings = settings
        self.store = store
        self.llm_client = llm_client

    def execute(
        self,
        *,
        run_id: str,
        requirement_model: dict[str, object],
        landscape: dict[str, object],
        target_path: Path,
        snapshot_binding: dict[str, object],
        existing_graph: dict[str, object] | None = None,
        repair_feedback: list[dict[str, object]] | None = None,
        session_ordinal: int = 1,
        on_progress: Any | None = None,
        contract_version: str = GRAPH_VERSION,
    ) -> DesignerExecutionResult:
        session_name = f"design_v{max(1, session_ordinal)}"
        session_dir = self.store.run_dir(run_id) / "designer_execution" / session_name
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "designer_loop_session_v1.json"
        manifest_file = session_dir / "designer_loop_manifest_v1.json"
        journal_file = session_dir / "designer_loop_journal_v1.jsonl"
        session_path = self.store.relative(session_file)
        manifest_path = self.store.relative(manifest_file)
        journal_path = self.store.relative(journal_file)
        manifest = self._manifest(
            run_id=run_id,
            requirement_model=requirement_model,
            landscape=landscape,
            snapshot_binding=snapshot_binding,
            target_path=target_path,
            repair_feedback=repair_feedback or [],
            session_ordinal=session_ordinal,
            contract_version=contract_version,
        )
        if manifest_file.exists():
            persisted_manifest = _read_json(manifest_file, run_id=run_id)
            if persisted_manifest.get("input_hash") != manifest.get("input_hash"):
                raise DesignerExecutionError(
                    "designer_input_drift",
                    "Frozen Designer inputs changed after the session was created.",
                    run_id=run_id,
                    retriable=False,
                    session_path=session_path,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                )
            manifest = persisted_manifest
        else:
            _write_json_atomic(manifest_file, manifest)

        if session_file.exists():
            session = _read_json(session_file, run_id=run_id)
            if session.get("schema_version") != SESSION_VERSION:
                raise DesignerExecutionError(
                    "designer_session_invalid",
                    "Persisted Designer session version is invalid.",
                    run_id=run_id,
                    retriable=False,
                    session_path=session_path,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                )
            graph = validate_vertical_design_graph(
                session.get("graph"),
                expected_run_id=run_id,
                expected_binding=snapshot_binding,
            )
        else:
            graph = existing_graph or new_vertical_design_graph(
                run_id=run_id,
                snapshot_binding=snapshot_binding,
                contract_version=contract_version,
            )
            graph = validate_vertical_design_graph(graph, expected_run_id=run_id, expected_binding=snapshot_binding)
            session = {
                "schema_version": SESSION_VERSION,
                "run_id": run_id,
                "session_name": session_name,
                "status": "running",
                "manifest_input_hash": manifest["input_hash"],
                "cursor": {"next_action_index": 1, "pending_call": None},
                "budgets": {
                    "action": {"limit": int(self.settings.design_llm_call_budget), "used": 0},
                    "invalid_action": {"limit": INVALID_ACTION_LIMIT, "used": 0},
                    "repair_iteration": max(0, session_ordinal - 1),
                },
                "graph": graph,
                "delta_paths": [],
                "observations": [],
                "observation_actions_since_delta": 0,
                "source_observations": {},
                "repair_targets_completed": [],
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            _write_json_atomic(session_file, session)

        registry = DesignerToolRegistry(
            run_id=run_id,
            target_path=target_path,
            landscape=landscape,
            graph=graph,
            fake_mode=self.settings.llm_mode == "fake",
        )
        for refs in _dict(session.get("source_observations")).values():
            registry.register_evidence_refs(_strings(refs))
        self._reconcile_pending(
            run_id=run_id,
            session=session,
            session_file=session_file,
            session_dir=session_dir,
            registry=registry,
            journal_file=journal_file,
        )
        try:
            if self.settings.llm_mode == "fake":
                self._execute_fake(
                    run_id=run_id,
                    requirement_model=requirement_model,
                    landscape=landscape,
                    repair_feedback=repair_feedback or [],
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                )
            else:
                self._execute_model_loop(
                    run_id=run_id,
                    requirement_model=requirement_model,
                    landscape=landscape,
                    repair_feedback=repair_feedback or [],
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                )
        except DesignerExecutionError:
            raise
        except HarnessError as exc:
            retriable = exc.error_code in {"llm_transport_failed", "target_repo_codegraph_failed"}
            session["status"] = "paused" if retriable else "blocked"
            session["updated_at"] = utc_now_iso()
            session["last_error"] = exc.to_response()
            session["graph"] = registry.graph
            _write_json_atomic(session_file, session)
            raise DesignerExecutionError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                retriable=retriable,
                session_path=session_path,
                manifest_path=manifest_path,
                journal_path=journal_path,
                progress=_progress(session),
            ) from exc

        session["status"] = "completed"
        session["completed_at"] = utc_now_iso()
        session["updated_at"] = utc_now_iso()
        session["graph"] = registry.graph
        _write_json_atomic(session_file, session)
        return DesignerExecutionResult(
            graph=registry.graph,
            session_path=session_path,
            manifest_path=manifest_path,
            journal_path=journal_path,
            delta_paths=_strings(session.get("delta_paths")),
            progress=_progress(session),
        )

    def _execute_fake(
        self,
        *,
        run_id: str,
        requirement_model: dict[str, object],
        landscape: dict[str, object],
        repair_feedback: list[dict[str, object]],
        registry: DesignerToolRegistry,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
        on_progress: Any | None,
    ) -> None:
        existing_revision = int(registry.graph.get("revision") or 0) > 0
        if repair_feedback or existing_revision:
            self._repair_fake_bundle_contracts(
                run_id=run_id,
                landscape=landscape,
                registry=registry,
                session=session,
                session_file=session_file,
                session_dir=session_dir,
                journal_file=journal_file,
                on_progress=on_progress,
            )
        if existing_revision:
            findings = completion_findings(
                registry.graph,
                required_surfaces=_required_surfaces(requirement_model),
                landscape=landscape,
            )
            if not findings:
                return
            if any(
                str(item.get("code") or "") in {"runtime_path_disconnected", "boundary_contract_mismatch"}
                for item in findings
            ):
                self._repair_fake_composition_edges(
                    run_id=run_id,
                    landscape=landscape,
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                )
                findings = completion_findings(
                    registry.graph,
                    required_surfaces=_required_surfaces(requirement_model),
                    landscape=landscape,
                )
                if not findings:
                    return
            categories = sorted({str(item.get("code") or "unknown") for item in findings})
            raise HarnessError(
                "designer_completion_rejected",
                f"Designer fake repair left {len(findings)} completion blocker(s): {', '.join(categories)}.",
                run_id=run_id,
            )
        anchors = {str(item.get("anchor_id") or ""): item for item in _dicts(landscape.get("anchors"))}
        matches = {
            (str(item.get("scenario_id") or ""), str(item.get("surface") or "")): _strings(item.get("candidate_anchor_refs"))
            for item in _dicts(landscape.get("scenario_matches"))
        }
        scenarios = _scenarios(requirement_model)
        for scenario in scenarios:
            scenario_id = str(scenario.get("id") or "")
            by_component: dict[str, dict[str, object]] = {}
            for surface in sorted(_required_surface_set(scenario)):
                candidate_refs = matches.get((scenario_id, surface), [])
                candidates = [anchors[ref] for ref in candidate_refs if ref in anchors]
                preferred_kinds = {
                    "ui": {"ui_component", "ui_entrypoint"},
                    "api": {"api_route", "service_symbol"},
                    "business_rule": {"service_symbol", "api_route"},
                    "data": {"data_model", "data_operation"},
                    "interaction": {"service_symbol"},
                }.get(surface, set())
                anchor = (
                    sorted(
                        candidates,
                        key=lambda item: (
                            0 if str(item.get("kind") or "") in preferred_kinds else 1,
                            0 if str(item.get("symbol") or "") else 1,
                            str(item.get("anchor_id") or ""),
                        ),
                    )[0]
                    if candidates
                    else None
                )
                component_id = str((anchor or {}).get("component_id") or _default_component(landscape, hint=surface))
                group = by_component.setdefault(component_id, {"coverage": {}, "nodes": []})
                group["coverage"][surface] = "covered"
                group["nodes"].append((surface, anchor))
            for component_id, group in sorted(by_component.items()):
                source_refs = [
                    str(anchor.get("anchor_id") or "")
                    for _, anchor in group["nodes"]
                    if isinstance(anchor, dict) and str(anchor.get("anchor_id") or "")
                ] or [f"fake:source:{component_id}"]
                _record_source_observations(session, component_id=component_id, refs=source_refs)
                registry.register_evidence_refs(source_refs)
                partition = {
                    "use_case_id": _requirement_use_case_id(requirement_model),
                    "scenario_id": scenario_id,
                    "component_id": component_id,
                    "source_observation_refs": source_refs,
                }
                operations: list[dict[str, object]] = [
                    {"op": "set_partition_coverage", "coverage": dict(group["coverage"])}
                ]
                temp_refs: list[str] = []
                for surface, anchor in group["nodes"]:
                    surface_contract = _dict(_dict(scenario.get("surfaces")).get(surface))
                    requirement_refs = list(
                        dict.fromkeys(
                            [
                                *_strings(surface_contract.get("requirement_refs")),
                                *_strings(scenario.get("requirement_refs")),
                                *_strings(scenario.get("acceptance_refs")),
                            ]
                        )
                    ) or [scenario_id]
                    temp_ref = f"tmp:{scenario_id}:{surface}"
                    temp_refs.append(temp_ref)
                    operations.append(
                        {
                            "op": "add_node",
                            "node": {
                                "temp_ref": temp_ref,
                                "semantic_key": f"{scenario_id}:{surface}",
                                "name": str((anchor or {}).get("symbol") or f"{scenario_id} {surface}"),
                                "kind": _kind_for_surface(surface),
                                "surface": surface,
                                "disposition": "modify" if anchor else "add",
                                "requirement_refs": requirement_refs,
                                "evidence_refs": [str(anchor["anchor_id"])] if anchor else [],
                                "evidence_reason": "Required surface has no existing anchor; add within the frozen component root." if not anchor else "",
                                "contract": f"Realize required {surface} behavior for scenario {scenario_id}.",
                                "surface_contract": _fake_surface_contract(
                                    surface=surface,
                                    scenario_id=scenario_id,
                                    name=str((anchor or {}).get("symbol") or f"{scenario_id} {surface}"),
                                ),
                                "runtime_binding": _fake_runtime_binding(
                                    landscape=landscape,
                                    component_id=component_id,
                                    surface=surface,
                                    anchor=anchor,
                                    source_refs=source_refs,
                                    fallback_symbol=f"{scenario_id} {surface}",
                                ),
                            },
                        }
                    )
                delta = {
                    "schema_version": delta_version_for(str(registry.graph.get("schema_version") or "")),
                    "base_graph_digest": registry.graph["artifact_digest"],
                    "partition": partition,
                    "operations": operations,
                    "completion_claims": [{"scenario_id": scenario_id, "surfaces": sorted(group["coverage"])}],
                    "unresolved_items": [],
                }
                self._execute_tool_action(
                    run_id=run_id,
                    tool_id="design.propose_delta",
                    arguments={"delta": delta},
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                )
            scenario_nodes = sorted(
                [
                    item
                    for item in _dicts(registry.graph.get("nodes"))
                    if str(item.get("scenario_id") or "") == scenario_id
                ],
                key=lambda item: (_surface_rank(str(item.get("surface") or "")), str(item.get("id") or "")),
            )
            if len(scenario_nodes) > 1:
                edge_partition_node = scenario_nodes[0]
                edge_component = str(edge_partition_node.get("component_id") or "")
                edge_refs = _source_observation_refs(session, edge_component)
                edge_operations: list[dict[str, object]] = []
                for index, (source, target) in enumerate(zip(scenario_nodes, scenario_nodes[1:]), start=1):
                    edge: dict[str, object] = {
                        "from": str(source.get("id") or ""),
                        "to": str(target.get("id") or ""),
                        "kind": "calls",
                        "label": "continue scenario",
                        "scenario_id": scenario_id,
                        "terminal": index == len(scenario_nodes) - 1,
                    }
                    if str(source.get("component_id") or "") != str(target.get("component_id") or ""):
                        edge["boundary_contract"] = _fake_boundary_contract(source=source, target=target)
                    edge_operations.append({"op": "add_edge", "edge": edge})
                self._execute_tool_action(
                    run_id=run_id,
                    tool_id="design.propose_delta",
                    arguments={
                        "delta": {
                            "schema_version": delta_version_for(str(registry.graph.get("schema_version") or "")),
                            "base_graph_digest": registry.graph["artifact_digest"],
                            "partition": {
                                "use_case_id": _requirement_use_case_id(requirement_model),
                                "scenario_id": scenario_id,
                                "component_id": edge_component,
                                "source_observation_refs": edge_refs,
                            },
                            "operations": edge_operations,
                            "completion_claims": [],
                            "unresolved_items": [],
                        }
                    },
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                )
        findings = completion_findings(
            registry.graph,
            required_surfaces=_required_surfaces(requirement_model),
            landscape=landscape,
        )
        if findings:
            categories = sorted({str(item.get("code") or item.get("category") or "unknown") for item in findings})
            raise HarnessError(
                "designer_completion_rejected",
                f"Designer fake graph has {len(findings)} completion blocker(s): {', '.join(categories)}.",
                run_id=run_id,
            )

    def _repair_fake_bundle_contracts(
        self,
        *,
        run_id: str,
        landscape: dict[str, object],
        registry: DesignerToolRegistry,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
        on_progress: Any | None,
    ) -> None:
        nodes = [
            item
            for item in _dicts(registry.graph.get("nodes"))
            if not isinstance(item.get("surface_contract"), dict)
            or (
                str(registry.graph.get("schema_version") or "") == GRAPH_VERSION_V2
                and not isinstance(item.get("runtime_binding"), dict)
            )
        ]
        grouped: dict[tuple[str, str, str], list[dict[str, object]]] = {}
        for node in nodes:
            key = (
                str(node.get("use_case_id") or ""),
                str(node.get("scenario_id") or ""),
                str(node.get("component_id") or ""),
            )
            grouped.setdefault(key, []).append(node)
        partitions = {
            (
                str(item.get("use_case_id") or ""),
                str(item.get("scenario_id") or ""),
                str(item.get("component_id") or ""),
            ): item
            for item in _dicts(registry.graph.get("partitions"))
        }
        for key, partition_nodes in sorted(grouped.items()):
            existing_partition = _dict(partitions.get(key, {}))
            component_anchors = [
                item
                for item in _dicts(landscape.get("anchors"))
                if str(item.get("component_id") or "") == key[2]
            ]
            source_refs = _strings(existing_partition.get("source_observation_refs"))
            if not source_refs:
                source_refs = list(
                    dict.fromkeys(
                        [
                            ref
                            for node in partition_nodes
                            for ref in _strings(node.get("evidence_refs"))
                            if any(str(anchor.get("anchor_id") or "") == ref for anchor in component_anchors)
                        ]
                    )
                )
            if not source_refs:
                source_refs = [f"fake:source:{key[2]}"]
            _record_source_observations(session, component_id=key[2], refs=source_refs)
            registry.register_evidence_refs(source_refs)
            partition = {
                "use_case_id": key[0],
                "scenario_id": key[1],
                "component_id": key[2],
                "source_observation_refs": source_refs,
            }
            coverage = dict(_dict(existing_partition.get("coverage")) or {})
            operations: list[dict[str, object]] = [{"op": "set_partition_coverage", "coverage": coverage}]
            for node in partition_nodes:
                changes: dict[str, object] = {}
                if not isinstance(node.get("surface_contract"), dict):
                    changes["surface_contract"] = _fake_surface_contract(
                        surface=str(node.get("surface") or "interaction"),
                        scenario_id=str(node.get("scenario_id") or "SC-1"),
                        name=str(node.get("name") or node.get("semantic_key") or "Design element"),
                    )
                if (
                    str(registry.graph.get("schema_version") or "") == GRAPH_VERSION_V2
                    and not isinstance(node.get("runtime_binding"), dict)
                ):
                    node_refs = set(_strings(node.get("evidence_refs")))
                    anchor = next(
                        (item for item in component_anchors if str(item.get("anchor_id") or "") in node_refs),
                        component_anchors[0] if component_anchors else None,
                    )
                    changes["runtime_binding"] = _fake_runtime_binding(
                        landscape=landscape,
                        component_id=key[2],
                        surface=str(node.get("surface") or "interaction"),
                        anchor=anchor,
                        source_refs=source_refs,
                        fallback_symbol=str(node.get("name") or node.get("semantic_key") or "DesignElement"),
                    )
                operations.append(
                    {
                        "op": "update_node",
                        "node_id": str(node.get("id") or ""),
                        "changes": changes,
                    }
                )
            delta = {
                "schema_version": delta_version_for(str(registry.graph.get("schema_version") or "")),
                "base_graph_digest": registry.graph["artifact_digest"],
                "partition": partition,
                "operations": operations,
                "completion_claims": [{"scenario_id": key[1], "surfaces": sorted(coverage)}],
                "unresolved_items": [],
            }
            self._execute_tool_action(
                run_id=run_id,
                tool_id="design.propose_delta",
                arguments={"delta": delta},
                registry=registry,
                session=session,
                session_file=session_file,
                session_dir=session_dir,
                journal_file=journal_file,
                on_progress=on_progress,
            )

    def _repair_fake_composition_edges(
        self,
        *,
        run_id: str,
        landscape: dict[str, object],
        registry: DesignerToolRegistry,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
        on_progress: Any | None,
    ) -> None:
        nodes = _dicts(registry.graph.get("nodes"))
        partitions = _dicts(registry.graph.get("partitions"))
        edges = _dicts(registry.graph.get("edges"))
        scenario_ids = sorted({str(item.get("scenario_id") or "") for item in nodes if str(item.get("scenario_id") or "")})
        for scenario_id in scenario_ids:
            scenario_nodes = sorted(
                [item for item in nodes if str(item.get("scenario_id") or "") == scenario_id],
                key=lambda item: (_surface_rank(str(item.get("surface") or "")), str(item.get("id") or "")),
            )
            if len(scenario_nodes) < 2:
                continue
            component_id = str(scenario_nodes[0].get("component_id") or "")
            partition_state = next(
                (
                    item
                    for item in partitions
                    if str(item.get("scenario_id") or "") == scenario_id
                    and str(item.get("component_id") or "") == component_id
                ),
                {},
            )
            source_refs = _strings(partition_state.get("source_observation_refs")) or [f"fake:source:{component_id}"]
            _record_source_observations(session, component_id=component_id, refs=source_refs)
            registry.register_evidence_refs(source_refs)
            operations: list[dict[str, object]] = [
                {"op": "remove_edge", "edge_id": str(item.get("id") or "")}
                for item in edges
                if str(item.get("scenario_id") or "") == scenario_id
            ]
            for index, (source, target) in enumerate(zip(scenario_nodes, scenario_nodes[1:]), start=1):
                edge: dict[str, object] = {
                    "from": str(source.get("id") or ""),
                    "to": str(target.get("id") or ""),
                    "kind": "calls",
                    "label": "continue scenario",
                    "scenario_id": scenario_id,
                    "terminal": index == len(scenario_nodes) - 1,
                }
                if str(source.get("component_id") or "") != str(target.get("component_id") or ""):
                    edge["boundary_contract"] = _fake_boundary_contract(source=source, target=target)
                operations.append({"op": "add_edge", "edge": edge})
            self._execute_tool_action(
                run_id=run_id,
                tool_id="design.propose_delta",
                arguments={
                    "delta": {
                        "schema_version": delta_version_for(str(registry.graph.get("schema_version") or "")),
                        "base_graph_digest": registry.graph["artifact_digest"],
                        "partition": {
                            "use_case_id": str(partition_state.get("use_case_id") or "UC1"),
                            "scenario_id": scenario_id,
                            "component_id": component_id,
                            "source_observation_refs": source_refs,
                        },
                        "operations": operations,
                        "completion_claims": [],
                        "unresolved_items": [],
                    }
                },
                registry=registry,
                session=session,
                session_file=session_file,
                session_dir=session_dir,
                journal_file=journal_file,
                on_progress=on_progress,
            )

    def _execute_model_loop(
        self,
        *,
        run_id: str,
        requirement_model: dict[str, object],
        landscape: dict[str, object],
        repair_feedback: list[dict[str, object]],
        registry: DesignerToolRegistry,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
        on_progress: Any | None,
    ) -> None:
        while True:
            active_partition = _active_partition(
                requirement_model=requirement_model,
                landscape=landscape,
                graph=registry.graph,
            )
            repair_task = _active_repair_task(
                repair_feedback=repair_feedback,
                graph=registry.graph,
                completed_targets=_strings(session.get("repair_targets_completed")),
            )
            active_component_id = str(
                _dict((repair_task or {}).get("partition")).get("component_id")
                or _dict((active_partition or {}).get("partition")).get("component_id")
                or ""
            )
            session["active_component_id"] = active_component_id
            if repair_feedback and repair_task is None and _strings(session.get("delta_paths")):
                return
            if not repair_feedback and active_partition is None and not completion_findings(
                registry.graph,
                required_surfaces=_required_surfaces(requirement_model),
                landscape=landscape,
            ):
                return
            budget = _dict(_dict(session.get("budgets")).get("action"))
            if int(budget.get("used") or 0) >= int(budget.get("limit") or 0):
                raise HarnessError("design_llm_budget_exhausted", "Designer tool loop exhausted its action budget.", run_id=run_id)
            call_id = f"designer-{int(_dict(session.get('cursor')).get('next_action_index') or 1):04d}"
            cursor = _dict(session.get("cursor"))
            cursor["pending_call"] = {"call_id": call_id, "stage": "llm", "created_at": utc_now_iso()}
            session["cursor"] = cursor
            _write_json_atomic(session_file, session)
            observations_since_delta = int(session.get("observation_actions_since_delta") or 0)
            prompt = _designer_prompt(
                requirement_model=requirement_model,
                landscape=landscape,
                graph=registry.graph,
                observations=_dicts(session.get("observations"))[-8:],
                repair_feedback=repair_feedback,
                active_partition=active_partition,
                repair_task=repair_task,
                observations_since_delta=observations_since_delta,
                last_invalid_action=str(session.get("last_invalid_action") or ""),
            )
            completion = self.llm_client.complete_messages(
                messages=[
                    {"role": "system", "content": "You are SDLC Harness Designer L3. Return exactly one JSON tool action."},
                    {"role": "user", "content": prompt},
                ],
                json_mode=True,
                max_tokens=self.settings.llm_structured_max_tokens,
            )
            try:
                action = parse_first_json_object(completion.content, expected_keys={"action", "arguments"})
            except json.JSONDecodeError as exc:
                self._invalid_action(session, session_file, message="Designer returned invalid JSON.")
                continue
            tool_id = str(action.get("action") or "")
            arguments = action.get("arguments")
            graph_is_v2 = str(registry.graph.get("schema_version") or "") == GRAPH_VERSION_V2
            source_refs = _source_observation_refs(session, active_component_id)
            if graph_is_v2 and tool_id == "design.propose_delta" and active_component_id and not source_refs:
                if observations_since_delta >= OBSERVATION_ACTION_LIMIT:
                    raise HarnessError(
                        "design_evidence_insufficient",
                        f"Designer exhausted source investigation for component {active_component_id}.",
                        run_id=run_id,
                    )
                self._invalid_action(
                    session,
                    session_file,
                    message="Designer must read source or an available CodeGraph call path before proposing this component delta.",
                )
                self._record_observation(
                    session,
                    {
                        "tool_id": tool_id,
                        "status": "rejected",
                        "code": "design_investigation_missing",
                        "component_id": active_component_id,
                    },
                )
                _write_json_atomic(session_file, session)
                continue
            if graph_is_v2 and tool_id == "design.propose_delta" and active_component_id:
                arguments = _attach_source_observations(arguments, component_id=active_component_id, refs=source_refs)
            if (active_partition is not None or repair_task is not None) and observations_since_delta >= OBSERVATION_ACTION_LIMIT and tool_id != "design.propose_delta":
                self._invalid_action(
                    session,
                    session_file,
                    message="Designer must propose a delta for the active partition before any more observation or completion action.",
                )
                self._record_observation(
                    session,
                    {
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "status": "rejected",
                        "message": "Observation quota reached. Propose design.propose_delta for active_partition now.",
                        "active_partition": active_partition,
                        "repair_task": repair_task,
                    },
                )
                _write_json_atomic(session_file, session)
                continue
            if tool_id == "complete":
                findings = completion_findings(
                    registry.graph,
                    required_surfaces=_required_surfaces(requirement_model),
                    landscape=landscape,
                )
                if not findings:
                    cursor["pending_call"] = None
                    cursor["next_action_index"] = int(cursor.get("next_action_index") or 1) + 1
                    session["cursor"] = cursor
                    _consume_action(session)
                    _write_json_atomic(session_file, session)
                    return
                if any(str(item.get("code") or "") == "human_decision_required" for item in findings):
                    raise HarnessError(
                        "designer_human_required",
                        "Designer found an unresolved Requirement or human-authority decision.",
                        run_id=run_id,
                    )
                self._record_observation(session, {"tool_id": "complete", "status": "rejected", "findings": findings})
                cursor["pending_call"] = None
                session["cursor"] = cursor
                _consume_action(session)
                _write_json_atomic(session_file, session)
                continue
            cursor["pending_call"] = {"call_id": call_id, "stage": "tool", "tool_id": tool_id, "arguments": arguments}
            session["cursor"] = cursor
            _write_json_atomic(session_file, session)
            action_fingerprint = canonical_json_hash({"tool_id": tool_id, "arguments": arguments})
            if action_fingerprint == str(session.get("last_action_fingerprint") or ""):
                self._invalid_action(
                    session,
                    session_file,
                    message="Designer repeated an identical successful tool action without changing the graph.",
                )
                self._record_observation(
                    session,
                    {
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "status": "rejected",
                        "message": "Do not repeat this action; use the observation already returned and advance to another tool or propose a delta.",
                    },
                )
                _write_json_atomic(session_file, session)
                continue
            try:
                self._execute_tool_action(
                    run_id=run_id,
                    tool_id=tool_id,
                    arguments=arguments,
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                    call_id=call_id,
                )
                if tool_id == "design.propose_delta" and repair_task is not None:
                    _mark_repair_targets_completed(
                        session=session,
                        arguments=arguments,
                        repair_task=repair_task,
                    )
                    _write_json_atomic(session_file, session)
            except HarnessError as exc:
                if exc.error_code in {
                    "designer_tool_unknown",
                    "designer_tool_input_invalid",
                    "designer_delta_invalid",
                    "designer_delta_no_effect",
                    "designer_delta_precondition_failed",
                    "vertical_design_graph_invalid",
                }:
                    self._invalid_action(session, session_file, message=exc.message)
                    self._record_observation(
                        session,
                        {
                            "tool_id": tool_id,
                            "arguments": arguments,
                            "status": "rejected",
                            "error": exc.to_response(),
                        },
                    )
                    _write_json_atomic(session_file, session)
                    continue
                raise

    def _execute_tool_action(
        self,
        *,
        run_id: str,
        tool_id: str,
        arguments: object,
        registry: DesignerToolRegistry,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
        on_progress: Any | None,
        call_id: str | None = None,
    ) -> None:
        cursor = _dict(session.get("cursor"))
        effective_call_id = call_id or f"designer-{int(cursor.get('next_action_index') or 1):04d}"
        cursor["pending_call"] = {
            "call_id": effective_call_id,
            "stage": "tool",
            "tool_id": tool_id,
            "arguments": arguments,
        }
        session["cursor"] = cursor
        _write_json_atomic(session_file, session)
        output, evidence_refs = registry.execute(tool_id, arguments)
        session["last_invalid_action"] = ""
        if tool_id == "repo.read" and evidence_refs:
            component_id = registry.component_for_path(str(_dict(output).get("path") or ""))
            _record_source_observations(session, component_id=component_id, refs=evidence_refs)
        elif tool_id == "codegraph.query" and str(_dict(output).get("status") or "") == "available":
            _record_source_observations(
                session,
                component_id=str(session.get("active_component_id") or ""),
                refs=evidence_refs,
            )
        result = RoleToolResultV1.build(
            call_id=effective_call_id,
            tool_id=tool_id,
            status="succeeded",
            retryable=False,
            input_payload=arguments,
            output_payload=output,
            evidence_refs=evidence_refs,
        )
        delta_path = ""
        if tool_id == "design.propose_delta":
            record = {
                "schema_version": delta_version_for(str(registry.graph.get("schema_version") or "")),
                "call_id": effective_call_id,
                "input": arguments,
                "result": output,
                "graph": registry.graph,
            }
            delta_file = session_dir / f"designer_delta_{effective_call_id}.json"
            _write_json_atomic(delta_file, record)
            delta_path = self.store.relative(delta_file)
            delta_paths = _strings(session.get("delta_paths"))
            if delta_path not in delta_paths:
                delta_paths.append(delta_path)
            session["delta_paths"] = delta_paths
        entry = {
            "schema_version": JOURNAL_VERSION,
            "sequence": int(cursor.get("next_action_index") or 1),
            "timestamp": utc_now_iso(),
            "call_id": effective_call_id,
            "tool_id": tool_id,
            "result": result.to_dict(),
            "observation": output,
            **({"delta_artifact": delta_path} if delta_path else {}),
        }
        _append_journal(journal_file, entry)
        self._record_observation(
            session,
            {
                "tool_id": tool_id,
                "arguments": arguments,
                "status": "succeeded",
                "output": output,
                "evidence_refs": evidence_refs,
            },
        )
        session["last_action_fingerprint"] = canonical_json_hash({"tool_id": tool_id, "arguments": arguments})
        if tool_id == "design.propose_delta":
            session["observation_actions_since_delta"] = 0
        elif tool_id in OBSERVATION_TOOL_IDS:
            session["observation_actions_since_delta"] = int(session.get("observation_actions_since_delta") or 0) + 1
        cursor["pending_call"] = None
        cursor["next_action_index"] = int(cursor.get("next_action_index") or 1) + 1
        session["cursor"] = cursor
        session["graph"] = registry.graph
        session["updated_at"] = utc_now_iso()
        _consume_action(session)
        _write_json_atomic(session_file, session)
        if on_progress is not None:
            on_progress(
                _progress(session),
                session_path=self.store.relative(session_file),
                journal_path=self.store.relative(journal_file),
                tool_id=tool_id,
            )

    def _reconcile_pending(
        self,
        *,
        run_id: str,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        registry: DesignerToolRegistry,
        journal_file: Path,
    ) -> None:
        cursor = _dict(session.get("cursor"))
        pending = cursor.get("pending_call")
        if not isinstance(pending, dict):
            return
        call_id = str(pending.get("call_id") or "")
        if str(pending.get("stage") or "") != "tool" or str(pending.get("tool_id") or "") != "design.propose_delta":
            cursor["pending_call"] = None
            session["cursor"] = cursor
            _write_json_atomic(session_file, session)
            return
        delta_file = session_dir / f"designer_delta_{call_id}.json"
        if not delta_file.exists():
            return
        record = _read_json(delta_file, run_id=run_id)
        graph = validate_vertical_design_graph(record.get("graph"), expected_run_id=run_id)
        registry.graph = graph
        session["graph"] = graph
        delta_path = self.store.relative(delta_file)
        session["delta_paths"] = list(dict.fromkeys([*_strings(session.get("delta_paths")), delta_path]))
        session["observation_actions_since_delta"] = 0
        cursor["pending_call"] = None
        cursor["next_action_index"] = int(cursor.get("next_action_index") or 1) + 1
        session["cursor"] = cursor
        _consume_action(session)
        _append_journal(
            journal_file,
            {
                "schema_version": JOURNAL_VERSION,
                "sequence": int(cursor.get("next_action_index") or 1) - 1,
                "timestamp": utc_now_iso(),
                "call_id": call_id,
                "tool_id": "design.propose_delta",
                "status": "reconciled",
                "delta_artifact": delta_path,
            },
        )
        _write_json_atomic(session_file, session)

    def _manifest(
        self,
        *,
        run_id: str,
        requirement_model: dict[str, object],
        landscape: dict[str, object],
        snapshot_binding: dict[str, object],
        target_path: Path,
        repair_feedback: list[dict[str, object]],
        session_ordinal: int,
        contract_version: str,
    ) -> dict[str, object]:
        authority = {
            "run_id": run_id,
            "requirement_digest": snapshot_binding.get("requirement_digest"),
            "repo_landscape_digest": landscape.get("artifact_digest"),
            "snapshot_binding": snapshot_binding,
            "repair_feedback": repair_feedback,
            "session_ordinal": session_ordinal,
            "designer_contract_version": contract_version,
        }
        return {
            "schema_version": MANIFEST_VERSION,
            "run_id": run_id,
            "input_hash": canonical_json_hash(authority),
            "authority": authority,
            "workspace_ref": "run_workspace_v1",
            "workspace_name": target_path.name,
            "tools": [
                "landscape.search",
                "landscape.get_anchor",
                "repo.search",
                "repo.read",
                "codegraph.query",
                "design.inspect_partition",
                "design.propose_delta",
                "complete",
            ],
            "action_budget": int(self.settings.design_llm_call_budget),
            "invalid_action_budget": INVALID_ACTION_LIMIT,
            "requirement_summary_hash": canonical_json_hash(requirement_model),
            "created_at": utc_now_iso(),
        }

    @staticmethod
    def _record_observation(session: dict[str, object], observation: dict[str, object]) -> None:
        observations = _dicts(session.get("observations"))
        observations.append(observation)
        session["observations"] = observations[-12:]

    @staticmethod
    def _invalid_action(session: dict[str, object], session_file: Path, *, message: str) -> None:
        budgets = _dict(session.get("budgets"))
        invalid = _dict(budgets.get("invalid_action"))
        invalid["used"] = int(invalid.get("used") or 0) + 1
        budgets["invalid_action"] = invalid
        session["budgets"] = budgets
        cursor = _dict(session.get("cursor"))
        cursor["pending_call"] = None
        cursor["next_action_index"] = int(cursor.get("next_action_index") or 1) + 1
        session["cursor"] = cursor
        _consume_action(session)
        session["last_invalid_action"] = message
        _write_json_atomic(session_file, session)
        if int(invalid.get("used") or 0) > int(invalid.get("limit") or 0):
            raise HarnessError("designer_invalid_action_exhausted", "Designer exceeded its invalid-action budget.")


def _designer_prompt(
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    graph: dict[str, object],
    observations: list[dict[str, object]],
    repair_feedback: list[dict[str, object]],
    active_partition: dict[str, object] | None,
    repair_task: dict[str, object] | None,
    observations_since_delta: int,
    last_invalid_action: str,
) -> str:
    graph_contract = str(graph.get("schema_version") or GRAPH_VERSION)
    delta_contract = delta_version_for(graph_contract)
    payload = {
        "graph_contract": graph_contract,
        "delta_contract": delta_contract,
        "requirement": requirement_model,
        "landscape_summary": landscape.get("summary"),
        "components": landscape.get("components"),
        "scenario_matches": landscape.get("scenario_matches"),
        "graph": graph,
        "active_partition": active_partition,
        "active_repair_task": repair_task,
        "observation_policy": {
            "limit_before_delta": OBSERVATION_ACTION_LIMIT,
            "used_since_delta": observations_since_delta,
            "must_propose_delta_now": (active_partition is not None or repair_task is not None) and observations_since_delta >= OBSERVATION_ACTION_LIMIT,
        },
        "recent_observations": observations,
        "repair_feedback": repair_feedback,
        "last_invalid_action": last_invalid_action,
    }
    correction = (
        "Your previous action was rejected. Fix this exact error before doing anything else: "
        f"{last_invalid_action} Do not resend the rejected delta unchanged.\n\n"
        if last_invalid_action
        else ""
    )
    return (
        correction
        + "Investigate frozen AS-IS evidence, identify GAP, and propose TO-BE one tool action at a time. "
        "Use only the listed tools. Existing behavior must use reuse/modify/no_change with RLA evidence; "
        "new behavior uses add with an evidence_reason. Every node needs requirement_refs, a concise contract, and a "
        f"{SURFACE_CONTRACT_VERSION} surface_contract matching its surface. "
        "Candidate anchors are AS-IS evidence, not automatic TO-BE targets. For disposition=add, choose a concrete new "
        "profile-owned target path/symbol for the required behavior and attach it to an inspected existing composition "
        "owner; never bind new domain behavior to an unrelated health, smoke, demo, or test symbol. The add target must "
        "not be identical to its owner or to an existing anchor. Use modify/reuse when the target itself already exists. "
        "Never repeat an identical successful tool action. If a search returns zero matches, either broaden it once "
        "or model the required surface as add using a component/root from the frozen landscape. "
        "Work only on active_partition, or on active_repair_task when repair feedback exists. active_partition groups "
        "all currently missing surfaces for one scenario/component. active_repair_task contains the one scenario/component "
        "partition whose blockers must be repaired now; do not repeat changes for completed repair targets and never mutate "
        "a node from another component partition. "
        "In composition repair, active_partition.nodes are the only writable nodes; scenario_nodes_read_only exist only "
        "to identify cross-component edge endpoints and must never be updated or removed. "
        "Use its exact partition identity, preserve required_coverage, and add or update a node for every listed surface. "
        "For Graph V2, candidate anchors locate source but do not satisfy investigation: use repo.read or an available "
        "codegraph.query for the active component before its first delta. Make at most three distinct observation actions, "
        "then you must call design.propose_delta before investigating another partition. If must_propose_delta_now is true, "
        "the only valid next action is design.propose_delta. Call complete only when active_partition is null.\n\n"
        "Tools: landscape.search(query?,scenario_id?,surface?,component_id?,kind?); "
        "landscape.get_anchor(anchor_id); repo.search(query,component_id?); "
        "repo.read(path,start_line?,end_line?); codegraph.query(query); "
        "design.inspect_partition(scenario_id,component_id?); "
        f"design.propose_delta(delta). The delta requires schema_version={delta_contract}, "
        "base_graph_digest, partition{use_case_id,scenario_id,component_id}, operations, "
        "completion_claims, and unresolved_items. Supported operations are set_partition_coverage, "
        "add_node, update_node, remove_node, add_edge, and remove_edge. An add_node contains "
        "temp_ref, semantic_key, name, kind, surface, disposition, requirement_refs, evidence_refs, "
        "evidence_reason, contract, surface_contract, and for Graph V2 runtime_binding. Reuse/modify/no_change need an RLA evidence ref; add needs "
        "an evidence_reason and no invented RLA ref. For a mutation, the complete outer envelope must be "
        f"{{\"action\":\"design.propose_delta\",\"arguments\":{{\"delta\":{{\"schema_version\":\"{delta_contract}\",...}}}}}}; "
        f"never use {graph_contract} as the delta schema and never put delta fields directly under arguments. "
        "set_partition_coverage must be exactly {\"op\":\"set_partition_coverage\",\"coverage\":{...}}. "
        "add_node must be exactly {\"op\":\"add_node\",\"node\":{...}}. temp_ref must start with 'tmp:'. "
        "Node kind is the graph kind supplied by node_scaffolds, not a landscape anchor kind. Node contract must be "
        "one non-empty string, never an object. surface_contract must keep schema_version/design_surface_contract_v1 "
        "and surface unchanged. UI requires entrypoint, states, transitions[{from,action,to,outcome}], validations "
        "(an array of non-empty strings, never objects; it may be empty) and "
        "outcomes. API requires operation_id, method, path, request_fields[{name,type,location}], responses[{status,outcome}] "
        "and authorization. business_rule requires inputs (an array of non-empty strings, never objects), decision and "
        "outcomes (an array of non-empty strings). data requires entity, operation, "
        "fields[{name,type}], constraints, relations and migration_intent. interaction requires steps and outcomes. "
        "Use update_node with changes.surface_contract to enrich an existing node during bundle repair. add_edge must be exactly "
        "{\"op\":\"add_edge\",\"edge\":{\"from\":\"VDN-* or tmp:*\",\"to\":\"VDN-* or tmp:*\","
        "\"kind\":\"calls|reads|writes|returns|contains|realized_by|depends_on\",\"label\":\"specific interaction\","
        "\"scenario_id\":\"SC-*\",\"branch_group\":\"optional-safe-key\",\"guard\":\"optional condition\","
        "\"terminal\":false}}. branch_group and guard must be supplied together on every consecutive edge belonging "
        "to a conditional error/alternative branch; set terminal=true on the final observable outcome edge. "
        "Multiple interactions between the same endpoints are allowed when labels differ. "
        "For Graph V2, every edge crossing component/runtime boundaries uses the field boundary_contract; "
        "boundary_contract_v1 is only its schema_version, never the field name. Use exactly "
        "{\"boundary_contract\":{\"schema_version\":\"boundary_contract_v1\","
        "\"consumer_operation\":\"specific caller operation\",\"provider_operation\":\"METHOD /path for an API, "
        "otherwise the specific provider operation\",\"input_mapping\":[{\"from\":\"consumer field\","
        "\"to\":\"provider field\",\"required\":true}],\"auth_mapping\":[],"
        "\"response_mapping\":[{\"from\":\"provider success\",\"to\":\"consumer outcome\","
        "\"required\":true}],\"error_mapping\":[]}}. Map every required API request field, non-public "
        "authorization, success response, and 4xx/5xx response. "
        "In repair mode, assign every edge to the scenario named by the finding, even when an endpoint is reused from "
        "another scenario partition. Edge order is assigned from add_edge operation order. To change an existing "
        "scenario order, remove every obsolete/current edge first using exactly "
        "{\"op\":\"remove_edge\",\"edge_id\":\"VDE-*\"}; never put an edge object inside remove_edge. Then add the "
        "corrected edges in exact behavioral order. Use the IDs in active_repair_task.edges_in_current_order. "
        "Sequence projections are linear: only calls/reads/writes/returns become messages. For an alternative or "
        "exception scenario, connect the main owner to that branch with a depends_on edge (class-only) and keep the "
        "branch interactions in their own scenario. Never create a cross-scenario edge between two main scenarios; a "
        "standalone quality scenario remains separate. Never inline every branch "
        "as unconditional calls in the main success sequence. "
        "Start from delta_scaffold and replace every angle-bracket placeholder.\n\n"
        "Action JSON: {\"action\":\"tool.id\",\"arguments\":{...}}\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _surface_contract_scaffold(surface: str) -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": SURFACE_CONTRACT_VERSION,
        "surface": surface,
    }
    if surface == "ui":
        return {
            **common,
            "entrypoint": "<route, screen, or component>",
            "states": ["<initial state>", "<observable final state>"],
            "transitions": [
                {"from": "<state>", "action": "<user action>", "to": "<state>", "outcome": "<observable outcome>"}
            ],
            "validations": ["<validation rule, or remove this item when none>"],
            "outcomes": ["<observable outcome>"],
        }
    if surface == "api":
        return {
            **common,
            "operation_id": "<stable operation id>",
            "method": "<GET|POST|PUT|PATCH|DELETE>",
            "path": "/<resource path>",
            "request_fields": [{"name": "<field>", "type": "<type>", "location": "<body|path|query>"}],
            "responses": [{"status": "<HTTP status>", "outcome": "<observable outcome>"}],
            "authorization": "<none, authenticated, role-based, or not specified with rationale>",
        }
    if surface == "business_rule":
        return {
            **common,
            "inputs": ["<input>"],
            "decision": "<deterministic decision rule>",
            "outcomes": ["<allowed or rejected outcome>"],
        }
    if surface == "data":
        return {
            **common,
            "entity": "<entity or aggregate>",
            "operation": "<read, write, query, or migration behavior>",
            "fields": [{"name": "<field>", "type": "<type>"}],
            "constraints": [],
            "relations": [],
            "migration_intent": "<add, alter, reuse, or no change with rationale>",
        }
    return {
        **common,
        "steps": ["<interaction step>"],
        "outcomes": ["<observable outcome>"],
    }


def _runtime_binding_scaffold(
    *,
    surface: str,
    component: dict[str, object],
    candidate_anchors: list[dict[str, object]],
    owner_anchors: list[dict[str, object]],
) -> dict[str, object]:
    owner = owner_anchors[0] if owner_anchors else {}
    component_id = str(component.get("component_id") or "<component_id>")
    runtime_kind = {
        "ui": "ui_component",
        "api": "api_route",
        "business_rule": "service",
        "data": "persistence_adapter",
        "interaction": "service",
    }.get(surface, "service")
    relation = {
        "ui_component": "rendered_by",
        "api_route": "registered_by",
        "service": "called_by",
        "persistence_adapter": "injected_by",
    }[runtime_kind]
    return {
        "schema_version": RUNTIME_BINDING_VERSION,
        "runtime_kind": runtime_kind,
        "target": {
            "component_id": component_id,
            "path": "<new profile-owned path for add; exact AS-IS path for modify/reuse>",
            "symbol": "<new symbol for add; exact AS-IS symbol for modify/reuse>",
        },
        "attachment": {
            "relation": relation,
            "owner_ref": str(owner.get("anchor_id") or "<RLA-* or runtime-bound VDN-*>") ,
            "owner_path": str(owner.get("path") or "<composition owner path>"),
            "owner_symbol": str(owner.get("symbol") or "<composition owner symbol>"),
        },
        "evidence_refs": list(
            dict.fromkeys(
                [
                    *([str(owner.get("anchor_id"))] if owner else []),
                    *([str(candidate_anchors[0].get("anchor_id"))] if candidate_anchors else []),
                ]
            )
        )
        or ["<repo:* or codegraph:* evidence ref>"],
    }


def _fake_surface_contract(*, surface: str, scenario_id: str, name: str) -> dict[str, object]:
    common: dict[str, object] = {"schema_version": SURFACE_CONTRACT_VERSION, "surface": surface}
    label = name or f"{scenario_id} {surface}"
    if surface == "ui":
        return {
            **common,
            "entrypoint": label,
            "states": ["ready", "completed"],
            "transitions": [{"from": "ready", "action": "submit", "to": "completed", "outcome": f"{scenario_id} outcome"}],
            "validations": [],
            "outcomes": [f"{scenario_id} outcome"],
        }
    if surface == "api":
        return {
            **common,
            "operation_id": f"handle{scenario_id.replace('-', '')}",
            "method": "POST",
            "path": f"/{scenario_id.casefold()}",
            "request_fields": [],
            "responses": [{"status": "200", "outcome": f"{scenario_id} outcome"}],
            "authorization": "not specified by the approved requirement",
        }
    if surface == "business_rule":
        return {**common, "inputs": ["request"], "decision": f"Evaluate {scenario_id}.", "outcomes": [f"{scenario_id} outcome"]}
    if surface == "data":
        return {
            **common,
            "entity": label,
            "operation": f"Persist or query data for {scenario_id}.",
            "fields": [{"name": "id", "type": "string"}],
            "constraints": [],
            "relations": [],
            "migration_intent": "Add or modify the model required by the approved scenario.",
        }
    return {**common, "steps": [f"Execute {scenario_id}."], "outcomes": [f"{scenario_id} outcome"]}


def _fake_runtime_binding(
    *,
    landscape: dict[str, object],
    component_id: str,
    surface: str,
    anchor: dict[str, object] | None,
    source_refs: list[str],
    fallback_symbol: str,
) -> dict[str, object]:
    component = next(
        (item for item in _dicts(landscape.get("components")) if str(item.get("component_id") or "") == component_id),
        {},
    )
    component_anchors = [
        item
        for item in _dicts(landscape.get("anchors"))
        if str(item.get("component_id") or "") == component_id and str(item.get("anchor_id") or "")
    ]
    owner = anchor or (component_anchors[0] if component_anchors else {})
    roots = _strings(component.get("roots"))
    root = roots[0] if roots else "src"
    if root == ".":
        root = "src"
    symbol = str((anchor or {}).get("symbol") or re.sub(r"[^A-Za-z0-9_]", "", fallback_symbol) or "PlannedTarget")
    path = str((anchor or {}).get("path") or f"{root}/{symbol}.ts")
    runtime_kind = {
        "ui": "ui_component",
        "api": "api_route",
        "business_rule": "service",
        "data": "data_model" if str((anchor or {}).get("kind") or "") == "data_model" else "persistence_adapter",
        "interaction": "service",
    }.get(surface, "service")
    relation = {
        "ui_component": "rendered_by",
        "api_route": "registered_by",
        "service": "called_by",
        "persistence_adapter": "injected_by",
        "data_model": "owned_by",
    }[runtime_kind]
    return {
        "schema_version": RUNTIME_BINDING_VERSION,
        "runtime_kind": runtime_kind,
        "target": {"component_id": component_id, "path": path, "symbol": symbol},
        "attachment": {
            "relation": relation,
            "owner_ref": str(owner.get("anchor_id") or f"RLA-fake-{component_id}"),
            "owner_path": str(owner.get("path") or path),
            "owner_symbol": str(owner.get("symbol") or symbol),
        },
        "evidence_refs": list(dict.fromkeys([*source_refs, *([str(anchor.get("anchor_id"))] if anchor else [])])),
    }


def _fake_boundary_contract(
    *,
    source: dict[str, object],
    target: dict[str, object],
) -> dict[str, object]:
    provider = _dict(target.get("surface_contract"))
    request_fields = _dicts(provider.get("request_fields"))
    responses = _dicts(provider.get("responses"))
    provider_operation = str(target.get("name") or target.get("contract") or "provide")
    if str(target.get("surface") or "") == "api":
        provider_operation = f"{str(provider.get('method') or '').upper()} {str(provider.get('path') or '')}".strip()
    authorization = str(provider.get("authorization") or "").strip().casefold()
    return {
        "schema_version": BOUNDARY_CONTRACT_VERSION,
        "consumer_operation": str(source.get("name") or source.get("contract") or "consume"),
        "provider_operation": provider_operation,
        "input_mapping": [
            {"from": str(item.get("name") or "input"), "to": str(item.get("name") or "input"), "required": True}
            for item in request_fields
        ],
        "auth_mapping": (
            []
            if not authorization or authorization in {"none", "not required", "public"}
            else [{"from": "session.authorization", "to": "authorization", "required": True}]
        ),
        "response_mapping": [
            {"from": str(item.get("status") or "success"), "to": str(item.get("outcome") or "outcome"), "required": True}
            for item in responses
            if not str(item.get("status") or "").startswith(("4", "5"))
        ] or [{"from": "success", "to": "outcome", "required": True}],
        "error_mapping": [
            {"from": str(item.get("status") or "error"), "to": str(item.get("outcome") or "error"), "required": True}
            for item in responses
            if str(item.get("status") or "").startswith(("4", "5"))
        ],
    }


def _surface_rank(surface: str) -> int:
    return {"ui": 0, "api": 1, "business_rule": 2, "data": 3, "interaction": 4}.get(surface, 9)


def _active_partition(
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    graph: dict[str, object],
) -> dict[str, object] | None:
    """Select one deterministic scenario/component slice that still needs Design coverage."""

    anchors = {str(item.get("anchor_id") or ""): item for item in _dicts(landscape.get("anchors"))}
    matches = {
        (str(item.get("scenario_id") or ""), str(item.get("surface") or "")): _strings(item.get("candidate_anchor_refs"))
        for item in _dicts(landscape.get("scenario_matches"))
    }
    partitions = _dicts(graph.get("partitions"))
    nodes = _dicts(graph.get("nodes"))
    surface_order = ("ui", "api", "business_rule", "data", "interaction")

    for scenario in _scenarios(requirement_model):
        scenario_id = str(scenario.get("id") or "")
        required = _required_surface_set(scenario)
        missing: list[str] = []
        for surface in surface_order:
            if surface not in required:
                continue
            covered = any(
                str(item.get("scenario_id") or "") == scenario_id
                and str(_dict(item.get("coverage")).get(surface) or "") in {"covered", "no_change"}
                for item in partitions
            )
            has_node = any(
                str(item.get("scenario_id") or "") == scenario_id and str(item.get("surface") or "") == surface
                for item in nodes
            )
            if not covered or not has_node:
                missing.append(surface)
        if not missing:
            continue

        component_id = _component_for_missing_surface(
            scenario_id=scenario_id,
            surface=missing[0],
            matches=matches,
            anchors=anchors,
            landscape=landscape,
        )
        grouped_surfaces = [
            surface
            for surface in missing
            if _component_for_missing_surface(
                scenario_id=scenario_id,
                surface=surface,
                matches=matches,
                anchors=anchors,
                landscape=landscape,
            )
            == component_id
        ]
        existing_partition = next(
            (
                item
                for item in partitions
                if str(item.get("scenario_id") or "") == scenario_id
                and str(item.get("component_id") or "") == component_id
            ),
            None,
        )
        required_coverage = dict(_dict((existing_partition or {}).get("coverage")))
        required_coverage.update({surface: "covered" for surface in grouped_surfaces})
        surface_work: list[dict[str, object]] = []
        for surface in grouped_surfaces:
            surface_contract = _dict(_dict(scenario.get("surfaces")).get(surface))
            requirement_refs = list(
                dict.fromkeys(
                    [
                        *_strings(surface_contract.get("requirement_refs")),
                        *_strings(scenario.get("requirement_refs")),
                        *_strings(scenario.get("acceptance_refs")),
                    ]
                )
            ) or [scenario_id]
            candidate_refs = matches.get((scenario_id, surface), [])
            candidate_anchors = [anchors[ref] for ref in candidate_refs if ref in anchors and str(anchors[ref].get("component_id") or "") == component_id]
            preferred_kinds = {
                "ui": {"ui_component", "ui_entrypoint"},
                "api": {"api_route", "service_symbol"},
                "business_rule": {"service_symbol", "api_route"},
                "data": {"data_model", "data_operation"},
                "interaction": {"service_symbol"},
            }.get(surface, set())
            candidate_anchors.sort(
                key=lambda item: (
                    0 if str(item.get("kind") or "") in preferred_kinds else 1,
                    0 if str(item.get("symbol") or "") else 1,
                    str(item.get("anchor_id") or ""),
                )
            )
            owner_anchors = _composition_owner_candidates(
                surface=surface,
                component_id=component_id,
                landscape=landscape,
            )
            surface_work.append(
                {
                    "surface": surface,
                    "requirement_refs": requirement_refs,
                    "contract": surface_contract,
                    "candidate_anchors": candidate_anchors[:3],
                    "composition_owner_candidates": owner_anchors[:3],
                }
            )
        component = next(
            (item for item in _dicts(landscape.get("components")) if str(item.get("component_id") or "") == component_id),
            {"component_id": component_id},
        )
        partition = {
            "use_case_id": _requirement_use_case_id(requirement_model),
            "scenario_id": scenario_id,
            "component_id": component_id,
        }
        node_scaffolds = [
            {
                "op": "add_node",
                "node": {
                    "temp_ref": f"tmp:{scenario_id}:{item['surface']}",
                    "semantic_key": f"{scenario_id}:{component_id}:{item['surface']}",
                    "name": "<specific design node name>",
                    "kind": _kind_for_surface(str(item["surface"])),
                    "surface": item["surface"],
                    "disposition": "<reuse|modify|add|no_change>",
                    "requirement_refs": item["requirement_refs"],
                    "evidence_refs": [],
                    "evidence_reason": "<required for add; empty otherwise>",
                    "contract": "<one concise non-empty string>",
                    "surface_contract": _surface_contract_scaffold(str(item["surface"])),
                    "runtime_binding": _runtime_binding_scaffold(
                        surface=str(item["surface"]),
                        component=component,
                        candidate_anchors=_dicts(item.get("candidate_anchors")),
                        owner_anchors=_dicts(item.get("composition_owner_candidates")),
                    ),
                },
            }
            for item in surface_work
        ]
        return {
            "partition": partition,
            "component": component,
            "missing_surfaces": grouped_surfaces,
            "required_coverage": required_coverage,
            "surface_work": surface_work,
            "base_graph_digest": graph.get("artifact_digest"),
            "delta_scaffold": {
                "schema_version": delta_version_for(str(graph.get("schema_version") or "")),
                "base_graph_digest": graph.get("artifact_digest"),
                "partition": partition,
                "operations": [
                    {"op": "set_partition_coverage", "coverage": required_coverage},
                    *node_scaffolds,
                ],
                "completion_claims": [{"scenario_id": scenario_id, "surfaces": grouped_surfaces}],
                "unresolved_items": [],
            },
        }
    composition_findings = completion_findings(
        graph,
        required_surfaces=_required_surfaces(requirement_model),
        landscape=landscape,
    )
    composition_findings = [
        item
        for item in composition_findings
        if str(item.get("code") or "")
        in {
            "design_investigation_missing",
            "runtime_binding_missing",
            "runtime_attachment_missing",
            "component_ownership_invalid",
            "surface_component_mismatch",
            "planned_target_conflicts_as_is",
            "runtime_path_disconnected",
            "boundary_contract_mismatch",
        }
    ]
    if composition_findings:
        finding = composition_findings[0]
        finding_partition = _dict(finding.get("partition"))
        subject_ref = str(finding.get("subject_ref") or "")
        subject_node = next((item for item in nodes if str(item.get("id") or "") == subject_ref), None)
        scenario_id = str(
            finding_partition.get("scenario_id")
            or (subject_node or {}).get("scenario_id")
            or ""
        )
        if not scenario_id:
            diagram_ids = _strings(finding.get("diagram_ids"))
            scenario_id = diagram_ids[0] if diagram_ids else subject_ref.split("@", 1)[0]
        scenario_nodes = [item for item in nodes if str(item.get("scenario_id") or "") == scenario_id]
        component_id = str(
            finding_partition.get("component_id")
            or (subject_node or {}).get("component_id")
            or ""
        )
        if not component_id and "@" in subject_ref:
            component_id = subject_ref.split("@", 1)[1]
        if not component_id and scenario_nodes:
            component_id = str(scenario_nodes[0].get("component_id") or "")
        matching_partition = next(
            (
                item
                for item in partitions
                if str(item.get("scenario_id") or "") == scenario_id
                and str(item.get("component_id") or "") == component_id
            ),
            {},
        )
        component = next(
            (item for item in _dicts(landscape.get("components")) if str(item.get("component_id") or "") == component_id),
            {"component_id": component_id},
        )
        partition = {
            "use_case_id": _requirement_use_case_id(requirement_model),
            "scenario_id": scenario_id,
            "component_id": component_id,
        }
        return {
            "partition": partition,
            "component": component,
            "missing_surfaces": [],
            "required_coverage": _dict(matching_partition.get("coverage")),
            "surface_work": [],
            "composition_findings": [finding],
            "nodes": [
                item
                for item in scenario_nodes
                if str(item.get("component_id") or "") == component_id
            ],
            "scenario_nodes_read_only": scenario_nodes,
            "edges_in_current_order": [
                item for item in _dicts(graph.get("edges")) if str(item.get("scenario_id") or "") == scenario_id
            ],
            "base_graph_digest": graph.get("artifact_digest"),
            "delta_scaffold": {
                "schema_version": delta_version_for(str(graph.get("schema_version") or "")),
                "base_graph_digest": graph.get("artifact_digest"),
                "partition": partition,
                "operations": [
                    {
                        "op": "set_partition_coverage",
                        "coverage": _dict(matching_partition.get("coverage")),
                    }
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
        }
    return None


def _active_repair_task(
    *,
    repair_feedback: list[dict[str, object]],
    graph: dict[str, object],
    completed_targets: list[str],
) -> dict[str, object] | None:
    if not repair_feedback:
        return None
    nodes = _dicts(graph.get("nodes"))
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    targets = _repair_work_targets(repair_feedback, nodes_by_id=nodes_by_id)
    target = next((item for item in targets if str(item.get("target_id") or "") not in set(completed_targets)), None)
    if not isinstance(target, dict):
        return None
    target_id = str(target.get("target_id") or "")
    scenario_id = str(target.get("scenario_id") or "")
    component_id = str(target.get("component_id") or "")
    edges = _dicts(graph.get("edges"))
    if target_id == "global":
        target_edges = edges
        target_nodes = nodes
        findings = repair_feedback
    else:
        target_edges = [item for item in edges if str(item.get("scenario_id") or "") == scenario_id]
        node_ids = {
            str(item.get("id") or "")
            for item in nodes
            if str(item.get("scenario_id") or "") == scenario_id
            and (not component_id or str(item.get("component_id") or "") == component_id)
        }
        target_nodes = [item for item in nodes if str(item.get("id") or "") in node_ids]
        findings = [
            item
            for item in repair_feedback
            if target_id in {
                str(candidate.get("target_id") or "")
                for candidate in _repair_work_targets([item], nodes_by_id=nodes_by_id)
            }
        ]
    partitions = _dicts(graph.get("partitions"))
    matching_partitions = [
        item
        for item in partitions
        if target_id == "global"
        or (
            str(item.get("scenario_id") or "") == scenario_id
            and (not component_id or str(item.get("component_id") or "") == component_id)
        )
    ]
    matching_partitions.sort(key=lambda item: str(item.get("component_id") or ""))
    primary_partition = matching_partitions[0] if matching_partitions else {}
    return {
        "target_id": target_id,
        "partition": {
            key: primary_partition.get(key)
            for key in ("use_case_id", "scenario_id", "component_id")
            if primary_partition.get(key)
        },
        "findings": findings,
        "nodes": target_nodes,
        "edges_in_current_order": target_edges,
        "remaining_targets": [
            str(item.get("target_id") or "")
            for item in targets
            if str(item.get("target_id") or "") not in set(completed_targets)
        ],
        "instruction": (
            "Repair every listed finding for this exact scenario/component partition in one delta. "
            "Do not update nodes listed only in another component partition. Preserve unrelated partitions. "
            "Use remove_edge plus ordered add_edge operations when interaction order must change."
        ),
    }


def _repair_work_targets(
    repair_feedback: list[dict[str, object]],
    *,
    nodes_by_id: dict[str, dict[str, object]],
) -> list[dict[str, str]]:
    """Split node-scoped bundle repairs by the graph's mutation partition."""

    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for finding in repair_feedback:
        partition = _dict(finding.get("partition"))
        scenario_id = str(partition.get("scenario_id") or "")
        component_id = str(partition.get("component_id") or "")
        if scenario_id and component_id:
            target_id = f"{scenario_id}@{component_id}"
            if target_id not in seen:
                seen.add(target_id)
                targets.append(
                    {
                        "target_id": target_id,
                        "scenario_id": scenario_id,
                        "component_id": component_id,
                    }
                )
            continue
        if str(finding.get("category") or "") == "surface_contract_missing":
            subject_ref = str(finding.get("subject_ref") or "")
            node = next((item for node_id, item in nodes_by_id.items() if node_id and node_id in subject_ref), None)
            if node is not None:
                scenario_id = str(node.get("scenario_id") or "")
                component_id = str(node.get("component_id") or "")
                target_id = f"{scenario_id}@{component_id}"
                if target_id not in seen:
                    seen.add(target_id)
                    targets.append(
                        {
                            "target_id": target_id,
                            "scenario_id": scenario_id,
                            "component_id": component_id,
                        }
                    )
                continue
        finding_targets = _finding_repair_targets(finding)
        for scenario_id in finding_targets:
            if scenario_id not in seen:
                seen.add(scenario_id)
                targets.append({"target_id": scenario_id, "scenario_id": scenario_id, "component_id": ""})
    if not targets:
        return [{"target_id": "global", "scenario_id": "", "component_id": ""}]
    return targets


def _repair_targets(repair_feedback: list[dict[str, object]]) -> list[str]:
    targets: list[str] = []
    for finding in repair_feedback:
        for target in _finding_repair_targets(finding):
            if target not in targets:
                targets.append(target)
    return targets or ["global"]


def _finding_repair_targets(finding: dict[str, object]) -> list[str]:
    values = [
        *_strings(finding.get("diagram_ids")),
        str(finding.get("subject_ref") or ""),
    ]
    targets: list[str] = []
    for value in values:
        for match in re.finditer(r"(?:sequence[_-])?sc[_-](\d+)", value, flags=re.IGNORECASE):
            target = f"SC-{int(match.group(1))}"
            if target not in targets:
                targets.append(target)
    return targets


def _mark_repair_targets_completed(
    *,
    session: dict[str, object],
    arguments: object,
    repair_task: dict[str, object],
) -> None:
    del arguments
    target = str(repair_task.get("target_id") or "")
    completed = _strings(session.get("repair_targets_completed"))
    if target and target not in completed:
        completed.append(target)
    session["repair_targets_completed"] = completed


def _requirement_use_case_id(requirement_model: dict[str, object]) -> str:
    return str(
        requirement_model.get("use_case_id")
        or requirement_model.get("id")
        or _dict(requirement_model.get("use_case")).get("id")
        or "use-case"
    )


def _required_surfaces(requirement_model: dict[str, object]) -> dict[str, set[str]]:
    return {str(item.get("id") or ""): _required_surface_set(item) for item in _scenarios(requirement_model)}


def _required_surface_set(scenario: dict[str, object]) -> set[str]:
    surfaces = _dict(scenario.get("surfaces"))
    return {
        str(surface)
        for surface, value in surfaces.items()
        if isinstance(value, dict) and str(value.get("status") or "") == "required"
    }


def _scenarios(requirement_model: dict[str, object]) -> list[dict[str, object]]:
    contract = _dict(requirement_model.get("scenario_contract"))
    return _dicts(contract.get("scenarios"))


def _component_for_missing_surface(
    *,
    scenario_id: str,
    surface: str,
    matches: dict[tuple[str, str], list[str]],
    anchors: dict[str, dict[str, object]],
    landscape: dict[str, object],
) -> str:
    candidates = [
        anchors[ref]
        for ref in matches.get((scenario_id, surface), [])
        if ref in anchors and str(anchors[ref].get("component_id") or "")
    ]
    preferred_components = {
        "ui": ("web", "frontend", "ui", "client"),
        "api": ("api", "backend", "server"),
        "business_rule": ("api", "backend", "service", "domain"),
        "data": ("database", "db", "persistence"),
        "interaction": ("api", "backend", "web", "frontend"),
    }.get(surface, ())
    candidates.sort(
        key=lambda item: (
            next(
                (
                    index
                    for index, alias in enumerate(preferred_components)
                    if alias in str(item.get("component_id") or "").casefold()
                ),
                len(preferred_components),
            ),
            0 if str(item.get("symbol") or "") else 1,
            str(item.get("anchor_id") or ""),
        )
    )
    return str(candidates[0].get("component_id") or "") if candidates else _default_component(landscape, hint=surface)


def _composition_owner_candidates(
    *,
    surface: str,
    component_id: str,
    landscape: dict[str, object],
) -> list[dict[str, object]]:
    """Prefer real composition roots over feature/demo anchors used only as evidence."""

    preferred_kinds = {
        "ui": ("ui_component", "ui_entrypoint", "source_file"),
        "api": ("service_symbol", "api_route", "source_file"),
        "business_rule": ("service_symbol", "api_route", "source_file"),
        "data": ("service_symbol", "source_file", "data_model", "data_operation"),
        "interaction": ("service_symbol", "ui_component", "source_file"),
    }.get(surface, ("service_symbol", "source_file"))
    anchors = [
        item
        for item in _dicts(landscape.get("anchors"))
        if str(item.get("component_id") or "") == component_id
        and str(item.get("anchor_id") or "")
        and str(item.get("kind") or "") not in {"test", "manifest", "verification_config", "migration"}
    ]

    def rank(item: dict[str, object]) -> tuple[int, int, int, str]:
        kind = str(item.get("kind") or "")
        haystack = " ".join(
            [str(item.get("path") or ""), str(item.get("symbol") or "")]
        ).casefold()
        incidental = int(any(token in haystack for token in ("health", "smoke", "demo", "example", "seed")))
        composition_hint = int(
            not any(
                token in haystack
                for token in ("appshell", "main", "createapp", "server", "client", "index")
            )
        )
        kind_rank = preferred_kinds.index(kind) if kind in preferred_kinds else len(preferred_kinds)
        return incidental, composition_hint, kind_rank, str(item.get("anchor_id") or "")

    return sorted(anchors, key=rank)


def _default_component(landscape: dict[str, object], *, hint: str = "") -> str:
    component_rows = [
        item for item in _dicts(landscape.get("components")) if str(item.get("component_id") or "")
    ]
    components = sorted(str(item.get("component_id") or "") for item in component_rows)
    if not components:
        raise HarnessError("design_evidence_insufficient", "Frozen repo landscape has no component owner.")
    normalized_hint = hint.casefold().replace("business_rule", "service")
    matching = sorted(
        str(item.get("component_id") or "")
        for item in component_rows
        if normalized_hint
        and normalized_hint
        in " ".join([str(item.get("component_id") or ""), *_strings(item.get("roots"))]).casefold()
    )
    if matching:
        return matching[0]
    return components[0]


def _kind_for_surface(surface: str) -> str:
    return {"ui": "ui_interaction", "api": "api_contract", "business_rule": "business_rule", "data": "data_operation", "interaction": "interaction_step"}.get(surface, "planned_work")


def _consume_action(session: dict[str, object]) -> None:
    budgets = _dict(session.get("budgets"))
    action = _dict(budgets.get("action"))
    action["used"] = int(action.get("used") or 0) + 1
    budgets["action"] = action
    session["budgets"] = budgets


def _progress(session: dict[str, object]) -> dict[str, object]:
    budgets = _dict(session.get("budgets"))
    action = _dict(budgets.get("action"))
    invalid = _dict(budgets.get("invalid_action"))
    graph = _dict(session.get("graph"))
    return {
        "status": session.get("status"),
        "action_budget": {"limit": int(action.get("limit") or 0), "used": int(action.get("used") or 0), "remaining": max(0, int(action.get("limit") or 0) - int(action.get("used") or 0))},
        "invalid_action_budget": {"limit": int(invalid.get("limit") or 0), "used": int(invalid.get("used") or 0), "remaining": max(0, int(invalid.get("limit") or 0) - int(invalid.get("used") or 0))},
        "repair_iteration": int(budgets.get("repair_iteration") or 0),
        "graph_revision": int(graph.get("revision") or 0),
        "graph_digest": str(graph.get("artifact_digest") or ""),
    }


def _read_json(path: Path, *, run_id: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("designer_session_invalid", f"Invalid Designer runtime artifact: {path.name}", run_id=run_id) from exc
    if not isinstance(value, dict):
        raise HarnessError("designer_session_invalid", f"Designer runtime artifact is not an object: {path.name}", run_id=run_id)
    return value


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_journal(path: Path, entry: dict[str, object]) -> None:
    entries: list[dict[str, object]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    entries.append(value)
    if not any(str(item.get("call_id") or "") == str(entry.get("call_id") or "") for item in entries):
        entries.append(entry)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in entries), encoding="utf-8")
    temporary.replace(path)


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []


def _source_observation_refs(session: dict[str, object], component_id: str) -> list[str]:
    return _strings(_dict(session.get("source_observations")).get(component_id)) if component_id else []


def _record_source_observations(
    session: dict[str, object],
    *,
    component_id: str,
    refs: list[str],
) -> None:
    if not component_id or not refs:
        return
    observations = _dict(session.get("source_observations"))
    observations[component_id] = list(dict.fromkeys([*_strings(observations.get(component_id)), *refs]))
    session["source_observations"] = observations


def _attach_source_observations(
    arguments: object,
    *,
    component_id: str,
    refs: list[str],
) -> dict[str, object]:
    payload = json.loads(json.dumps(arguments, ensure_ascii=False)) if isinstance(arguments, dict) else {}
    delta = _dict(payload.get("delta"))
    partition = _dict(delta.get("partition"))
    if str(partition.get("component_id") or "") != component_id:
        raise HarnessError(
            "designer_delta_invalid",
            "Designer delta component does not match the investigated active component.",
        )
    partition["source_observation_refs"] = list(dict.fromkeys(refs))
    delta["partition"] = partition
    payload["delta"] = delta
    return payload
