"""Durable, bounded read-only tool loop for the L3 Designer role."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from ...core.config import phase2_llm_call_options
from ...core.errors import HarnessError
from ...llm.client import strip_code_fence
from ...llm.json_utils import parse_first_json_object
from ...llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from ...loop import RoleToolResultV1, canonical_json_hash
from ...storage.runtime import RuntimeStore, utc_now_iso
from .graph import (
    BOUNDARY_CONTRACT_VERSION,
    DELTA_VERSION,
    EXTERNAL_DEPENDENCY_REF_PREFIXES,
    PLACEABLE_SURFACES,
    GRAPH_VERSION,
    RUNTIME_ATTACHMENT_RELATIONS,
    RUNTIME_BINDING_VERSION,
    STRUCTURAL_EDGE_KINDS,
    SURFACE_CONTRACT_VERSION,
    _delta_written_names,
    _sequence_behavior_findings,
    apply_designer_delta,
    completion_findings,
    graph_digest,
    UI_EXPERIENCE_FIELDS,
    is_composition_wiring,
    new_vertical_design_graph,
    validate_vertical_design_graph,
)
from .tools import DesignerToolRegistry
from .ui_context import chain_path_to, required_props
from .uiux import (
    RUNTIME_SURFACE_BINDING_VERSION,
    UI_EXPERIENCE_CONTRACT_V2,
    UiUxGuidanceProvider,
)
from ...core.coerce import dict_copies as _dicts
from .facts import (
    OBSERVATION_ACTION_LIMIT,
    _dict,
    _feature_guidance_satisfied,
    _required_surface_set,
    _scenarios,
    _strings,
    _ui_guidance_required,
    _uiux_call_allowed,
    _uiux_guidance_status,
)
from .prompting import (
    _active_requirement_slice,
    _construction_graph_view,
    _designer_prompt,
    _feature_ux_decisions,
)


SESSION_VERSION = "designer_loop_session_v1"
MANIFEST_VERSION = "designer_loop_manifest_v1"
JOURNAL_VERSION = "designer_loop_journal_v1"
ACTION_BUDGET_POLICY_VERSION = "designer_action_budget_policy_v1"
INVALID_ACTION_LIMIT = 3
REPAIR_OBSERVATION_ACTION_LIMIT = 6
UI_OBSERVATION_ACTION_LIMIT = 5
UI_REPAIR_OBSERVATION_ACTION_LIMIT = 8
OBSERVATION_TOOL_IDS = frozenset(
    {
        "landscape.search",
        "landscape.get_anchor",
        "repo.search",
        "repo.read",
        "repo.read_batch",
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
        uiux_provider: UiUxGuidanceProvider | None = None,
    ):
        self.settings = settings
        self.store = store
        self.llm_client = llm_client
        self.step_review = dict(step_review or {})
        self.uiux_provider = uiux_provider

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
        session: dict[str, object] | None = None
        registry: DesignerToolRegistry | None = None
        try:
            uiux_provider = self.uiux_provider or UiUxGuidanceProvider(
                skill_root=self.settings.uiux_skill_root,
                timeout_seconds=self.settings.uiux_skill_timeout_seconds,
                fake_mode=self.settings.llm_mode == "fake",
            )
            uiux_profile = uiux_provider.preflight()
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
                )
                graph = validate_vertical_design_graph(
                    graph,
                    expected_run_id=run_id,
                    expected_binding=snapshot_binding,
                )
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
                expected_codegraph_digest=str(snapshot_binding.get("repo_codegraph_snapshot_digest") or ""),
            )
            registry.register_evidence_refs(
                _strings(_dict(snapshot_binding.get("ui_repo_context")).get("evidence_refs"))
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
        except DesignerExecutionError:
            raise
        except HarnessError as exc:
            raise self._execution_error(
                exc=exc,
                run_id=run_id,
                session=session,
                registry=registry,
                session_file=session_file,
                session_path=session_path,
                manifest_path=manifest_path,
                journal_path=journal_path,
            ) from exc
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
            raise self._execution_error(
                exc=exc,
                run_id=run_id,
                session=session,
                registry=registry,
                session_file=session_file,
                session_path=session_path,
                manifest_path=manifest_path,
                journal_path=journal_path,
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
                    fake_contract = _fake_surface_contract(
                        surface=surface,
                        scenario_id=scenario_id,
                        name=str((anchor or {}).get("symbol") or f"{scenario_id} {surface}"),
                        guidance_refs=guidance_refs,
                    )
                    fake_binding = _fake_runtime_binding(
                        landscape=landscape,
                        component_id=component_id,
                        surface=surface,
                        anchor=anchor,
                        source_refs=source_refs,
                        fallback_symbol=f"{scenario_id} {surface}",
                    )
                    if surface == "ui":
                        fake_contract["runtime_surface"], fake_binding = _fake_runtime_surface(
                            graph=registry.graph,
                            landscape=landscape,
                            binding=fake_binding,
                            scenario_id=scenario_id,
                        )
                        if isinstance(fake_contract.get("experience_contract"), dict):
                            fake_contract["experience_contract"].update(
                                _experience_v2_fields(
                                    registry.graph,
                                    screen_id=str(fake_contract["runtime_surface"]["screen_id"]),
                                    fake_label=f"{scenario_id} ui",
                                )
                            )
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
                                "surface_contract": fake_contract,
                                "runtime_binding": fake_binding,
                            },
                        }
                    )
                delta = {
                    "schema_version": DELTA_VERSION,
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
                            "schema_version": DELTA_VERSION,
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
        partition_key = f"{scenario_id}|{component_id}"
        session["active_partition_key"] = partition_key
        session["active_guidance_key"] = partition_key
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
                str(item.get("surface") or "") == "ui"
                and not isinstance(_dict(item.get("surface_contract")).get("experience_contract"), dict)
            )
            or not isinstance(item.get("runtime_binding"), dict)
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
                        guidance_refs=guidance_refs,
                    )
                elif (
                    str(node.get("surface") or "") == "ui"
                    and not isinstance(_dict(node.get("surface_contract")).get("experience_contract"), dict)
                ):
                    changes["surface_contract"] = _fake_surface_contract(
                        surface="ui",
                        scenario_id=str(node.get("scenario_id") or "SC-1"),
                        name=str(node.get("name") or node.get("semantic_key") or "Design element"),
                        guidance_refs=guidance_refs,
                    )
                if not isinstance(node.get("runtime_binding"), dict):
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
                "schema_version": DELTA_VERSION,
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
                        "schema_version": DELTA_VERSION,
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
                requirement_model=requirement_model,
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
            guidance_key = _guidance_key(
                graph=registry.graph,
                requirement_model=requirement_model,
                partition_key=active_partition_key,
            )
            session["active_guidance_key"] = guidance_key
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
            scope_actions_remaining = int(
                _dict(_progress(session).get("active_action_budget")).get("remaining") or 0
            )
            observation_limit = _observation_allowance(observation_limit, actions_remaining=scope_actions_remaining)
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
                            require_runtime_binding=True,
                            repair_closers=str(exchange.get("finish_reason") or "") == "stop",
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
                    # Approving unparsable output confirms the Harness rejection: retried as in unattended mode,
                    # with the Harness's own hint, so no operator has to write the correction.
                    raw = str(_read_json(exchange_path, run_id=run_id).get("raw_completion") or "")
                    invalid_json_message = "Designer returned invalid JSON." + _json_structure_hint(raw)
                    session["pending_step_review"] = None
                    session["status"] = "running"
                    self._invalid_action(session, session_file, message=invalid_json_message)
                    self._record_observation(
                        session,
                        {"tool_id": "llm", "status": "rejected", "message": invalid_json_message},
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {
                            "status": "rejected",
                            "rejection": {"code": "invalid_json", "message": invalid_json_message},
                            "operator_review": {"decision": "approve", "note": str(self.step_review.get("note") or "").strip()},
                        },
                    )
                    _write_json_atomic(session_file, session)
                    continue
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
                    session["last_review_target"] = str(pending_review.get("review_target") or "")
                    # The next prompt must show what was rejected, or "keep the rest" cannot be honored.
                    session["last_review_rejected_delta"] = (
                        _dict(action.get("arguments")).get("delta")
                        if str(action.get("action") or "") == "design.propose_delta"
                        else None
                    )
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
                session["last_review_target"] = ""
                session["last_review_rejected_delta"] = None
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
                # The graph this prompt shows; a delta from this call is applied against it (see
                # _apply_prompt_graph_digest).
                session["prompt_graph_digest"] = str(registry.graph.get("artifact_digest") or "")
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
                    last_review_feedback=_scoped_review_feedback(
                        session, current_target=_review_target_key(repair_task, active_partition)
                    ),
                    last_rejected_delta=_dict(session.get("last_review_rejected_delta")),
                    active_guidance=_guidance_observations(session, guidance_key),
                    source_inspected=bool(
                        _inspected_source_refs(session, active_component_id, construction=repair_task is None)
                    ),
                    actions_remaining=scope_actions_remaining,
                    design_notes=_design_notes(session, work_key=_review_target_key(repair_task, active_partition)),
                )
                system_prompt = "You are SDLC Harness Designer L3. Return exactly one JSON tool action."
                # One reasoning level for every construction call: with repo.read_batch a partition needs few reads,
                # and a low-effort read call followed by a high-effort delta call cost a call and the prompt cache.
                profile_name = "designer_repair" if repair_task is not None else "designer_delta"
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
                        require_runtime_binding=True,
                        # A length-truncated output must never be completed into a partial delta.
                        repair_closers=str(getattr(completion, "finish_reason", "") or "") == "stop",
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
                            "review_target": _review_target_key(repair_task, active_partition),
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
                    invalid_json_message = "Designer returned invalid JSON." + _json_structure_hint(
                        str(completion.content or "")
                    )
                    self._invalid_action(session, session_file, message=invalid_json_message)
                    self._record_observation(
                        session,
                        {"tool_id": "llm", "status": "rejected", "message": invalid_json_message},
                    )
                    _update_designer_exchange(
                        exchange_path,
                        {"rejection": {"code": "invalid_json", "message": invalid_json_message}},
                    )
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
                        "review_target": _review_target_key(repair_task, active_partition),
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
            _record_design_note(session, action, work_key=_review_target_key(repair_task, active_partition))
            source_refs = _source_observation_refs(session, active_component_id)
            inspected_refs = _inspected_source_refs(session, active_component_id, construction=repair_task is None)
            exact_deterministic_repair = _is_exact_deterministic_repair_delta(
                arguments=arguments,
                repair_task=repair_task,
            )
            if tool_id == "uiux.search_guidance":
                args = _dict(arguments)
                if not ui_guidance_required:
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
                if not inspected_refs:
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
                    _guidance_observations(session, guidance_key),
                    source_inspected=bool(inspected_refs),
                )
                if not _uiux_call_allowed(guidance_status, mode=mode, selector=selector):
                    required_next = guidance_status.get("required_next_action")
                    expected = json.dumps(
                        required_next or guidance_status.get("allowed_next_actions") or [],
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                    label = "Required next action" if required_next else "Allowed next actions"
                    self._invalid_action(
                        session,
                        session_file,
                        message=f"UI/UX guidance step mismatch. {label}: {expected}",
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
                if _guidance_attempt_count(session, guidance_key, mode=mode, selector=selector) >= 2:
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
                tool_id == "design.propose_delta"
                and active_component_id
                and not inspected_refs
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
                and ui_guidance_required
                and not _has_required_ui_guidance(session, guidance_key)
                and not _feature_guidance_satisfied(registry.graph)
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
            unread_targets = (
                _unread_existing_targets(
                    arguments,
                    graph=registry.graph,
                    read_paths=(
                        _scenario_read_paths(session, _partition_scenario(str(session.get("active_partition_key") or "")))
                        if repair_task is None
                        else set(_strings(session.get("source_read_paths")))
                    ),
                )
                if tool_id == "design.propose_delta" and not exact_deterministic_repair
                else []
            )
            if unread_targets:
                # The delta designs from existing files (targets it keeps or changes, hops that pass its inputs);
                # the Designer must have read each of them itself.
                session["required_target_reads"] = unread_targets
                self._invalid_action(
                    session,
                    session_file,
                    message=(
                        "The delta designs from existing files that no repo.read or repo.read_batch of this "
                        + ("scenario" if repair_task is None else "session")
                        + " returned (runtime targets of reuse, modify or no_change nodes, or provided_by hops of required "
                        "inputs): " + ", ".join(unread_targets) + ". Read the listed files with one repo.read_batch "
                        "(allowed even after the observation limit), then propose the delta again."
                    ),
                )
                self._record_observation(
                    session,
                    {
                        "tool_id": tool_id,
                        "status": "rejected",
                        "code": "design_target_not_read",
                        "unread_target_paths": unread_targets,
                    },
                )
                _update_designer_exchange(
                    exchange_path,
                    {
                        "status": "rejected",
                        "rejection": {"code": "design_target_not_read", "unread_target_paths": unread_targets},
                    },
                )
                _write_json_atomic(session_file, session)
                continue
            if tool_id == "design.propose_delta":
                arguments, digest_fix = _apply_prompt_graph_digest(
                    arguments, prompt_digest=str(session.get("prompt_graph_digest") or "")
                )
                if digest_fix:
                    _update_designer_exchange(exchange_path, {"harness_graph_digest": digest_fix})
            if tool_id == "design.propose_delta" and active_component_id and source_refs:
                arguments = _attach_source_observations(arguments, component_id=active_component_id, refs=source_refs)
            if tool_id == "complete" and (active_partition is not None or repair_task is not None):
                if active_partition is not None:
                    partition = _dict(active_partition.get("partition"))
                    work_item = (
                        "active_partition "
                        f"{partition.get('scenario_id') or '?'}|{partition.get('component_id') or '?'}"
                    )
                else:
                    work_item = f"active_repair_task {(repair_task or {}).get('target_id') or '?'}"
                message = (
                    f"Cannot call complete while {work_item} is active. "
                    "Propose a design.propose_delta for that work item first."
                )
                self._invalid_action(session, session_file, message=message)
                self._record_observation(
                    session,
                    {
                        "tool_id": tool_id,
                        "arguments": arguments,
                        "status": "rejected",
                        "code": "active_work_pending",
                        "message": message,
                    },
                )
                _update_designer_exchange(
                    exchange_path,
                    {
                        "status": "rejected",
                        "rejection": {"code": "active_work_pending", "message": message},
                    },
                )
                _write_json_atomic(session_file, session)
                continue
            # inspect_partition only shows designed graph nodes, so it stays allowed after the quota (S5-B). UI/UX guidance
            # has its own two-attempt lanes and is not counted; a read of a target the delta gate asked for stays allowed.
            requested_read_paths = _requested_read_paths(tool_id, arguments)
            required_target_read = bool(requested_read_paths) and set(requested_read_paths) <= set(
                _strings(session.get("required_target_reads"))
            )
            if (active_partition is not None or repair_task is not None) and observations_since_delta >= observation_limit and tool_id not in {"design.propose_delta", "design.inspect_partition", "design.report_blocker", "uiux.search_guidance"} and not required_target_read:
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
            designer_arguments = arguments
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
                if tool_id == "design.report_blocker" and not inspected_refs and not any(
                    item.get("tool_id") in {"repo.read", "repo.read_batch", "codegraph.query"}
                    for item in _prompt_observations(session, repair_task=repair_task, active_component_id=active_component_id)
                ):
                    raise HarnessError("designer_tool_input_invalid", "Investigate the active source or CodeGraph before reporting an evidence blocker.", run_id=run_id)
                if tool_id == "design.propose_delta" and active_partition is not None:
                    arguments = _materialize_delta_wiring(
                        arguments=arguments,
                        active_partition=active_partition,
                        graph=registry.graph,
                        run_id=run_id,
                    )
                    arguments = _materialize_branch_parent(
                        arguments=arguments,
                        active_partition=active_partition,
                        requirement_model=requirement_model,
                        graph=registry.graph,
                    )
                if tool_id == "design.propose_delta" and repair_task is not None:
                    _validate_repair_delta_scope(
                        arguments=arguments,
                        repair_task=repair_task,
                        graph=registry.graph,
                        run_id=run_id,
                    )
                    _reject_stranded_scenario_nodes(
                        arguments=arguments,
                        repair_task=repair_task,
                        graph=registry.graph,
                        run_id=run_id,
                    )
                if tool_id == "design.propose_delta":
                    _reject_unbound_new_nodes(arguments, run_id=run_id)
                    arguments = _validate_surface_placement_ops(
                        arguments,
                        requirement_model=requirement_model,
                        landscape=landscape,
                        run_id=run_id,
                    )
                if tool_id == "design.propose_delta" and active_partition is not None and repair_task is None:
                    _reject_discontinuous_completed_scenario(
                        arguments,
                        registry=registry,
                        requirement_model=requirement_model,
                        landscape=landscape,
                        run_id=run_id,
                    )
                if tool_id == "design.propose_delta" and repair_task is not None:
                    _reject_unserved_repeat_requests(
                        arguments,
                        repair_task=repair_task,
                        registry=registry,
                        run_id=run_id,
                    )
                    _reject_new_node_binding_findings(
                        arguments,
                        repair_task=repair_task,
                        registry=registry,
                        requirement_model=requirement_model,
                        landscape=landscape,
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
                    fingerprint_arguments=designer_arguments,
                )
                _update_designer_exchange(exchange_path, {"status": "succeeded"})
                if tool_id == "design.report_blocker":
                    session["evidence_blocker"] = {**_dict(arguments), "component_id": active_component_id}
                    _write_json_atomic(session_file, session)
                    raise HarnessError(
                        "design_evidence_insufficient",
                        f"Cannot verify {_dict(arguments).get('target_ref')}: {_dict(arguments).get('missing_facts')}. {_dict(arguments).get('reason')}",
                        run_id=run_id,
                    )
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
                if _accepts_shared_edge_no_change(exc, tool_id=tool_id, repair_task=repair_task):
                    _mark_repair_targets_completed(session=session, arguments=arguments, repair_task=repair_task)
                    self._record_observation(
                        session,
                        {
                            "tool_id": tool_id,
                            "arguments": arguments,
                            "status": "accepted_no_change",
                            "message": "This component needs no change for the shared edge finding; the other end has its own turn.",
                        },
                    )
                    _update_designer_exchange(exchange_path, {"status": "succeeded", "no_change_for_shared_edge": True})
                    _write_json_atomic(session_file, session)
                    continue
                if exc.error_code in {
                    "designer_tool_unknown",
                    "designer_tool_input_invalid",
                    "designer_repo_path_unsafe",
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

    def _execution_error(
        self,
        *,
        exc: HarnessError,
        run_id: str,
        session: dict[str, object] | None,
        registry: DesignerToolRegistry | None,
        session_file: Path,
        session_path: str,
        manifest_path: str,
        journal_path: str,
    ) -> DesignerExecutionError:
        retriable = exc.error_code in {
            "llm_transport_failed",
            "target_repo_codegraph_failed",
            "target_repo_codegraph_stale",
            "designer_uiux_unavailable",
            "designer_uiux_failed",
        }
        progress: dict[str, object] = {}
        if session is not None:
            session["status"] = "paused" if retriable else "blocked"
            session["updated_at"] = utc_now_iso()
            session["last_error"] = exc.to_response()
            if registry is not None:
                session["graph"] = registry.graph
            _write_json_atomic(session_file, session)
            progress = _progress(session)
        return DesignerExecutionError(
            exc.error_code,
            exc.message,
            run_id=run_id,
            retriable=retriable,
            session_path=session_path,
            manifest_path=manifest_path,
            journal_path=journal_path,
            progress=progress,
        )

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
        fingerprint_arguments: object | None = None,
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
        if tool_id in {"repo.read", "repo.read_batch"} and evidence_refs:
            file_reads = [_dict(output)] if tool_id == "repo.read" else _dicts(_dict(output).get("results"))
            for file_read in file_reads:
                ref = str(file_read.get("evidence_ref") or "")
                if not ref:
                    continue
                component_id = registry.component_for_path(str(file_read.get("path") or ""))
                _record_source_observations(session, component_id=component_id, refs=[ref])
                read_path = _normalized_path(file_read.get("path"))
                session["source_read_paths"] = list(dict.fromkeys([*_strings(session.get("source_read_paths")), read_path]))
                session["required_target_reads"] = [
                    path for path in _strings(session.get("required_target_reads")) if path != read_path
                ]
        elif (tool_id == "codegraph.query" and str(_dict(output).get("status") or "") == "available"
              and str(_dict(arguments).get("operation") or "explore") in {"explore", "callers", "callees", "impact"}):
            _record_source_observations(
                session,
                component_id=str(session.get("active_component_id") or ""),
                refs=evidence_refs,
            )
        elif tool_id == "uiux.search_guidance":
            _record_guidance_observation(
                session,
                partition_key=str(session.get("active_guidance_key") or session.get("active_partition_key") or ""),
                arguments=_dict(arguments),
                output=_dict(output),
                refs=evidence_refs,
            )
        elif tool_id == "design.propose_delta" and _delta_contains_ui_experience(arguments):
            _compact_guidance_partition(
                session,
                partition_key=str(session.get("active_guidance_key") or session.get("active_partition_key") or ""),
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
                "schema_version": DELTA_VERSION,
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
        session["last_action_fingerprint"] = canonical_json_hash(
            {"tool_id": tool_id, "arguments": arguments if fingerprint_arguments is None else fingerprint_arguments}
        )
        if tool_id == "design.propose_delta":
            session["observation_actions_since_delta"] = 0
        elif tool_id in OBSERVATION_TOOL_IDS and tool_id not in {"design.inspect_partition", "uiux.search_guidance"}:
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
            "designer_contract_version": GRAPH_VERSION,
            "designer_uiux_mode": "enforce",
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
                "repo.read_batch",
                "codegraph.query",
                "uiux.search_guidance",
                "design.inspect_partition",
                "design.report_blocker",
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
        # Never trim what a prompt still shows (the active partition's items, reads pinned as source
        # observations): trimming them lost context mid-partition and broke the prompt prefix cache.
        pinned = {ref for refs in _dict(session.get("source_observations")).values() for ref in _strings(refs)}
        trimmable = [
            index
            for index, item in enumerate(observations)
            if not (partition_key and item.get("partition_key") == partition_key)
            and not pinned.intersection(_strings(item.get("evidence_refs")))
        ]
        dropped = set(trimmable[:-12])
        session["observations"] = [item for index, item in enumerate(observations) if index not in dropped]
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
    repair_closers: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    payload, normalization = _parse_designer_tool_action_text(
        raw,
        require_runtime_binding=require_runtime_binding,
        repair_closers=repair_closers,
    )
    relocated = _relocate_misplaced_runtime_surface(payload)
    if relocated:
        normalization = {
            "schema_version": "designer_output_normalization_v1",
            "kind": normalization.get("kind") or "relocated_runtime_surface",
            **{key: value for key, value in normalization.items() if key not in {"schema_version", "kind"}},
            "relocated_runtime_surface": relocated,
        }
    relocated_experience = _relocate_misplaced_experience_fields(payload)
    if relocated_experience:
        normalization = {
            "schema_version": "designer_output_normalization_v1",
            "kind": normalization.get("kind") or "relocated_experience_fields",
            **{key: value for key, value in normalization.items() if key not in {"schema_version", "kind"}},
            "relocated_experience_fields": relocated_experience,
        }
    relocated_summary = _relocate_argument_summary(payload)
    if relocated_summary:
        normalization = {
            "schema_version": "designer_output_normalization_v1",
            "kind": normalization.get("kind") or "relocated_summary",
            **{key: value for key, value in normalization.items() if key not in {"schema_version", "kind"}},
            "relocated_summary": True,
        }
    arguments, external_inputs = _normalize_external_inputs(payload.get("arguments"))
    if external_inputs:
        payload["arguments"] = arguments
        normalization = {
            "schema_version": "designer_output_normalization_v1",
            "kind": normalization.get("kind") or "canonicalized_external_inputs",
            **{key: value for key, value in normalization.items() if key not in {"schema_version", "kind"}},
            "canonicalized_external_inputs": external_inputs,
        }
    return payload, normalization


def _relocate_argument_summary(payload: dict[str, object]) -> bool:
    """Move a summary written inside arguments to the action's top level, where design notes read it.

    design_v20 designer-0025, 0041 and 0046 wrote it as arguments.summary, so their notes were lost."""

    arguments = payload.get("arguments")
    if not isinstance(arguments, dict) or str(payload.get("summary") or "").strip():
        return False
    summary = arguments.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False
    payload["summary"] = arguments.pop("summary")
    return True


def _relocate_misplaced_experience_fields(payload: dict[str, object]) -> list[str]:
    """Move experience-only fields written directly under surface_contract into its experience_contract.

    The field names are disjoint from the surface_contract keys, so the move never changes what was written
    (design_v12 designer-0008 closed experience_contract before visual_contract on all three UI nodes)."""

    moved: list[str] = []
    arguments = payload.get("arguments")
    delta = arguments.get("delta") if isinstance(arguments, dict) else None
    operations = delta.get("operations") if isinstance(delta, dict) else None
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, dict):
            continue
        holder = operation.get("node") if operation.get("op") == "add_node" else operation.get("changes")
        contract = holder.get("surface_contract") if isinstance(holder, dict) else None
        experience = contract.get("experience_contract") if isinstance(contract, dict) else None
        if not isinstance(experience, dict):
            continue
        name = str(holder.get("temp_ref") or operation.get("node_id") or "node")
        for key in UI_EXPERIENCE_FIELDS:
            if key in contract and key not in experience:
                experience[key] = contract.pop(key)
                moved.append(f"{name}.{key}")
        # The experience version is unambiguous by value (design_v26 designer-0002 wrote it on the surface_contract).
        if contract.get("schema_version") == UI_EXPERIENCE_CONTRACT_V2 and "schema_version" not in experience:
            experience["schema_version"] = contract.pop("schema_version")
            moved.append(f"{name}.schema_version")
    return moved


def _relocate_misplaced_runtime_surface(payload: dict[str, object]) -> list[str]:
    """Move runtime_surface written under experience_contract or the node back to surface_contract."""

    moved: list[str] = []
    arguments = payload.get("arguments")
    delta = arguments.get("delta") if isinstance(arguments, dict) else None
    operations = delta.get("operations") if isinstance(delta, dict) else None
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, dict):
            continue
        holder = operation.get("node") if operation.get("op") == "add_node" else operation.get("changes")
        contract = holder.get("surface_contract") if isinstance(holder, dict) else None
        if not isinstance(contract, dict) or "runtime_surface" in contract:
            continue
        experience = contract.get("experience_contract")
        if isinstance(experience, dict) and isinstance(experience.get("runtime_surface"), dict):
            contract["runtime_surface"] = experience.pop("runtime_surface")
        elif isinstance(holder.get("runtime_surface"), dict):
            contract["runtime_surface"] = holder.pop("runtime_surface")
        else:
            continue
        moved.append(str(holder.get("temp_ref") or operation.get("node_id") or "node"))
    return moved


def _repair_unbalanced_closers(text: str) -> str | None:
    """Close an array the model ended with '}' and append missing closers; content is never rewritten."""

    out: list[str] = []
    stack: list[str] = []
    in_string = escaped = changed = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return None
            expected = "}" if stack[-1] == "{" else "]"
            if char != expected:
                opener = "{" if char == "}" else "["
                # Close the inner container(s) the model forgot, until this closer matches its opener.
                while stack and stack[-1] != opener:
                    out.append("]" if stack[-1] == "[" else "}")
                    stack.pop()
                    changed = True
                if not stack:
                    return None
            stack.pop()
        out.append(char)
    if in_string:
        return None
    if stack:
        changed = True
        out.extend("}" if item == "{" else "]" for item in reversed(stack))
    return "".join(out) if changed else None


# DeepSeek sometimes appends tool-call markup such as `</｜｜DSML｜｜ invoke>` after a complete object (design_v20 0011).
_DSML_TAG = re.compile(r"</?[｜|][^<>]*>")


def _text_after_action_object(raw: str) -> tuple[int, str]:
    """Where the first action object ends and the non-markup text that follows it."""

    text = strip_code_fence(raw).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and {"action", "arguments"} <= set(candidate):
            return end, _DSML_TAG.sub("", text[end:]).strip()
    return -1, ""


def _rejoin_cut_summary(payload: dict[str, object], trailing: str) -> dict[str, object] | None:
    """The action object when an extra closer cut off only its top-level summary.

    design_v20 designer-0049 and 0065 closed the object one bracket early, right before `,"summary":"..."}`; the
    object itself was complete. Anything else after the object (an operation, prose) still fails closed."""

    if not trailing.startswith(",") or "summary" in payload:
        return None
    try:
        rest = json.loads("{" + trailing[1:])
    except json.JSONDecodeError:
        return None
    if not isinstance(rest, dict) or set(rest) != {"summary"} or not isinstance(rest["summary"], str):
        return None
    return {**payload, "summary": rest["summary"]}


def _json_structure_hint(raw: str) -> str:
    """Point the next Designer call at the exact place its JSON broke so it can fix its own nesting."""

    start = raw.find("{")
    if start < 0:
        return ""
    try:
        _, end = json.JSONDecoder().raw_decode(raw, start)
    except json.JSONDecodeError as error:
        position, reason = error.pos, error.msg
    else:
        if not _DSML_TAG.sub("", raw[end:]).strip():
            return ""
        return (
            f" The action object ended at character {end}, but more text follows it: {raw[end:end + 80]!r}. An extra "
            "closing bracket ended the object early, or text was added after it; return exactly one JSON object and "
            "nothing after it."
        )
    # Containers still open where parsing stopped, innermost last.
    stack: list[int] = []
    in_string = escaped = False
    for index in range(start, position):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(index)
        elif char in "}]" and stack:
            stack.pop()
    if position >= len(raw.rstrip()):
        return f" The JSON ended with {len(stack)} unclosed bracket(s); output the complete action object."
    hint = f" Parsing stopped at character {position} ({reason}) near {raw[max(0, position - 40):position + 40]!r}."
    if stack:
        hint += (
            f" The innermost container still open there is {_json_container_label(raw, stack[-1])} opened at "
            f"character {stack[-1]}."
        )
    return hint + " Close every object with '}' and every array with ']', innermost first."


def _json_container_label(raw: str, opened_at: int) -> str:
    kind = "array" if raw[opened_at] == "[" else "object"
    key = re.findall(r'"([A-Za-z_]+)"\s*:\s*$', raw[max(0, opened_at - 80):opened_at])
    if key:
        return f"the '{key[-1]}' {kind}"
    operation = re.match(r'\{\s*"op"\s*:\s*"([A-Za-z_]+)"', raw[opened_at:opened_at + 60])
    return f"the {operation.group(1)} operation object" if operation else f"an {kind}"


def _remove_redundant_closers(text: str, *, require_runtime_binding: bool, limit: int = 3) -> dict[str, object] | None:
    """Drop stray closing brackets exactly where the JSON parser stops on them (at most `limit`)."""

    candidate = text
    for _ in range(limit + 1):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as error:
            # The parser expected a delimiter or end of input but met a closer: that closer is the stray one.
            if error.pos >= len(candidate) or candidate[error.pos] not in "}]":
                return None
            candidate = candidate[: error.pos] + candidate[error.pos + 1:]
            continue
        if candidate is text or not isinstance(payload, dict):
            return None
        return payload if _designer_action_shape(payload, require_runtime_binding=require_runtime_binding) else None
    return None


def _innermost_open_container(text: str, end: int) -> str | None:
    """Return '{' or '[' for the innermost container still open before `end` (strings are skipped)."""

    stack: list[str] = []
    in_string = escaped = False
    for char in text[:end]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    return stack[-1] if stack else None


def _swap_mismatched_closers(text: str, *, require_runtime_binding: bool, limit: int = 3) -> dict[str, object] | None:
    """Swap a closer that does not match its open container ('}' closing an array, or the reverse).

    Real DeepSeek slip (Conduit UC4 H-r1): `"operations":[...}` followed by `,"completion_claims":...`. Closing the
    array before that brace would leave the later keys outside `delta`; only swapping keeps every key where the model
    wrote it. The result must still be a complete Designer action, so an ambiguous repair is never accepted.
    """

    candidate = text
    for _ in range(limit + 1):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as error:
            if error.pos >= len(candidate) or candidate[error.pos] not in "}]":
                return None
            opener = _innermost_open_container(candidate, error.pos)
            closer = candidate[error.pos]
            if opener is None or (opener == "{") == (closer == "}"):
                return None
            candidate = candidate[: error.pos] + ("]" if closer == "}" else "}") + candidate[error.pos + 1:]
            continue
        if candidate is text or not isinstance(payload, dict):
            return None
        return payload if _designer_action_shape(payload, require_runtime_binding=require_runtime_binding) else None
    return None


def _close_open_array_items(
    text: str, *, require_runtime_binding: bool, limit: int = 32
) -> tuple[dict[str, object], list[int]] | None:
    """Close an array item object left open where the next item or the array's end begins.

    Real DeepSeek slip (Conduit UC4 design_v13 designer-0008): every add_node operation ended one '}' short, so
    `,{"op":...` and the final `]` of operations arrived while the operation object was still open. The '}' goes
    exactly where parsing stops (before that ',' or ']'), and only when closing that object leaves its array as the
    open container; nothing is moved or rewritten, and only a complete Designer action is accepted.
    """

    candidate = text
    inserted: list[int] = []
    for _ in range(limit + 1):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as error:
            if _innermost_open_container(candidate, error.pos) != "{" or error.pos >= len(candidate):
                return None
            if candidate[error.pos] == "]":
                at = error.pos
            elif candidate[error.pos] == "{":
                comma = candidate.rfind(",", 0, error.pos)
                if comma < 0 or candidate[comma + 1:error.pos].strip():
                    return None
                at = comma
            else:
                return None
            candidate = candidate[:at] + "}" + candidate[at:]
            if _innermost_open_container(candidate, at + 1) != "[":
                return None
            inserted.append(at)
            continue
        if not inserted or not isinstance(payload, dict):
            return None
        if not _designer_action_shape(payload, require_runtime_binding=require_runtime_binding):
            return None
        return payload, inserted
    return None


def _drop_trailing_commas(
    text: str, *, require_runtime_binding: bool, limit: int = 32
) -> tuple[dict[str, object], list[int]] | None:
    """Remove a ',' that directly precedes the '}' or ']' where parsing stops.

    Real DeepSeek slip (Conduit UC4 design_v14 designer-0008): the last add_node operation ended with `},` before the
    `]` of operations. Only whitespace may separate that comma from the closer; nothing else changes, and only a
    complete Designer action is accepted.
    """

    candidate = text
    removed: list[int] = []
    for _ in range(limit + 1):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as error:
            if error.pos >= len(candidate) or candidate[error.pos] not in "]}":
                return None
            before = candidate[: error.pos].rstrip()
            if not before.endswith(","):
                return None
            comma = len(before) - 1
            candidate = candidate[:comma] + candidate[comma + 1:]
            removed.append(comma)
            continue
        if not removed or not isinstance(payload, dict):
            return None
        if not _designer_action_shape(payload, require_runtime_binding=require_runtime_binding):
            return None
        return payload, removed
    return None


def _parse_designer_tool_action_text(
    raw: str,
    *,
    require_runtime_binding: bool,
    repair_closers: bool = False,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        payload = parse_first_json_object(raw, expected_keys={"action", "arguments"})
        end, trailing = _text_after_action_object(raw)
        rejoined = _rejoin_cut_summary(payload, trailing)
        if rejoined is not None:
            return rejoined, {"schema_version": "designer_output_normalization_v1", "kind": "rejoined_cut_summary"}
        if trailing:
            # design_v20 designer-0031/0049: text after a complete object (an extra closer that ended it early, or
            # "Wait ... producing the full delta now") was silently dropped; fail closed into the repair path.
            raise json.JSONDecodeError("Extra data after the action object", strip_code_fence(raw).strip(), end)
        return payload, {}
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
            if repair_closers:
                stripped = _remove_redundant_closers(text, require_runtime_binding=require_runtime_binding)
                if stripped is not None:
                    return stripped, {
                        "schema_version": "designer_output_normalization_v1",
                        "kind": "removed_redundant_closers",
                    }
                swapped = _swap_mismatched_closers(text, require_runtime_binding=require_runtime_binding)
                if swapped is not None:
                    return swapped, {
                        "schema_version": "designer_output_normalization_v1",
                        "kind": "swapped_mismatched_closers",
                    }
            closed = _repair_unbalanced_closers(text) if repair_closers else None
            if closed is not None:
                try:
                    payload = json.loads(closed)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and _designer_action_shape(
                    payload,
                    require_runtime_binding=require_runtime_binding,
                ):
                    return payload, {
                        "schema_version": "designer_output_normalization_v1",
                        "kind": "closed_unbalanced_brackets",
                    }
            closed_items = (
                _close_open_array_items(text, require_runtime_binding=require_runtime_binding) if repair_closers else None
            )
            if closed_items is not None:
                return closed_items[0], {
                    "schema_version": "designer_output_normalization_v1",
                    "kind": "closed_open_array_items",
                    "inserted_closer_positions": closed_items[1],
                }
            dropped = _drop_trailing_commas(text, require_runtime_binding=require_runtime_binding) if repair_closers else None
            if dropped is not None:
                return dropped[0], {
                    "schema_version": "designer_output_normalization_v1",
                    "kind": "dropped_trailing_commas",
                    "removed_comma_positions": dropped[1],
                }
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
    source_refs = set(
        _strings(_dict(session.get("source_observations")).get(active_component_id))
    )
    if repair_task is None:
        partition_key = str(session.get("active_partition_key") or "")
        # Chronological and append-only (no sliding window, pinned sources not moved first): each prompt of a
        # partition then extends the previous one, so DeepSeek's prefix cache covers every observation already sent.
        # The partition action budget bounds how many observations a partition adds.
        chronological: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in all_observations:
            if str(item.get("status") or "") == "operator_rejected":
                continue
            in_partition = (
                (not partition_key or str(item.get("partition_key") or "") == partition_key)
                and str(item.get("tool_id") or "") != "uiux.search_guidance"
            )
            # A new scenario starts without the reads of earlier scenarios (owner, 2026-11-24): carried reads made
            # design_v20 SC-2 draft whole deltas before noticing a file it still had to read (designer-0015, 0017).
            same_scenario = _partition_scenario(str(item.get("partition_key") or "")) == _partition_scenario(partition_key)
            if not in_partition and not (
                same_scenario and source_refs.intersection(_strings(item.get("evidence_refs")))
            ):
                continue
            fingerprint = canonical_json_hash(item)
            if fingerprint not in seen:
                seen.add(fingerprint)
                chronological.append(item)
        return chronological
    target_id = str(repair_task.get("target_id") or "")
    selected = _dicts(_dict(session.get("repair_observations")).get(target_id))
    repair_scenario_id = str(_dict(repair_task.get("partition")).get("scenario_id") or "")
    # A scenario repair sees what its own scenario read, on every component: pinning the component's reads of every
    # scenario sent design_v20 SC-3@web all 13 web batches (311k chars) but not SC-3's api/database reads.
    pinned = (
        _scenario_observations(session, repair_scenario_id)
        if repair_scenario_id
        else [
            item
            for item in all_observations
            if source_refs.intersection(_strings(item.get("evidence_refs")))
            and str(item.get("status") or "") != "operator_rejected"
        ]
    )
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


def _guidance_key(
    *,
    graph: dict[str, object],
    requirement_model: dict[str, object],
    partition_key: str,
) -> str:
    """UI contract v2 shares one guidance lane per feature so every partition designs the same screen."""

    if partition_key:
        return f"ui-feature|{_requirement_use_case_id(requirement_model)}"
    return partition_key


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
    ui_nodes = [
        _dict(operation.get("node"))
        for operation in _dicts(delta.get("operations"))
        if str(operation.get("op") or "") == "add_node" and str(_dict(operation.get("node")).get("surface") or "") == "ui"
    ]
    if ui_nodes and all(is_composition_wiring(node) for node in ui_nodes):
        return True
    for operation in _dicts(delta.get("operations")):
        if str(operation.get("op") or "") == "add_node":
            node = _dict(operation.get("node"))
            if str(node.get("surface") or "") != "ui":
                continue
            experience = _dict(_dict(node.get("surface_contract")).get("experience_contract"))
            if experience.get("schema_version") == UI_EXPERIENCE_CONTRACT_V2:
                return True
        if str(operation.get("op") or "") == "update_node":
            experience = _dict(_dict(_dict(operation.get("changes")).get("surface_contract")).get("experience_contract"))
            # A partial update keeps the node's schema_version (apply_designer_delta merges experience_contract).
            if experience and experience.get("schema_version", UI_EXPERIENCE_CONTRACT_V2) == UI_EXPERIENCE_CONTRACT_V2:
                return True
    return False


def _review_target_key(
    repair_task: dict[str, object] | None, active_partition: dict[str, object] | None
) -> str:
    if repair_task is not None:
        return f"repair:{repair_task.get('target_id') or ''}"
    partition = _dict(_dict(active_partition).get("partition"))
    return f"partition:{partition.get('scenario_id') or ''}@{partition.get('component_id') or ''}"


def _scoped_review_feedback(session: dict[str, object], *, current_target: str) -> str:
    """An operator note only applies to the target it reviewed; older notes have no target and still apply."""
    target = str(session.get("last_review_target") or "")
    if target and target != current_target:
        return ""
    return str(session.get("last_review_feedback") or "")


def _wiring_operation_templates(
    graph: dict[str, object],
    *,
    landscape: dict[str, object],
    scenario_id: str,
    component_id: str,
    owner_anchors: list[dict[str, object]],
    requirement_refs: list[str],
) -> list[dict[str, object]]:
    """Pre-filled composition_wiring modify nodes for each AS-IS hop on the screen's mount path."""

    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    components = _dict(context.get("components"))
    entry = str(_dict(context.get("entrypoint")).get("ref") or "")
    owner = owner_anchors[0] if owner_anchors else {}
    owner_key = str(owner.get("path") or "") + "#" + str(owner.get("symbol") or "")
    mounted = chain_path_to(context, owner_key) or ([entry] if entry else [])
    existing_targets = set()
    for node in _dicts(graph.get("nodes")):
        target = _dict(_dict(node.get("runtime_binding")).get("target"))
        existing_targets.add(str(target.get("path") or "") + "#" + str(target.get("symbol") or ""))
    anchors = _dicts(landscape.get("anchors"))
    templates: list[dict[str, object]] = []
    for index, hop in enumerate(mounted):
        if hop in existing_targets or hop not in components:
            continue
        path, _, symbol = hop.partition("#")
        evidence_ref = str(_dict(components.get(hop)).get("evidence_ref") or "")
        if index == 0:
            anchor = next(
                (item for item in anchors if item.get("kind") == "ui_entrypoint" and item.get("path") == path),
                {},
            )
            runtime_kind, relation = "entrypoint", "owned_by"
            owner_ref = str(anchor.get("anchor_id") or "")
            owner_path, owner_symbol = str(anchor.get("path") or path), str(anchor.get("symbol") or "")
        else:
            parent = mounted[index - 1]
            parent_path, _, parent_symbol = parent.partition("#")
            anchor = next(
                (item for item in anchors if item.get("path") == parent_path and item.get("symbol") == parent_symbol),
                {},
            )
            runtime_kind, relation = "ui_component", "rendered_by"
            owner_ref, owner_path, owner_symbol = str(anchor.get("anchor_id") or parent), parent_path, parent_symbol
        inherited = [
            {
                "name": name,
                "source": "existing_runtime",
                "binding": mounted[index - 1],
                "provided_by": mounted[index - 1],
                "requirement_refs": [],
                "states": [],
            }
            for name in (required_props(context, hop) if index > 0 else [])
        ]
        refs = [evidence_ref] if evidence_ref else []
        templates.append(
            {
                "op": "add_node",
                "node": {
                    "temp_ref": f"tmp:{scenario_id}:wire:{symbol}",
                    "semantic_key": f"{scenario_id}:{component_id}:wire:{symbol}",
                    "name": f"{symbol} forwards runtime inputs",
                    "kind": _kind_for_surface("ui"),
                    "surface": "ui",
                    "disposition": "modify",
                    "requirement_refs": requirement_refs,
                    "evidence_refs": refs,
                    "evidence_reason": "",
                    "contract": f"{symbol} forwards <input names> to the next component on composition_path.",
                    "surface_contract": {
                        "schema_version": SURFACE_CONTRACT_VERSION,
                        "surface": "ui",
                        "entrypoint": hop,
                        "states": ["mounted", "forwarding"],
                        "transitions": [
                            {
                                "from": "mounted",
                                "action": "receive <input names>",
                                "to": "forwarding",
                                "outcome": "next component receives <input names>",
                            }
                        ],
                        "validations": [],
                        "outcomes": ["next component receives <input names>"],
                        "runtime_surface": {
                            "schema_version": RUNTIME_SURFACE_BINDING_VERSION,
                            "role": "composition_wiring",
                            "screen_id": f"{scenario_id.casefold()}-wiring",
                            "entrypoint": entry,
                            "composition_path": mounted[: index + 1],
                            "required_inputs": inherited,
                            "forwarded_inputs": [],
                            "runnable_status": "bound",
                            "evidence_refs": refs,
                        },
                    },
                    "runtime_binding": {
                        "schema_version": RUNTIME_BINDING_VERSION,
                        "runtime_kind": runtime_kind,
                        "target": {"component_id": component_id, "path": path, "symbol": symbol},
                        "attachment": {
                            "relation": relation,
                            "owner_ref": owner_ref,
                            "owner_path": owner_path,
                            "owner_symbol": owner_symbol,
                        },
                        "evidence_refs": [ref for ref in (evidence_ref, owner_ref) if ref.startswith(("repo:", "RLA-"))],
                    },
                },
            }
        )
    return templates


