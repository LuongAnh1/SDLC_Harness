"""Durable, bounded read-only tool loop for the L3 Designer role."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ...core.config import phase2_llm_call_options
from ...core.errors import HarnessError
from ...llm.json_utils import parse_first_json_object
from ...llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
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
    graph_digest,
    new_vertical_design_graph,
    validate_vertical_design_graph,
)
from .tools import DesignerToolRegistry
from .uiux import (
    UIUX_CONTRACT_VERSION,
    UIUX_EXPERIENCE_CONTRACT_VERSION,
    UiUxGuidanceProvider,
)


SESSION_VERSION = "designer_loop_session_v1"
MANIFEST_VERSION = "designer_loop_manifest_v1"
JOURNAL_VERSION = "designer_loop_journal_v1"
ACTION_BUDGET_POLICY_VERSION = "designer_action_budget_policy_v1"
INVALID_ACTION_LIMIT = 3
OBSERVATION_ACTION_LIMIT = 3
REPAIR_OBSERVATION_ACTION_LIMIT = 6
UI_OBSERVATION_ACTION_LIMIT = 5
UI_REPAIR_OBSERVATION_ACTION_LIMIT = 8
OBSERVATION_TOOL_IDS = frozenset(
    {
        "landscape.search",
        "landscape.get_anchor",
        "repo.search",
        "repo.read",
        "codegraph.query",
        "uiux.search_guidance",
        "design.inspect_partition",
    }
)
COMPOSITION_REPAIR_CODES = frozenset(
    {
        "design_investigation_missing",
        "runtime_binding_missing",
        "runtime_attachment_missing",
        "component_ownership_invalid",
        "planned_target_conflicts_as_is",
        "runtime_path_disconnected",
        "boundary_contract_mismatch",
        "runtime_boundary_unreachable",
        "runtime_operation_binding_mismatch",
        "duplicate_runtime_api_operation",
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
    def __init__(
        self,
        *,
        settings: Any,
        store: RuntimeStore,
        llm_client: Any,
        step_review: dict[str, object] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.llm_client = llm_client
        self.step_review = dict(step_review or {})

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
        uiux_mode: str = "off",
        uiux_contract_version: str = "",
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
        uiux_provider: UiUxGuidanceProvider | None = None
        uiux_profile: dict[str, object] = {}
        if uiux_mode == "enforce":
            if uiux_contract_version != UIUX_CONTRACT_VERSION:
                raise DesignerExecutionError(
                    "designer_uiux_contract_missing",
                    "Run has no supported pinned UI/UX guidance contract.",
                    run_id=run_id,
                    retriable=False,
                    session_path=session_path,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                )
            uiux_provider = UiUxGuidanceProvider(
                skill_root=self.settings.uiux_skill_root,
                timeout_seconds=self.settings.uiux_skill_timeout_seconds,
                fake_mode=self.settings.llm_mode == "fake",
            )
            try:
                uiux_profile = uiux_provider.preflight()
            except HarnessError as exc:
                raise DesignerExecutionError(
                    exc.error_code,
                    exc.message,
                    run_id=run_id,
                    retriable=exc.error_code in {"designer_uiux_unavailable", "designer_uiux_failed"},
                    session_path=session_path,
                    manifest_path=manifest_path,
                    journal_path=journal_path,
                ) from exc
        action_budget_policy = _designer_action_budget_policy(
            settings=self.settings,
            requirement_model=requirement_model,
            landscape=landscape,
        )
        manifest = self._manifest(
            run_id=run_id,
            requirement_model=requirement_model,
            landscape=landscape,
            snapshot_binding=snapshot_binding,
            target_path=target_path,
            repair_feedback=repair_feedback or [],
            session_ordinal=session_ordinal,
            contract_version=contract_version,
            uiux_mode=uiux_mode,
            uiux_profile=uiux_profile,
            action_budget_policy=action_budget_policy,
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
            persisted_manifest["action_budget"] = manifest["action_budget"]
            persisted_manifest["action_budget_policy"] = manifest["action_budget_policy"]
            _write_json_atomic(manifest_file, persisted_manifest)
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
            _configure_action_budgets(session, action_budget_policy)
            _write_json_atomic(session_file, session)
        else:
            graph = existing_graph or new_vertical_design_graph(
                run_id=run_id,
                snapshot_binding=snapshot_binding,
                contract_version=contract_version,
            )
            graph = validate_vertical_design_graph(graph, expected_run_id=run_id, expected_binding=snapshot_binding)
            graph, reconciled_stale_partitions = _reconcile_stale_placeholder_partitions(
                requirement_model=requirement_model,
                landscape=landscape,
                graph=graph,
            )
            session = {
                "schema_version": SESSION_VERSION,
                "run_id": run_id,
                "session_name": session_name,
                "status": "running",
                "manifest_input_hash": manifest["input_hash"],
                "cursor": {"next_action_index": 1, "pending_call": None},
                "budgets": {
                    **_new_action_budgets(action_budget_policy),
                    "invalid_action": {"limit": INVALID_ACTION_LIMIT, "used": 0},
                    "repair_iteration": max(0, session_ordinal - 1),
                },
                "graph": graph,
                "delta_paths": [],
                "observations": [],
                "observation_actions_since_delta": 0,
                "source_observations": {},
                "guidance_observations": {},
                "repair_observations": {},
                "invalid_actions_by_target": {},
                "active_repair_target_id": "",
                "repair_targets_completed": [],
                "reconciled_stale_partitions": reconciled_stale_partitions,
                "exchange_paths": [],
                "pending_step_review": None,
                "last_review_feedback": "",
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
            uiux_provider=uiux_provider,
            uiux_mode=uiux_mode,
            expected_codegraph_digest=str(snapshot_binding.get("repo_codegraph_snapshot_digest") or ""),
        )
        for refs in _dict(session.get("source_observations")).values():
            registry.register_evidence_refs(_strings(refs))
        for values in _dict(session.get("guidance_observations")).values():
            for item in _dicts(values):
                registry.register_guidance_refs(_strings(item.get("evidence_refs")))
        self._reconcile_pending(
            run_id=run_id,
            session=session,
            session_file=session_file,
            session_dir=session_dir,
            registry=registry,
            journal_file=journal_file,
        )
        if str(session.get("status") or "") == "completed":
            return DesignerExecutionResult(
                graph=registry.graph,
                session_path=session_path,
                manifest_path=manifest_path,
                journal_path=journal_path,
                delta_paths=_strings(session.get("delta_paths")),
                progress=_progress(session),
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
            retriable = exc.error_code in {
                "llm_transport_failed",
                "target_repo_codegraph_failed",
                "target_repo_codegraph_stale",
                "designer_uiux_unavailable",
                "designer_uiux_failed",
            }
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
                guidance_refs = (
                    self._ensure_fake_ui_guidance(
                        run_id=run_id,
                        scenario_id=scenario_id,
                        component_id=component_id,
                        registry=registry,
                        session=session,
                        session_file=session_file,
                        session_dir=session_dir,
                        journal_file=journal_file,
                        on_progress=on_progress,
                    )
                    if any(surface == "ui" for surface, _ in group["nodes"])
                    else []
                )
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
                                    uiux_mode=registry.uiux_mode,
                                    guidance_refs=guidance_refs,
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

    def _ensure_fake_ui_guidance(
        self,
        *,
        run_id: str,
        scenario_id: str,
        component_id: str,
        registry: DesignerToolRegistry,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
        on_progress: Any | None,
    ) -> list[str]:
        if registry.uiux_mode != "enforce":
            return []
        partition_key = f"{scenario_id}|{component_id}"
        session["active_partition_key"] = partition_key
        session["active_component_id"] = component_id
        calls = (
            {"query": "confirmation focus feedback", "mode": "domain", "domain": "ux"},
            {"query": "dialog state accessibility", "mode": "stack", "stack": "react"},
        )
        for arguments in calls:
            selector = str(arguments.get("domain") or arguments.get("stack") or "")
            if _guidance_attempt_count(
                session,
                partition_key,
                mode=str(arguments["mode"]),
                selector=selector,
            ):
                continue
            self._execute_tool_action(
                run_id=run_id,
                tool_id="uiux.search_guidance",
                arguments=arguments,
                registry=registry,
                session=session,
                session_file=session_file,
                session_dir=session_dir,
                journal_file=journal_file,
                on_progress=on_progress,
            )
        return [
            ref
            for item in _guidance_observations(session, partition_key)
            for ref in _strings(item.get("evidence_refs"))
        ]

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
                registry.uiux_mode == "enforce"
                and str(item.get("surface") or "") == "ui"
                and not isinstance(_dict(item.get("surface_contract")).get("experience_contract"), dict)
            )
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
            guidance_refs = (
                self._ensure_fake_ui_guidance(
                    run_id=run_id,
                    scenario_id=key[1],
                    component_id=key[2],
                    registry=registry,
                    session=session,
                    session_file=session_file,
                    session_dir=session_dir,
                    journal_file=journal_file,
                    on_progress=on_progress,
                )
                if any(str(node.get("surface") or "") == "ui" for node in partition_nodes)
                else []
            )
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
                        uiux_mode=registry.uiux_mode,
                        guidance_refs=guidance_refs,
                    )
                elif (
                    registry.uiux_mode == "enforce"
                    and str(node.get("surface") or "") == "ui"
                    and not isinstance(_dict(node.get("surface_contract")).get("experience_contract"), dict)
                ):
                    changes["surface_contract"] = _fake_surface_contract(
                        surface="ui",
                        scenario_id=str(node.get("scenario_id") or "SC-1"),
                        name=str(node.get("name") or node.get("semantic_key") or "Design element"),
                        uiux_mode=registry.uiux_mode,
                        guidance_refs=guidance_refs,
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
            inline_feedback = (
                []
                if repair_feedback or active_partition is not None
                else _composition_repair_findings(
                    registry.graph,
                    requirement_model=requirement_model,
                    landscape=landscape,
                )
            )
            effective_repair_feedback = _applicable_repair_feedback(
                repair_feedback or inline_feedback,
                graph=registry.graph,
            )
            repair_task = _active_repair_task(
                repair_feedback=effective_repair_feedback,
                graph=registry.graph,
                completed_targets=(
                    _strings(session.get("repair_targets_completed")) if repair_feedback else []
                ),
                landscape=landscape,
                source="validator_feedback" if repair_feedback else "inline_composition",
                active_partition=active_partition,
            )
            if repair_task is not None and not _repair_task_has_mutation_path(repair_task):
                repair_task = None
            _activate_repair_target(session, str((repair_task or {}).get("target_id") or ""))
            active_component_id = str(
                _dict((repair_task or {}).get("partition")).get("component_id")
                or _dict((active_partition or {}).get("partition")).get("component_id")
                or ""
            )
            session["active_component_id"] = active_component_id
            active_partition_key = _guidance_partition_key(
                active_partition=active_partition,
                repair_task=repair_task,
            )
            ui_guidance_required = _ui_guidance_required(
                active_partition=active_partition,
                repair_task=repair_task,
                graph=registry.graph,
            )
            session["active_partition_key"] = active_partition_key
            session["ui_guidance_required"] = ui_guidance_required
            if repair_feedback and repair_task is None and active_partition is None:
                return
            if not repair_feedback and active_partition is None and repair_task is None:
                return
            _activate_action_budget_scope(
                session,
                active_partition_key=active_partition_key,
                repair_task=repair_task,
            )
            if _invalid_action_exhausted(session):
                raise HarnessError("designer_invalid_action_exhausted", "Designer exceeded its invalid-action budget.")
            exhausted_message = _action_budget_exhausted_message(session)
            if exhausted_message:
                raise HarnessError("design_llm_budget_exhausted", exhausted_message, run_id=run_id)
            observations_since_delta = int(session.get("observation_actions_since_delta") or 0)
            observation_limit = (
                UI_REPAIR_OBSERVATION_ACTION_LIMIT
                if ui_guidance_required and repair_task is not None
                else UI_OBSERVATION_ACTION_LIMIT
                if ui_guidance_required
                else REPAIR_OBSERVATION_ACTION_LIMIT
                if repair_task is not None
                else OBSERVATION_ACTION_LIMIT
            )
            pending_review = _dict(session.get("pending_step_review"))
            if pending_review and str(pending_review.get("owner") or "") == "llm_pre_call":
                session["pending_step_review"] = None
                session["status"] = "running"
                cursor = _dict(session.get("cursor"))
                cursor["pending_call"] = None
                session["cursor"] = cursor
                _write_json_atomic(session_file, session)
                pending_review = {}
            if pending_review:
                call_id = str(pending_review.get("call_id") or "")
                exchange_path = session_dir / str(pending_review.get("exchange_file") or "")
                action = _dict(pending_review.get("action"))
                parse_error = _dict(pending_review.get("parse_error"))
                if parse_error:
                    exchange = _read_json(exchange_path, run_id=run_id)
                    try:
                        action, normalization = _parse_designer_tool_action(
                            str(exchange.get("raw_completion") or ""),
                            require_runtime_binding=(
                                str(registry.graph.get("schema_version") or "") == GRAPH_VERSION_V2
                            ),
                        )
                    except json.JSONDecodeError:
                        pass
                    else:
                        pending_review["action"] = action
                        pending_review.pop("parse_error", None)
                        pending_review["normalization"] = normalization
                        session["pending_step_review"] = pending_review
                        _update_designer_exchange(
                            exchange_path,
                            {
                                "parsed_action": action,
                                "normalization": normalization,
                                "rejection": None,
                            },
                        )
                        _write_json_atomic(session_file, session)
                        parse_error = {}
                decision = _step_review_decision(self.step_review, call_id=call_id)
                if decision == "approve" and parse_error:
                    decision = ""
                if not decision:
                    raise self._step_review_required(
                        run_id=run_id,
                        call_id=call_id,
                        session=session,
                        session_file=session_file,
                        session_dir=session_dir,
                        journal_file=journal_file,
                    )
                if decision == "reject":
                    note = str(self.step_review.get("note") or "").strip() or "Operator rejected this Designer action."
                    session["pending_step_review"] = None
                    session["last_review_feedback"] = note
                    if parse_error:
                        session["last_invalid_action"] = ""
                    session["status"] = "running"
                    cursor = _dict(session.get("cursor"))
                    cursor["pending_call"] = None
                    cursor["next_action_index"] = int(cursor.get("next_action_index") or 1) + 1
                    session["cursor"] = cursor
                    _consume_action(session)
                    self._record_observation(
                        session,
                        {
                            "tool_id": str(action.get("action") or "llm"),
                            "arguments": action.get("arguments"),
                            "status": "operator_rejected",
                            "message": note,
                        },
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"status": "operator_rejected", "operator_review": {"decision": "reject", "note": note}},
                    )
                    _write_json_atomic(session_file, session)
                    continue
                session["pending_step_review"] = None
                session["last_review_feedback"] = ""
                session["status"] = "running"
                _update_designer_exchange(
                    exchange_path,
                    {"operator_review": {"decision": "approve", "note": str(self.step_review.get("note") or "").strip()}},
                )
                _write_json_atomic(session_file, session)
            else:
                call_id = f"designer-{int(_dict(session.get('cursor')).get('next_action_index') or 1):04d}"
                cursor = _dict(session.get("cursor"))
                cursor["pending_call"] = {"call_id": call_id, "stage": "llm", "created_at": utc_now_iso()}
                session["cursor"] = cursor
                _write_json_atomic(session_file, session)
                prompt = _designer_prompt(
                    requirement_model=requirement_model,
                    landscape=landscape,
                    graph=registry.graph,
                    observations=_prompt_observations(
                        session,
                        repair_task=repair_task,
                        active_component_id=active_component_id,
                    ),
                    repair_feedback=([] if active_partition is not None else effective_repair_feedback),
                    active_partition=active_partition,
                    repair_task=repair_task,
                    observations_since_delta=observations_since_delta,
                    last_invalid_action=str(session.get("last_invalid_action") or ""),
                    observation_limit=observation_limit,
                    last_review_feedback=str(session.get("last_review_feedback") or ""),
                    uiux_mode=registry.uiux_mode,
                    active_guidance=_guidance_observations(session, active_partition_key),
                    source_inspected=bool(_source_observation_refs(session, active_component_id)),
                )
                system_prompt = "You are SDLC Harness Designer L3. Return exactly one JSON tool action."
                profile_name = _designer_llm_profile_name(
                    repair_task=repair_task,
                    source_observation_refs=_source_observation_refs(session, active_component_id),
                    observations_since_delta=observations_since_delta,
                    observation_limit=observation_limit,
                )
                call_options = phase2_llm_call_options(
                    self.settings,
                    profile_name,
                    fallback_max_tokens=self.settings.llm_structured_max_tokens,
                )
                if bool(self.step_review.get("enabled")) and bool(self.step_review.get("review_input")):
                    llm_client = ReviewableLlmClient(
                        self.llm_client,
                        store=self.store,
                        run_id=run_id,
                        review={**self.step_review, "review_output": False},
                        call_prefix="designer-input",
                        execution_dir=(
                            f"designer_execution/{str(session.get('session_name') or 'design_v1')}/input_step_review"
                        ),
                        error_prefix="designer_input",
                        label="Designer input",
                        start_ordinal=int(cursor.get("next_action_index") or 1) - 1,
                    )
                    try:
                        completion = llm_client.complete_with_metadata(
                            system=system_prompt,
                            prompt=prompt,
                            json_mode=True,
                            **call_options,
                        )
                    except LlmStepReviewRequired as exc:
                        cursor = _dict(session.get("cursor"))
                        cursor["pending_call"] = None
                        session["cursor"] = cursor
                        session["pending_step_review"] = {
                            "call_id": str(exc.progress.get("call_id") or ""),
                            "exchange_path": str(exc.progress.get("exchange_path") or ""),
                            "action": {},
                            "owner": "llm_pre_call",
                            "review_stage": "input",
                            "created_at": utc_now_iso(),
                        }
                        session["status"] = "review_required"
                        _write_json_atomic(session_file, session)
                        raise self._step_review_required(
                            run_id=run_id,
                            call_id=str(exc.progress.get("call_id") or ""),
                            session=session,
                            session_file=session_file,
                            session_dir=session_dir,
                            journal_file=journal_file,
                        ) from exc
                else:
                    completion = self.llm_client.complete_messages(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": prompt},
                        ],
                        json_mode=True,
                        **call_options,
                    )
                exchange_path = _write_designer_exchange(
                    session_dir=session_dir,
                    call_id=call_id,
                    prompt=prompt,
                    completion=completion,
                )
                exchange_paths = _strings(session.get("exchange_paths"))
                relative_exchange_path = self.store.relative(exchange_path)
                if relative_exchange_path not in exchange_paths:
                    exchange_paths.append(relative_exchange_path)
                session["exchange_paths"] = exchange_paths
                _write_json_atomic(session_file, session)
                try:
                    action, normalization = _parse_designer_tool_action(
                        completion.content,
                        require_runtime_binding=(
                            str(registry.graph.get("schema_version") or "") == GRAPH_VERSION_V2
                        ),
                    )
                except json.JSONDecodeError:
                    if bool(self.step_review.get("enabled")):
                        cursor = _dict(session.get("cursor"))
                        cursor["pending_call"] = None
                        session["cursor"] = cursor
                        session["pending_step_review"] = {
                            "call_id": call_id,
                            "exchange_file": exchange_path.name,
                            "exchange_path": self.store.relative(exchange_path),
                            "action": {},
                            "parse_error": {
                                "code": "invalid_json",
                                "message": "Designer returned invalid JSON.",
                            },
                            "created_at": utc_now_iso(),
                        }
                        session["status"] = "review_required"
                        _update_designer_exchange(
                            exchange_path,
                            {
                                "status": "review_required",
                                "rejection": {"code": "invalid_json", "operator_review_required": True},
                            },
                        )
                        _write_json_atomic(session_file, session)
                        raise self._step_review_required(
                            run_id=run_id,
                            call_id=call_id,
                            session=session,
                            session_file=session_file,
                            session_dir=session_dir,
                            journal_file=journal_file,
                        )
                    self._invalid_action(session, session_file, message="Designer returned invalid JSON.")
                    self._record_observation(
                        session,
                        {"tool_id": "llm", "status": "rejected", "message": "Designer returned invalid JSON."},
                    )
                    _update_designer_exchange(exchange_path, {"rejection": {"code": "invalid_json"}})
                    _write_json_atomic(session_file, session)
                    continue
                exchange_update: dict[str, object] = {"parsed_action": action}
                if normalization:
                    exchange_update["normalization"] = normalization
                _update_designer_exchange(exchange_path, exchange_update)
                if bool(self.step_review.get("enabled")):
                    cursor = _dict(session.get("cursor"))
                    cursor["pending_call"] = None
                    session["cursor"] = cursor
                    session["pending_step_review"] = {
                        "call_id": call_id,
                        "exchange_file": exchange_path.name,
                        "exchange_path": self.store.relative(exchange_path),
                        "action": action,
                        "created_at": utc_now_iso(),
                    }
                    session["status"] = "review_required"
                    _update_designer_exchange(exchange_path, {"status": "review_required"})
                    _write_json_atomic(session_file, session)
                    raise self._step_review_required(
                        run_id=run_id,
                        call_id=call_id,
                        session=session,
                        session_file=session_file,
                        session_dir=session_dir,
                        journal_file=journal_file,
                    )
            cursor = _dict(session.get("cursor"))
            tool_id = str(action.get("action") or "")
            arguments = action.get("arguments")
            graph_is_v2 = str(registry.graph.get("schema_version") or "") == GRAPH_VERSION_V2
            source_refs = _source_observation_refs(session, active_component_id)
            exact_deterministic_repair = _is_exact_deterministic_repair_delta(
                arguments=arguments,
                repair_task=repair_task,
            )
            if tool_id == "uiux.search_guidance":
                args = _dict(arguments)
                if registry.uiux_mode != "enforce" or not ui_guidance_required:
                    self._invalid_action(
                        session,
                        session_file,
                        message="uiux.search_guidance is only allowed for the active UI partition.",
                    )
                    self._record_observation(
                        session,
                        {"tool_id": tool_id, "status": "rejected", "code": "uiux_not_applicable"},
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"status": "rejected", "rejection": {"code": "uiux_not_applicable"}},
                    )
                    _write_json_atomic(session_file, session)
                    continue
                if not source_refs:
                    self._invalid_action(
                        session,
                        session_file,
                        message="Read the active UI source or CodeGraph path before requesting UI/UX guidance.",
                    )
                    self._record_observation(
                        session,
                        {"tool_id": tool_id, "status": "rejected", "code": "uiux_source_required"},
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"status": "rejected", "rejection": {"code": "uiux_source_required"}},
                    )
                    _write_json_atomic(session_file, session)
                    continue
                selector = str(args.get("domain") or args.get("stack") or "").strip().lower()
                mode = str(args.get("mode") or "").strip().lower()
                guidance_status = _uiux_guidance_status(
                    _guidance_observations(session, active_partition_key),
                    source_inspected=bool(source_refs),
                )
                required_next = _dict(guidance_status.get("required_next_action"))
                required_arguments = _dict(required_next.get("arguments"))
                required_selector = str(
                    required_arguments.get("domain") or required_arguments.get("stack") or ""
                )
                if required_next and (
                    str(required_next.get("action") or "") != tool_id
                    or str(required_arguments.get("mode") or "") != mode
                    or required_selector != selector
                ):
                    expected = json.dumps(required_next, ensure_ascii=True, separators=(",", ":"))
                    self._invalid_action(
                        session,
                        session_file,
                        message=f"UI/UX guidance step mismatch. Required next action: {expected}",
                    )
                    self._record_observation(
                        session,
                        {"tool_id": tool_id, "status": "rejected", "code": "uiux_step_mismatch"},
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"status": "rejected", "rejection": {"code": "uiux_step_mismatch"}},
                    )
                    _write_json_atomic(session_file, session)
                    continue
                if _guidance_attempt_count(session, active_partition_key, mode=mode, selector=selector) >= 2:
                    self._invalid_action(
                        session,
                        session_file,
                        message="UI/UX guidance already used its one narrower retry for this selector.",
                    )
                    self._record_observation(
                        session,
                        {"tool_id": tool_id, "status": "rejected", "code": "uiux_retry_exhausted"},
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"status": "rejected", "rejection": {"code": "uiux_retry_exhausted"}},
                    )
                    _write_json_atomic(session_file, session)
                    continue
            if (
                graph_is_v2
                and tool_id == "design.propose_delta"
                and active_component_id
                and not source_refs
                and not exact_deterministic_repair
            ):
                if observations_since_delta >= observation_limit:
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
                _update_designer_exchange(
                    exchange_path,
                    {"status": "rejected", "rejection": {"code": "design_investigation_missing"}},
                )
                _write_json_atomic(session_file, session)
                continue
            if (
                tool_id == "design.propose_delta"
                and registry.uiux_mode == "enforce"
                and ui_guidance_required
                and not _has_required_ui_guidance(session, active_partition_key)
            ):
                self._invalid_action(
                    session,
                    session_file,
                    message=(
                        "UI delta requires one successful domain=ux guidance and one successful stack=react guidance "
                        "for the active partition."
                    ),
                )
                self._record_observation(
                    session,
                    {"tool_id": tool_id, "status": "rejected", "code": "uiux_guidance_missing"},
                )
                _update_designer_exchange(
                    exchange_path,
                    {"status": "rejected", "rejection": {"code": "uiux_guidance_missing"}},
                )
                _write_json_atomic(session_file, session)
                continue
            if (
                tool_id == "design.propose_delta"
                and registry.uiux_mode == "enforce"
                and ui_guidance_required
                and not _delta_contains_ui_experience(arguments)
            ):
                self._invalid_action(
                    session,
                    session_file,
                    message="UI delta must add or update a ui_experience_contract_v1 using observed guidance refs.",
                )
                self._record_observation(
                    session,
                    {"tool_id": tool_id, "status": "rejected", "code": "uiux_contract_missing"},
                )
                _update_designer_exchange(
                    exchange_path,
                    {"status": "rejected", "rejection": {"code": "uiux_contract_missing"}},
                )
                _write_json_atomic(session_file, session)
                continue
            if graph_is_v2 and tool_id == "design.propose_delta" and active_component_id and source_refs:
                arguments = _attach_source_observations(arguments, component_id=active_component_id, refs=source_refs)
            if (active_partition is not None or repair_task is not None) and observations_since_delta >= observation_limit and tool_id != "design.propose_delta":
                self._invalid_action(
                    session,
                    session_file,
                    message="Designer must propose a delta for the active work item before any more observation or completion action.",
                )
                self._record_observation(
                    session,
                    {
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "status": "rejected",
                        "message": "Observation quota reached. Propose design.propose_delta for the active work item now.",
                        "active_partition": active_partition,
                        "repair_task": repair_task,
                    },
                )
                _update_designer_exchange(
                    exchange_path,
                    {"status": "rejected", "rejection": {"code": "observation_quota_reached"}},
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
                    _update_designer_exchange(exchange_path, {"status": "completed", "validation_findings": []})
                    _write_json_atomic(session_file, session)
                    return
                if any(str(item.get("code") or "") == "human_decision_required" for item in findings):
                    raise HarnessError(
                        "designer_human_required",
                        "Designer found an unresolved Requirement or human-authority decision.",
                        run_id=run_id,
                    )
                self._record_observation(session, {"tool_id": "complete", "status": "rejected", "findings": findings})
                _update_designer_exchange(
                    exchange_path,
                    {"status": "rejected", "validation_findings": findings},
                )
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
                _update_designer_exchange(
                    exchange_path,
                    {"status": "rejected", "rejection": {"code": "repeated_successful_action"}},
                )
                _write_json_atomic(session_file, session)
                continue
            try:
                if tool_id == "design.propose_delta" and repair_task is not None:
                    _validate_repair_delta_scope(
                        arguments=arguments,
                        repair_task=repair_task,
                        graph=registry.graph,
                        run_id=run_id,
                    )
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
                _update_designer_exchange(exchange_path, {"status": "succeeded"})
                if tool_id == "design.propose_delta" and repair_task is not None:
                    source = str(repair_task.get("source") or "")
                    remaining_inline = _composition_repair_findings(
                        registry.graph,
                        requirement_model=requirement_model,
                        landscape=landscape,
                    )
                    target_still_blocked = _repair_target_present(
                        target_id=str(repair_task.get("target_id") or ""),
                        findings=remaining_inline,
                        graph=registry.graph,
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"post_delta_findings": remaining_inline, "target_still_blocked": target_still_blocked},
                    )
                    if source != "inline_composition":
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
                    "designer_evidence_not_found",
                    "designer_delta_invalid",
                    "designer_delta_no_effect",
                    "designer_delta_precondition_failed",
                    "vertical_design_graph_invalid",
                }:
                    invalid_message = _delta_invalid_message(
                        message=exc.message,
                        arguments=arguments,
                        graph=registry.graph,
                    )
                    self._invalid_action(session, session_file, message=invalid_message)
                    self._record_observation(
                        session,
                        {
                            "tool_id": tool_id,
                            "arguments": arguments,
                            "status": "rejected",
                            "error": exc.to_response(),
                        },
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"status": "rejected", "rejection": exc.to_response()},
                    )
                    _write_json_atomic(session_file, session)
                    continue
                raise

    def _step_review_required(
        self,
        *,
        run_id: str,
        call_id: str,
        session: dict[str, object],
        session_file: Path,
        session_dir: Path,
        journal_file: Path,
    ) -> DesignerExecutionError:
        pending = _dict(session.get("pending_step_review"))
        exchange_path = str(pending.get("exchange_path") or "")
        return DesignerExecutionError(
            "designer_step_review_required",
            f"Designer call {call_id} is waiting for operator review at {exchange_path}.",
            run_id=run_id,
            retriable=True,
            session_path=self.store.relative(session_file),
            manifest_path=self.store.relative(session_dir / "designer_loop_manifest_v1.json"),
            journal_path=self.store.relative(journal_file),
            progress={**_progress(session), "pending_step_review": pending},
        )

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
        elif tool_id == "uiux.search_guidance":
            _record_guidance_observation(
                session,
                partition_key=str(session.get("active_partition_key") or ""),
                arguments=_dict(arguments),
                output=_dict(output),
                refs=evidence_refs,
            )
        elif tool_id == "design.propose_delta" and _delta_contains_ui_experience(arguments):
            _compact_guidance_partition(
                session,
                partition_key=str(session.get("active_partition_key") or ""),
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
        _consume_action(session, arguments=arguments)
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
        _consume_action(session, arguments=_dict(record.get("input")))
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
        uiux_mode: str,
        uiux_profile: dict[str, object],
        action_budget_policy: dict[str, object],
    ) -> dict[str, object]:
        authority = {
            "run_id": run_id,
            "requirement_digest": snapshot_binding.get("requirement_digest"),
            "repo_landscape_digest": landscape.get("artifact_digest"),
            "snapshot_binding": snapshot_binding,
            "repair_feedback": repair_feedback,
            "session_ordinal": session_ordinal,
            "designer_contract_version": contract_version,
            "designer_uiux_mode": uiux_mode,
            "designer_uiux_profile": uiux_profile,
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
                *(["uiux.search_guidance"] if uiux_mode == "enforce" else []),
                "design.inspect_partition",
                "design.propose_delta",
                "complete",
            ],
            "action_budget": int(action_budget_policy["calculated_global_limit"]),
            "action_budget_policy": action_budget_policy,
            "invalid_action_budget": INVALID_ACTION_LIMIT,
            "requirement_summary_hash": canonical_json_hash(requirement_model),
            "created_at": utc_now_iso(),
        }

    @staticmethod
    def _record_observation(session: dict[str, object], observation: dict[str, object]) -> None:
        target_id = str(session.get("active_repair_target_id") or "")
        partition_key = str(session.get("active_partition_key") or "")
        component_id = str(session.get("active_component_id") or "")
        recorded = {
            **observation,
            **({"partition_key": partition_key} if partition_key else {}),
            **({"component_id": component_id} if component_id else {}),
            **({"repair_target_id": target_id} if target_id else {}),
        }
        observations = _dicts(session.get("observations"))
        observations.append(recorded)
        session["observations"] = observations[-12:]
        if target_id:
            by_target = _dict(session.get("repair_observations"))
            target_observations = _dicts(by_target.get(target_id))
            target_observations.append(recorded)
            by_target[target_id] = target_observations
            session["repair_observations"] = by_target

    @staticmethod
    def _invalid_action(session: dict[str, object], session_file: Path, *, message: str) -> None:
        budgets = _dict(session.get("budgets"))
        invalid = _dict(budgets.get("invalid_action"))
        invalid["used"] = int(invalid.get("used") or 0) + 1
        budgets["invalid_action"] = invalid
        session["budgets"] = budgets
        target_id = str(session.get("active_invalid_action_scope") or "__construction__")
        by_target = _dict(session.get("invalid_actions_by_target"))
        by_target[target_id] = int(invalid.get("used") or 0)
        session["invalid_actions_by_target"] = by_target
        cursor = _dict(session.get("cursor"))
        cursor["pending_call"] = None
        cursor["next_action_index"] = int(cursor.get("next_action_index") or 1) + 1
        session["cursor"] = cursor
        _consume_action(session)
        session["last_invalid_action"] = message
        _write_json_atomic(session_file, session)


def _activate_repair_target(session: dict[str, object], target_id: str) -> None:
    previous = str(session.get("active_repair_target_id") or "")
    if previous == target_id:
        return
    session["active_repair_target_id"] = target_id
    session["observation_actions_since_delta"] = 0
    if target_id:
        session["active_invalid_action_scope"] = target_id
        used = int(_dict(session.get("invalid_actions_by_target")).get(target_id) or 0)
        budgets = _dict(session.get("budgets"))
        budgets["invalid_action"] = {"limit": INVALID_ACTION_LIMIT, "used": used}
        session["budgets"] = budgets
    session["last_invalid_action"] = ""


def _parse_designer_tool_action(
    raw: str,
    *,
    require_runtime_binding: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        return parse_first_json_object(raw, expected_keys={"action", "arguments"}), {}
    except json.JSONDecodeError as original_error:
        text = raw.strip()
        candidates: dict[str, dict[str, object]] = {}
        positions: dict[str, list[int]] = {}
        for index, token in enumerate(text):
            if token != "}":
                continue
            repaired = f"{text[:index]}{text[index + 1:]}"
            try:
                payload = json.loads(repaired)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or not _designer_action_shape(
                payload,
                require_runtime_binding=require_runtime_binding,
            ):
                continue
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            candidates[canonical] = payload
            positions.setdefault(canonical, []).append(index)
        if len(candidates) != 1:
            raise original_error
        canonical, payload = next(iter(candidates.items()))
        return payload, {
            "schema_version": "designer_output_normalization_v1",
            "kind": "removed_single_extra_closing_brace",
            "candidate_equivalent_positions": positions[canonical],
        }


def _designer_action_shape(payload: dict[str, object], *, require_runtime_binding: bool) -> bool:
    tool_id = str(payload.get("action") or "")
    arguments = payload.get("arguments")
    if not tool_id or not isinstance(arguments, dict):
        return False
    if tool_id != "design.propose_delta":
        return False
    delta = arguments.get("delta")
    if not isinstance(delta, dict) or not {
        "schema_version",
        "base_graph_digest",
        "partition",
        "operations",
        "completion_claims",
        "unresolved_items",
    }.issubset(delta):
        return False
    operations = delta.get("operations")
    if not isinstance(operations, list) or not operations:
        return False
    for operation in operations:
        if not isinstance(operation, dict):
            return False
        if str(operation.get("op") or "") != "add_node":
            continue
        node = operation.get("node")
        if not isinstance(node, dict) or not isinstance(node.get("surface_contract"), dict):
            return False
        surface_contract = node["surface_contract"]
        if "runtime_binding" in surface_contract:
            return False
        if require_runtime_binding:
            runtime_binding = node.get("runtime_binding")
            if not isinstance(runtime_binding, dict):
                return False
            target = runtime_binding.get("target")
            attachment = runtime_binding.get("attachment")
            if (
                not {"schema_version", "runtime_kind", "target", "attachment", "evidence_refs"}.issubset(runtime_binding)
                or not isinstance(target, dict)
                or not {"component_id", "path", "symbol"}.issubset(target)
                or not isinstance(attachment, dict)
                or not {"relation", "owner_ref", "owner_path", "owner_symbol"}.issubset(attachment)
                or not isinstance(runtime_binding.get("evidence_refs"), list)
            ):
                return False
    return True


def _step_review_decision(review: dict[str, object], *, call_id: str) -> str:
    if not bool(review.get("enabled")) or str(review.get("call_id") or "") != call_id:
        return ""
    decision = str(review.get("decision") or "").strip().casefold()
    return decision if decision in {"approve", "reject"} else ""


def _invalid_action_exhausted(session: dict[str, object]) -> bool:
    invalid = _dict(_dict(session.get("budgets")).get("invalid_action"))
    return int(invalid.get("used") or 0) > int(invalid.get("limit") or 0)


def _prompt_observations(
    session: dict[str, object],
    *,
    repair_task: dict[str, object] | None,
    active_component_id: str = "",
) -> list[dict[str, object]]:
    all_observations = _dicts(session.get("observations"))
    if repair_task is None:
        partition_key = str(session.get("active_partition_key") or "")
        selected = [
            item
            for item in all_observations
            if (not partition_key or str(item.get("partition_key") or "") == partition_key)
            and str(item.get("tool_id") or "") != "uiux.search_guidance"
            and str(item.get("status") or "") != "operator_rejected"
        ][-8:]
    else:
        target_id = str(repair_task.get("target_id") or "")
        selected = _dicts(_dict(session.get("repair_observations")).get(target_id))
    source_refs = set(
        _strings(_dict(session.get("source_observations")).get(active_component_id))
    )
    pinned = [
        item
        for item in all_observations
        if source_refs.intersection(_strings(item.get("evidence_refs")))
        and str(item.get("status") or "") != "operator_rejected"
    ]
    merged: list[dict[str, object]] = []
    fingerprints: set[str] = set()
    for item in [*pinned, *selected]:
        fingerprint = canonical_json_hash(item)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        merged.append(item)
    return merged


def _guidance_partition_key(
    *,
    active_partition: dict[str, object] | None,
    repair_task: dict[str, object] | None,
) -> str:
    partition = _dict((repair_task or {}).get("partition")) or _dict((active_partition or {}).get("partition"))
    scenario_id = str(partition.get("scenario_id") or "")
    component_id = str(partition.get("component_id") or "")
    return f"{scenario_id}|{component_id}" if scenario_id and component_id else ""


def _ui_guidance_required(
    *,
    active_partition: dict[str, object] | None,
    repair_task: dict[str, object] | None,
    graph: dict[str, object],
) -> bool:
    if str(_dict(graph.get("snapshot_binding")).get("designer_uiux_mode") or "off") != "enforce":
        return False
    if active_partition is not None:
        return "ui" in _strings(active_partition.get("missing_surfaces")) or any(
            str(item.get("surface") or "") == "ui"
            for item in _dicts(active_partition.get("surface_work"))
        )
    codes = {
        str(item.get("code") or item.get("category") or "")
        for item in _dicts((repair_task or {}).get("findings"))
    }
    ui_repair = any(
        code.startswith("ui_")
        or code in {"surface_contract_missing", "accessibility", "interaction_feedback", "responsive_attachment"}
        for code in codes
    )
    return ui_repair and any(
        str(item.get("surface") or "") == "ui" for item in _dicts((repair_task or {}).get("nodes"))
    )


def _record_guidance_observation(
    session: dict[str, object],
    *,
    partition_key: str,
    arguments: dict[str, object],
    output: dict[str, object],
    refs: list[str],
) -> None:
    if not partition_key:
        return
    by_partition = _dict(session.get("guidance_observations"))
    observations = _dicts(by_partition.get(partition_key))
    observations.append(
        {
            "mode": str(arguments.get("mode") or ""),
            "selector": str(arguments.get("domain") or arguments.get("stack") or ""),
            "query": str(arguments.get("query") or ""),
            "count": int(output.get("count") or 0),
            "no_match": bool(output.get("no_match")),
            "results": _dicts(output.get("results")),
            "evidence_refs": list(refs),
        }
    )
    by_partition[partition_key] = observations[-4:]
    session["guidance_observations"] = by_partition


def _guidance_observations(session: dict[str, object], partition_key: str) -> list[dict[str, object]]:
    return _dicts(_dict(session.get("guidance_observations")).get(partition_key)) if partition_key else []


def _compact_guidance_partition(session: dict[str, object], *, partition_key: str) -> None:
    if not partition_key:
        return
    by_partition = _dict(session.get("guidance_observations"))
    compacted: list[dict[str, object]] = []
    for item in _dicts(by_partition.get(partition_key)):
        results = _dicts(item.get("results"))
        compacted.append(
            {
                key: value
                for key, value in {
                    **item,
                    "results_digest": canonical_json_hash(results),
                }.items()
                if key != "results"
            }
        )
    by_partition[partition_key] = compacted
    session["guidance_observations"] = by_partition


def _guidance_attempt_count(
    session: dict[str, object],
    partition_key: str,
    *,
    mode: str,
    selector: str,
) -> int:
    return sum(
        1
        for item in _guidance_observations(session, partition_key)
        if str(item.get("mode") or "") == mode and str(item.get("selector") or "") == selector
    )


def _uiux_guidance_status(
    active_guidance: list[dict[str, object]],
    *,
    source_inspected: bool,
) -> dict[str, object]:
    def lane(mode: str, selector: str) -> dict[str, object]:
        attempts = [
            item
            for item in active_guidance
            if str(item.get("mode") or "") == mode and str(item.get("selector") or "") == selector
        ]
        return {
            "mode": mode,
            "selector": selector,
            "attempts": len(attempts),
            "max_attempts": 2,
            "retry_remaining": max(0, 2 - len(attempts)),
            "latest_result_count": int(attempts[-1].get("count") or 0) if attempts else 0,
            "has_evidence": any(_strings(item.get("evidence_refs")) for item in attempts),
        }

    domain = lane("domain", "ux")
    stack = lane("stack", "react")
    required_next: dict[str, object] | None = None
    allowed_next: list[dict[str, object]] = []
    blocked_reason = ""
    if not source_inspected:
        allowed_next = [{"action": "repo.read"}, {"action": "codegraph.query"}]
    elif not bool(domain["has_evidence"]):
        if int(domain["retry_remaining"]) > 0:
            required_next = {
                "action": "uiux.search_guidance",
                "arguments": {"mode": "domain", "domain": "ux"},
            }
        else:
            blocked_reason = "domain=ux guidance produced no usable evidence after two attempts"
    elif int(domain["attempts"]) == 1 and int(stack["attempts"]) == 0:
        allowed_next = [
            {"action": "uiux.search_guidance", "arguments": {"mode": "domain", "domain": "ux"}},
            {"action": "uiux.search_guidance", "arguments": {"mode": "stack", "stack": "react"}},
        ]
    elif not bool(stack["has_evidence"]):
        if int(stack["retry_remaining"]) > 0:
            required_next = {
                "action": "uiux.search_guidance",
                "arguments": {"mode": "stack", "stack": "react"},
            }
        else:
            blocked_reason = "stack=react guidance produced no usable evidence after two attempts"
    elif int(stack["attempts"]) == 1:
        allowed_next = [
            {"action": "uiux.search_guidance", "arguments": {"mode": "stack", "stack": "react"}},
            {"action": "design.propose_delta"},
        ]
    else:
        required_next = {"action": "design.propose_delta", "arguments": {}}
    return {
        "source_inspected": source_inspected,
        "domain": domain,
        "stack": stack,
        "required_next_action": required_next,
        "allowed_next_actions": allowed_next,
        "retry_policy": "Retry the same selector once only when its result is empty or off-topic.",
        "blocked_reason": blocked_reason,
    }


def _has_required_ui_guidance(session: dict[str, object], partition_key: str) -> bool:
    successful = [
        item
        for item in _guidance_observations(session, partition_key)
        if _strings(item.get("evidence_refs"))
    ]
    return any(
        str(item.get("mode") or "") == "domain" and str(item.get("selector") or "") == "ux"
        for item in successful
    ) and any(
        str(item.get("mode") or "") == "stack" and str(item.get("selector") or "") == "react"
        for item in successful
    )


def _delta_contains_ui_experience(arguments: object) -> bool:
    delta = _dict(_dict(arguments).get("delta"))
    for operation in _dicts(delta.get("operations")):
        if str(operation.get("op") or "") == "add_node":
            node = _dict(operation.get("node"))
            if str(node.get("surface") or "") != "ui":
                continue
            experience = _dict(_dict(node.get("surface_contract")).get("experience_contract"))
            if experience.get("schema_version") == UIUX_EXPERIENCE_CONTRACT_VERSION:
                return True
        if str(operation.get("op") or "") == "update_node":
            experience = _dict(_dict(_dict(operation.get("changes")).get("surface_contract")).get("experience_contract"))
            if experience.get("schema_version") == UIUX_EXPERIENCE_CONTRACT_VERSION:
                return True
    return False


def _active_requirement_slice(
    *,
    requirement_model: dict[str, object],
    active_partition: dict[str, object] | None,
) -> dict[str, object]:
    if active_partition is None:
        return {}
    scenario_id = str(_dict(active_partition.get("partition")).get("scenario_id") or "")
    scenario = next(
        (item for item in _scenarios(requirement_model) if str(item.get("id") or "") == scenario_id),
        None,
    )
    if scenario is None:
        return {}

    surface_work = _dicts(active_partition.get("surface_work"))
    allowed_refs = list(
        dict.fromkeys(
            ref
            for work in surface_work
            for ref in _strings(work.get("requirement_refs"))
        )
    )
    requirement_items: list[dict[str, object]] = []
    collections: list[object] = [
        requirement_model.get("preconditions"),
        requirement_model.get("postconditions"),
        requirement_model.get("business_rules"),
        requirement_model.get("acceptance_criteria"),
        requirement_model.get("quality_requirements"),
        requirement_model.get("assumptions"),
        requirement_model.get("decisions"),
    ]
    collections.extend(_dict(requirement_model.get("flows")).values())
    for item in (row for collection in collections for row in _dicts(collection)):
        if str(item.get("id") or "") in allowed_refs:
            requirement_items.append(item)

    return {
        "use_case": requirement_model.get("use_case"),
        "scenario": {
            key: scenario.get(key)
            for key in ("id", "kind", "name", "actor_refs")
            if scenario.get(key) is not None
        },
        "surfaces": [
            {
                "surface": work.get("surface"),
                "requirement_refs": _strings(work.get("requirement_refs")),
                "contract": work.get("contract"),
            }
            for work in surface_work
        ],
        "allowed_requirement_refs": allowed_refs,
        "requirements": requirement_items,
    }


def _requirement_context_index(requirement_model: dict[str, object]) -> dict[str, object]:
    use_case = _dict(requirement_model.get("use_case"))
    return {
        "schema_version": requirement_model.get("schema_version"),
        "revision": requirement_model.get("revision"),
        "run_id": requirement_model.get("run_id"),
        "use_case": {key: use_case.get(key) for key in ("id", "name") if use_case.get(key) is not None},
        "scenario_index": [
            {
                "id": scenario.get("id"),
                "kind": scenario.get("kind"),
                "required_surfaces": sorted(_required_surface_set(scenario)),
            }
            for scenario in _scenarios(requirement_model)
        ],
    }


def _prompt_review_feedback(
    feedback: str,
    *,
    requirement_model: dict[str, object],
    active_partition: dict[str, object] | None,
) -> str:
    if not feedback or active_partition is None:
        return feedback
    active_scenario_id = str(_dict(active_partition.get("partition")).get("scenario_id") or "")
    sibling_ids = {
        str(scenario.get("id") or "")
        for scenario in _scenarios(requirement_model)
        if str(scenario.get("id") or "") != active_scenario_id
    }
    if any(sibling_id and sibling_id in feedback for sibling_id in sibling_ids):
        return (
            f"The operator rejected the previous action because it mixed sibling-scenario behavior into "
            f"{active_scenario_id}. Regenerate from active_requirement_slice only; scenario_index is navigation-only."
        )
    return feedback


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
    observation_limit: int = OBSERVATION_ACTION_LIMIT,
    last_review_feedback: str = "",
    uiux_mode: str = "off",
    active_guidance: list[dict[str, object]] | None = None,
    source_inspected: bool = False,
) -> str:
    graph_contract = str(graph.get("schema_version") or GRAPH_VERSION)
    delta_contract = delta_version_for(graph_contract)
    repair_partition = _dict((repair_task or {}).get("partition"))
    repair_scenario_id = str(repair_partition.get("scenario_id") or "")
    prompt_graph = (
        {
            "schema_version": graph.get("schema_version"),
            "revision": graph.get("revision"),
            "artifact_digest": graph.get("artifact_digest"),
            "summary": {
                "partitions": len(_dicts(graph.get("partitions"))),
                "nodes": len(_dicts(graph.get("nodes"))),
                "edges": len(_dicts(graph.get("edges"))),
            },
        }
        if repair_task is not None
        else graph
    )
    scenario_matches = _dicts(landscape.get("scenario_matches"))
    if repair_scenario_id:
        scenario_matches = [
            item for item in scenario_matches if str(item.get("scenario_id") or "") == repair_scenario_id
        ]
    active_requirement_slice = _active_requirement_slice(
        requirement_model=requirement_model,
        active_partition=active_partition,
    )
    prompt_review_feedback = _prompt_review_feedback(
        last_review_feedback,
        requirement_model=requirement_model,
        active_partition=active_partition,
    )
    payload = {
        "graph_contract": graph_contract,
        "delta_contract": delta_contract,
        "landscape_summary": landscape.get("summary"),
        "components": landscape.get("components"),
        "scenario_matches": scenario_matches,
        "graph": prompt_graph,
        "active_partition": active_partition,
        "active_repair_task": repair_task,
        "observation_policy": {
            "limit_before_delta": observation_limit,
            "used_since_delta": observations_since_delta,
            "must_propose_delta_now": (active_partition is not None or repair_task is not None) and observations_since_delta >= observation_limit,
        },
        "recent_observations": observations,
        "active_uiux_guidance": active_guidance or [],
        "uiux_mode": uiux_mode,
        "repair_feedback": repair_feedback,
        "last_invalid_action": last_invalid_action,
        "last_review_feedback": prompt_review_feedback,
    }
    if active_requirement_slice:
        payload["requirement_context"] = _requirement_context_index(requirement_model)
        payload["active_requirement_slice"] = active_requirement_slice
    else:
        payload["requirement"] = requirement_model
    correction = (
        "Your previous action was rejected. Fix this exact error before doing anything else: "
        f"{last_invalid_action} Do not resend the rejected delta unchanged.\n\n"
        if last_invalid_action
        else ""
    )
    review_correction = (
        "The operator rejected your previous proposed action. Address this review before choosing the next action: "
        f"{prompt_review_feedback}\n\n"
        if prompt_review_feedback
        else ""
    )
    ui_guidance_required = _ui_guidance_required(
        active_partition=active_partition,
        repair_task=repair_task,
        graph=graph,
    )
    uiux_status = (
        _uiux_guidance_status(active_guidance or [], source_inspected=source_inspected)
        if uiux_mode == "enforce" and ui_guidance_required
        else {}
    )
    payload["uiux_guidance_status"] = uiux_status
    uiux_status_instruction = (
        "CURRENT UI/UX TOOL STATE (authoritative; apply before all general instructions):\n"
        f"{json.dumps(uiux_status, ensure_ascii=True, separators=(',', ':'))}\n"
        "When required_next_action is present, match its action, mode, and fixed selector exactly; choose only the "
        "query text yourself. When it is null, choose only from allowed_next_actions. A domain query asks for the "
        "observable UX outcome; a stack query asks how React implements that outcome.\n\n"
        if uiux_status
        else ""
    )
    uiux_instruction = (
        "For this active UI work item, first inspect source, then call uiux.search_guidance twice: "
        "one mode=domain, domain=ux query and one mode=stack, stack=react query. Each query must express one dominant "
        "intent in 2-5 meaningful generic ASCII terms without repository paths, symbols, or project-private text. "
        "Retry the same selector at most once, and only when its previous result is empty or off-topic; otherwise advance "
        f"to the next mode shown in CURRENT UI/UX TOOL STATE. Every UI node must include {UIUX_EXPERIENCE_CONTRACT_VERSION} under "
        "surface_contract.experience_contract with component_structure, feedback[{trigger,response}], accessibility, "
        "responsive, focus_flow, and the successful uiux guidance_refs. Guidance informs UX only; Requirement and "
        "repository source remain authoritative. When success removes the initiating control or selected row, focus_flow "
        "must not restore focus to that removed element; move focus to the status region or the next surviving control. "
        "If the active scenario itself owns a cancel/close path and the trigger still exists, return focus to that trigger. "
        "Never add cancel/close behavior from another scenario; keep each alternative or exception in its own projection. "
        if uiux_mode == "enforce" and ui_guidance_required
        else ""
    )
    construction_instruction = (
        "Use active_partition only to build missing surfaces. active_requirement_slice is the authoritative behavior "
        "source for this construction step: contracts, responses, outcomes, and edges may only implement IDs listed "
        "in allowed_requirement_refs. requirement_context.scenario_index is navigation-only; never copy behavior from "
        "a sibling scenario into the active projection. A runtime target may be shared by later projections, but this "
        "projection must declare only the active scenario's behavior. delta_scaffold is the minimum complete operation "
        "plan, not a node-only example. When active_partition.path_insertion.required is true, preserve every supplied "
        "remove_edge and copied re-add operation, fill the two inserted edge contracts, and keep the terminal edge last; "
        "never submit the new surface as a disconnected node. For an alternative or exception scenario, inspect "
        "surface_work.planned_reuse_candidates before inventing a new runtime target. Graph projection identity and "
        "runtime target identity are separate: when the branch starts from or returns to the same interaction owner or "
        "entrypoint as a candidate (for example confirm and cancel in one dialog), add the required branch projection "
        "node and reuse that candidate's exact runtime target even when its contract only describes the main outcome. "
        "Write the branch projection's contract only from active_requirement_slice; sharing a runtime target never "
        "copies sibling requirement refs or sibling behavior into the branch contract. Only create a new runtime target "
        "when the branch needs an independently mounted owner/entrypoint or the candidate architecture cannot host it, "
        "and state that concrete reason in evidence_reason. "
        "For an alternative or exception scenario, add the depends_on edge only when "
        "active_partition.branch_parent_requirement supplies it; null means the branch already has its one canonical "
        "parent. When supplied, connect that branch entry node to its parent main entry node (source branch depends on "
        "target main, class-only), set edge.scenario_id to the branch scenario, and keep the branch interactions in "
        "their own scenario. Never create a cross-scenario edge between two main scenarios; a standalone quality "
        "scenario remains separate. Never inline every branch as unconditional calls in the main success sequence. "
        if active_partition is not None
        else ""
    )
    repair_instruction = (
        "Use active_repair_task for every composition or validator repair. The active_repair_task write_scope is a "
        "strict mutation boundary, but its read_scope is not: use landscape, repo, CodeGraph, and "
        "design.inspect_partition to inspect any related profile component needed to understand the path. If "
        "active_repair_task.write_scope.allow_add_node is false, never use add_node; update a listed writable_node or "
        "repair its edges instead. Never update or remove a read_only_node, and preserve every scenario/partition "
        "outside write_scope. active_repair_task.graph_context.preserved_edges are structural cross-scenario edges: "
        "never remove or re-add them; only edges_in_current_order participate in a repair reorder. Do not rewrite an "
        "entire scenario edge chain when one named edge or node can satisfy the finding. When every active repair "
        "finding is boundary_contract_mismatch, submit exactly the delta_scaffold operations: keep each replacement "
        "edge's from, to, kind, and label unchanged, and do not add or reorder unrelated edges. When every active "
        "repair finding is runtime_operation_binding_mismatch, submit exactly the delta_scaffold update_node operation "
        "and preserve every runtime_binding field except the corrected target symbol. Use its exact partition identity "
        "and preserve required_coverage. "
        "When every active repair finding is runtime_boundary_unreachable, submit exactly the delta_scaffold removals; "
        "do not invent an API or try to mutate a node's component identity. The next construction partition will place "
        "the still-required surface in a runtime-feasible component. "
        "For other repairs, preserve required_coverage and add or update a node for every listed surface. In repair mode, assign every edge "
        "to the scenario named by the finding, even when an endpoint is reused from another scenario partition. Edge "
        "order is assigned from add_edge operation order. To change an existing scenario order, remove every "
        "obsolete/current edge first using exactly {\"op\":\"remove_edge\",\"edge_id\":\"VDE-*\"}; never put an edge "
        "object inside remove_edge. Then add the corrected edges in exact behavioral order. Use the IDs in "
        "active_repair_task.edges_in_current_order. "
        if repair_task is not None
        else ""
    )
    uiux_tool_instruction = (
        "uiux.search_guidance(query,mode=domain,domain=ux|icons|react) or "
        "uiux.search_guidance(query,mode=stack,stack=react); "
        if uiux_mode == "enforce" and ui_guidance_required
        else ""
    )
    delta_scaffold_instruction = (
        "Start from delta_scaffold and replace every angle-bracket placeholder.\n\n"
        if active_partition is not None or repair_task is not None
        else ""
    )
    return (
        uiux_status_instruction
        + correction
        + review_correction
        + uiux_instruction
        + "Investigate frozen AS-IS evidence, identify GAP, and propose TO-BE one tool action at a time. "
        "Use only the listed tools. Existing behavior must use reuse/modify/no_change with RLA evidence; "
        "new behavior uses add with an evidence_reason. Every node needs requirement_refs, a concise contract, and a "
        f"{SURFACE_CONTRACT_VERSION} surface_contract matching its surface. "
        "Candidate anchors are AS-IS evidence, not automatic TO-BE targets. For disposition=add, choose a concrete new "
        "profile-owned target path/symbol for the required behavior and attach it to an inspected existing composition "
        "owner; never bind new domain behavior to an unrelated health, smoke, demo, or test symbol. The add target must "
        "not be identical to its owner or to an existing anchor. Use modify/reuse when the target itself already exists. "
        "repo.read only accepts existing AS-IS files. When an add target does not exist yet, read its existing composition "
        "owner or use an available CodeGraph call path instead of trying to read the TO-BE target. "
        "Never repeat an identical successful tool action. If a search returns zero matches, either broaden it once "
        "or model the required surface as add using a component/root from the frozen landscape. "
        + construction_instruction
        + repair_instruction
        + "For Graph V2, candidate anchors locate source but do not satisfy investigation: use repo.read or an available "
        f"codegraph.query for the active component before its first delta. Make at most {observation_limit} distinct observation actions, "
        "then you must call design.propose_delta before investigating another partition. If must_propose_delta_now is true, "
        "the only valid next action is design.propose_delta. Call complete only when active_partition and active_repair_task are null.\n\n"
        "Tools: landscape.search(query?,scenario_id?,surface?,component_id?,kind?); "
        "landscape.get_anchor(anchor_id); repo.search(query,component_id?); "
        "repo.read(path,start_line?,end_line?); codegraph.query(operation=explore|symbols|callers|callees|impact|files,query?,symbol?,kind?,limit?,depth?,filter?,pattern?); "
        + uiux_tool_instruction
        + "design.inspect_partition(scenario_id,component_id?); "
        f"design.propose_delta(delta); complete(). The delta requires schema_version={delta_contract}, "
        "base_graph_digest, partition{use_case_id,scenario_id,component_id}, operations, "
        "completion_claims, and unresolved_items. completion_claims must be objects shaped exactly like "
        "{\"scenario_id\":\"SC-1\",\"surfaces\":[\"ui\",\"api\"]}; allowed surface values are ui, api, "
        "business_rule, data, and interaction. The completion_claims array must never contain bare strings. "
        "Supported operations are set_partition_coverage, "
        "add_node, update_node, remove_node, add_edge, and remove_edge. An add_node contains "
        "temp_ref, semantic_key, name, kind, surface, disposition, requirement_refs, evidence_refs, "
        "evidence_reason, contract, surface_contract, and for Graph V2 runtime_binding. node.evidence_refs accepts only "
        "frozen repository evidence returned by Designer tools (RLA-* or repo:*); never put Requirement provenance E-*, "
        "Requirement IDs, or uiux:* refs there. Reuse/modify/no_change need an observed repository evidence ref. For "
        "disposition=add, leave node.evidence_refs empty, explain the GAP in evidence_reason, and keep inspected "
        "composition-owner or entrypoint anchors under runtime_binding.evidence_refs. For a mutation, the complete outer envelope must be "
        f"{{\"action\":\"design.propose_delta\",\"arguments\":{{\"delta\":{{\"schema_version\":\"{delta_contract}\",...}}}}}}; "
        f"never use {graph_contract} as the delta schema and never put delta fields directly under arguments. "
        "set_partition_coverage must be exactly {\"op\":\"set_partition_coverage\",\"coverage\":{...}}. "
        "add_node must be exactly {\"op\":\"add_node\",\"node\":{...}}. temp_ref must start with 'tmp:'. "
        "Node kind is the graph kind supplied by node_scaffolds, not a landscape anchor kind. Node contract must be "
        "one non-empty string, never an object. surface_contract must keep schema_version exactly "
        "design_surface_contract_v1 and keep surface unchanged. UI requires entrypoint, states, "
        "transitions[{from,action,to,outcome}], validations "
        "(an array of non-empty strings, never objects; it may be empty) and "
        "outcomes. API requires operation_id, method, path, request_fields[{name,type,location}], responses[{status,outcome}], "
        "authorization and boolean authorization_required. business_rule requires inputs (an array of non-empty strings, never objects), decision and "
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
        "authorization, success response, and 4xx/5xx response. This full provider mapping is required on both calls "
        "and returns edges when the provider is an API; a returns edge must not map only the response. "
        "For service and persistence_adapter providers, when boundary_contract.provider_operation is a concrete "
        "identifier, runtime_binding.target.symbol must expose that exact callable. A file may host multiple symbols; "
        "never bind a query operation to a create/update/delete callable. "
        "A boundary_contract describes data mapping but does not make a direct cross-application call executable. "
        "Code in one apps/* application may call a non-API provider in another apps/* application only through an "
        "explicit approved transport surface. Business rules do not imply backend placement: when UI is required and "
        "API/data are not applicable, keep the rule in the UI application or an importable shared package. Never add a "
        "server endpoint merely to repair a scenario whose approved API surface is not_applicable. "
        "Sequence projections are linear within each scenario: only calls/reads/writes/returns become messages. Every "
        "scenario with a UI surface needs actual behavior beyond depends_on, exactly one terminal edge at the "
        "final observable outcome, and a continuous ordered message path. If the requirement requires human confirmation "
        "before persistence, represent that confirmation in the UI surface contract before the outbound mutation call or "
        "in the interaction path before any writes edge. Do not invent a preliminary backend verification round trip unless "
        "the approved requirement explicitly requires it. "
        + delta_scaffold_instruction
        + "Action JSON: {\"action\":\"tool.id\",\"arguments\":{...}}\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _surface_contract_scaffold(surface: str, *, uiux_mode: str = "off") -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": SURFACE_CONTRACT_VERSION,
        "surface": surface,
    }
    if surface == "ui":
        contract: dict[str, object] = {
            **common,
            "entrypoint": "<route, screen, or component>",
            "states": ["<initial state>", "<observable final state>"],
            "transitions": [
                {"from": "<state>", "action": "<user action>", "to": "<state>", "outcome": "<observable outcome>"}
            ],
            "validations": ["<validation rule, or remove this item when none>"],
            "outcomes": ["<observable outcome>"],
        }
        if uiux_mode == "enforce":
            contract["experience_contract"] = {
                "schema_version": UIUX_EXPERIENCE_CONTRACT_VERSION,
                "component_structure": ["<component and responsibility>"],
                "feedback": [{"trigger": "<interaction or result>", "response": "<visible feedback>"}],
                "accessibility": ["<keyboard and semantic accessibility behavior>"],
                "responsive": ["<small and large viewport behavior>"],
                "focus_flow": ["<focus entry, movement, and restoration>"],
                "guidance_refs": ["<successful uiux:sha256 evidence refs>"],
            }
        return contract
    if surface == "api":
        return {
            **common,
            "operation_id": "<stable operation id>",
            "method": "<GET|POST|PUT|PATCH|DELETE>",
            "path": "/<resource path>",
            "request_fields": [{"name": "<field>", "type": "<type>", "location": "<body|path|query>"}],
            "responses": [{"status": "<HTTP status>", "outcome": "<observable outcome>"}],
            "authorization": "<none, authenticated, role-based, or not specified with rationale>",
            "authorization_required": False,
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


def _fake_surface_contract(
    *,
    surface: str,
    scenario_id: str,
    name: str,
    uiux_mode: str = "off",
    guidance_refs: list[str] | None = None,
) -> dict[str, object]:
    common: dict[str, object] = {"schema_version": SURFACE_CONTRACT_VERSION, "surface": surface}
    label = name or f"{scenario_id} {surface}"
    if surface == "ui":
        contract: dict[str, object] = {
            **common,
            "entrypoint": label,
            "states": ["ready", "completed"],
            "transitions": [{"from": "ready", "action": "submit", "to": "completed", "outcome": f"{scenario_id} outcome"}],
            "validations": [],
            "outcomes": [f"{scenario_id} outcome"],
        }
        if uiux_mode == "enforce":
            contract["experience_contract"] = {
                "schema_version": UIUX_EXPERIENCE_CONTRACT_VERSION,
                "component_structure": [f"{label} owns the approved interaction and observable states."],
                "feedback": [
                    {"trigger": "successful action", "response": "Show a concise success message."},
                    {"trigger": "failed action", "response": "Show an actionable error without losing user context."},
                ],
                "accessibility": ["Support keyboard operation and semantic labels."],
                "responsive": ["Preserve the interaction without horizontal overflow on small viewports."],
                "focus_flow": ["Move focus into confirmation UI and restore it to the trigger on close."],
                "guidance_refs": list(guidance_refs or []),
            }
        return contract
    if surface == "api":
        return {
            **common,
            "operation_id": f"handle{scenario_id.replace('-', '')}",
            "method": "POST",
            "path": f"/{scenario_id.casefold()}",
            "request_fields": [],
            "responses": [{"status": "200", "outcome": f"{scenario_id} outcome"}],
            "authorization": "not specified by the approved requirement",
            "authorization_required": False,
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
    authorization_required = provider.get("authorization_required") is True
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
            if not authorization_required
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


def _data_path_insertion_scaffold(
    *,
    scenario_id: str,
    grouped_surfaces: list[str],
    graph: dict[str, object],
    node_scaffolds: list[dict[str, object]],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    if "data" not in grouped_surfaces:
        return None, []
    scenario_nodes = [
        item
        for item in _dicts(graph.get("nodes"))
        if str(item.get("scenario_id") or "") == scenario_id and str(item.get("id") or "")
    ]
    predecessor = next(
        (item for item in scenario_nodes if str(item.get("surface") or "") == "business_rule"),
        None,
    )
    if predecessor is None:
        return None, []
    scenario_edges = sorted(
        [
            item
            for item in _dicts(graph.get("edges"))
            if str(item.get("scenario_id") or "") == scenario_id
            and str(item.get("kind") or "") in {"calls", "reads", "writes", "returns"}
        ],
        key=lambda item: int(item.get("order") or 0),
    )
    incoming = [item for item in scenario_edges if str(item.get("to") or "") == str(predecessor.get("id") or "")]
    if not incoming:
        return None, []
    insertion_order = max(int(item.get("order") or 0) for item in incoming)
    downstream = [item for item in scenario_edges if int(item.get("order") or 0) > insertion_order]
    if not downstream:
        return None, []
    data_node = next(
        (
            _dict(item.get("node"))
            for item in node_scaffolds
            if str(_dict(item.get("node")).get("surface") or "") == "data"
        ),
        {},
    )
    data_ref = str(data_node.get("temp_ref") or f"tmp:{scenario_id}:data")
    data_operation = str(
        _dict(_dict(data_node.get("runtime_binding")).get("target")).get("symbol")
        or "<exact data operation target symbol>"
    )
    predecessor_operation = str(
        _dict(_dict(predecessor.get("runtime_binding")).get("target")).get("symbol")
        or "<specific business-rule operation>"
    )
    boundary = {
        "schema_version": BOUNDARY_CONTRACT_VERSION,
        "consumer_operation": predecessor_operation,
        "provider_operation": data_operation,
        "input_mapping": [
            {"from": "<consumer identifier field>", "to": "<data identifier field>", "required": True}
        ],
        "auth_mapping": [],
        "response_mapping": [
            {"from": "<data deletion result>", "to": "<business-rule outcome>", "required": True}
        ],
        "error_mapping": [],
    }
    operations: list[dict[str, object]] = [
        {"op": "remove_edge", "edge_id": str(edge.get("id") or "")}
        for edge in downstream
    ]
    operations.extend(
        [
            {
                "op": "add_edge",
                "edge": {
                    "from": str(predecessor.get("id") or ""),
                    "to": data_ref,
                    "kind": "writes",
                    "label": "<specific confirmed data mutation>",
                    "scenario_id": scenario_id,
                    "terminal": False,
                    "boundary_contract": boundary,
                },
            },
            {
                "op": "add_edge",
                "edge": {
                    "from": data_ref,
                    "to": str(predecessor.get("id") or ""),
                    "kind": "returns",
                    "label": "<specific persistence result>",
                    "scenario_id": scenario_id,
                    "terminal": False,
                    "boundary_contract": boundary,
                },
            },
        ]
    )
    operations.extend(
        {
            "op": "add_edge",
            "edge": {key: value for key, value in edge.items() if key not in {"id", "order"}},
        }
        for edge in downstream
    )
    return (
        {
            "required": True,
            "insert_after_node_id": predecessor.get("id"),
            "insert_after_surface": predecessor.get("surface"),
            "new_surface_ref": data_ref,
            "downstream_edge_ids_in_order": [str(edge.get("id") or "") for edge in downstream],
            "rule": (
                "Remove the listed downstream edges, insert the data write and return, then re-add every downstream "
                "edge in the supplied order so the observable terminal remains last."
            ),
        },
        operations,
    )


def _reconcile_stale_placeholder_partitions(
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    graph: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    """Drop empty placeholders left behind by an older surface-routing policy."""

    anchors = {
        str(item.get("anchor_id") or ""): item
        for item in _dicts(landscape.get("anchors"))
        if str(item.get("anchor_id") or "")
    }
    matches = {
        (str(item.get("scenario_id") or ""), str(item.get("surface") or "")): _strings(
            item.get("candidate_anchor_refs")
        )
        for item in _dicts(landscape.get("scenario_matches"))
    }
    required_by_scenario = {
        str(scenario.get("id") or ""): _required_surface_set(scenario)
        for scenario in _scenarios(requirement_model)
        if str(scenario.get("id") or "")
    }
    nodes = _dicts(graph.get("nodes"))
    retained: list[dict[str, object]] = []
    removed: list[str] = []
    changed = False
    for partition in _dicts(graph.get("partitions")):
        scenario_id = str(partition.get("scenario_id") or "")
        component_id = str(partition.get("component_id") or "")
        coverage = dict(_dict(partition.get("coverage")))
        materialized = (
            bool(_strings(partition.get("source_observation_refs")))
            or any(str(value or "") in {"covered", "no_change"} for value in coverage.values())
            or any(
                str(node.get("scenario_id") or "") == scenario_id
                and str(node.get("component_id") or "") == component_id
                for node in nodes
            )
        )
        if materialized:
            retained.append(partition)
            continue
        required_surfaces = required_by_scenario.get(scenario_id, set())
        remaining_coverage = {
            surface: status
            for surface, status in coverage.items()
            if surface in required_surfaces
            and _component_for_missing_surface(
                scenario_id=scenario_id,
                surface=surface,
                required_surfaces=required_surfaces,
                matches=matches,
                anchors=anchors,
                landscape=landscape,
            )
            == component_id
        }
        if remaining_coverage:
            if remaining_coverage != coverage:
                partition = {**partition, "coverage": remaining_coverage}
                changed = True
            retained.append(partition)
            continue
        removed.append(f"{scenario_id}|{component_id}")
        changed = True

    if not changed:
        return graph, []
    reconciled = json.loads(json.dumps(graph))
    reconciled["partitions"] = retained
    reconciled["revision"] = int(reconciled.get("revision") or 0) + 1
    reconciled["artifact_digest"] = graph_digest(reconciled)
    return reconciled, removed


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
            required_surfaces=required,
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
                required_surfaces=required,
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
            requirement_refs = list(dict.fromkeys(_strings(surface_contract.get("requirement_refs")))) or [scenario_id]
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
                    "planned_reuse_candidates": _planned_reuse_candidates(
                        scenario=scenario,
                        requirement_model=requirement_model,
                        graph=graph,
                        component_id=component_id,
                        surface=surface,
                    ),
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
                    "surface_contract": _surface_contract_scaffold(
                        str(item["surface"]),
                        uiux_mode=str(_dict(graph.get("snapshot_binding")).get("designer_uiux_mode") or "off"),
                    ),
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
        branch_parent = _branch_parent_scaffold(
            scenario=scenario,
            requirement_model=requirement_model,
            graph=graph,
            branch_entry_ref=f"tmp:{scenario_id}:{grouped_surfaces[0]}",
        )
        operations = [
            {"op": "set_partition_coverage", "coverage": required_coverage},
            *node_scaffolds,
        ]
        path_insertion, path_operations = _data_path_insertion_scaffold(
            scenario_id=scenario_id,
            grouped_surfaces=grouped_surfaces,
            graph=graph,
            node_scaffolds=node_scaffolds,
        )
        operations.extend(path_operations)
        if branch_parent is not None:
            operations.append({"op": "add_edge", "edge": branch_parent["edge"]})
        return {
            "partition": partition,
            "component": component,
            "missing_surfaces": grouped_surfaces,
            "required_coverage": required_coverage,
            "surface_work": surface_work,
            "branch_parent_requirement": branch_parent,
            "path_insertion": path_insertion,
            "base_graph_digest": graph.get("artifact_digest"),
            "delta_scaffold": {
                "schema_version": delta_version_for(str(graph.get("schema_version") or "")),
                "base_graph_digest": graph.get("artifact_digest"),
                "partition": partition,
                "operations": operations,
                "completion_claims": [{"scenario_id": scenario_id, "surfaces": grouped_surfaces}],
                "unresolved_items": [],
            },
        }
    return None


def _planned_reuse_candidates(
    *,
    scenario: dict[str, object],
    requirement_model: dict[str, object],
    graph: dict[str, object],
    component_id: str,
    surface: str,
) -> list[dict[str, object]]:
    """Expose main-scenario targets that a branch can project without duplicating runtime work."""

    if str(scenario.get("kind") or "") not in {"alternative", "exception"}:
        return []
    main_ids = {
        str(item.get("id") or "")
        for item in _scenarios(requirement_model)
        if str(item.get("kind") or "") == "main"
    }
    candidates = [
        item
        for item in _dicts(graph.get("nodes"))
        if str(item.get("scenario_id") or "") in main_ids
        and str(item.get("component_id") or "") == component_id
        and str(item.get("surface") or "") == surface
        and _dict(item.get("runtime_binding"))
    ]
    return [
        {
            "node_id": item.get("id"),
            "scenario_id": item.get("scenario_id"),
            "name": item.get("name"),
            "contract": item.get("contract"),
            "surface_contract": item.get("surface_contract"),
            "runtime_binding": item.get("runtime_binding"),
        }
        for item in sorted(candidates, key=lambda item: str(item.get("id") or ""))
    ]


def _branch_parent_scaffold(
    *,
    scenario: dict[str, object],
    requirement_model: dict[str, object],
    graph: dict[str, object],
    branch_entry_ref: str,
) -> dict[str, object] | None:
    """Seed the mandatory V2 branch-to-primary-main dependency in the first branch delta."""

    if (
        str(graph.get("schema_version") or "") != GRAPH_VERSION_V2
        or str(scenario.get("kind") or "") not in {"alternative", "exception"}
    ):
        return None
    branch_id = str(scenario.get("id") or "")
    nodes = _dicts(graph.get("nodes"))
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    main_scenario_ids = [
        str(item.get("id") or "")
        for item in _scenarios(requirement_model)
        if str(item.get("kind") or "") == "main"
    ]
    if not branch_id or not main_scenario_ids:
        return None
    main_id_set = set(main_scenario_ids)
    branch_node_ids = {
        str(item.get("id") or "")
        for item in nodes
        if str(item.get("scenario_id") or "") == branch_id
    }
    for edge in _dicts(graph.get("edges")):
        target = nodes_by_id.get(str(edge.get("to") or ""), {})
        if (
            str(edge.get("kind") or "") == "depends_on"
            and str(edge.get("scenario_id") or "") == branch_id
            and str(edge.get("from") or "") in branch_node_ids
            and str(target.get("scenario_id") or "") in main_id_set
        ):
            return None
    main_rank = {scenario_id: index for index, scenario_id in enumerate(main_scenario_ids)}
    candidates = sorted(
        (
            item
            for item in nodes
            if str(item.get("scenario_id") or "") in main_id_set and str(item.get("id") or "")
        ),
        key=lambda item: (
            main_rank.get(str(item.get("scenario_id") or ""), len(main_rank)),
            _surface_rank(str(item.get("surface") or "")),
            str(item.get("id") or ""),
        ),
    )
    if not candidates:
        return None
    parent = candidates[0]
    parent_scenario_id = str(parent.get("scenario_id") or "")
    return {
        "branch_scenario_id": branch_id,
        "parent_scenario_id": parent_scenario_id,
        "instruction": "Keep this exact add_edge operation so the branch has one canonical parent.",
        "edge": {
            "from": branch_entry_ref,
            "to": str(parent.get("id") or ""),
            "kind": "depends_on",
            "label": f"{branch_id} depends on {parent_scenario_id}",
            "scenario_id": branch_id,
            "branch_group": "",
            "guard": "",
            "terminal": False,
        },
    }


def _composition_repair_findings(
    graph: dict[str, object],
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
) -> list[dict[str, object]]:
    return [
        item
        for item in completion_findings(
            graph,
            required_surfaces=_required_surfaces(requirement_model),
            landscape=landscape,
        )
        if str(item.get("code") or "") in COMPOSITION_REPAIR_CODES
    ]


def _active_repair_task(
    *,
    repair_feedback: list[dict[str, object]],
    graph: dict[str, object],
    completed_targets: list[str],
    landscape: dict[str, object] | None = None,
    source: str = "validator_feedback",
    active_partition: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if active_partition is not None or not repair_feedback:
        return None
    nodes = _dicts(graph.get("nodes"))
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    repair_feedback = _applicable_repair_feedback(repair_feedback, graph=graph)
    if not repair_feedback:
        return None
    targets = _repair_work_targets(repair_feedback, nodes_by_id=nodes_by_id)
    target = next((item for item in targets if str(item.get("target_id") or "") not in set(completed_targets)), None)
    if not isinstance(target, dict):
        return None
    target_id = str(target.get("target_id") or "")
    scenario_id = str(target.get("scenario_id") or "")
    component_id = str(target.get("component_id") or "")
    edges = _dicts(graph.get("edges"))
    scenario_nodes = [item for item in nodes if str(item.get("scenario_id") or "") == scenario_id]
    preserved_edges: list[dict[str, object]] = []
    repairable_parent_edges: set[str] = set()
    if target_id == "global":
        scenario_nodes = nodes
        target_edges = edges
        target_nodes = nodes
        findings = repair_feedback
    else:
        findings = [
            item
            for item in repair_feedback
            if target_id in {
                str(candidate.get("target_id") or "")
                for candidate in _repair_work_targets([item], nodes_by_id=nodes_by_id)
            }
        ]
        target_edges = [item for item in edges if str(item.get("scenario_id") or "") == scenario_id]
        scenario_node_ids = {str(item.get("id") or "") for item in scenario_nodes}
        repairable_parent_edges = _ambiguous_branch_parent_excess_edge_ids(findings)
        preserved_edges = [
            item
            for item in target_edges
            if str(item.get("kind") or "") == "depends_on"
            and not {str(item.get("from") or ""), str(item.get("to") or "")} <= scenario_node_ids
            and str(item.get("id") or "") not in repairable_parent_edges
        ]
        target_edges = [item for item in target_edges if item not in preserved_edges]
        node_ids = {
            str(item.get("id") or "")
            for item in nodes
            if str(item.get("scenario_id") or "") == scenario_id
            and (not component_id or str(item.get("component_id") or "") == component_id)
        }
        target_nodes = [item for item in nodes if str(item.get("id") or "") in node_ids]
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
    task_partition = {
        key: primary_partition.get(key)
        for key in ("use_case_id", "scenario_id")
        if primary_partition.get(key)
    }
    partition_component_id = component_id or str(primary_partition.get("component_id") or "")
    if partition_component_id:
        task_partition["component_id"] = partition_component_id
    if target_id != "global" and not component_id and partition_component_id:
        target_nodes = [
            item
            for item in target_nodes
            if str(item.get("component_id") or "") == partition_component_id
        ]
    subject_edge_ids = {
        str(item.get("subject_ref") or "")
        for item in findings
        if str(item.get("subject_ref") or "").startswith("VDE-")
    }
    finding_codes = {str(item.get("code") or item.get("category") or "") for item in findings}
    scenario_edge_ids = [str(item.get("id") or "") for item in target_edges if str(item.get("id") or "")]
    removable_edge_ids = (
        [edge_id for edge_id in scenario_edge_ids if edge_id in subject_edge_ids]
        if finding_codes and finding_codes <= {"boundary_contract_mismatch"}
        else (
            [edge_id for edge_id in scenario_edge_ids if edge_id in repairable_parent_edges]
            if finding_codes and finding_codes <= {"branch_parent_ambiguous"}
            else scenario_edge_ids
        )
    )
    writable_node_ids = [str(item.get("id") or "") for item in target_nodes if str(item.get("id") or "")]
    read_only_nodes = [item for item in scenario_nodes if str(item.get("id") or "") not in set(writable_node_ids)]
    component_roots = {
        str(item.get("component_id") or ""): _strings(item.get("roots"))
        for item in _dicts((landscape or {}).get("components"))
        if str(item.get("component_id") or "")
    }
    write_scope = {
        "scenario_id": scenario_id,
        "component_id": partition_component_id,
        "writable_node_ids": writable_node_ids,
        "removable_edge_ids": removable_edge_ids,
        "allowed_edge_endpoint_ids": [
            str(item.get("id") or "") for item in scenario_nodes if str(item.get("id") or "")
        ],
        "restricted_edge_pairs": [
            {
                "from": str(item.get("from") or ""),
                "to": str(item.get("to") or ""),
                "kind": str(item.get("kind") or ""),
                "label": str(item.get("label") or ""),
            }
            for item in target_edges
            if str(item.get("id") or "") in subject_edge_ids
        ]
        if finding_codes and finding_codes <= {"boundary_contract_mismatch"}
        else [],
        "allow_add_node": bool(component_id and not target_nodes),
    }
    graph_context = {
        "writable_nodes": target_nodes,
        "read_only_nodes": read_only_nodes,
        "edges_in_current_order": target_edges,
        "preserved_edges": preserved_edges,
    }
    return {
        "source": source,
        "target_id": target_id,
        "partition": task_partition,
        "findings": findings,
        "graph_context": graph_context,
        "nodes": target_nodes,
        "edges_in_current_order": target_edges,
        "write_scope": write_scope,
        "read_scope": {
            "component_ids": sorted(
                {
                    str(item.get("component_id") or "")
                    for item in scenario_nodes
                    if str(item.get("component_id") or "")
                }
            ),
            "profile_roots": component_roots,
            "instruction": "Read any related profile component; only write inside write_scope.",
        },
        "expected_postconditions": [
            *[
                str(item.get("expected_change") or item.get("message") or "").strip()
                for item in findings
                if str(item.get("expected_change") or item.get("message") or "").strip()
            ],
            "No blocking finding remains for this repair target.",
            "Unrelated scenarios and component partitions remain unchanged.",
        ],
        "delta_scaffold": _repair_delta_scaffold(
            graph=graph,
            partition=task_partition,
            findings=findings,
            target_edges=target_edges,
            target_nodes=target_nodes,
            coverage=_dict(primary_partition.get("coverage")),
        ),
        "remaining_targets": [
            str(item.get("target_id") or "")
            for item in targets
            if str(item.get("target_id") or "") not in set(completed_targets)
        ],
        "instruction": (
            "Repair every listed finding for this exact target. Read related components when needed, but mutate only write_scope. "
            "Do not update read_only_nodes. Preserve unrelated partitions. "
            "Use remove_edge plus ordered add_edge operations when interaction order must change."
        ),
    }


def _repair_delta_scaffold(
    *,
    graph: dict[str, object],
    partition: dict[str, object],
    findings: list[dict[str, object]],
    target_edges: list[dict[str, object]],
    target_nodes: list[dict[str, object]],
    coverage: dict[str, object],
) -> dict[str, object]:
    operations: list[dict[str, object]] = []
    if coverage:
        operations.append({"op": "set_partition_coverage", "coverage": coverage})
    edges_by_id = {str(item.get("id") or ""): item for item in target_edges}
    nodes_by_id = {str(item.get("id") or ""): item for item in _dicts(graph.get("nodes"))}
    codes = {str(item.get("code") or item.get("category") or "") for item in findings}
    if codes and codes <= {"boundary_contract_mismatch"}:
        removals: list[dict[str, object]] = []
        additions: list[dict[str, object]] = []
        for finding in findings:
            edge_id = str(finding.get("subject_ref") or "")
            edge = edges_by_id.get(edge_id)
            if edge is None:
                continue
            replacement = {
                key: value
                for key, value in edge.items()
                if key not in {"id", "order", "boundary_contract"}
            }
            provider_id = str(edge.get("from") if str(edge.get("kind") or "") == "returns" else edge.get("to") or "")
            replacement["boundary_contract"] = _boundary_contract_repair_scaffold(
                edge=edge,
                provider=nodes_by_id.get(provider_id, {}),
            )
            removals.append({"op": "remove_edge", "edge_id": edge_id})
            additions.append({"op": "add_edge", "edge": replacement})
        operations.extend([*removals, *additions])
    elif codes and codes <= {"branch_parent_ambiguous"}:
        operations.extend(
            {"op": "remove_edge", "edge_id": edge_id}
            for edge_id in sorted(_ambiguous_branch_parent_excess_edge_ids(findings))
            if edge_id in edges_by_id
        )
    elif codes and codes <= {"runtime_operation_binding_mismatch"}:
        for finding in findings:
            node_id = str(finding.get("subject_ref") or "")
            node = nodes_by_id.get(node_id)
            diagnostics = _dict(finding.get("contract_diagnostics"))
            expected_symbol = str(diagnostics.get("expected_provider_operation") or "")
            if node is None or not expected_symbol:
                continue
            runtime_binding = json.loads(
                json.dumps(_dict(node.get("runtime_binding")), ensure_ascii=False)
            )
            target = _dict(runtime_binding.get("target"))
            target["symbol"] = expected_symbol
            runtime_binding["target"] = target
            operations.append(
                {
                    "op": "update_node",
                    "node_id": node_id,
                    "changes": {"runtime_binding": runtime_binding},
                }
            )
    elif codes and codes <= {"runtime_boundary_unreachable"}:
        invalid_node_ids = {
            str(item.get("subject_ref") or "")
            for item in findings
            if str(item.get("subject_ref") or "") in nodes_by_id
        }
        if operations and operations[0].get("op") == "set_partition_coverage":
            repair_coverage = dict(_dict(operations[0].get("coverage")))
            for node_id in invalid_node_ids:
                surface = str(nodes_by_id[node_id].get("surface") or "")
                if surface:
                    repair_coverage[surface] = "missing"
            operations[0]["coverage"] = repair_coverage
        incident_edge_ids = [
            str(item.get("id") or "")
            for item in target_edges
            if str(item.get("from") or "") in invalid_node_ids
            or str(item.get("to") or "") in invalid_node_ids
        ]
        operations.extend(
            {"op": "remove_edge", "edge_id": edge_id}
            for edge_id in incident_edge_ids
            if edge_id
        )
        operations.extend(
            {"op": "remove_node", "node_id": node_id}
            for node_id in sorted(invalid_node_ids)
        )
    elif "runtime_path_disconnected" in codes:
        operations.append(
            {
                "op": "add_edge",
                "edge": {
                    "from": "<existing scenario node id>",
                    "to": "<next existing scenario node id>",
                    "kind": "calls",
                    "label": "<specific runtime interaction>",
                    "scenario_id": str(partition.get("scenario_id") or "<scenario id>"),
                    "branch_group": "",
                    "guard": "",
                    "terminal": False,
                },
            }
        )
    else:
        subject_node_ids = [
            str(item.get("subject_ref") or "")
            for item in findings
            if any(str(node.get("id") or "") == str(item.get("subject_ref") or "") for node in target_nodes)
        ]
        if subject_node_ids:
            operations.append(
                {
                    "op": "update_node",
                    "node_id": subject_node_ids[0],
                    "changes": {"<field named by finding>": "<corrected value>"},
                }
            )
    return {
        "schema_version": delta_version_for(str(graph.get("schema_version") or "")),
        "base_graph_digest": graph.get("artifact_digest"),
        "partition": partition,
        "operations": operations,
        "completion_claims": [],
        "unresolved_items": [],
    }


def _is_exact_deterministic_repair_delta(
    *,
    arguments: object,
    repair_task: dict[str, object] | None,
) -> bool:
    if repair_task is None:
        return False
    codes = {
        str(item.get("code") or item.get("category") or "")
        for item in _dicts(repair_task.get("findings"))
    }
    return (
        codes == {"runtime_boundary_unreachable"}
        and _dict(arguments).get("delta") == repair_task.get("delta_scaffold")
    )


def _boundary_contract_repair_scaffold(
    *, edge: dict[str, object], provider: dict[str, object]
) -> dict[str, object]:
    current = _dict(edge.get("boundary_contract"))
    surface = _dict(provider.get("surface_contract"))
    if str(provider.get("surface") or "") != "api":
        return {
            "schema_version": BOUNDARY_CONTRACT_VERSION,
            "consumer_operation": str(current.get("consumer_operation") or edge.get("label") or "<specific caller operation>"),
            "provider_operation": str(current.get("provider_operation") or "<specific provider operation>"),
            "input_mapping": _dicts(current.get("input_mapping")) or [{"from": "<consumer field>", "to": "<provider field>", "required": True}],
            "auth_mapping": _dicts(current.get("auth_mapping")),
            "response_mapping": _dicts(current.get("response_mapping")) or [{"from": "<provider success>", "to": "<consumer outcome>", "required": True}],
            "error_mapping": _dicts(current.get("error_mapping")),
        }

    fields = [str(item.get("name") or "") for item in _dicts(surface.get("request_fields")) if str(item.get("name") or "")]
    current_inputs = {
        str(item.get("to") or ""): item
        for item in _dicts(current.get("input_mapping"))
        if bool(item.get("required"))
    }
    authorization = str(surface.get("authorization") or "").strip()
    auth_required = surface.get("authorization_required") is True
    responses = _dicts(surface.get("responses"))
    success = [item for item in responses if not str(item.get("status") or "").startswith(("4", "5"))]
    errors = [item for item in responses if str(item.get("status") or "").startswith(("4", "5"))]
    return {
        "schema_version": BOUNDARY_CONTRACT_VERSION,
        "consumer_operation": str(current.get("consumer_operation") or edge.get("label") or "consume API outcome"),
        "provider_operation": f"{str(surface.get('method') or '').upper()} {str(surface.get('path') or '')}".strip(),
        "input_mapping": [
            current_inputs.get(name, {"from": name, "to": name, "required": True}) for name in fields
        ],
        "auth_mapping": _dicts(current.get("auth_mapping"))
        or ([{"from": authorization, "to": authorization, "required": True}] if auth_required else []),
        "response_mapping": _dicts(current.get("response_mapping"))
        or [
            {
                "from": str(item.get("status") or "success"),
                "to": str(item.get("outcome") or "consumer success outcome"),
                "required": True,
            }
            for item in success
        ],
        "error_mapping": _dicts(current.get("error_mapping"))
        or [
            {
                "from": str(item.get("status") or "error"),
                "to": str(item.get("outcome") or "consumer error outcome"),
                "required": True,
            }
            for item in errors
        ],
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


def _repair_finding_still_applies(
    finding: dict[str, object],
    *,
    nodes_by_id: dict[str, dict[str, object]],
    materialized_partition_keys: set[str],
    covered_surface_keys: set[tuple[str, str]],
) -> bool:
    category = str(finding.get("code") or finding.get("category") or "")
    subject_ref = str(finding.get("subject_ref") or "")
    if category == "design_investigation_missing":
        return subject_ref.casefold() in materialized_partition_keys
    if category == "required_surface_missing":
        parts = subject_ref.casefold().split(":")
        if parts and parts[0] == "surf":
            parts = parts[1:]
        return len(parts) < 2 or (parts[0], parts[1]) not in covered_surface_keys
    return not (
        category == "runtime_boundary_unreachable"
        and subject_ref.startswith("VDN-")
        and subject_ref not in nodes_by_id
    )


def _applicable_repair_feedback(
    repair_feedback: list[dict[str, object]],
    *,
    graph: dict[str, object],
) -> list[dict[str, object]]:
    nodes = _dicts(graph.get("nodes"))
    nodes_by_id = {str(item.get("id") or ""): item for item in nodes}
    node_surface_keys = {
        (str(node.get("scenario_id") or "").casefold(), str(node.get("surface") or "").casefold())
        for node in nodes
        if str(node.get("scenario_id") or "") and str(node.get("surface") or "")
    }
    covered_surface_keys = {
        (str(partition.get("scenario_id") or "").casefold(), str(surface).casefold())
        for partition in _dicts(graph.get("partitions"))
        for surface, status in _dict(partition.get("coverage")).items()
        if str(status or "") in {"covered", "no_change"}
        and (str(partition.get("scenario_id") or "").casefold(), str(surface).casefold())
        in node_surface_keys
    }
    materialized_partition_keys = {
        f"{scenario_id}@{component_id}".casefold()
        for partition in _dicts(graph.get("partitions"))
        for scenario_id in [str(partition.get("scenario_id") or "")]
        for component_id in [str(partition.get("component_id") or "")]
        if any(
            str(value or "") in {"covered", "no_change"}
            for value in _dict(partition.get("coverage")).values()
        )
        or any(
            str(node.get("scenario_id") or "") == scenario_id
            and str(node.get("component_id") or "") == component_id
            for node in nodes
        )
    }
    return [
        finding
        for finding in repair_feedback
        if _repair_finding_still_applies(
            finding,
            nodes_by_id=nodes_by_id,
            materialized_partition_keys=materialized_partition_keys,
            covered_surface_keys=covered_surface_keys,
        )
    ]


def _repair_task_has_mutation_path(repair_task: dict[str, object]) -> bool:
    scope = _dict(repair_task.get("write_scope"))
    if (
        bool(scope.get("allow_add_node"))
        or bool(_strings(scope.get("writable_node_ids")))
        or bool(_strings(scope.get("removable_edge_ids")))
    ):
        return True
    operations = _dicts(_dict(repair_task.get("delta_scaffold")).get("operations"))
    return any(str(item.get("op") or "") != "set_partition_coverage" for item in operations)


def _ambiguous_branch_parent_excess_edge_ids(findings: list[dict[str, object]]) -> set[str]:
    return {
        edge_id
        for finding in findings
        if str(finding.get("code") or finding.get("category") or "") == "branch_parent_ambiguous"
        for edge_id in _strings(finding.get("diagram_ids"))[1:]
        if edge_id.startswith("VDE-")
    }


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
    delta = _dict(_dict(arguments).get("delta"))
    if _blocking_unresolved_items(delta):
        return
    target = str(repair_task.get("target_id") or "")
    completed = _strings(session.get("repair_targets_completed"))
    if target and target not in completed:
        completed.append(target)
    session["repair_targets_completed"] = completed


def _repair_target_present(
    *,
    target_id: str,
    findings: list[dict[str, object]],
    graph: dict[str, object],
) -> bool:
    if not target_id:
        return False
    nodes_by_id = {str(item.get("id") or ""): item for item in _dicts(graph.get("nodes"))}
    return any(
        target_id
        in {
            str(candidate.get("target_id") or "")
            for candidate in _repair_work_targets([finding], nodes_by_id=nodes_by_id)
        }
        for finding in findings
    )


def _validate_repair_delta_scope(
    *,
    arguments: object,
    repair_task: dict[str, object],
    graph: dict[str, object],
    run_id: str,
) -> None:
    delta = _dict(_dict(arguments).get("delta"))
    if _blocking_unresolved_items(delta):
        raise HarnessError(
            "designer_delta_invalid",
            "Validator repair cannot defer its active blocker through unresolved_items.",
            run_id=run_id,
        )
    partition = _dict(delta.get("partition"))
    task_partition = _dict(repair_task.get("partition"))
    for key in ("scenario_id", "component_id"):
        expected = str(task_partition.get(key) or "")
        if expected and str(partition.get(key) or "") != expected:
            raise HarnessError(
                "designer_delta_invalid",
                f"Repair delta {key} must remain {expected} for target {repair_task.get('target_id')}.",
                run_id=run_id,
            )
    scope = _dict(repair_task.get("write_scope"))
    writable_nodes = set(_strings(scope.get("writable_node_ids")))
    removable_edges = set(_strings(scope.get("removable_edge_ids")))
    endpoints = set(_strings(scope.get("allowed_edge_endpoint_ids")))
    restricted_pairs = {
        (
            str(item.get("from") or ""),
            str(item.get("to") or ""),
            str(item.get("kind") or ""),
            str(item.get("label") or ""),
        )
        for item in _dicts(scope.get("restricted_edge_pairs"))
    }
    graph_nodes = {str(item.get("id") or "") for item in _dicts(graph.get("nodes"))}
    temp_refs = {
        str(_dict(operation.get("node")).get("temp_ref") or "")
        for operation in _dicts(delta.get("operations"))
        if str(operation.get("op") or "") == "add_node"
    }
    for operation in _dicts(delta.get("operations")):
        op = str(operation.get("op") or "")
        if op in {"set_partition_coverage"}:
            continue
        if op == "add_node":
            if not bool(scope.get("allow_add_node")):
                raise HarnessError(
                    "designer_delta_invalid",
                    "Repair write_scope does not allow adding a node for this target.",
                    run_id=run_id,
                )
            continue
        if op in {"update_node", "remove_node"}:
            node_id = str(operation.get("node_id") or "")
            if node_id not in writable_nodes:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"Repair cannot {op} read-only node {node_id}.",
                    run_id=run_id,
                )
            continue
        if op == "remove_edge":
            edge_id = str(operation.get("edge_id") or "")
            if edge_id not in removable_edges:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"Repair cannot remove edge outside write_scope: {edge_id}.",
                    run_id=run_id,
                )
            continue
        if op == "add_edge":
            edge = _dict(operation.get("edge"))
            scenario_id = str(edge.get("scenario_id") or partition.get("scenario_id") or "")
            scoped_scenario_id = str(scope.get("scenario_id") or "")
            if scoped_scenario_id and scenario_id != scoped_scenario_id:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"Repair cannot add an edge outside scenario {scoped_scenario_id}.",
                    run_id=run_id,
                )
            source = str(edge.get("from") or "")
            target = str(edge.get("to") or "")
            valid_refs = endpoints | temp_refs
            if source not in valid_refs or target not in valid_refs:
                unknown = sorted({ref for ref in (source, target) if ref not in valid_refs and ref not in graph_nodes})
                raise HarnessError(
                    "designer_delta_invalid",
                    f"Repair edge uses endpoints outside its scenario slice: {unknown or [source, target]}.",
                    run_id=run_id,
                )
            identity = (source, target, str(edge.get("kind") or ""), str(edge.get("label") or ""))
            if restricted_pairs and identity not in restricted_pairs:
                raise HarnessError(
                    "designer_delta_invalid",
                    "Boundary repair may only replace a listed target edge; copy its from, to, kind, and label "
                    "exactly from active_repair_task.delta_scaffold and do not add unrelated edges.",
                    run_id=run_id,
                )


def _blocking_unresolved_items(delta: dict[str, object]) -> list[dict[str, object]]:
    return [
        item
        for item in _dicts(delta.get("unresolved_items"))
        if item.get("blocking") is not False
    ]


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
    required_surfaces: set[str] | None = None,
    matches: dict[tuple[str, str], list[str]],
    anchors: dict[str, dict[str, object]],
    landscape: dict[str, object],
) -> str:
    local_ui_rule = (
        surface == "business_rule"
        and "ui" in (required_surfaces or set())
        and "api" not in (required_surfaces or set())
        and "data" not in (required_surfaces or set())
    )
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
    }.get("ui" if local_ui_rule else surface, ())
    if local_ui_rule:
        candidates = [
            item
            for item in candidates
            if any(alias in str(item.get("component_id") or "").casefold() for alias in preferred_components)
        ]
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
    if candidates:
        return str(candidates[0].get("component_id") or "")
    components = sorted(
        str(item.get("component_id") or "")
        for item in _dicts(landscape.get("components"))
        if str(item.get("component_id") or "")
    )
    for alias in preferred_components:
        match = next((item for item in components if alias in item.casefold()), "")
        if match:
            return match
    return _default_component(landscape, hint="ui" if local_ui_rule else surface)


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
        "business_rule": ("service_symbol", "ui_component", "ui_entrypoint", "api_route", "source_file"),
        "data": ("service_symbol", "source_file", "data_model", "data_operation"),
        "interaction": ("service_symbol", "ui_component", "source_file"),
    }.get(surface, ("service_symbol", "source_file"))
    anchors = [
        item
        for item in _dicts(landscape.get("anchors"))
        if str(item.get("component_id") or "") == component_id
        and str(item.get("anchor_id") or "")
        and str(item.get("symbol") or "")
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


def _designer_action_budget_policy(
    *,
    settings: object,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
) -> dict[str, object]:
    partition_limit = max(1, int(getattr(settings, "designer_partition_action_budget", 8) or 8))
    scenario_repair_limit = max(
        0,
        int(getattr(settings, "designer_scenario_repair_action_budget", 4) or 0),
    )
    finalization_limit = max(
        0,
        int(getattr(settings, "designer_finalization_action_budget", 4) or 0),
    )
    hard_cap = max(1, int(getattr(settings, "designer_global_action_hard_cap", 128) or 128))
    partition_keys = _required_partition_keys(requirement_model=requirement_model, landscape=landscape)
    scenario_ids = [str(item.get("id") or "") for item in _scenarios(requirement_model) if str(item.get("id") or "")]
    calculated_limit = (
        len(partition_keys) * partition_limit
        + len(scenario_ids) * scenario_repair_limit
        + finalization_limit
    )
    if calculated_limit > hard_cap:
        raise HarnessError(
            "designer_action_budget_hard_cap_exceeded",
            (
                f"Calculated Designer action budget {calculated_limit} exceeds hard cap {hard_cap}; "
                "split the use case or raise SDLC_DESIGNER_GLOBAL_ACTION_HARD_CAP."
            ),
        )
    return {
        "schema_version": ACTION_BUDGET_POLICY_VERSION,
        "partition_limit": partition_limit,
        "scenario_repair_limit": scenario_repair_limit,
        "finalization_limit": finalization_limit,
        "partition_count": len(partition_keys),
        "scenario_count": len(scenario_ids),
        "calculated_global_limit": calculated_limit,
        "hard_cap": hard_cap,
    }


def _required_partition_keys(
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
) -> list[str]:
    anchors = {str(item.get("anchor_id") or ""): item for item in _dicts(landscape.get("anchors"))}
    matches = {
        (str(item.get("scenario_id") or ""), str(item.get("surface") or "")): _strings(
            item.get("candidate_anchor_refs")
        )
        for item in _dicts(landscape.get("scenario_matches"))
    }
    keys = {
        f"{scenario_id}|{_component_for_missing_surface(scenario_id=scenario_id, surface=surface, required_surfaces=_required_surface_set(scenario), matches=matches, anchors=anchors, landscape=landscape)}"
        for scenario in _scenarios(requirement_model)
        for scenario_id in [str(scenario.get("id") or "")]
        for surface in _required_surface_set(scenario)
        if scenario_id
    }
    return sorted(keys)


def _new_action_budgets(policy: dict[str, object]) -> dict[str, object]:
    return {
        "action": {"limit": int(policy["calculated_global_limit"]), "used": 0},
        "partition_action": {"limit": int(policy["partition_limit"]), "used_by_key": {}},
        "scenario_repair_action": {
            "limit": int(policy["scenario_repair_limit"]),
            "used_by_key": {},
        },
        "finalization_action": {"limit": int(policy["finalization_limit"]), "used": 0},
        "policy": policy,
    }


def _configure_action_budgets(session: dict[str, object], policy: dict[str, object]) -> None:
    budgets = _dict(session.get("budgets"))
    action = _dict(budgets.get("action"))
    action["limit"] = int(policy["calculated_global_limit"])
    action["used"] = int(action.get("used") or 0)
    budgets["action"] = action
    partition = _dict(budgets.get("partition_action"))
    budgets["partition_action"] = {
        "limit": int(policy["partition_limit"]),
        "used_by_key": _dict(partition.get("used_by_key")),
    }
    scenario = _dict(budgets.get("scenario_repair_action"))
    budgets["scenario_repair_action"] = {
        "limit": int(policy["scenario_repair_limit"]),
        "used_by_key": _dict(scenario.get("used_by_key")),
    }
    finalization = _dict(budgets.get("finalization_action"))
    budgets["finalization_action"] = {
        "limit": int(policy["finalization_limit"]),
        "used": int(finalization.get("used") or 0),
    }
    budgets["policy"] = policy
    session["budgets"] = budgets


def _activate_action_budget_scope(
    session: dict[str, object],
    *,
    active_partition_key: str,
    repair_task: dict[str, object] | None,
) -> None:
    if repair_task is not None:
        scenario_id = active_partition_key.split("|", 1)[0] if active_partition_key else ""
        scope = {"kind": "scenario_repair", "key": scenario_id}
    elif active_partition_key:
        scope = {"kind": "partition", "key": active_partition_key}
    else:
        scope = {"kind": "finalization", "key": "finalization"}
    session["active_action_budget_scope"] = scope
    invalid_key = (
        str(session.get("active_repair_target_id") or "")
        or f"{scope['kind']}:{scope['key']}"
    )
    if str(session.get("active_invalid_action_scope") or "") == invalid_key:
        return
    session["active_invalid_action_scope"] = invalid_key
    used = int(_dict(session.get("invalid_actions_by_target")).get(invalid_key) or 0)
    budgets = _dict(session.get("budgets"))
    budgets["invalid_action"] = {"limit": INVALID_ACTION_LIMIT, "used": used}
    session["budgets"] = budgets
    session["last_invalid_action"] = ""


def _action_budget_scope(session: dict[str, object], arguments: object = None) -> dict[str, str]:
    active = _dict(session.get("active_action_budget_scope"))
    if str(active.get("kind") or "") in {"partition", "scenario_repair", "finalization"}:
        return {"kind": str(active["kind"]), "key": str(active.get("key") or "")}
    partition = _dict(_dict(_dict(arguments).get("delta")).get("partition"))
    scenario_id = str(partition.get("scenario_id") or "")
    component_id = str(partition.get("component_id") or "")
    partition_key = (
        f"{scenario_id}|{component_id}"
        if scenario_id and component_id
        else str(session.get("active_partition_key") or "")
    )
    if partition_key:
        return {"kind": "partition", "key": partition_key}
    return {"kind": "finalization", "key": "finalization"}


def _action_budget_exhausted_message(session: dict[str, object]) -> str:
    budgets = _dict(session.get("budgets"))
    action = _dict(budgets.get("action"))
    if int(action.get("used") or 0) >= int(action.get("limit") or 0):
        return "Designer tool loop exhausted its calculated global action budget."
    scope = _action_budget_scope(session)
    if scope["kind"] == "partition":
        budget = _dict(budgets.get("partition_action"))
        used = int(_dict(budget.get("used_by_key")).get(scope["key"]) or 0)
        if used >= int(budget.get("limit") or 0):
            return f"Designer partition {scope['key']} exhausted its action budget."
    elif scope["kind"] == "scenario_repair":
        budget = _dict(budgets.get("scenario_repair_action"))
        used = int(_dict(budget.get("used_by_key")).get(scope["key"]) or 0)
        if used >= int(budget.get("limit") or 0):
            return f"Designer scenario {scope['key']} exhausted its repair action budget."
    else:
        budget = _dict(budgets.get("finalization_action"))
        if int(budget.get("used") or 0) >= int(budget.get("limit") or 0):
            return "Designer exhausted its finalization action budget."
    return ""


def _consume_action(session: dict[str, object], *, arguments: object = None) -> None:
    budgets = _dict(session.get("budgets"))
    action = _dict(budgets.get("action"))
    action["used"] = int(action.get("used") or 0) + 1
    budgets["action"] = action
    scope = _action_budget_scope(session, arguments)
    if scope["kind"] == "partition":
        budget = _dict(budgets.get("partition_action"))
        used_by_key = _dict(budget.get("used_by_key"))
        used_by_key[scope["key"]] = int(used_by_key.get(scope["key"]) or 0) + 1
        budget["used_by_key"] = used_by_key
        budgets["partition_action"] = budget
    elif scope["kind"] == "scenario_repair":
        budget = _dict(budgets.get("scenario_repair_action"))
        used_by_key = _dict(budget.get("used_by_key"))
        used_by_key[scope["key"]] = int(used_by_key.get(scope["key"]) or 0) + 1
        budget["used_by_key"] = used_by_key
        budgets["scenario_repair_action"] = budget
    else:
        budget = _dict(budgets.get("finalization_action"))
        budget["used"] = int(budget.get("used") or 0) + 1
        budgets["finalization_action"] = budget
    session["budgets"] = budgets


def _progress(session: dict[str, object]) -> dict[str, object]:
    budgets = _dict(session.get("budgets"))
    action = _dict(budgets.get("action"))
    invalid = _dict(budgets.get("invalid_action"))
    graph = _dict(session.get("graph"))
    scope = _action_budget_scope(session)
    if scope["kind"] == "partition":
        scoped = _dict(budgets.get("partition_action"))
        scoped_used = int(_dict(scoped.get("used_by_key")).get(scope["key"]) or 0)
    elif scope["kind"] == "scenario_repair":
        scoped = _dict(budgets.get("scenario_repair_action"))
        scoped_used = int(_dict(scoped.get("used_by_key")).get(scope["key"]) or 0)
    else:
        scoped = _dict(budgets.get("finalization_action"))
        scoped_used = int(scoped.get("used") or 0)
    scoped_limit = int(scoped.get("limit") or 0)
    return {
        "status": session.get("status"),
        "action_budget": {"limit": int(action.get("limit") or 0), "used": int(action.get("used") or 0), "remaining": max(0, int(action.get("limit") or 0) - int(action.get("used") or 0))},
        "active_action_budget": {
            **scope,
            "limit": scoped_limit,
            "used": scoped_used,
            "remaining": max(0, scoped_limit - scoped_used),
        },
        "action_budget_policy": _dict(budgets.get("policy")),
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


def _write_designer_exchange(
    *,
    session_dir: Path,
    call_id: str,
    prompt: str,
    completion: object,
) -> Path:
    path = session_dir / f"designer_exchange_{call_id}.json"
    usage = getattr(completion, "usage", {})
    _write_json_atomic(
        path,
        {
            "schema_version": "designer_llm_exchange_v1",
            "call_id": call_id,
            "timestamp": utc_now_iso(),
            "prompt": prompt,
            "raw_completion": str(getattr(completion, "content", "")),
            "finish_reason": str(getattr(completion, "finish_reason", "") or ""),
            "usage": dict(usage) if isinstance(usage, dict) else {},
        },
    )
    return path


def _update_designer_exchange(path: Path, updates: dict[str, object]) -> None:
    value = _read_json(path, run_id="")
    value.update(updates)
    _write_json_atomic(path, value)


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


def _designer_llm_profile_name(
    *,
    repair_task: dict[str, object] | None,
    source_observation_refs: list[str],
    observations_since_delta: int,
    observation_limit: int,
) -> str:
    if repair_task is not None:
        return "designer_repair"
    if source_observation_refs or observations_since_delta >= observation_limit:
        return "designer_delta"
    return "designer_investigation"


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


def _delta_invalid_message(
    *,
    message: str,
    arguments: object,
    graph: dict[str, object],
) -> str:
    """Report every exact existing edge re-added by one rejected delta."""

    if not message.startswith("Design edge already exists:"):
        return message
    delta = _dict(_dict(arguments).get("delta"))
    existing = {
        (
            str(edge.get("scenario_id") or ""),
            str(edge.get("from") or ""),
            str(edge.get("kind") or ""),
            str(edge.get("to") or ""),
            str(edge.get("label") or "").strip(),
            str(edge.get("branch_group") or "").strip(),
            str(edge.get("guard") or "").strip(),
        ): str(edge.get("id") or "")
        for edge in _dicts(graph.get("edges"))
    }
    removed: set[str] = set()
    duplicates: list[str] = []
    partition = _dict(delta.get("partition"))
    for operation in _dicts(delta.get("operations")):
        if str(operation.get("op") or "") == "remove_edge":
            removed.add(str(operation.get("edge_id") or ""))
            continue
        if str(operation.get("op") or "") != "add_edge":
            continue
        edge = _dict(operation.get("edge"))
        if str(edge.get("from") or "").startswith("tmp:") or str(edge.get("to") or "").startswith("tmp:"):
            continue
        key = (
            str(edge.get("scenario_id") or partition.get("scenario_id") or ""),
            str(edge.get("from") or ""),
            str(edge.get("kind") or ""),
            str(edge.get("to") or ""),
            str(edge.get("label") or "").strip(),
            str(edge.get("branch_group") or "").strip(),
            str(edge.get("guard") or "").strip(),
        )
        edge_id = existing.get(key, "")
        if edge_id and edge_id not in removed and edge_id not in duplicates:
            duplicates.append(edge_id)
    if len(duplicates) < 2:
        return message
    return (
        "Design edges already exist: "
        + ", ".join(duplicates)
        + ". Remove every listed exact edge_id before re-adding those edges, in remove-before-add order."
    )