def _runtime_surface_scaffold(
    graph: dict[str, object],
    *,
    owner_anchors: list[dict[str, object]],
) -> dict[str, object]:
    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    entry = str(_dict(context.get("entrypoint")).get("ref") or "<entrypoint path>#<symbol>")
    owner = owner_anchors[0] if owner_anchors else {}
    mounted = chain_path_to(context, f"{owner.get('path') or ''}#{owner.get('symbol') or ''}") or [entry]
    return {
        "schema_version": RUNTIME_SURFACE_BINDING_VERSION,
        "screen_id": "<stable screen id shared by every node on this screen>",
        "entrypoint": entry,
        "composition_path": [*mounted, "<runtime_binding.target path>#<symbol>"],
        "required_inputs": [
            {
                "name": "<input the screen needs at runtime; remove this item when none>",
                "source": "<existing_runtime|external_use_case|new_boundary|demo_fixture>",
                "binding": "<provider path#symbol | VDN-* | PRE-*/MF-*/AF-*/EF-* from dependency_refs | accepted D-*>",
                "provided_by": (
                    f"<hop before the target on composition_path that supplies it; {entry} for external_use_case; "
                    "never the target itself>"
                ),
                "requirement_refs": ["<ids from ui_runtime_context.dependency_refs when applicable>"],
                "states": ["loading", "ready", "empty", "error"],
            }
        ],
        "runnable_status": "<bound|externally_blocked|fixture_required>",
        "evidence_refs": [
            str(hop.get("evidence_ref"))
            for hop in _dicts(context.get("composition_chain"))
            if hop.get("ref") in mounted and hop.get("evidence_ref")
        ],
    }


def _experience_v2_fields(graph: dict[str, object], *, screen_id: str, fake_label: str = "") -> dict[str, object]:
    """Holistic UX groups of ui_experience_contract_v2; placeholders for the model, concrete values for fake mode."""

    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    has_stylesheet = bool(_strings(_dict(context.get("styles")).get("stylesheet_files")))
    if not fake_label:
        return {
            "screen": {
                "screen_id": screen_id,
                "purpose": "<user goal on this screen>",
                "entry_condition": "<how the user reaches this screen>",
            },
            "runtime_states": {
                state: {"presentation": f"<what the user sees while {state}>", "trigger": "<what causes it>"}
                for state in ("loading", "ready", "empty", "error", "success")
            },
            "visual_contract": {
                "hierarchy": ["<primary to secondary content order>"],
                "layout": [{"region": "<region name>", "layer": "<page|overlay>", "content": "<what the region shows>"}],
                "typography": ["<heading/body/label decisions>"],
                "color_and_tokens": ["<semantic color/token decisions>"],
                "focus_appearance": ["<visible focus indicator>"],
                "responsive_rules": ["<small viewport behavior>"],
                "style_target_policy": "<reuse_existing|new_stylesheet>",
                "style_target_reason": "<why, based on ui_runtime_context styles>",
            },
            "focus_policies": [
                {
                    "event": "<event that moves focus, or remove this item when none>",
                    "source_removed": False,
                    "removed_element": "<required when source_removed is true>",
                    "primary_target": "<element that receives focus>",
                    "fallback_target": "<element when primary is unavailable>",
                    "selection_strategy": "<deterministic rule choosing the target>",
                    "announcement": {"target": "<status/alert region>", "moves_focus": False},
                }
            ],
            "repo_evidence_refs": _strings(context.get("evidence_refs")),
        }
    return {
        "screen": {
            "screen_id": screen_id,
            "purpose": f"Complete {fake_label} from the application shell.",
            "entry_condition": "User opens the application.",
        },
        "runtime_states": {
            "loading": {"presentation": "Show a busy indicator in the content region.", "trigger": "Data request pending."},
            "ready": {"presentation": "Show the interactive content.", "trigger": "Data available."},
            "empty": {"not_applicable": True, "reason": "The fake scenario has no list data."},
            "error": {"presentation": "Show an actionable alert.", "trigger": "Request failed."},
            "success": {"presentation": "Announce the outcome in the status region.", "trigger": "Action completed."},
        },
        "visual_contract": {
            "hierarchy": ["Heading, then primary content, then status."],
            "layout": (
                [
                    {"region": "header", "layer": "page", "content": "screen title"},
                    {"region": "main", "layer": "page", "content": "primary content"},
                    {"region": "footer", "layer": "page", "content": "status region"},
                ]
            ),
            "typography": ["One h1 heading; body text for content."],
            "color_and_tokens": ["Semantic error and success colors."],
            "focus_appearance": ["Visible outline on focused controls."],
            "responsive_rules": ["Stack regions vertically below 600px."],
            "style_target_policy": "reuse_existing" if has_stylesheet else "new_stylesheet",
            "style_target_reason": "Follow the frozen web style convention.",
        },
        "focus_policies": [],
        "repo_evidence_refs": _strings(context.get("evidence_refs")),
    }


def _surface_contract_scaffold(surface: str) -> dict[str, object]:
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
        contract["experience_contract"] = {
            "schema_version": UI_EXPERIENCE_CONTRACT_V2,
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
            "authentication": "<identity/session mechanism and rationale>",
            "authentication_required": "<true|false from cross_cutting_conditions and repository evidence>",
            "authentication_source": "<existing_runtime|external_dependency|new_boundary|none>",
            "authentication_requirement_refs": [],
            "authentication_evidence_refs": [],
            "authorization": "<role/permission policy, separate from authentication>",
            "authorization_required": "<true|false from the approved Requirement>",
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
    route_modules: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    # A UI owner is the composition_path hop just before the target, and a business rule's owner is the code that
    # calls it; only the Designer knows either once it picks the target: a pre-filled guess (e.g. the app root)
    # contradicts that rule, so both keep placeholders.
    owner = owner_anchors[0] if owner_anchors and surface not in {"ui", "business_rule"} else {}
    if surface == "api" and route_modules:
        # An api route is owned by the file registering its sibling routes (whole-file anchor, empty symbol), not by
        # the app root that mounts the router (design_v20 0011/0012 reasoned at length past a createApp prefill).
        owner = (
            {"anchor_id": route_modules[0].get("anchor_id"), "path": route_modules[0].get("path"), "symbol": ""}
            if len(route_modules) == 1
            else {
                "anchor_id": "<anchor_id of the route_registration_modules entry whose routes this route joins>",
                "path": "<path of that entry>",
                "symbol": "",
            }
        )
    if surface == "ui":
        candidate_anchors = []
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
            "owner_ref": str(
                owner.get("anchor_id")
                or "<RLA-*, runtime-bound VDN-*, the owner_ref of a ui_runtime_context.composition_chain entry, "
                "or the temp_ref of a node this delta adds>"
            ),
            "owner_path": str(
                owner.get("path")
                or {
                    "ui": "<path of the composition_path hop just before the target>",
                    "business_rule": "<path of the code that calls this target>",
                }.get(surface, "<composition owner path>")
            ),
            "owner_symbol": str(
                owner["symbol"]
                if surface == "api" and "symbol" in owner  # whole-file route owner: empty symbol is the value
                else owner.get("symbol")
                or {"ui": "<symbol of that hop>", "business_rule": "<symbol of that caller>"}.get(
                    surface, "<composition owner symbol>"
                )
            ),
        },
        "evidence_refs": list(
            dict.fromkeys(
                [
                    *([str(owner.get("anchor_id"))] if owner and not str(owner.get("anchor_id")).startswith("<") else []),
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
        contract["experience_contract"] = {
            "schema_version": UI_EXPERIENCE_CONTRACT_V2,
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
            "authentication": "not required by the fake requirement",
            "authentication_required": False,
            "authentication_source": "none",
            "authentication_requirement_refs": [],
            "authentication_evidence_refs": [],
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


def _fake_runtime_surface(
    *,
    graph: dict[str, object],
    landscape: dict[str, object],
    binding: dict[str, object],
    scenario_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Mount the fake UI target on the AS-IS composition chain and re-point its owner to the parent hop."""

    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    entry = str(_dict(context.get("entrypoint")).get("ref") or "")
    target = _dict(binding.get("target"))
    target_ref = f"{target.get('path') or ''}#{target.get('symbol') or ''}"
    path = chain_path_to(context, target_ref)
    if len(path) < 2:
        root_children = next(
            (_strings(hop.get("renders")) for hop in _dicts(context.get("composition_chain")) if hop.get("ref") == entry),
            [],
        )
        path = [*([entry, root_children[0]] if root_children else [entry]), target_ref]
    owner_path, _, owner_symbol = path[-2].partition("#")
    owner_anchor = next(
        (
            item
            for item in _dicts(landscape.get("anchors"))
            if item.get("path") == owner_path and item.get("symbol") == owner_symbol
        ),
        {},
    )
    updated = {
        **binding,
        "attachment": {
            **_dict(binding.get("attachment")),
            "owner_ref": str(owner_anchor.get("anchor_id") or _dict(binding.get("attachment")).get("owner_ref") or ""),
            "owner_path": owner_path,
            "owner_symbol": owner_symbol,
        },
    }
    inputs = [
        {
            "name": name,
            "source": "existing_runtime",
            "binding": path[-2],
            "provided_by": path[-2],
            "requirement_refs": [],
            "states": ["ready"],
        }
        for name in required_props(context, target_ref)
    ]
    surface = {
        "schema_version": RUNTIME_SURFACE_BINDING_VERSION,
        "screen_id": f"screen-{scenario_id.casefold()}",
        "entrypoint": entry,
        "composition_path": path,
        "required_inputs": inputs,
        "runnable_status": "bound",
        "evidence_refs": _strings(context.get("evidence_refs")),
    }
    return surface, updated


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
    access_required = (
        provider.get("authentication_required") is True
        or provider.get("authorization_required") is True
    )
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
            if not access_required
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
                placements=_surface_placements(graph),
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
    placements = _surface_placements(graph)
    not_exercised = {
        (str(item.get("scenario_id") or ""), str(item.get("surface") or ""))
        for item in _dicts(graph.get("surface_not_exercised"))
    }
    placeable_component_ids = sorted(
        str(item.get("component_id") or "")
        for item in _dicts(landscape.get("components"))
        if _strings(item.get("roots"))
    )

    for scenario in _scenarios(requirement_model):
        scenario_id = str(scenario.get("id") or "")
        required = _required_surface_set(scenario)
        missing: list[str] = []
        for surface in surface_order:
            if surface not in required or (scenario_id, surface) in not_exercised:
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
            placements=placements,
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
                placements=placements,
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
            chain_refs = _ui_chain_refs(graph) if surface == "ui" else []
            component_anchor_ids = {
                ref for ref, item in anchors.items() if ref and str(item.get("component_id") or "") == component_id
            }
            matched_ids = {str(item.get("anchor_id") or "") for item in candidate_anchors}
            # A match that lists every anchor of the component does not discriminate, so cutting it to the top 3 is an
            # arbitrary hash-order pick (e.g. a DELETE route for an edit scenario): the Designer searches instead.
            # For UI, ui_runtime_context.composition_chain already lists every mounted component.
            if chain_refs or (
                len(candidate_anchors) > 3 and component_anchor_ids and matched_ids >= component_anchor_ids
            ):
                candidate_anchors = []
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
                chain_refs=chain_refs,
            )
            surface_work.append(
                {
                    "surface": surface,
                    "requirement_refs": requirement_refs,
                    "contract": surface_contract,
                    "candidate_anchors": candidate_anchors[:3],
                    "composition_owner_candidates": owner_anchors[:3],
                    **(
                        {"route_registration_modules": modules}
                        if surface == "api"
                        and (modules := _route_registration_modules(component_id=component_id, landscape=landscape))
                        else {}
                    ),
                    "shared_target_candidates": _shared_target_candidates(
                        scenario=scenario,
                        requirement_model=requirement_model,
                        graph=graph,
                        component_id=component_id,
                        surface=surface,
                    ),
                    "other_scenario_targets": _other_scenario_targets(
                        scenario_id=scenario_id,
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
                    "surface_contract": _surface_contract_scaffold(str(item["surface"])),
                    "runtime_binding": _runtime_binding_scaffold(
                        surface=str(item["surface"]),
                        component=component,
                        candidate_anchors=_dicts(item.get("candidate_anchors")),
                        owner_anchors=_dicts(item.get("composition_owner_candidates")),
                        route_modules=_dicts(item.get("route_registration_modules")),
                    ),
                },
            }
            for item in surface_work
        ]
        wiring_templates: list[dict[str, object]] = []
        for scaffold, item in zip(node_scaffolds, surface_work):
            if item["surface"] != "ui":
                continue
            wiring_templates.extend(
                _wiring_operation_templates(
                    graph,
                    landscape=landscape,
                    scenario_id=scenario_id,
                    component_id=component_id,
                    owner_anchors=_dicts(item.get("composition_owner_candidates")),
                    requirement_refs=_strings(item.get("requirement_refs")),
                )
            )
            contract = scaffold["node"]["surface_contract"]
            contract["runtime_surface"] = _runtime_surface_scaffold(
                graph,
                owner_anchors=_dicts(item.get("composition_owner_candidates")),
            )
            if isinstance(contract.get("experience_contract"), dict):
                contract["experience_contract"].update(
                    _experience_v2_fields(graph, screen_id=str(contract["runtime_surface"]["screen_id"]))
                )
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
        if branch_parent is not None:
            operations.append({"op": "add_edge", "edge": branch_parent["edge"]})
        # The scenario's surfaces designed by later partitions, so the Designer knows its terminal edge comes later.
        later: dict[str, list[str]] = {}
        for surface in missing:
            if surface not in grouped_surfaces:
                later_component = _component_for_missing_surface(
                    scenario_id=scenario_id,
                    surface=surface,
                    required_surfaces=required,
                    matches=matches,
                    anchors=anchors,
                    landscape=landscape,
                    placements=placements,
                )
                later.setdefault(later_component, []).append(surface)
        later_partitions = [{"component_id": key, "surfaces": value} for key, value in later.items()]
        # Where a business rule runs is only known from the code: show the default and let the Designer place it.
        placeable_surfaces: list[dict[str, object]] = []
        for surface in sorted(PLACEABLE_SURFACES):
            if surface not in missing:
                continue
            placed = next(
                (item for item in _dicts(graph.get("surface_placements"))
                 if item.get("scenario_id") == scenario_id and item.get("surface") == surface),
                None,
            )
            work = next((item for item in surface_work if item["surface"] == surface), None)
            if placed is not None:
                if work is not None:
                    work["placement"] = {key: placed.get(key) for key in ("component_id", "reason", "placed_in_partition")}
                continue
            default_component = _component_for_missing_surface(
                scenario_id=scenario_id,
                surface=surface,
                required_surfaces=required,
                matches=matches,
                anchors=anchors,
                landscape=landscape,
            )
            option = {
                "harness_routing_guess": default_component,
                "routing_guess_note": "guessed from the scenario's required surfaces, not a decision",
                "placeable_component_ids": placeable_component_ids,
            }
            if work is not None:
                work["placement"] = {
                    **option,
                    "if_placed_elsewhere": (
                        "drop business_rule from the delta_scaffold set_partition_coverage and completion_claims and "
                        "add no business_rule node"
                    ),
                }
            elif "ui" in grouped_surfaces:
                surface_contract = _dict(_dict(scenario.get("surfaces")).get(surface))
                placeable_surfaces.append(
                    {"surface": surface, "requirement_refs": _strings(surface_contract.get("requirement_refs")), **option}
                )
        return {
            "partition": partition,
            "component": component,
            "missing_surfaces": grouped_surfaces,
            "required_coverage": required_coverage,
            "surface_work": surface_work,
            "branch_parent_requirement": branch_parent,
            "base_graph_digest": graph.get("artifact_digest"),
            "delta_scaffold": {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": graph.get("artifact_digest"),
                "partition": partition,
                "operations": operations,
                "completion_claims": [{"scenario_id": scenario_id, "surfaces": grouped_surfaces}],
                "unresolved_items": [],
            },
            **({"harness_wiring_operations": wiring_templates} if wiring_templates else {}),
            **({"later_partitions": later_partitions} if later_partitions else {}),
            **({"placeable_surfaces": placeable_surfaces} if placeable_surfaces else {}),
        }
    return None


def _shared_target_candidates(
    *,
    scenario: dict[str, object],
    requirement_model: dict[str, object],
    graph: dict[str, object],
    component_id: str,
    surface: str,
) -> list[dict[str, object]]:
    """Expose main-scenario targets that a branch can share without changing target origin."""

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
            "candidate_disposition": item.get("disposition"),
            "target_origin": "planned" if str(item.get("disposition") or "") == "add" else "repository",
            "contract": item.get("contract"),
            "surface_contract": item.get("surface_contract"),
            "runtime_binding": item.get("runtime_binding"),
        }
        for item in sorted(candidates, key=lambda item: str(item.get("id") or ""))
    ]


def _other_scenario_targets(
    *,
    scenario_id: str,
    requirement_model: dict[str, object],
    graph: dict[str, object],
    component_id: str,
    surface: str,
) -> list[dict[str, object]]:
    """Point at other branch scenarios' nodes on this surface; design.inspect_partition shows them in full.

    shared_target_candidates covers main-scenario nodes only, so a branch decided from a graph.index name alone
    whether a sibling branch's rule makes the same decision (UC4 designer-0085 vs SC-9's title policy).
    """

    main_ids = {
        str(item.get("id") or "")
        for item in _scenarios(requirement_model)
        if str(item.get("kind") or "") == "main"
    }
    rows = [
        item
        for item in _dicts(graph.get("nodes"))
        if str(item.get("scenario_id") or "") not in {scenario_id, *main_ids}
        and str(item.get("component_id") or "") == component_id
        and str(item.get("surface") or "") == surface
        and _dict(item.get("runtime_binding"))
    ]
    return [
        {
            "node_id": item.get("id"),
            "scenario_id": item.get("scenario_id"),
            "name": item.get("name"),
            "target": "{path}#{symbol}".format(
                path=_dict(_dict(item.get("runtime_binding")).get("target")).get("path") or "",
                symbol=_dict(_dict(item.get("runtime_binding")).get("target")).get("symbol") or "",
            ),
        }
        for item in sorted(rows, key=lambda item: (str(item.get("scenario_id") or ""), str(item.get("id") or "")))
    ]


def _other_scenario_projections(
    *,
    target_nodes: list[dict[str, object]],
    findings: list[dict[str, object]],
    nodes: list[dict[str, object]],
    scenario_id: str,
) -> list[dict[str, object]]:
    """Read-only contracts of other scenarios' nodes a repair must stay compatible with.

    A node counts when it shares a runtime target with a writable node or a finding cites it (subject_ref, design
    evidence). The repair task listed only the target scenario's nodes, so UC4 H-r1 design_v25 designer-0005 repaired
    SC-1's articleCreatePrisma without seeing that SC-9's projection of the same adapter already disambiguates the
    title-derived slug, and left two same-title drafts colliding on the slug primary key.
    """

    def runtime_target(node: dict[str, object]) -> str:
        target = _dict(_dict(node.get("runtime_binding")).get("target"))
        path = str(target.get("path") or "")
        return f"{path}#{target.get('symbol') or ''}" if path else ""

    targets = {runtime_target(item) for item in target_nodes} - {""}
    cited = {
        ref
        for item in findings
        for ref in [
            str(item.get("subject_ref") or ""),
            *(
                str(_dict(evidence).get("ref") or "")
                for evidence in item.get("evidence") or []
                if str(_dict(evidence).get("source") or "") == "design"
            ),
        ]
    }
    rows = [
        item
        for item in nodes
        if str(item.get("scenario_id") or "") not in {"", scenario_id}
        and (runtime_target(item) in targets or str(item.get("id") or "") in cited)
    ]
    projections = []
    for item in sorted(rows, key=lambda node: (str(node.get("scenario_id") or ""), str(node.get("id") or ""))):
        surface_contract = {
            key: value for key, value in _dict(item.get("surface_contract")).items() if key != "experience_contract"
        }
        projections.append(
            {
                "id": item.get("id"),
                "scenario_id": item.get("scenario_id"),
                "component_id": item.get("component_id"),
                "name": item.get("name"),
                "runtime_target": runtime_target(item),
                "contract": item.get("contract"),
                "surface_contract": surface_contract,
            }
        )
    return projections


def _main_flow_entry_node(
    *,
    requirement_model: dict[str, object],
    graph: dict[str, object],
) -> dict[str, object] | None:
    nodes = _dicts(graph.get("nodes"))
    main_scenario_ids = [
        str(item.get("id") or "")
        for item in _scenarios(requirement_model)
        if str(item.get("kind") or "") == "main"
    ]
    main_id_set = set(main_scenario_ids)
    main_rank = {scenario_id: index for index, scenario_id in enumerate(main_scenario_ids)}
    entry_order: dict[str, int] = {}
    for edge in _dicts(graph.get("edges")):
        order = edge.get("order")
        source_id = str(edge.get("from") or "")
        if (
            isinstance(order, int)
            and str(edge.get("kind") or "") not in {"contains", "depends_on", "realized_by"}
            and str(edge.get("scenario_id") or "") in main_id_set
        ):
            entry_order[source_id] = min(order, entry_order.get(source_id, order))
    candidates = sorted(
        (
            item
            for item in nodes
            if str(item.get("scenario_id") or "") in main_id_set
            and str(item.get("id") or "")
            and not is_composition_wiring(item)
        ),
        key=lambda item: (
            main_rank.get(str(item.get("scenario_id") or ""), len(main_rank)),
            entry_order.get(str(item.get("id") or ""), 1 << 30),
            _surface_rank(str(item.get("surface") or "")),
            str(item.get("id") or ""),
        ),
    )
    return candidates[0] if candidates else None


def _branch_parent_scaffold(
    *,
    scenario: dict[str, object],
    requirement_model: dict[str, object],
    graph: dict[str, object],
    branch_entry_ref: str,
) -> dict[str, object] | None:
    """Seed the mandatory V2 branch-to-primary-main dependency in the first branch delta."""

    if str(scenario.get("kind") or "") not in {"alternative", "exception"}:
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
    # The parent is where the main flow starts: the source of its first ordered behavior edge, never wiring.
    parent = _main_flow_entry_node(requirement_model=requirement_model, graph=graph)
    if parent is None:
        return None
    parent_scenario_id = str(parent.get("scenario_id") or "")
    return {
        "branch_scenario_id": branch_id,
        "parent_scenario_id": parent_scenario_id,
        # design_v20 designer-0017/0020 found the edge to the main editor "odd" and once read it as breaking the path.
        "instruction": (
            "Keep this exact add_edge operation so the branch has one canonical parent. depends_on is structural: it "
            "records which main scenario this branch varies; it is not a user step and is not part of the ordered "
            "behavior path."
        ),
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
    requirement_model: dict[str, object] | None = None,
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
    edges_by_id = {str(item.get("id") or ""): item for item in _dicts(graph.get("edges"))}
    targets = _repair_work_targets(repair_feedback, nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)
    completed_target_ids = set(completed_targets)
    target = next(
        (
            item
            for item in targets
            if str(item.get("target_id") or "") not in completed_target_ids
            and set(_strings(item.get("depends_on"))) <= completed_target_ids
        ),
        None,
    )
    if not isinstance(target, dict):
        if any(str(item.get("target_id") or "") not in completed_target_ids for item in targets):
            raise HarnessError(
                "designer_repair_scope_invalid",
                "No structured repair target is ready; a required dependency has not completed.",
            )
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
        findings = []
        for item in repair_feedback:
            repair_step = next(
                (
                    step
                    for step in _dicts(item.get("repair_steps"))
                    if str(step.get("target_id") or "") == target_id
                ),
                None,
            )
            if repair_step is not None:
                findings.append(
                    {
                        **item,
                        "diagram_ids": _strings(repair_step.get("diagram_ids")),
                        "expected_change": str(repair_step.get("expected_change") or ""),
                        "repair_step": repair_step,
                    }
                )
                continue
            if item.get("repair_steps"):
                continue
            if target_id in {
                str(candidate.get("target_id") or "")
                for candidate in _repair_work_targets([item], nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)
            }:
                findings.append(item)
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
    remediation_mode = (
        "partition_rebuild"
        if any(str(item.get("remediation_mode") or "") == "partition_rebuild" for item in findings)
        else "local_patch"
    )
    scenario_edge_ids = [str(item.get("id") or "") for item in target_edges if str(item.get("id") or "")]
    removable_edge_ids = (
        [edge_id for edge_id in scenario_edge_ids if edge_id in subject_edge_ids]
        if remediation_mode == "local_patch" and finding_codes and finding_codes <= {"boundary_contract_mismatch"}
        else (
            [edge_id for edge_id in scenario_edge_ids if edge_id in repairable_parent_edges]
            if finding_codes and finding_codes <= {"branch_parent_ambiguous"}
            else scenario_edge_ids
        )
    )
    writable_node_ids = [str(item.get("id") or "") for item in target_nodes if str(item.get("id") or "")]
    read_only_nodes = [item for item in scenario_nodes if str(item.get("id") or "") not in set(writable_node_ids)]
    allowed_edge_endpoint_ids = [
        str(item.get("id") or "") for item in scenario_nodes if str(item.get("id") or "")
    ]
    if finding_codes and finding_codes <= {"branch_parent_missing", "branch_parent_invalid"}:
        parent = _main_flow_entry_node(requirement_model=requirement_model or {}, graph=graph)
        parent_id = str((parent or {}).get("id") or "")
        if parent_id and parent_id not in allowed_edge_endpoint_ids:
            allowed_edge_endpoint_ids.append(parent_id)
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
        "allowed_edge_endpoint_ids": allowed_edge_endpoint_ids,
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
        if remediation_mode == "local_patch" and finding_codes and finding_codes <= {"boundary_contract_mismatch"}
        else [],
        "allow_add_node": remediation_mode == "partition_rebuild" or bool(component_id and not target_nodes),
    }
    # The edges are sent once, as active_repair_task.edges_in_current_order (the name the prompt rules use).
    # Each writable node also lists its own scenario edges: UC4 H-r1 design_v23 designer-0021 removed the only two
    # edges of EditArticle without noticing, because it had to rebuild that fact from the whole edge list.
    graph_context = {
        "writable_nodes": [
            {
                **item,
                "scenario_edges": _node_scenario_edges(
                    item, edges=[*target_edges, *preserved_edges], nodes_by_id=nodes_by_id
                ),
            }
            for item in target_nodes
        ],
        "read_only_nodes": read_only_nodes,
        "preserved_edges": preserved_edges,
    }
    projections = _other_scenario_projections(
        target_nodes=target_nodes, findings=findings, nodes=nodes, scenario_id=scenario_id
    )
    if projections:
        graph_context["other_scenario_projections"] = projections
    shared_edge_only = (
        remediation_mode == "local_patch"
        and source != "inline_composition"
        and target_id != "global"
        and bool(findings)
        and all(
            _is_shared_edge_finding(item, nodes_by_id=nodes_by_id, edges_by_id=edges_by_id, target_id=target_id)
            for item in findings
        )
    )
    has_repair_steps = any(_dict(item.get("repair_step")) for item in findings)
    # The Validator's step text reaches the Designer once, as each finding's expected_change. design_v23 0017/0021 got
    # it five times plus "No blocking finding remains" and read it as the answer key.
    prompt_findings = [
        {key: value for key, value in item.items() if key not in {"repair_step", "repair_steps"}}
        for item in findings
    ]
    shared_edge_instruction = (
        " Every finding here is on an edge shared with another component of this scenario, and that component gets its "
        "own repair turn. Change this side only if the fix belongs here. If this side needs no change, submit a delta "
        "whose only operation is the delta_scaffold set_partition_coverage operation unchanged; the Harness records this "
        "side as unchanged and the Validator re-checks the edge afterwards."
        if shared_edge_only
        else ""
    )
    return {
        "source": source,
        "target_id": target_id,
        "remediation_mode": remediation_mode,
        "shared_edge_only": shared_edge_only,
        "partition": task_partition,
        "findings": prompt_findings,
        "graph_context": graph_context,
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
            if str(item.get("target_id") or "") not in completed_target_ids
        ],
        "instruction": (
            (
                "Rebuild this exact scenario/component partition because the Validator classified a local patch as "
                "insufficient. You may add, replace or remove nodes and interactions only inside write_scope. "
                if remediation_mode == "partition_rebuild"
                else "Repair every listed finding for this exact target. "
            )
            + (
                "Each finding's diagram_ids and expected_change are scoped to this target; prerequisite targets are "
                "already complete and later targets are deferred. "
                if has_repair_steps
                else ""
            )
            # The read-only, preserve, update_edge/order_edges and no-remove-and-re-add rules are in the prompt's repair
            # and delta rules already; repeated here they framed a repair as edit-and-reorder only (design_v23 0026/0027).
            + "Read related components when needed, but mutate only write_scope. "
            "A returns edge's boundary_contract.input_mapping repeats its paired calls/reads/writes edge exactly "
            "(consumer field -> provider field); only response_mapping and error_mapping flow provider -> consumer."
            + shared_edge_instruction
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
        scaffolded_edge_ids: set[str] = set()
        for finding in findings:
            edge_id = str(finding.get("subject_ref") or "")
            edge = edges_by_id.get(edge_id)
            if edge is None or edge_id in scaffolded_edge_ids:
                continue
            provider_id = str(edge.get("from") if str(edge.get("kind") or "") == "returns" else edge.get("to") or "")
            contract = _boundary_contract_repair_scaffold(
                edge=edge,
                provider=nodes_by_id.get(provider_id, {}),
                paired_input_mapping=_dicts(
                    _dict(finding.get("contract_diagnostics")).get("paired_input_mapping")
                ),
            )
            operations.append({"op": "update_edge", "edge_id": edge_id, "changes": {"boundary_contract": contract}})
            scaffolded_edge_ids.add(edge_id)
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
    # Other findings get no placeholder operation: the Designer chooses the nodes and edges the finding needs
    # (UC4 H-r1 design_v23 designer-0010 debated a node update the edge-only finding did not need).
    return {
        "schema_version": DELTA_VERSION,
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
    *,
    edge: dict[str, object],
    provider: dict[str, object],
    paired_input_mapping: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    current = _dict(edge.get("boundary_contract"))
    surface = _dict(provider.get("surface_contract"))
    if str(provider.get("surface") or "") != "api":
        return {
            "schema_version": BOUNDARY_CONTRACT_VERSION,
            "consumer_operation": str(current.get("consumer_operation") or edge.get("label") or "<specific caller operation>"),
            "provider_operation": str(current.get("provider_operation") or "<specific provider operation>"),
            "input_mapping": paired_input_mapping
            or _dicts(current.get("input_mapping"))
            or [{"from": "<consumer field>", "to": "<provider field>", "required": True}],
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
    authorization = str(surface.get("authentication") or surface.get("authorization") or "").strip()
    auth_required = (
        surface.get("authentication_required") is True
        or surface.get("authorization_required") is True
    )
    responses = _dicts(surface.get("responses"))
    success = [item for item in responses if not str(item.get("status") or "").startswith(("4", "5"))]
    errors = [item for item in responses if str(item.get("status") or "").startswith(("4", "5"))]
    return {
        "schema_version": BOUNDARY_CONTRACT_VERSION,
        "consumer_operation": str(current.get("consumer_operation") or edge.get("label") or "consume API outcome"),
        "provider_operation": f"{str(surface.get('method') or '').upper()} {str(surface.get('path') or '')}".strip(),
        "input_mapping": paired_input_mapping
        or [current_inputs.get(name, {"from": name, "to": name, "required": True}) for name in fields],
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


def _accepts_shared_edge_no_change(
    exc: HarnessError, *, tool_id: str, repair_task: dict[str, object] | None
) -> bool:
    """A no-op delta closes a repair turn only when every finding is shared with another component's turn."""

    return (
        exc.error_code == "designer_delta_no_effect"
        and tool_id == "design.propose_delta"
        and repair_task is not None
        and bool(repair_task.get("shared_edge_only"))
    )


def _shared_edge_endpoints(
    edge: dict[str, object], *, nodes_by_id: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Endpoint nodes of an edge that belong to the edge's own scenario; other scenarios stay read-only."""

    endpoints = [nodes_by_id.get(str(edge.get(key) or "")) for key in ("from", "to")]
    scenario_id = str(edge.get("scenario_id") or "") or str((endpoints[0] or {}).get("scenario_id") or "")
    return [item for item in endpoints if item is not None and str(item.get("scenario_id") or "") == scenario_id]


def _is_shared_edge_finding(
    finding: dict[str, object],
    *,
    nodes_by_id: dict[str, dict[str, object]],
    edges_by_id: dict[str, dict[str, object]],
    target_id: str,
) -> bool:
    """True only when the edge's other end really gets its own repair turn for this finding."""

    edge = edges_by_id.get(str(finding.get("subject_ref") or ""))
    if edge is None:
        return False
    endpoints = _shared_edge_endpoints(edge, nodes_by_id=nodes_by_id)
    if len({str(item.get("component_id") or "") for item in endpoints}) <= 1:
        return False
    steps = _dicts(finding.get("repair_steps"))
    if not steps:
        # Without structured steps _repair_work_targets gives every endpoint partition its own target.
        return True
    # Adjudication may route a cross-component finding to one side only (UC4 H-r1 design_v23 SC-2@api, SC-3@web):
    # then nobody else repairs it and "that component gets its own repair turn" would be false.
    other_ends = {
        f"{item.get('scenario_id') or ''}@{item.get('component_id') or ''}" for item in endpoints
    } - {target_id}
    return bool(other_ends & {str(step.get("target_id") or "") for step in steps})


def _node_scenario_edges(
    node: dict[str, object],
    *,
    edges: list[dict[str, object]],
    nodes_by_id: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """The edges of the repair scenario that start or end at this node, in scenario order."""

    node_id = str(node.get("id") or "")
    result: list[dict[str, object]] = []
    for edge in edges:
        source, target = str(edge.get("from") or ""), str(edge.get("to") or "")
        if node_id not in {source, target}:
            continue
        peer_id = target if source == node_id else source
        result.append(
            {
                "edge_id": str(edge.get("id") or ""),
                "order": edge.get("order"),
                "kind": str(edge.get("kind") or ""),
                "direction": "outgoing" if source == node_id else "incoming",
                "peer_id": peer_id,
                "peer_name": str(nodes_by_id.get(peer_id, {}).get("name") or ""),
            }
        )
    return sorted(result, key=lambda item: item["order"] if isinstance(item["order"], int) else float("inf"))


def _repair_work_targets(
    repair_feedback: list[dict[str, object]],
    *,
    nodes_by_id: dict[str, dict[str, object]],
    edges_by_id: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Split node- and edge-scoped bundle repairs by the graph's mutation partition."""

    targets: list[dict[str, object]] = []
    seen: set[str] = set()

    def add_target(*, target_id: str, scenario_id: str, component_id: str, depends_on: list[str] | None = None) -> None:
        dependencies = depends_on or []
        if target_id in seen:
            existing = next(item for item in targets if str(item.get("target_id") or "") == target_id)
            existing["depends_on"] = list(
                dict.fromkeys([*_strings(existing.get("depends_on")), *dependencies])
            )
            return
        seen.add(target_id)
        targets.append(
            {
                "target_id": target_id,
                "scenario_id": scenario_id,
                "component_id": component_id,
                "depends_on": dependencies,
            }
        )

    for finding in repair_feedback:
        repair_steps = _dicts(finding.get("repair_steps"))
        if repair_steps:
            for step in repair_steps:
                target_id = str(step.get("target_id") or "")
                if "@" not in target_id:
                    raise HarnessError(
                        "designer_repair_scope_invalid",
                        f"Structured repair target {target_id!r} is not scenario@component.",
                    )
                scenario_id, component_id = target_id.split("@", 1)
                add_target(
                    target_id=target_id,
                    scenario_id=scenario_id,
                    component_id=component_id,
                    depends_on=_strings(step.get("depends_on")),
                )
            continue
        partition = _dict(finding.get("partition"))
        scenario_id = str(partition.get("scenario_id") or "")
        component_id = str(partition.get("component_id") or "")
        if scenario_id and component_id:
            target_id = f"{scenario_id}@{component_id}"
            add_target(target_id=target_id, scenario_id=scenario_id, component_id=component_id)
            continue
        subject_ref = str(finding.get("subject_ref") or "")
        mutation_refs = [
            item
            for item in _strings(finding.get("diagram_ids"))
            if item in nodes_by_id or item in (edges_by_id or {})
        ]
        # Semantic findings declare their typed mutation scope in diagram_ids. Older deterministic findings fall back
        # to subject_ref, which is already mechanically scoped by the Harness.
        semantic_scope = mutation_refs if str(finding.get("origin") or "") == "semantic" else []
        scoped_refs = semantic_scope or [subject_ref]
        scoped_nodes: list[dict[str, object]] = []
        for ref in scoped_refs:
            node = nodes_by_id.get(ref)
            if node is None and not semantic_scope:
                node = next((item for node_id, item in nodes_by_id.items() if node_id and node_id in ref), None)
            edge = (edges_by_id or {}).get(ref) if node is None else None
            endpoints = _shared_edge_endpoints(edge, nodes_by_id=nodes_by_id) if edge is not None else []
            for endpoint in endpoints or ([node] if node is not None else []):
                if endpoint not in scoped_nodes:
                    scoped_nodes.append(endpoint)
        for scoped_node in scoped_nodes:
            scenario_id = str(scoped_node.get("scenario_id") or "")
            component_id = str(scoped_node.get("component_id") or "")
            target_id = f"{scenario_id}@{component_id}"
            add_target(target_id=target_id, scenario_id=scenario_id, component_id=component_id)
        if scoped_nodes:
            continue
        finding_targets = _finding_repair_targets(finding)
        for scenario_id in finding_targets:
            add_target(target_id=scenario_id, scenario_id=scenario_id, component_id="")
    if not targets:
        return [{"target_id": "global", "scenario_id": "", "component_id": "", "depends_on": []}]

    ordered: list[dict[str, object]] = []
    completed: set[str] = set()
    remaining = {str(item.get("target_id") or ""): item for item in targets}
    while remaining:
        ready = [
            target_id
            for target_id, item in remaining.items()
            if set(_strings(item.get("depends_on"))) <= completed
        ]
        if not ready:
            raise HarnessError("designer_repair_scope_invalid", "Structured repair target dependencies contain a cycle.")
        for target_id in ready:
            ordered.append(remaining.pop(target_id))
            completed.add(target_id)
    return ordered


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
    } | {
        # A surface declared not exercised needs no node: its required_surface_missing feedback no longer applies.
        (str(item.get("scenario_id") or "").casefold(), str(item.get("surface") or "").casefold())
        for item in _dicts(graph.get("surface_not_exercised"))
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
    edges_by_id = {str(item.get("id") or ""): item for item in _dicts(graph.get("edges"))}
    return any(
        target_id
        in {
            str(candidate.get("target_id") or "")
            for candidate in _repair_work_targets([finding], nodes_by_id=nodes_by_id, edges_by_id=edges_by_id)
        }
        for finding in findings
    )


def _wiring_hop_inputs(arguments: object, graph: dict[str, object]) -> dict[str, list[str]]:
    """Inputs each AS-IS hop between provided_by and the screen must forward, for hops no earlier partition modeled."""

    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    chain = {str(hop.get("ref") or ""): hop for hop in _dicts(context.get("composition_chain"))}
    nodes = [_dict(op.get("node")) for op in _dicts(_dict(_dict(arguments).get("delta")).get("operations"))]
    modeled = {
        f"{target.get('path') or ''}#{target.get('symbol') or ''}"
        for node in _dicts(graph.get("nodes"))
        if str(node.get("disposition") or "") in {"modify", "add"} and is_composition_wiring(node)
        for target in [_dict(_dict(node.get("runtime_binding")).get("target"))]
    }
    needed: dict[str, list[str]] = {}
    for node in nodes:
        surface = _dict(_dict(node.get("surface_contract")).get("runtime_surface"))
        if str(node.get("surface") or "") != "ui" or str(surface.get("role") or "") == "composition_wiring":
            continue
        path = _strings(surface.get("composition_path"))
        for item in _dicts(surface.get("required_inputs")):
            name, provided_by = str(item.get("name") or ""), str(item.get("provided_by") or "")
            if provided_by not in path[:-1]:
                continue  # ui_required_input_unbound reports this case
            for index in range(path.index(provided_by), len(path) - 1):
                hop, child = path[index], path[index + 1]
                passed = _strings(_dict(_dict(chain.get(hop)).get("passes_props")).get(child))
                if hop in chain and name not in passed and hop not in modeled and name not in needed.get(hop, []):
                    needed.setdefault(hop, []).append(name)
    return needed


def _normalize_external_inputs(arguments: object) -> tuple[object, list[str]]:
    """Canonicalize mechanical external-input fields before review and persistence.

    Data from another use case enters at composition_path[0]. Its approved dependency binding is also a trace ref.
    """

    changed = False
    normalized: list[str] = []
    result = json.loads(json.dumps(arguments, ensure_ascii=False))
    # Walk the copied JSON directly: _dict/_dicts return copies, and this function edits in place.
    operations = (result.get("delta") or {}).get("operations") if isinstance(result, dict) else None
    for op in operations if isinstance(operations, list) else []:
        node = op.get("node") if isinstance(op, dict) else None
        surface = ((node or {}).get("surface_contract") or {}).get("runtime_surface") if isinstance(node, dict) else None
        if not isinstance(surface, dict) or str(node.get("surface") or "") != "ui":
            continue
        path = _strings(surface.get("composition_path"))
        inputs = surface.get("required_inputs")
        if str(surface.get("role") or "") == "composition_wiring" or len(path) < 2 or not isinstance(inputs, list):
            continue
        for item in inputs:
            if not isinstance(item, dict) or str(item.get("source") or "") != "external_use_case":
                continue
            name = str(item.get("name") or "external_input")
            item_changed = False
            if str(item.get("provided_by") or "") != path[0]:
                item["provided_by"] = path[0]
                item_changed = True
                changed = True
            binding = str(item.get("binding") or "")
            raw_refs = item.get("requirement_refs", [])
            refs = [str(ref) for ref in raw_refs if str(ref)] if isinstance(raw_refs, list) else []
            if binding.startswith(EXTERNAL_DEPENDENCY_REF_PREFIXES) and binding not in refs:
                item["requirement_refs"] = [*refs, binding]
                item_changed = True
                changed = True
            if item_changed:
                normalized.append(name)
    return (result if changed else arguments), normalized


def _normalize_external_providers(arguments: object) -> object:
    """Compatibility wrapper for callers that only need the normalized arguments."""

    normalized, _ = _normalize_external_inputs(arguments)
    return normalized


def _materialize_delta_wiring(
    *,
    arguments: object,
    active_partition: dict[str, object],
    graph: dict[str, object],
    run_id: str,
) -> object:
    """Materialize every mechanical composition-wiring node and edge idempotently."""

    normalized = _normalize_external_providers(arguments)
    needed = _wiring_hop_inputs(normalized, graph)
    delta = dict(_dict(_dict(normalized).get("delta")))
    operations = list(_dicts(delta.get("operations")))
    changed = normalized is not arguments
    if needed:
        templates = {
            str(_dict(_dict(op.get("node")).get("surface_contract")).get("entrypoint") or ""): op
            for op in _dicts(active_partition.get("harness_wiring_operations"))
        }
        unwired = [hop for hop in needed if hop not in templates]
        if unwired:
            raise HarnessError(
                "designer_delta_invalid",
                "Required inputs pass through hops the Harness cannot wire: "
                + "; ".join(f"{hop} does not pass {', '.join(needed[hop])}" for hop in unwired)
                + ". Bind each input to a provided_by hop on composition_path whose path down to the screen is AS-IS.",
                run_id=run_id,
            )
        authored: dict[str, int] = {}
        for index, op in enumerate(operations):
            node = _dict(op.get("node"))
            target = _dict(_dict(node.get("runtime_binding")).get("target"))
            ref = f"{target.get('path') or ''}#{target.get('symbol') or ''}"
            if op.get("op") == "add_node" and ref in needed:
                authored[ref] = index
        inserted = []
        for hop, names in needed.items():
            filled = json.loads(
                json.dumps(templates[hop], ensure_ascii=False).replace(
                    "<input names>", json.dumps(", ".join(names))[1:-1]
                )
            )
            if hop in authored:
                original = _dict(operations[authored[hop]].get("node"))
                filled["node"]["temp_ref"] = str(original.get("temp_ref") or filled["node"]["temp_ref"])
                operations[authored[hop]] = filled
            else:
                inserted.append(filled)
        # Wiring nodes go before every Designer-authored node/edge that may reference their temp_ref.
        head = 1 if operations and operations[0].get("op") == "set_partition_coverage" else 0
        operations = [*operations[:head], *inserted, *operations[head:]]
        changed = True

    scenario_id = str(
        _dict(active_partition.get("partition")).get("scenario_id")
        or _dict(delta.get("partition")).get("scenario_id")
        or ""
    )
    operations, edge_count = _materialize_composition_wiring_edges(
        operations=operations,
        graph=graph,
        scenario_id=scenario_id,
    )
    if not changed and edge_count == 0:
        return arguments
    delta["operations"] = operations
    return {**_dict(normalized), "delta": delta}


def _materialize_composition_wiring_edges(
    *,
    operations: list[dict[str, object]],
    graph: dict[str, object],
    scenario_id: str,
) -> tuple[list[dict[str, object]], int]:
    """Connect consecutive Harness-owned wiring hops from each declared composition path."""

    refs: dict[str, str] = {}
    wiring_refs: set[str] = set()
    screens: list[dict[str, object]] = []

    def remember(node: dict[str, object], endpoint: str) -> None:
        target = _dict(_dict(node.get("runtime_binding")).get("target"))
        ref = f"{target.get('path') or ''}#{target.get('symbol') or ''}"
        if ref == "#" or not endpoint:
            return
        refs[ref] = endpoint
        if is_composition_wiring(node):
            wiring_refs.add(ref)
        elif str(node.get("surface") or "") == "ui":
            screens.append(node)

    for node in _dicts(graph.get("nodes")):
        if scenario_id and str(node.get("scenario_id") or "") != scenario_id:
            continue
        remember(node, str(node.get("id") or ""))
    for operation in operations:
        if str(operation.get("op") or "") != "add_node":
            continue
        node = _dict(operation.get("node"))
        remember(node, str(node.get("temp_ref") or ""))

    connected = {
        (str(edge.get("from") or ""), str(edge.get("to") or ""))
        for edge in [*_dicts(graph.get("edges")), *(_dict(op.get("edge")) for op in operations if op.get("op") == "add_edge")]
        if str(edge.get("kind") or "") == "calls"
    }
    additions: list[dict[str, object]] = []
    for screen in screens:
        path = _strings(_dict(_dict(screen.get("surface_contract")).get("runtime_surface")).get("composition_path"))
        for parent, child in zip(path, path[1:]):
            if parent not in wiring_refs or child not in wiring_refs:
                continue
            source, target = refs.get(parent, ""), refs.get(child, "")
            if not source or not target or (source, target) in connected:
                continue
            additions.append(
                {
                    "op": "add_edge",
                    "edge": {
                        "from": source,
                        "to": target,
                        "kind": "calls",
                        "label": f"{parent.rsplit('#', 1)[-1]} forwards runtime inputs to {child.rsplit('#', 1)[-1]}",
                        "scenario_id": scenario_id,
                        "terminal": False,
                    },
                }
            )
            connected.add((source, target))
    if not additions:
        return operations, 0
    first_edge = next((index for index, op in enumerate(operations) if op.get("op") == "add_edge"), len(operations))
    return [*operations[:first_edge], *additions, *operations[first_edge:]], len(additions)


def _materialize_branch_parent(
    *,
    arguments: object,
    active_partition: dict[str, object],
    requirement_model: dict[str, object],
    graph: dict[str, object],
) -> object:
    requirement = _dict(active_partition.get("branch_parent_requirement"))
    scaffold_edge = _dict(requirement.get("edge"))
    branch_scenario_id = str(requirement.get("branch_scenario_id") or "")
    if not scaffold_edge or not branch_scenario_id:
        return arguments
    delta = _dict(_dict(arguments).get("delta"))
    operations = _dicts(delta.get("operations"))
    branch_entry_ref = str(scaffold_edge.get("from") or "")
    added_temp_refs = {
        str(_dict(op.get("node")).get("temp_ref") or "")
        for op in operations
        if str(op.get("op") or "") == "add_node"
    }
    if branch_entry_ref not in added_temp_refs:
        return arguments
    branch_node_ids = {
        str(node.get("id") or "")
        for node in _dicts(graph.get("nodes"))
        if str(node.get("scenario_id") or "") == branch_scenario_id
    }
    branch_refs = branch_node_ids | added_temp_refs
    main_scenario_ids = {
        str(scenario.get("id") or "")
        for scenario in _scenarios(requirement_model)
        if str(scenario.get("kind") or "") == "main"
    }
    main_node_ids = {
        str(node.get("id") or "")
        for node in _dicts(graph.get("nodes"))
        if str(node.get("scenario_id") or "") in main_scenario_ids
    }
    if any(
        str(edge.get("kind") or "") == "depends_on"
        and str(edge.get("from") or "") in branch_refs
        and str(edge.get("to") or "") in main_node_ids
        for op in operations
        for edge in [_dict(op.get("edge"))]
        if str(op.get("op") or "") == "add_edge"
    ):
        return arguments
    result = deepcopy(_dict(arguments))
    result_delta = _dict(result.get("delta"))
    result_delta["operations"] = [
        *_dicts(result_delta.get("operations")),
        {"op": "add_edge", "edge": deepcopy(scaffold_edge)},
    ]
    result["delta"] = result_delta
    return result


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
        if op in {"update_edge", "remove_edge"}:
            action = "update" if op == "update_edge" else "remove"
            edge_id = str(operation.get("edge_id") or "")
            if edge_id not in removable_edges:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"Repair cannot {action} edge outside write_scope: {edge_id}.",
                    run_id=run_id,
                )
            if op == "update_edge" and restricted_pairs and _dict(operation.get("changes")).keys() - {"boundary_contract"}:
                raise HarnessError(
                    "designer_delta_invalid",
                    "Boundary repair may only change boundary_contract with update_edge.",
                    run_id=run_id,
                )
            continue
        if op == "order_edges":
            # Reordering moves edges as remove + re-add would, so a repair may only order edges it could remove.
            outside = sorted(
                ref
                for ref in _strings(operation.get("edges"))
                if ref.startswith("VDE-") and ref not in removable_edges
            )
            if outside:
                raise HarnessError(
                    "designer_delta_invalid",
                    f"Repair cannot reorder edges outside write_scope: {outside}.",
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
                    "Boundary repair may only edit a listed target edge's boundary_contract. Use the update_edge "
                    "operation from active_repair_task.delta_scaffold. A remove_edge + add_edge replacement must "
                    "preserve the listed edge's from, to, kind, and label.",
                    run_id=run_id,
                )


def _reject_stranded_scenario_nodes(
    *,
    arguments: object,
    repair_task: dict[str, object],
    graph: dict[str, object],
    run_id: str,
) -> None:
    """A repair may not leave a node that takes part in its scenario's messages with no message left there.

    UC4 H-r1 design_v23 designer-0021 removed EditArticle's only two SC-3 edges on the Validator's hint; every gate
    passed. Only calls/reads/writes/returns count, and only a node that had one before the delta, so nodes that never
    had messages (a schema node with only depends_on) are not affected. A Harness-built exact scaffold is exempt: the
    runtime_boundary_unreachable removals leave the surface for the next construction partition on purpose.
    """

    delta = _dict(_dict(arguments).get("delta"))
    scenario_id = str(_dict(repair_task.get("write_scope")).get("scenario_id") or "")
    if not scenario_id or delta == repair_task.get("delta_scaffold"):
        return
    operations = _dicts(delta.get("operations"))
    removed_edges = {
        str(item.get("edge_id") or "") for item in operations if str(item.get("op") or "") == "remove_edge"
    }
    removed_nodes = {
        str(item.get("node_id") or "") for item in operations if str(item.get("op") or "") == "remove_node"
    }

    def is_message(edge: dict[str, object], default_scenario: str = "") -> bool:
        return (
            str(edge.get("scenario_id") or default_scenario) == scenario_id
            and str(edge.get("kind") or "") not in STRUCTURAL_EDGE_KINDS
        )

    lost: dict[str, list[str]] = {}
    kept: set[str] = set()
    for edge in _dicts(graph.get("edges")):
        if not is_message(edge):
            continue
        ends = {str(edge.get("from") or ""), str(edge.get("to") or "")}
        if str(edge.get("id") or "") in removed_edges or ends & removed_nodes:
            for node_id in ends:
                lost.setdefault(node_id, []).append(str(edge.get("id") or ""))
        else:
            kept |= ends
    delta_scenario = str(_dict(delta.get("partition")).get("scenario_id") or "")
    for item in operations:
        edge = _dict(item.get("edge"))
        if str(item.get("op") or "") == "add_edge" and is_message(edge, delta_scenario):
            kept |= {str(edge.get("from") or ""), str(edge.get("to") or "")}
    names = {str(item.get("id") or ""): str(item.get("name") or "") for item in _dicts(graph.get("nodes"))}
    stranded = [node_id for node_id in lost if node_id and node_id not in kept and node_id not in removed_nodes]
    if stranded:
        details = "; ".join(f"{node_id} ({names.get(node_id) or '?'}) loses {lost[node_id]}" for node_id in stranded)
        raise HarnessError(
            "designer_delta_invalid",
            f"Repair delta leaves {scenario_id} node(s) with no calls/reads/writes/returns edge in the scenario: "
            f"{details}. Give each node the {scenario_id} messages it really sends or receives, or remove_node it "
            "when it no longer takes part in the scenario.",
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


def _reject_unbound_new_nodes(arguments: object, *, run_id: str) -> None:
    """The completion rule (runtime_binding_missing) applied when the node is proposed, while its partition is active."""

    for operation in _dicts(_dict(_dict(arguments).get("delta")).get("operations")):
        if str(operation.get("op") or "") != "add_node":
            continue
        node = _dict(operation.get("node"))
        if str(node.get("kind") or "") == "interaction_step" or _dict(node.get("runtime_binding")):
            continue
        raise HarnessError(
            "designer_delta_invalid",
            f"Node {node.get('temp_ref') or node.get('semantic_key') or '<unnamed>'} has no runtime_binding; every node "
            "except an interaction_step realized by a runtime-bound node needs runtime_binding_v1 (start from the "
            "delta_scaffold one).",
            run_id=run_id,
        )


def _surface_placements(graph: dict[str, object]) -> dict[tuple[str, str], str]:
    return {
        (str(item.get("scenario_id") or ""), str(item.get("surface") or "")): str(item.get("component_id") or "")
        for item in _dicts(graph.get("surface_placements"))
    }


def _validate_surface_placement_ops(
    arguments: object,
    *,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    run_id: str,
) -> object:
    """place_surface / not_exercised may only name a surface the Requirement requires (and place_surface a component
    the landscape owns). Returns the arguments with each not_exercised op carrying that surface's Requirement ids."""

    delta = _dict(_dict(arguments).get("delta"))
    scenario_id = str(_dict(delta.get("partition")).get("scenario_id") or "")
    required = _required_surfaces(requirement_model).get(scenario_id, set())
    scenario = next((item for item in _scenarios(requirement_model) if str(item.get("id") or "") == scenario_id), {})
    components = {
        str(item.get("component_id") or "")
        for item in _dicts(landscape.get("components"))
        if _strings(item.get("roots"))
    }
    operations = _dicts(delta.get("operations"))
    changed = False
    for index, operation in enumerate(operations):
        op = str(operation.get("op") or "")
        if op not in {"place_surface", "not_exercised"}:
            continue
        surface = str(operation.get("surface") or "")
        component_id = str(operation.get("component_id") or "")
        if surface not in required:
            raise HarnessError(
                "designer_delta_invalid",
                f"{op} {surface or '<missing>'}: the approved Requirement does not require it for {scenario_id}.",
                run_id=run_id,
            )
        if op == "not_exercised":
            refs = _strings(_dict(_dict(scenario.get("surfaces")).get(surface)).get("requirement_refs"))
            operations[index] = {**operation, "requirement_refs": refs}
            changed = True
            continue
        if component_id not in components:
            raise HarnessError(
                "designer_delta_invalid",
                f"place_surface component_id {component_id or '<missing>'} is not a landscape component; use one of "
                f"{', '.join(sorted(components))}.",
                run_id=run_id,
            )
    if not changed:
        return arguments
    return {**_dict(arguments), "delta": {**delta, "operations": operations}}


def _component_for_missing_surface(
    *,
    scenario_id: str,
    surface: str,
    required_surfaces: set[str] | None = None,
    matches: dict[tuple[str, str], list[str]],
    anchors: dict[str, dict[str, object]],
    landscape: dict[str, object],
    placements: dict[tuple[str, str], str] | None = None,
) -> str:
    # The Designer's place_surface (from repository evidence) overrides the default routing below.
    placed = (placements or {}).get((scenario_id, surface))
    if placed:
        return placed
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
    chain_refs: list[str] | None = None,
) -> list[dict[str, object]]:
    """Prefer real composition roots over feature/demo anchors used only as evidence.

    For UI, components on ui_repo_context's composition chain come first in chain order (entrypoint first), so the
    scaffolds that mount from the first owner keep starting at the entrypoint.
    """

    chain_position = {ref: index for index, ref in enumerate(chain_refs or [])}

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

    def rank(item: dict[str, object]) -> tuple[int, int, int, int, str]:
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
        on_chain = chain_position.get(_anchor_ref(item), len(chain_position))
        return on_chain, incidental, composition_hint, kind_rank, str(item.get("anchor_id") or "")

    return sorted(anchors, key=rank)


def _route_registration_modules(*, component_id: str, landscape: dict[str, object]) -> list[dict[str, object]]:
    """The files that register the component's api routes, each with its file anchor and the routes it registers.

    A new route is registered next to its sibling routes, not by the app root that mounts their router; without these
    the only offered api owner was the app root (design_v20 designer-0008 mixed that root's anchor with the router
    path it had read), and Phase 3 plans the owner as code to change."""

    anchors = [item for item in _dicts(landscape.get("anchors")) if str(item.get("component_id") or "") == component_id]
    file_anchors = {
        str(item.get("path") or ""): str(item.get("anchor_id") or "")
        for item in anchors
        if str(item.get("kind") or "") == "source_file" and str(item.get("anchor_id") or "")
    }
    routes: dict[str, list[str]] = {}
    for item in anchors:
        path = str(item.get("path") or "")
        if str(item.get("kind") or "") == "api_route" and path in file_anchors:
            routes.setdefault(path, []).append(str(item.get("symbol") or ""))
    return [
        {"anchor_id": file_anchors[path], "path": path, "routes": sorted(route for route in routes[path] if route)}
        for path in sorted(routes)
    ]


def _anchor_ref(anchor: dict[str, object]) -> str:
    return f"{anchor.get('path') or ''}#{anchor.get('symbol') or ''}"


def _ui_chain_refs(graph: dict[str, object]) -> list[str]:
    """Entrypoint, then every mounted component of ui_repo_context's composition chain, in chain order."""

    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    refs = [
        str(_dict(context.get("entrypoint")).get("ref") or ""),
        *(str(hop.get("ref") or "") for hop in _dicts(context.get("composition_chain"))),
    ]
    return [ref for ref in dict.fromkeys(refs) if ref]


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
        # Each repair target (scenario@component) needs its own investigation and delta, so budget per target.
        scenario_id = active_partition_key.split("|", 1)[0] if active_partition_key else ""
        scope = {"kind": "scenario_repair", "key": str(repair_task.get("target_id") or "") or scenario_id}
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
            return f"Designer repair target {scope['key']} exhausted its repair action budget."
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
    _replace_retrying(temporary, path)


def _replace_retrying(temporary: Path, path: Path) -> None:
    """Replace like the Developer's writer: Windows briefly locks a file another process (a scanner) just opened."""

    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05 * (2**attempt))


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
    _replace_retrying(temporary, path)


DESIGN_NOTE_LIMIT = 12  # the partition action budget; a partition never trims, so its notes stay a prompt prefix
DESIGN_NOTE_CHARS = 600


def _record_design_note(session: dict[str, object], action: dict[str, object], *, work_key: str) -> None:
    """Keep the Designer's own action summary for the work item, like the Developer's action ledger summaries."""

    summary = " ".join(str(action.get("summary") or "").split())[:DESIGN_NOTE_CHARS]
    if not summary or not work_key:
        return
    notes = _dict(session.get("design_notes"))
    entries = [*_dicts(notes.get(work_key)), {"action": str(action.get("action") or ""), "summary": summary}]
    notes[work_key] = entries[-DESIGN_NOTE_LIMIT:]
    session["design_notes"] = notes


def _design_notes(session: dict[str, object], *, work_key: str) -> list[dict[str, object]]:
    return _dicts(_dict(session.get("design_notes")).get(work_key))


def _normalized_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


def _unread_existing_targets(
    arguments: object,
    *,
    graph: dict[str, object],
    read_paths: set[str],
) -> list[str]:
    """Files a delta designs from that no repo.read of the session returned: the runtime target of a
    reuse/modify/no_change node and the AS-IS hop that provides each declared required input."""

    nodes = {str(node.get("id") or ""): node for node in _dicts(graph.get("nodes"))}
    context = _dict(_dict(graph.get("snapshot_binding")).get("ui_repo_context"))
    as_is_refs = {str(hop.get("ref") or "") for hop in _dicts(context.get("composition_chain"))} | set(
        _dict(context.get("components"))
    )
    paths: list[str] = []
    for operation in _dicts(_dict(_dict(arguments).get("delta")).get("operations")):
        if operation.get("op") == "add_node":
            node = _dict(operation.get("node"))
            touched = set(node)
        elif operation.get("op") == "update_node":
            changes = _dict(operation.get("changes"))
            node = {**nodes.get(str(operation.get("node_id") or ""), {}), **changes}
            touched = set(changes)
        else:
            continue
        if touched & {"disposition", "runtime_binding"} and str(node.get("disposition") or "") in {
            "reuse", "modify", "no_change"
        }:
            paths.append(str(_dict(_dict(node.get("runtime_binding")).get("target")).get("path") or ""))
        if "surface_contract" in touched:
            runtime_surface = _dict(_dict(node.get("surface_contract")).get("runtime_surface"))
            for item in _dicts(runtime_surface.get("required_inputs")):
                provided_by = str(item.get("provided_by") or "")
                if provided_by in as_is_refs:
                    paths.append(provided_by.split("#", 1)[0])
    return [
        path
        for path in dict.fromkeys(_normalized_path(item) for item in paths)
        if path and path not in read_paths
    ]


def _observation_allowance(limit: int, *, actions_remaining: int) -> int:
    """Observations allowed before the delta, always keeping one action of the real scope budget for the delta."""

    return max(0, min(limit, actions_remaining - 1))


def _reject_discontinuous_completed_scenario(
    arguments: object,
    *,
    registry: DesignerToolRegistry,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    run_id: str,
) -> None:
    """The delta that completes a scenario must leave its ordered behavior as one continuous path.

    Sequence continuity was checked only for the whole bundle, so design_v19 designer-0009 completed SC-1 with
    layer-by-layer orders and the defect would have surfaced as a repair after every other scenario."""

    delta = _dict(_dict(arguments).get("delta"))
    scenario_id = str(_dict(delta.get("partition")).get("scenario_id") or "")
    if not scenario_id:
        return
    candidate, temp_refs = apply_designer_delta(
        registry.graph,
        delta,
        valid_evidence_refs=registry.valid_evidence_refs,
        valid_guidance_refs=registry.valid_guidance_refs,
    )
    following = _active_partition(requirement_model=requirement_model, landscape=landscape, graph=candidate)
    if following is not None and str(_dict(following.get("partition")).get("scenario_id") or "") == scenario_id:
        return
    nodes = _dicts(candidate.get("nodes"))
    sequence = _sequence_behavior_findings(nodes=nodes, edges=_dicts(candidate.get("edges")))
    findings = [item for item in sequence if str(item.get("subject_ref") or "") == scenario_id]
    unserved = _scenario_unserved_requests(sequence, scenario_id=scenario_id)
    if not findings and not unserved:
        return
    lines = _scenario_order_lines(registry.graph, candidate, temp_refs, scenario_id=scenario_id)
    if not findings:
        _raise_unserved_requests(unserved, lines=lines, scenario_id=scenario_id, run_id=run_id)
    raise HarnessError(
        "designer_delta_invalid",
        f"This delta completes scenario {scenario_id}, but its ordered behavior edges do not form one continuous path: "
        + " ".join(str(item.get("message") or "") for item in findings)
        + " Ordered by order, each calls/returns/reads/writes edge must start at the participant the previous edge "
        "reached, and the single terminal=true edge must be last. Current order: "
        + "; ".join(lines)
        + f". Resubmit the delta with a temp_ref on each new edge and one order_edges operation listing every behavior "
        f"edge of {scenario_id} (VDE-* ids of existing edges, temp_refs of new ones) in behavioral order, with terminal "
        "naming the last one. Where the user moves from one screen to the next, the path needs a calls edge from the "
        "ui node the user leaves to the ui node the user opens."
        + "".join(f" Also: {item.get('message')} {item.get('expected_change')}" for item in unserved),
        run_id=run_id,
    )


def _scenario_unserved_requests(sequence: list[dict[str, object]], *, scenario_id: str) -> list[dict[str, object]]:
    return [
        item
        for item in sequence
        if str(item.get("code") or "") == "sequence_request_unserved"
        and str(_dict(item.get("partition")).get("scenario_id") or "") == scenario_id
    ]


def _scenario_order_lines(
    graph: dict[str, object],
    candidate: dict[str, object],
    temp_refs: dict[str, str],
    *,
    scenario_id: str,
) -> list[str]:
    written = _delta_written_names(graph, candidate, temp_refs)
    existing_edges = {str(item.get("id") or "") for item in _dicts(graph.get("edges"))}
    node_names = {str(item.get("id") or ""): str(item.get("name") or "")[:48] for item in _dicts(candidate.get("nodes"))}

    def participant(node_id: str) -> str:
        return written.get(node_id) or f"{node_id} ({node_names.get(node_id, '')})"

    return [
        f"{item.get('order')}. {item.get('id') if str(item.get('id') or '') in existing_edges else 'new edge'} "
        f"{item.get('kind')}: {participant(str(item.get('from') or ''))} -> {participant(str(item.get('to') or ''))}"
        + (" [terminal]" if item.get("terminal") else "")
        for item in sorted(_dicts(candidate.get("edges")), key=lambda edge: int(edge.get("order") or 0))
        if str(item.get("scenario_id") or "") == scenario_id and str(item.get("kind") or "") not in STRUCTURAL_EDGE_KINDS
    ]


def _raise_unserved_requests(
    unserved: list[dict[str, object]], *, lines: list[str], scenario_id: str, run_id: str
) -> None:
    raise HarnessError(
        "designer_delta_invalid",
        " ".join(str(item.get("message") or "") for item in unserved)
        + " "
        + str(unserved[0].get("expected_change") or "")
        + f" Current order: {'; '.join(lines)}. Resubmit with a temp_ref on each new edge and one order_edges operation "
        f"listing every behavior edge of {scenario_id} in behavioral order, with terminal naming the last one.",
        run_id=run_id,
    )


def _reject_unserved_repeat_requests(
    arguments: object,
    *,
    repair_task: dict[str, object],
    registry: DesignerToolRegistry,
    run_id: str,
) -> None:
    """A repair may not leave a writable node answering one request of its scenario with no provider messages while it
    serves another request with them (sequence_request_unserved; UC4 H-r1 design_v23 designer-0026 SC-2@api).

    Only nodes this target may change count, so a step of a multi-target repair is not blocked by another component's
    side. Harness-built exact scaffolds and boundary-contract-only repairs (no add_edge allowed) are exempt.
    """

    delta = _dict(_dict(arguments).get("delta"))
    scope = _dict(repair_task.get("write_scope"))
    scenario_id = str(scope.get("scenario_id") or "")
    if not scenario_id or delta == repair_task.get("delta_scaffold") or _dicts(scope.get("restricted_edge_pairs")):
        return
    candidate, temp_refs = apply_designer_delta(
        registry.graph,
        delta,
        valid_evidence_refs=registry.valid_evidence_refs,
        valid_guidance_refs=registry.valid_guidance_refs,
    )
    writable = set(_strings(scope.get("writable_node_ids")))
    unserved = [
        item
        for item in _scenario_unserved_requests(
            _sequence_behavior_findings(nodes=_dicts(candidate.get("nodes")), edges=_dicts(candidate.get("edges"))),
            scenario_id=scenario_id,
        )
        if str(item.get("subject_ref") or "").split(":", 1)[-1] in writable
    ]
    if unserved:
        _raise_unserved_requests(
            unserved,
            lines=_scenario_order_lines(registry.graph, candidate, temp_refs, scenario_id=scenario_id),
            scenario_id=scenario_id,
            run_id=run_id,
        )


# Node-local binding facts: the delta that adds or updates the node fully decides them, so a repair may not introduce
# one and leave it for the next validation round.
NODE_BINDING_FINDING_CODES = frozenset(
    {"runtime_binding_missing", "runtime_attachment_missing", "component_ownership_invalid"}
)


def _reject_new_node_binding_findings(
    arguments: object,
    *,
    repair_task: dict[str, object],
    registry: DesignerToolRegistry,
    requirement_model: dict[str, object],
    landscape: dict[str, object],
    run_id: str,
) -> None:
    """A repair may not add or update a node into a binding defect the graph did not have (UC4 H-r1 design_v23
    designer-0042 attached a new persistence_adapter called_by its caller; post_delta_findings saw it, yet a
    Validator-feedback repair marks its target completed, so the defect waited for the next validation round)."""

    delta = _dict(_dict(arguments).get("delta"))
    if delta == repair_task.get("delta_scaffold"):
        return
    candidate, temp_refs = apply_designer_delta(
        registry.graph,
        delta,
        valid_evidence_refs=registry.valid_evidence_refs,
        valid_guidance_refs=registry.valid_guidance_refs,
    )
    touched = {
        temp_refs.get(str(_dict(operation.get("node")).get("temp_ref") or ""), "")
        if operation.get("op") == "add_node"
        else str(operation.get("node_id") or "")
        for operation in _dicts(delta.get("operations"))
        if operation.get("op") in {"add_node", "update_node"}
    } - {""}
    if not touched:
        return

    def binding_findings(graph: dict[str, object]) -> list[dict[str, object]]:
        return [
            item
            for item in _composition_repair_findings(graph, requirement_model=requirement_model, landscape=landscape)
            if str(item.get("code") or "") in NODE_BINDING_FINDING_CODES
        ]

    before = {(str(item.get("id") or ""), str(item.get("message") or "")) for item in binding_findings(registry.graph)}
    new = [
        item
        for item in binding_findings(candidate)
        if str(item.get("subject_ref") or "") in touched
        and (str(item.get("id") or ""), str(item.get("message") or "")) not in before
    ]
    if not new:
        return
    written = _delta_written_names(registry.graph, candidate, temp_refs)
    nodes_by_id = {str(item.get("id") or ""): item for item in _dicts(candidate.get("nodes"))}
    parts = [
        f"{written.get(str(item.get('subject_ref') or '')) or item.get('subject_ref')}: {item.get('message')} "
        f"{item.get('expected_change')}"
        for item in new
    ]
    for node_id in dict.fromkeys(str(item.get("subject_ref") or "") for item in new):
        kind = str(_dict(_dict(nodes_by_id.get(node_id)).get("runtime_binding")).get("runtime_kind") or "")
        allowed = ", ".join(sorted(RUNTIME_ATTACHMENT_RELATIONS.get(kind, frozenset()))) or "none"
        parts.append(f"Allowed attachment relations for {kind or '<missing runtime_kind>'}: {allowed}.")
    raise HarnessError("designer_delta_invalid", " ".join(parts), run_id=run_id)


def _requested_read_paths(tool_id: str, arguments: object) -> list[str]:
    if tool_id == "repo.read":
        return [_normalized_path(_dict(arguments).get("path"))]
    if tool_id == "repo.read_batch":
        return [_normalized_path(_dict(request).get("path")) for request in _dicts(_dict(arguments).get("requests"))]
    return []


def _source_observation_refs(session: dict[str, object], component_id: str) -> list[str]:
    return _strings(_dict(session.get("source_observations")).get(component_id)) if component_id else []


def _partition_scenario(partition_key: str) -> str:
    return partition_key.split("|", 1)[0] if "|" in partition_key else ""


def _scenario_observations(session: dict[str, object], scenario_id: str) -> list[dict[str, object]]:
    return [
        item
        for item in _dicts(session.get("observations"))
        if scenario_id
        and _partition_scenario(str(item.get("partition_key") or "")) == scenario_id
        and str(item.get("status") or "") == "succeeded"
    ]


def _inspected_source_refs(session: dict[str, object], component_id: str, *, construction: bool) -> list[str]:
    """The component's source refs that count as inspected now: for a construction partition only those read in
    its own scenario (a new scenario starts without earlier scenarios' reads), for a repair every one."""

    refs = _source_observation_refs(session, component_id)
    if not construction:
        return refs
    scenario_id = _partition_scenario(str(session.get("active_partition_key") or ""))
    if not scenario_id:
        return refs
    seen = {ref for item in _scenario_observations(session, scenario_id) for ref in _strings(item.get("evidence_refs"))}
    return [ref for ref in refs if ref in seen]


def _scenario_read_paths(session: dict[str, object], scenario_id: str) -> set[str]:
    """Files a repo.read or repo.read_batch of the scenario returned (the read-before-design gate's input)."""

    paths: set[str] = set()
    for item in _scenario_observations(session, scenario_id):
        tool_id = str(item.get("tool_id") or "")
        output = _dict(item.get("output"))
        rows = [output] if tool_id == "repo.read" else _dicts(output.get("results")) if tool_id == "repo.read_batch" else []
        paths.update(_normalized_path(row.get("path")) for row in rows if row.get("evidence_ref"))
    return paths - {""}


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


def _apply_prompt_graph_digest(arguments: object, *, prompt_digest: str) -> tuple[object, dict[str, str] | None]:
    """Set the delta's base_graph_digest to the digest of the graph the prompt showed.

    The Designer copied the 71-character digest by hand and mistyped it (design_v20 designer-0031 inserted 8
    characters, 0058 dropped 2), which the stale-revision check rejected as if the graph had changed. A graph that
    really changed since the prompt still fails that check, because it compares against this prompt digest."""

    delta = _dict(_dict(arguments).get("delta"))
    written = str(delta.get("base_graph_digest") or "")
    if not prompt_digest or not delta or written == prompt_digest:
        return arguments, None
    payload = json.loads(json.dumps(arguments, ensure_ascii=False))
    payload["delta"]["base_graph_digest"] = prompt_digest
    return payload, {"written": written, "applied": prompt_digest}


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
        + ". Omit the duplicate add_edge operations. Use update_edge to edit existing edges or order_edges to move them."
    )
