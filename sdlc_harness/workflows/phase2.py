"""Workflow Phase 2: requirement đã duyệt -> checkpoint thiết kế đã xác thực."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from ..agents.designer import (
    DESIGN_BUNDLE_STATE_KEY,
    DESIGN_BUNDLE_VERSION,
    DESIGNER_CONTRACT_STATE_KEY,
    GRAPH_VERSION,
    DesignerAgent,
    DesignerExecutionError,
    DesignerToolLoopExecutor,
    build_deterministic_trace,
    design_bundle_findings,
    projection_manifest,
    render_design_projections,
    validate_vertical_design_graph,
    parse_design_model,
)
from ..agents.validator import (
    DesignFinding,
    DesignValidationResult,
    DesignValidator,
    TraceabilityResult,
    VERTICAL_TRACE_GRAPH_VERSION,
    VERTICAL_TRACE_MODE_STATE_KEY,
    VERTICAL_TRACE_MODES,
    VERTICAL_TRACE_STATE_KEY,
    ValidatorAgent,
    bounded_trace_view,
    build_requirement_graph_v2,
    build_vertical_trace_graph,
    validate_vertical_trace_coverage,
)
from ..artifacts import read_approved_requirement_input, read_frozen_requirement_input
from ..artifacts.writers import write_phase2_design_artifacts, write_phase2_traceability_attempt
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..profiles import build_planning_path_policy, read_profile_snapshot
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint
from ..workspaces import RunWorkspaceManager
from .design_experiment import (
    ProgressiveDesignReviewEngine,
    SemanticCandidate,
    _missing_link_findings,
)
from .repo_discovery import (
    REPO_DISCOVERY_CONTRACT_STATE_KEY,
    REPO_DISCOVERY_CONTRACT_VERSION,
    RepoDiscoveryWorkflow,
)

ALLOWED_DESIGN_ACTIONS = [
    "approve_checkpoint",
    "revalidate_design",
    "sync_run",
    "request_change",
    "cancel_run",
    "get_status",
]

DESIGN_ROUTING_ERROR_CODES = frozenset(
    {
        "implementation_target_unclassified",
        "implementation_target_conflict",
        "implementation_target_ambiguous",
        "implementation_target_component_invalid",
    }
)


class PhaseTwoWorkflow:
    """Coordinate Designer and hybrid Design Validator before human review."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.repo_discovery = RepoDiscoveryWorkflow(settings)
        self._reset_progressive_engine()

    def _reset_progressive_engine(self) -> None:
        self.progressive = ProgressiveDesignReviewEngine(
            self.settings,
            llm_call_budget=self.settings.design_llm_call_budget,
            llm_client=LlmClient(self.settings),
        )
        self.designer = self.progressive.designer
        self.validator = self.progressive.trace_validator
        self.design_validator = self.progressive.design_validator
        self.designer_tool_loop = DesignerToolLoopExecutor(
            settings=self.settings,
            store=self.store,
            llm_client=self.progressive.llm,
        )

    def approve_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Approve the requirement checkpoint and create a validated design."""

        state = self.store.read_state(run_id)
        self._require_requirement_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        approved = read_approved_requirement_input(store=self.store, state=state, run_id=run_id)
        if str(state.get(REPO_DISCOVERY_CONTRACT_STATE_KEY) or "") == REPO_DISCOVERY_CONTRACT_VERSION:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "approved_artifacts": approved.approved_artifacts,
                    "pending_checkpoint": None,
                }
            )
            self.store.write_state(state)
            landscape = self.repo_discovery.run(
                run_id=run_id,
                requirement_model=approved.canonical_model.to_dict(),
            )
            if landscape is None:
                return self._status_response(self.store.read_state(run_id), created_new_run=False)
            state = self.store.read_state(run_id)
        return self._start_design(state=state, approved=approved)

    def continue_repo_discovery(self, *, run_id: str) -> dict[str, object]:
        """Resume một L2 environment failure trên cùng frozen workspace/input."""

        state = self.store.read_state(run_id)
        if str(state.get("status") or "") != "repo_discovery_paused":
            raise HarnessError(
                "repo_discovery_not_resumable",
                f"Repo discovery cannot continue from status {state.get('status')!r}.",
                run_id=run_id,
            )
        approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
        landscape = self.repo_discovery.run(
            run_id=run_id,
            requirement_model=approved.canonical_model.to_dict(),
        )
        if landscape is None:
            return self._status_response(self.store.read_state(run_id), created_new_run=False)
        return self._start_design(state=self.store.read_state(run_id), approved=approved)

    def _start_design(self, *, state: dict[str, object], approved: object) -> dict[str, object]:
        """Chạy Designer hiện hành sau legacy handoff hoặc L2 discovery thành công."""

        run_id = str(state.get("run_id") or "")
        if str(state.get(DESIGNER_CONTRACT_STATE_KEY) or "") == GRAPH_VERSION:
            return self._run_vertical_design(
                state=state,
                approved=approved,
                start_version=1,
                existing_versions=[],
            )
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_drafting",
                "status": "design_drafting",
                "approved_artifacts": approved.approved_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": None,
                "summary_for_user": "Đã duyệt requirement checkpoint, đang tạo design draft.",
            }
        )
        self.store.write_state(state)
        try:
            self.progressive.llm.require(2, category="designer_draft")
            draft = self.designer.draft(
                requirement_packet=_designer_requirement_packet(
                    state=state,
                    requirement_packet=approved.requirement_packet,
                    run_id=run_id,
                ),
                formal_use_case=approved.formal_use_case,
                canonical_requirement=approved.canonical_model,
            )
            return self._run_progressive_validation_loop(
                state=state,
                approved=approved,
                design_model=draft.design_model,
                base_analysis=draft.analysis_report,
                artifact_paths=artifact_paths,
                start_version=1,
                existing_versions=[],
            )
        except HarnessError as exc:
            return self._handle_error(state, run_id=run_id, error=exc)

    def revalidate_design(self, *, run_id: str) -> dict[str, object]:
        """Validate and, when needed, repair the latest Phase 2 design version."""

        self._reset_progressive_engine()
        state = self.store.read_state(run_id)
        status = str(state.get("status") or "")
        phase = str(state.get("current_phase") or "")
        last_error = _dict_value(state.get("last_error"))
        recoverable_trace_failure = (
            status == "failed"
            and phase.startswith("design_")
            and str(last_error.get("error_code") or "") == "validator_traceability_failed"
        )
        recoverable_routing_failure = (
            status == "failed"
            and phase == "implementation_planning"
            and str(last_error.get("error_code") or "") in DESIGN_ROUTING_ERROR_CODES
        )
        recoverable_vertical_design_block = (
            str(state.get(DESIGNER_CONTRACT_STATE_KEY) or "") == GRAPH_VERSION
            and status == "design_blocked"
            and phase.startswith("design_")
        )
        if not recoverable_trace_failure and not recoverable_routing_failure and not recoverable_vertical_design_block and (
            phase != "design_review" or status not in {"waiting_design_approval", "blocked"}
        ):
            raise HarnessError(
                "design_revalidation_not_allowed",
                f"Run is not waiting on a design checkpoint: {run_id}",
                run_id=run_id,
            )
        if str(state.get(DESIGNER_CONTRACT_STATE_KEY) or "") == GRAPH_VERSION:
            approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
            versions = _design_versions(state)
            existing_graph = self._latest_vertical_graph(state, run_id=run_id)
            start_version = _next_design_version(self.store.run_dir(run_id) / "artifacts")
            prior_execution = _dict_value(state.get("designer_execution"))
            session_ordinal = max(
                start_version,
                int(prior_execution.get("session_ordinal") or 0) + 1,
            )
            initial_repair_feedback = design_bundle_findings(existing_graph)
            initial_repair_feedback.extend(
                _vertical_trace_repair_feedback(
                    state=state,
                    approved=approved,
                    graph=existing_graph,
                )
            )
            return self._run_vertical_design(
                state=state,
                approved=approved,
                start_version=start_version,
                existing_versions=versions,
                existing_graph=existing_graph,
                session_ordinal=session_ordinal,
                initial_repair_feedback=initial_repair_feedback,
            )
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        design_path = _required_artifact(
            artifact_paths,
            "design_artifact",
            run_id=run_id,
        )
        resolved_design_path = Path(str(design_path))
        if not resolved_design_path.is_absolute():
            resolved_design_path = self.store.repo_root / resolved_design_path
        design_model = resolved_design_path.resolve().read_text(encoding="utf-8")
        analysis_path = artifact_paths.get("designer_analysis", "")
        base_analysis = "Revalidated the latest approved-requirement design."
        if analysis_path:
            candidate = (self.store.repo_root / analysis_path).resolve()
            if candidate.is_file():
                base_analysis = candidate.read_text(encoding="utf-8")
        approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
        versions = _design_versions(state)
        start_version = _next_design_version(self.store.run_dir(run_id) / "artifacts")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_revalidating",
                "status": "design_revalidating",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": None,
                "summary_for_user": f"Đang revalidate design từ V{start_version}.",
            }
        )
        self.store.write_state(state)
        try:
            return self._run_progressive_validation_loop(
                state=state,
                approved=approved,
                design_model=design_model,
                base_analysis=base_analysis,
                artifact_paths=artifact_paths,
                start_version=start_version,
                existing_versions=versions,
            )
        except HarnessError as exc:
            return self._handle_error(state, run_id=run_id, error=exc)

    def continue_design(self, *, run_id: str) -> dict[str, object]:
        """Resume đúng persisted L3 Designer session sau transport/environment pause."""

        self._reset_progressive_engine()
        state = self.store.read_state(run_id)
        if (
            str(state.get(DESIGNER_CONTRACT_STATE_KEY) or "") != GRAPH_VERSION
            or str(state.get("status") or "") != "design_paused"
        ):
            raise HarnessError(
                "designer_session_not_resumable",
                f"Design run cannot continue from status {state.get('status')!r}.",
                run_id=run_id,
            )
        approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
        execution = _dict_value(state.get("designer_execution"))
        return self._run_vertical_design(
            state=state,
            approved=approved,
            start_version=int(execution.get("design_version") or _next_design_version(self.store.run_dir(run_id) / "artifacts")),
            existing_versions=_design_versions(state),
            existing_graph=self._latest_vertical_graph(state, run_id=run_id, required=False),
            session_ordinal=int(execution.get("session_ordinal") or 1),
            initial_repair_iteration=int(execution.get("repair_iteration") or 0),
            initial_repair_feedback=_dict_items(state.get("designer_repair_feedback")),
        )

    def experiment_design_consensus(
        self,
        *,
        source_run_id: str,
        experiment_root: Path | None = None,
        llm_call_budget: int = 16,
    ) -> dict[str, object]:
        """Run the isolated progressive validator without touching production state."""

        from .design_experiment import ProgressiveDesignExperiment

        root = experiment_root or (self.settings.repo_root / ".experiments" / "design-validator")
        return ProgressiveDesignExperiment(
            self.settings,
            experiment_root=root,
            llm_call_budget=llm_call_budget,
        ).run(source_run_id=source_run_id)

    def _copy_requirement_snapshot(
        self,
        *,
        source_run_id: str,
        experiment_run_id: str,
        approved_artifacts: dict[str, str],
        source_artifact_ref: str,
    ) -> dict[str, str]:
        destination = self.store.run_dir(experiment_run_id) / "artifacts" / "requirement_snapshot"
        destination.mkdir(parents=True, exist_ok=True)
        copied: dict[str, str] = {}
        refs = {**approved_artifacts}
        if source_artifact_ref:
            refs["source_snapshot"] = source_artifact_ref
        for key, artifact_ref in refs.items():
            source = (self.store.repo_root / artifact_ref).resolve()
            if not source.is_file():
                raise HarnessError(
                    "approved_requirement_not_found",
                    f"Cannot copy frozen requirement artifact: {artifact_ref}",
                    run_id=source_run_id,
                )
            target = destination / source.name
            shutil.copy2(source, target)
            copied[key] = self.store.relative(target)
        return copied

    def _run_vertical_design(
        self,
        *,
        state: dict[str, object],
        approved: object,
        start_version: int,
        existing_versions: list[str],
        existing_graph: dict[str, object] | None = None,
        session_ordinal: int | None = None,
        initial_repair_iteration: int = 0,
        initial_repair_feedback: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Run the repo-aware Designer loop and publish compatibility projections."""

        run_id = str(state.get("run_id") or "")
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        landscape = self.repo_discovery.run(
            run_id=run_id,
            requirement_model=approved.canonical_model.to_dict(),
        )
        if landscape is None:
            return self._status_response(self.store.read_state(run_id), created_new_run=False)
        workspace = RunWorkspaceManager(self.settings).resolve(run_id)
        snapshot_binding = _vertical_snapshot_binding(
            state=state,
            approved=approved,
            landscape=landscape,
            workspace=workspace,
        )
        if existing_graph is not None:
            existing_graph = validate_vertical_design_graph(
                existing_graph,
                expected_run_id=run_id,
                expected_binding=snapshot_binding,
            )
        versions = list(existing_versions)
        merged_artifacts = dict(artifact_paths)
        prior_digest = str((existing_graph or {}).get("artifact_digest") or "")
        repair_feedback = list(initial_repair_feedback or [])
        validation: DesignValidationResult | None = None
        trace_validation = None
        design_paths: dict[str, str] = {}
        graph = existing_graph
        base_ordinal = session_ordinal or start_version

        for repair_iteration in range(initial_repair_iteration, self.settings.design_repair_limit + 1):
            relative_iteration = repair_iteration - initial_repair_iteration
            version = start_version + relative_iteration
            ordinal = base_ordinal + relative_iteration
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_investigating" if repair_iteration == 0 else "design_repairing",
                    "status": "design_investigating" if repair_iteration == 0 else "design_repairing",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": None,
                    "summary_for_user": (
                        "Designer đang khảo sát frozen repository evidence và dựng vertical Design graph."
                        if repair_iteration == 0
                        else f"Designer đang sửa partition bị chặn, vòng {repair_iteration}/{self.settings.design_repair_limit}."
                    ),
                    "designer_execution": {
                        "contract_version": GRAPH_VERSION,
                        "status": "running",
                        "session_ordinal": ordinal,
                        "design_version": version,
                        "repair_iteration": repair_iteration,
                    },
                    "designer_repair_feedback": repair_feedback,
                }
            )
            self.store.write_state(state)

            def on_progress(progress: dict[str, object], **metadata: object) -> None:
                current = self.store.read_state(run_id)
                current["updated_at"] = utc_now_iso()
                tool_id = str(metadata.get("tool_id") or "")
                current["current_phase"] = (
                    "design_applying_delta" if tool_id == "design.propose_delta" else "design_investigating"
                )
                current["status"] = current["current_phase"]
                current["designer_execution"] = {
                    "contract_version": GRAPH_VERSION,
                    "status": "running",
                    "session_ordinal": ordinal,
                    "design_version": version,
                    **progress,
                    "repair_iteration": repair_iteration,
                }
                current["summary_for_user"] = (
                    f"Designer đã áp dụng graph revision {progress.get('graph_revision', 0)}; "
                    f"action budget còn { _dict_value(progress.get('action_budget')).get('remaining', 0)}."
                )
                self.store.write_state(current)

            try:
                execution = self.designer_tool_loop.execute(
                    run_id=run_id,
                    requirement_model=approved.canonical_model.to_dict(),
                    landscape=landscape,
                    target_path=Path(workspace.worktree_path),
                    snapshot_binding=snapshot_binding,
                    existing_graph=graph,
                    repair_feedback=repair_feedback,
                    session_ordinal=ordinal,
                    on_progress=on_progress,
                )
            except DesignerExecutionError as exc:
                state = self.store.read_state(run_id)
                human_required = exc.error_code == "designer_human_required"
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "design_review" if human_required else "design_investigating",
                        "status": (
                            "design_human_required"
                            if human_required
                            else ("design_paused" if exc.retriable else "design_blocked")
                        ),
                        "pending_checkpoint": None,
                        "allowed_next_actions": (
                            ["continue_run", "get_status", "cancel_run"]
                            if exc.retriable
                            else (
                                ["get_status", "cancel_run"]
                                if human_required
                                else ["revalidate_design", "get_status", "cancel_run"]
                            )
                        ),
                        "last_error": exc.to_response(),
                        "designer_execution": {
                            "contract_version": GRAPH_VERSION,
                            "status": "human_required" if human_required else ("paused" if exc.retriable else "blocked"),
                            "session_ordinal": ordinal,
                            "design_version": version,
                            **exc.progress,
                            "repair_iteration": repair_iteration,
                        },
                        "summary_for_user": (
                            f"Designer tạm dừng và có thể tiếp tục: {exc.message}"
                            if exc.retriable
                            else (
                                f"Designer cần quyết định của human/Requirement owner: {exc.message}"
                                if human_required
                                else f"Designer bị chặn: {exc.message}"
                            )
                        ),
                    }
                )
                self.store.write_state(state)
                return self._status_response(state, created_new_run=False)

            graph = execution.graph
            state = self.store.read_state(run_id)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_validating",
                    "status": "design_validating",
                    "designer_execution": {
                        "contract_version": GRAPH_VERSION,
                        "status": "validating",
                        "session_ordinal": ordinal,
                        "design_version": version,
                        **execution.progress,
                        "repair_iteration": repair_iteration,
                    },
                    "summary_for_user": f"Đang kiểm tra vertical Design graph V{version}.",
                }
            )
            self.store.write_state(state)
            projections = render_design_projections(
                graph,
                requirement_model=approved.canonical_model.to_dict(),
            )
            requirement_graph = build_requirement_graph_v2(approved.canonical_model)
            class_trace, sequence_trace = build_deterministic_trace(
                requirement_graph=requirement_graph,
                graph=graph,
                class_graph=projections["class_graph"],
                sequence_graph=projections["sequence_graph"],
            )
            missing_links = {
                "class_diagram": [
                    {"requirement": str(item.get("requirement") or ""), "content": str(item.get("content") or "")}
                    for item in class_trace
                    if not item.get("links")
                ],
                "sequence_diagram": [
                    {"requirement": str(item.get("requirement") or ""), "content": str(item.get("content") or "")}
                    for item in sequence_trace
                    if not item.get("links")
                ],
            }
            traceability = TraceabilityResult(
                requirement_graph=requirement_graph,
                class_trace=class_trace,
                sequence_trace=sequence_trace,
                missing_links=missing_links,
                mode="traceability_v2",
            )
            trace_mode = str(state.get(VERTICAL_TRACE_MODE_STATE_KEY) or "")
            trace_enabled = (
                str(state.get(VERTICAL_TRACE_STATE_KEY) or "") == VERTICAL_TRACE_GRAPH_VERSION
                and trace_mode in VERTICAL_TRACE_MODES
            )
            trace_graph = (
                build_vertical_trace_graph(
                    requirement_model=approved.canonical_model,
                    design_graph=graph,
                )
                if trace_enabled
                else None
            )
            trace_validation = (
                validate_vertical_trace_coverage(trace_graph, mode=trace_mode)
                if trace_graph is not None
                else None
            )
            bundle_blockers = _bundle_design_findings(design_bundle_findings(graph))
            trace_blockers = list(trace_validation.blocking_findings) if trace_validation is not None else []
            enforced_trace_blockers = trace_blockers if trace_mode == "enforce" else []
            deterministic_blockers = [*bundle_blockers, *enforced_trace_blockers]
            if deterministic_blockers:
                validation = DesignValidationResult(
                    traceability=traceability,
                    blocking_findings=deterministic_blockers,
                    warnings=[],
                    rule_review={"ok": False, "findings": [item.to_dict() for item in deterministic_blockers]},
                    llm_review={
                        "ok": True,
                        "skipped": True,
                        "summary": "Semantic review waits for deterministic Design/trace blockers to be repaired.",
                    },
                    repair_iterations=repair_iteration,
                    mode="vertical_trace_preflight_v1" if enforced_trace_blockers else "design_bundle_preflight_v1",
                )
            else:
                validation = self.design_validator.review(
                    requirement_packet=approved.requirement_packet,
                    design_artifact=str(projections["design_markdown"]),
                    class_graph=projections["class_graph"],
                    sequence_graph=projections["sequence_graph"],
                    traceability=traceability,
                    canonical_requirement=approved.canonical_model,
                    vertical_trace=bounded_trace_view(trace_graph) if trace_graph is not None else None,
                    repair_iterations=repair_iteration,
                    validation_trials=self.settings.design_validation_trials,
                )
            validation_payload = validation.to_dict()
            validation_payload["version"] = "design_validation_report_v4_vertical_graph"
            validation_payload["mode"] = "vertical_design_graph_v1"
            graph_path = self.store.write_json_artifact(run_id, f"vertical_design_graph_v{version}.json", graph)
            component_path = self.store.write_json_artifact(
                run_id,
                f"design_component_view_v{version}.json",
                projections["component_view"],
            )
            surface_path = self.store.write_json_artifact(
                run_id,
                f"design_surface_view_v{version}.json",
                projections["surface_view"],
            )
            design_paths = write_phase2_design_artifacts(
                store=self.store,
                run_id=run_id,
                designer_analysis=(
                    "Repo-aware Designer investigated frozen AS-IS evidence and produced a bounded "
                    "AS-IS/GAP/TO-BE graph delta."
                ),
                design_model=str(projections["design_markdown"]),
                requirement_graph=requirement_graph,
                class_graph=projections["class_graph"],
                sequence_graph=projections["sequence_graph"],
                class_trace=class_trace,
                sequence_trace=sequence_trace,
                missing_links=missing_links,
                requirement_packet=approved.requirement_packet,
                formal_use_case=approved.formal_use_case,
                version=version,
                repair_iterations=repair_iteration,
                traceability_mode=traceability.mode,
                requirement_contract_version=approved.contract_version,
                requirement_artifact_refs=approved.approved_artifacts,
                requirement_revision=approved.canonical_model.revision,
                design_validation=validation_payload,
            )
            vertical_paths = {
                "vertical_design_graph": self.store.relative(graph_path),
                "design_component_view": self.store.relative(component_path),
                "design_surface_view": self.store.relative(surface_path),
            }
            if trace_graph is not None and trace_validation is not None:
                trace_graph_path = self.store.write_json_artifact(
                    run_id,
                    f"vertical_trace_graph_v{version}.json",
                    trace_graph,
                )
                trace_validation_payload = trace_validation.to_dict()
                trace_validation_payload.update(
                    {
                        "design_version": f"v{version}",
                        "requirement_revision": approved.canonical_model.revision,
                    }
                )
                trace_validation_path = self.store.write_json_artifact(
                    run_id,
                    f"vertical_trace_validation_v{version}.json",
                    trace_validation_payload,
                )
                vertical_paths.update(
                    {
                        "vertical_trace_graph": self.store.relative(trace_graph_path),
                        "vertical_trace_validation": self.store.relative(trace_validation_path),
                    }
                )
            candidate_artifacts = {**design_paths, **vertical_paths}
            manifest = projection_manifest(graph=graph, artifacts=candidate_artifacts)
            manifest_path = self.store.write_json_artifact(
                run_id,
                f"design_projection_manifest_v{version}.json",
                manifest,
            )
            finding_ledger = {
                "schema_version": "design_finding_ledger_v1",
                "graph_digest": graph.get("artifact_digest"),
                "design_version": f"v{version}",
                "blocking_findings": [item.to_dict() for item in validation.blocking_findings],
                "warnings": [item.to_dict() for item in validation.warnings],
                "disputed_findings": [item.to_dict() for item in validation.disputed_findings],
                "vertical_trace_findings": (
                    [item.to_dict() for item in trace_validation.blocking_findings]
                    if trace_validation is not None
                    else []
                ),
            }
            ledger_path = self.store.write_json_artifact(
                run_id,
                f"design_finding_ledger_v{version}.json",
                finding_ledger,
            )
            design_paths = {
                **candidate_artifacts,
                "design_projection_manifest": self.store.relative(manifest_path),
                "design_finding_ledger": self.store.relative(ledger_path),
            }
            version_label = f"v{version}"
            if version_label not in versions:
                versions.append(version_label)
            merged_artifacts = {**merged_artifacts, **design_paths}
            quality_reports = _quality_reports(state, validation, versions, design_paths)
            vertical_validation_summary = _dict_value(quality_reports.get("design_validation"))
            vertical_validation_summary["mode"] = GRAPH_VERSION
            quality_reports["design_validation"] = vertical_validation_summary
            quality_reports["design_vertical_graph"] = {
                "ok": validation.ok,
                "mode": GRAPH_VERSION,
                "graph_revision": graph.get("revision"),
                "graph_digest": graph.get("artifact_digest"),
                "graph": design_paths["vertical_design_graph"],
                "projection_manifest": design_paths["design_projection_manifest"],
                "finding_ledger": design_paths["design_finding_ledger"],
            }
            quality_reports["design_bundle"] = {
                "ok": validation.ok and not bundle_blockers,
                "contract_version": DESIGN_BUNDLE_VERSION,
                "component_count": len(_dict_items(projections["component_view"].get("components"))),
                "ui_count": len(_dict_items(projections["surface_view"].get("ui"))),
                "api_count": len(_dict_items(projections["surface_view"].get("api"))),
                "business_rule_count": len(_dict_items(projections["surface_view"].get("business_rules"))),
                "data_count": len(_dict_items(projections["surface_view"].get("data"))),
            }
            if trace_validation is not None:
                quality_reports["vertical_trace"] = {
                    **trace_validation.to_dict(),
                    "graph": design_paths["vertical_trace_graph"],
                    "report": design_paths["vertical_trace_validation"],
                }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": merged_artifacts,
                    "quality_reports": quality_reports,
                    "designer_execution": {
                        "contract_version": GRAPH_VERSION,
                        "status": "validated" if validation.ok else "repair_needed",
                        "session_ordinal": ordinal,
                        "design_version": version,
                        **execution.progress,
                        "blocking_count": len(validation.blocking_findings),
                        "warning_count": len(validation.warnings),
                    },
                }
            )
            self.store.write_state(state)
            if validation.ok:
                break
            current_digest = str(graph.get("artifact_digest") or "")
            if repair_iteration >= self.settings.design_repair_limit or (prior_digest and current_digest == prior_digest):
                break
            prior_digest = current_digest
            repair_feedback = [item.to_dict() for item in validation.blocking_findings]

        if validation is None or graph is None:
            raise HarnessError("designer_failed", "L3 Designer did not produce a validated graph.", run_id=run_id)
        if not validation.ok:
            error = HarnessError(
                "design_repair_stalled" if prior_digest == str(graph.get("artifact_digest") or "") else "design_repair_exhausted",
                f"Vertical Design graph still has {len(validation.blocking_findings)} blocker(s).",
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": "design_blocked",
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_design", "get_status", "cancel_run"],
                    "last_error": error.to_response(),
                    "summary_for_user": error.message,
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        last_sync = sync_design_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
        checkpoint_id = f"checkpoint_design_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_review",
                "status": "waiting_design_approval",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "design_approval",
                    "artifact_ref": _artifact_ref(design_paths["design_artifact"]),
                    "artifact_path": design_paths["design_artifact"],
                    "vertical_design_graph": design_paths["vertical_design_graph"],
                    "projection_manifest": design_paths["design_projection_manifest"],
                    "design_bundle_contract_version": DESIGN_BUNDLE_VERSION,
                    **(
                        {
                            "vertical_trace_graph": design_paths["vertical_trace_graph"],
                            "vertical_trace_validation": design_paths["vertical_trace_validation"],
                            "vertical_trace_contract_version": VERTICAL_TRACE_GRAPH_VERSION,
                            "vertical_trace_mode": str(state.get(VERTICAL_TRACE_MODE_STATE_KEY) or ""),
                        }
                        if "vertical_trace_graph" in design_paths
                        else {}
                    ),
                },
                DESIGN_BUNDLE_STATE_KEY: DESIGN_BUNDLE_VERSION,
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "designer_execution": {
                    **_dict_value(state.get("designer_execution")),
                    "contract_version": GRAPH_VERSION,
                    "status": "completed",
                    "graph_revision": graph.get("revision"),
                    "graph_digest": graph.get("artifact_digest"),
                },
                "summary_for_user": (
                    f"System Design Bundle {versions[-1]} đã pass: 0 blocker, "
                    f"{len(validation.warnings)} warning; đang chờ review Design checkpoint."
                ),
                "designer_repair_feedback": [],
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _latest_vertical_graph(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        required: bool = True,
    ) -> dict[str, object] | None:
        reference = _string_dict(state.get("latest_artifacts")).get("vertical_design_graph", "")
        if not reference:
            if required:
                raise HarnessError("vertical_design_graph_missing", "Run has no canonical vertical Design graph.", run_id=run_id)
            return None
        path = Path(reference)
        resolved = path.resolve() if path.is_absolute() else (self.store.repo_root / path).resolve()
        try:
            resolved.relative_to(self.store.run_dir(run_id).resolve())
        except ValueError as exc:
            raise HarnessError("vertical_design_graph_invalid", "Vertical Design graph escapes its run directory.", run_id=run_id) from exc
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("vertical_design_graph_invalid", "Vertical Design graph is not valid JSON.", run_id=run_id) from exc
        return validate_vertical_design_graph(payload, expected_run_id=run_id)

    def _run_progressive_validation_loop(
        self,
        *,
        state: dict[str, object],
        approved: object,
        design_model: str,
        base_analysis: str,
        artifact_paths: dict[str, str],
        start_version: int,
        existing_versions: list[str],
    ) -> dict[str, object]:
        """Run production Phase 2 through the progressive validation gates."""

        # Test doubles written for the legacy loop remain useful for focused unit
        # tests; production always uses DesignValidator.preflight.
        if not callable(getattr(self.design_validator, "preflight", None)):
            return self._run_validation_loop(
                state=state,
                approved=approved,
                design_model=design_model,
                base_analysis=base_analysis,
                artifact_paths=artifact_paths,
                start_version=start_version,
                existing_versions=existing_versions,
            )

        run_id = str(state.get("run_id") or "")
        versions = list(existing_versions)
        merged_artifacts = dict(artifact_paths)
        for stale_key in ("design_repair_ledger", "last_design_candidate", "last_candidate_validation_report"):
            merged_artifacts.pop(stale_key, None)
        previous_quality_report = ""
        repair_feedback = ""
        pending_semantic: list[SemanticCandidate] = []
        seen_states: dict[str, int] = {}
        trajectory: list[dict[str, object]] = []
        validation: DesignValidationResult | None = None
        design_paths: dict[str, str] = {}
        stalled = False
        oscillating = False
        passed = False
        inconclusive = False
        repair_limit = self.settings.design_repair_limit

        for repair_iteration in range(repair_limit + 1):
            version = start_version + repair_iteration
            version_label = f"v{version}"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_validating" if repair_iteration == 0 else "design_repairing",
                    "status": "design_validating" if repair_iteration == 0 else "design_repairing",
                    "summary_for_user": (
                        f"Đang chạy progressive Design Validator cho V{version}."
                        if repair_iteration == 0
                        else f"Designer đang repair design vòng {repair_iteration}/{repair_limit}."
                    ),
                }
            )
            self.store.write_state(state)

            if repair_iteration > 0:
                self.progressive.llm.require(1, category="designer_repair")
                design_model = self.designer.update_design(
                    requirement_packet=_designer_requirement_packet(
                        state=state,
                        requirement_packet=approved.requirement_packet,
                        run_id=run_id,
                    ),
                    formal_use_case=approved.formal_use_case,
                    current_design=design_model,
                    audit_report=repair_feedback,
                    canonical_requirement=approved.canonical_model,
                )

            parsed = parse_design_model(design_model)
            gate_stage = "deterministic_preflight"
            gate_details: dict[str, object] = {}
            disputed_findings: list[DesignFinding] = []
            traceability = _traceability_not_run(approved.canonical_model)
            selected_findings = (
                _profile_component_routing_findings(
                    state=state,
                    class_graph=parsed.class_graph,
                    run_id=run_id,
                )
                + self.design_validator.preflight(
                    class_graph=parsed.class_graph,
                    sequence_graph=parsed.sequence_graph,
                )
            )[:3]
            pending_after_gate: list[SemanticCandidate] = []

            if not selected_findings:
                gate_stage = "traceability"
                self.progressive.llm.require(2, category="traceability")
                traceability = self.validator.construct_traceability(
                    requirement_packet=approved.requirement_packet,
                    class_graph=parsed.class_graph,
                    sequence_graph=parsed.sequence_graph,
                    canonical_requirement=approved.canonical_model,
                    attempt_recorder=self._traceability_attempt_recorder(
                        state=state,
                        run_id=run_id,
                        design_version=version,
                    ),
                )
                selected_findings = _missing_link_findings(traceability)[:3]

            if not selected_findings and gate_stage == "traceability":
                if pending_semantic:
                    gate_stage = "semantic_targeted_verification"
                    self.progressive.llm.require(1, category="semantic_targeted_verification")
                    unresolved = self.progressive.verify_repair(
                        pending_semantic,
                        design_model=design_model,
                        class_graph=parsed.class_graph,
                        sequence_graph=parsed.sequence_graph,
                    )
                    if unresolved:
                        pending_after_gate = unresolved[:2]
                        selected_findings = _candidate_findings(pending_after_gate)
                    else:
                        pending_semantic = []

                if not selected_findings:
                    gate_stage = "semantic_consensus"
                    self.progressive.llm.require(
                        self.progressive.validation_trials,
                        category="semantic_claim_reviews",
                    )
                    confirmed, disputed, semantic_report = self.progressive.semantic_trials(
                        claims=_build_progressive_claims(traceability),
                        requirement_model=approved.canonical_model.to_dict(),
                        design_model=design_model,
                        class_graph=parsed.class_graph,
                        sequence_graph=parsed.sequence_graph,
                    )
                    gate_details = semantic_report
                    disputed_findings = _candidate_findings(disputed)
                    if not bool(semantic_report.get("complete")):
                        inconclusive = True
                    elif confirmed:
                        pending_after_gate = confirmed[:2]
                        selected_findings = _candidate_findings(pending_after_gate)
                    elif disputed:
                        gate_stage = "bounded_adjudication"
                        candidates = _prioritize_candidates(disputed)[:3]
                        self.progressive.llm.require(1, category="semantic_adjudication")
                        supported, uncertain, adjudication = self.progressive.adjudicate(
                            candidates,
                            requirement_model=approved.canonical_model.to_dict(),
                            design_model=design_model,
                        )
                        gate_details = {**semantic_report, "adjudication": adjudication}
                        if supported:
                            pending_after_gate = supported[:2]
                            selected_findings = _candidate_findings(pending_after_gate)
                            disputed_findings = _candidate_findings(uncertain)
                        elif uncertain:
                            disputed_findings = _candidate_findings(uncertain)
                            inconclusive = True
                        else:
                            disputed_findings = []

            validation = _progressive_validation_result(
                traceability=traceability,
                blockers=selected_findings,
                disputed=disputed_findings,
                gate_stage=gate_stage,
                details=gate_details,
                repair_iteration=repair_iteration,
                inconclusive=inconclusive,
                llm_calls=self.progressive.llm.summary(),
            )
            design_paths = write_phase2_design_artifacts(
                store=self.store,
                run_id=run_id,
                designer_analysis=_analysis_for_version(
                    base_analysis=base_analysis,
                    version=version,
                    previous_quality_report=previous_quality_report,
                ),
                design_model=design_model,
                requirement_graph=traceability.requirement_graph,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                class_trace=traceability.class_trace,
                sequence_trace=traceability.sequence_trace,
                missing_links=traceability.missing_links,
                requirement_packet=approved.requirement_packet,
                formal_use_case=approved.formal_use_case,
                version=version,
                repair_iterations=repair_iteration,
                traceability_mode=traceability.mode,
                requirement_contract_version=approved.contract_version,
                requirement_artifact_refs=approved.approved_artifacts,
                requirement_revision=approved.canonical_model.revision,
                design_validation=validation.to_dict(),
            )
            if version_label not in versions:
                versions.append(version_label)
            merged_artifacts = {**merged_artifacts, **design_paths}
            repair_state = _repair_state_fingerprint(validation=validation)
            trajectory.append(
                {
                    "version": version_label,
                    "repair_iteration": repair_iteration,
                    "gate_stage": gate_stage,
                    "blocking_count": len(validation.blocking_findings),
                    "disputed_count": len(validation.disputed_findings),
                    "fingerprint": repair_state if validation.blocking_findings else "",
                    "llm_calls_used": self.progressive.llm.used,
                    "details": gate_details,
                }
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": merged_artifacts,
                    "quality_reports": _quality_reports(state, validation, versions, design_paths),
                    "design_progressive_validation": {
                        "version": "progressive_design_validation_v1",
                        "trajectory": trajectory,
                        "llm_calls": self.progressive.llm.summary(),
                    },
                }
            )
            self.store.write_state(state)

            if validation.inconclusive:
                break
            if validation.ok:
                passed = True
                break
            if repair_state in seen_states:
                stalled = seen_states[repair_state] == repair_iteration - 1
                oscillating = not stalled
                break
            seen_states[repair_state] = repair_iteration
            if repair_iteration >= repair_limit:
                break
            repair_feedback = validation.feedback
            pending_semantic = pending_after_gate
            previous_quality_report = design_paths["design_quality_report"]

        if validation is None:
            raise HarnessError("designer_failed", "Phase 2 did not produce a progressive validation result.", run_id=run_id)
        if not passed:
            if inconclusive:
                error_code = "design_validation_inconclusive"
            elif stalled:
                error_code = "design_repair_stalled"
            elif oscillating:
                error_code = "design_repair_oscillating"
            else:
                error_code = "design_repair_exhausted"
            error = HarnessError(
                error_code,
                f"Design progressive validation stopped with {len(validation.blocking_findings)} blocker(s).",
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": "blocked",
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_design", "get_status", "request_change", "cancel_run"],
                    "last_error": error.to_response(),
                    "summary_for_user": (
                        f"Design chưa pass progressive validator: {len(validation.blocking_findings)} blocker, "
                        f"{len(validation.disputed_findings)} disputed; trạng thái={error_code}."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        last_sync = sync_design_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
        checkpoint_id = f"checkpoint_design_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_review",
                "status": "waiting_design_approval",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "design_approval",
                    "artifact_ref": _artifact_ref(design_paths["design_artifact"]),
                    "artifact_path": design_paths["design_artifact"],
                },
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "quality_reports": _quality_reports(state, validation, versions, design_paths),
                "summary_for_user": (
                    f"Design {versions[-1]} đã pass progressive validator: 0 blocker, "
                    f"0 disputed; sync status={last_sync.get('status')}. "
                    "Run đang chờ anh review design trước khi sang Phase 3."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _run_validation_loop(
        self,
        *,
        state: dict[str, object],
        approved: object,
        design_model: str,
        base_analysis: str,
        artifact_paths: dict[str, str],
        start_version: int,
        existing_versions: list[str],
        sync_checkpoint: bool = True,
        experiment_mode: bool = False,
    ) -> dict[str, object]:
        run_id = str(state.get("run_id") or "")
        versions = list(existing_versions)
        merged_artifacts = dict(artifact_paths)
        for stale_key in ("design_repair_ledger", "last_design_candidate", "last_candidate_validation_report"):
            merged_artifacts.pop(stale_key, None)
        design_paths: dict[str, str] = {}
        validation: DesignValidationResult | None = None
        previous_quality_report = ""
        seen_states: dict[str, int] = {}
        stalled = False
        oscillating = False
        passed = False
        inconclusive = False
        trajectory: list[dict[str, object]] = []
        repair_limit = self.settings.design_repair_limit

        for repair_iteration in range(repair_limit + 1):
            version = start_version + repair_iteration
            version_label = f"v{version}"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_validating" if repair_iteration == 0 else "design_repairing",
                    "status": "design_validating" if repair_iteration == 0 else "design_repairing",
                    "summary_for_user": (
                        f"Đang chạy Design Validator hybrid cho V{version}."
                        if repair_iteration == 0
                        else f"Designer đang repair design vòng {repair_iteration}/{repair_limit}."
                    ),
                }
            )
            self.store.write_state(state)

            if repair_iteration > 0:
                design_model = self.designer.update_design(
                    requirement_packet=_designer_requirement_packet(
                        state=state,
                        requirement_packet=approved.requirement_packet,
                        run_id=run_id,
                    ),
                    formal_use_case=approved.formal_use_case,
                    current_design=design_model,
                    audit_report=validation.feedback if validation is not None else "",
                    canonical_requirement=approved.canonical_model,
                )

            parsed = parse_design_model(design_model)
            traceability = self.validator.construct_traceability(
                requirement_packet=approved.requirement_packet,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                canonical_requirement=approved.canonical_model,
                attempt_recorder=self._traceability_attempt_recorder(
                    state=state,
                    run_id=run_id,
                    design_version=version,
                ),
            )
            validation = self.design_validator.review(
                requirement_packet=approved.requirement_packet,
                design_artifact=design_model,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                traceability=traceability,
                canonical_requirement=approved.canonical_model,
                repair_iterations=repair_iteration,
                validation_trials=self.settings.design_validation_trials,
            )
            design_paths = write_phase2_design_artifacts(
                store=self.store,
                run_id=run_id,
                designer_analysis=_analysis_for_version(
                    base_analysis=base_analysis,
                    version=version,
                    previous_quality_report=previous_quality_report,
                ),
                design_model=design_model,
                requirement_graph=traceability.requirement_graph,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
                class_trace=traceability.class_trace,
                sequence_trace=traceability.sequence_trace,
                missing_links=traceability.missing_links,
                requirement_packet=approved.requirement_packet,
                formal_use_case=approved.formal_use_case,
                version=version,
                repair_iterations=repair_iteration,
                traceability_mode=traceability.mode,
                requirement_contract_version=approved.contract_version,
                requirement_artifact_refs=approved.approved_artifacts,
                requirement_revision=approved.canonical_model.revision,
                design_validation=validation.to_dict(),
            )
            if version_label not in versions:
                versions.append(version_label)
            merged_artifacts = {**merged_artifacts, **design_paths}
            quality_reports = _quality_reports(
                state,
                validation,
                versions,
                design_paths,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": merged_artifacts,
                    "quality_reports": quality_reports,
                }
            )
            repair_state = _repair_state_fingerprint(validation=validation)
            consensus = validation.semantic_consensus
            trajectory_entry = {
                "version": version_label,
                "repair_iteration": repair_iteration,
                "blocking_count": len(validation.blocking_findings),
                "warning_count": len(validation.warnings),
                "disputed_count": len(validation.disputed_findings),
                "inconclusive": validation.inconclusive,
                "blocker_keys": sorted(item.canonical_key for item in validation.blocking_findings),
                "fingerprint": repair_state,
                "requested_trials": consensus.get("requested_trials", 1),
                "valid_trials": consensus.get("valid_trials", 1),
                "quorum": consensus.get("quorum", 1),
                "consistency_score": consensus.get("consistency_score", 1.0),
            }
            trajectory.append(trajectory_entry)
            if experiment_mode:
                experiment = _dict_value(state.get("experiment"))
                experiment["trajectory"] = trajectory
                state["experiment"] = experiment
            self.store.write_state(state)
            if validation.inconclusive:
                inconclusive = True
                break
            if validation.ok:
                passed = True
                break
            if repair_state in seen_states:
                stalled = seen_states[repair_state] == repair_iteration - 1
                oscillating = not stalled
                break
            seen_states[repair_state] = repair_iteration
            if repair_iteration >= repair_limit:
                break
            previous_quality_report = design_paths["design_quality_report"]

        if validation is None:
            raise HarnessError("designer_failed", "Phase 2 did not produce a design validation result.", run_id=run_id)
        if not passed:
            if inconclusive:
                error_code = "design_validation_inconclusive"
            elif stalled:
                error_code = "design_repair_stalled"
            elif oscillating:
                error_code = "design_repair_oscillating"
            else:
                error_code = "design_repair_exhausted"
            error = HarnessError(
                error_code,
                f"Design still has {len(validation.blocking_findings)} blocking finding(s).",
                run_id=run_id,
            )
            terminal_status = (
                {
                    "design_validation_inconclusive": "design_experiment_inconclusive",
                    "design_repair_stalled": "design_experiment_stalled",
                    "design_repair_oscillating": "design_experiment_oscillating",
                    "design_repair_exhausted": "design_experiment_exhausted",
                }[error_code]
                if experiment_mode
                else "blocked"
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": terminal_status,
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ["get_status"]
                        if experiment_mode
                        else ["revalidate_design", "get_status", "request_change", "cancel_run"]
                    ),
                    "last_error": error.to_response(),
                    "summary_for_user": (
                        f"Design chưa pass hybrid validator: còn {len(validation.blocking_findings)} blocker, "
                        f"{len(validation.warnings)} warning; trạng thái={error_code}."
                    ),
                }
            )
            if experiment_mode:
                self._write_experiment_report(state)
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if experiment_mode:
            disputed_blockers = sum(
                1 for item in validation.disputed_findings if item.severity == "blocking"
            )
            status = (
                "design_experiment_converged_strong"
                if disputed_blockers == 0
                else "design_experiment_converged_by_consensus"
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_experiment",
                    "status": status,
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status"],
                    "last_error": None,
                    "last_sync": {"status": "disabled", "mode": "none"},
                    "quality_reports": _quality_reports(state, validation, versions, design_paths),
                    "summary_for_user": (
                        f"Thử nghiệm design consensus hội tụ tại {versions[-1]}: "
                        f"0 blocker đồng thuận, {len(validation.disputed_findings)} finding còn tranh chấp."
                    ),
                }
            )
            self._write_experiment_report(state)
            self.store.write_state(state)
            return self._status_response(state, created_new_run=True)

        if not sync_checkpoint:
            raise HarnessError("internal_error", "Checkpoint sync disabled outside experiment mode.", run_id=run_id)
        last_sync = sync_design_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
        checkpoint_id = f"checkpoint_design_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_review",
                "status": "waiting_design_approval",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "design_approval",
                    "artifact_ref": _artifact_ref(design_paths["design_artifact"]),
                    "artifact_path": design_paths["design_artifact"],
                },
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "quality_reports": _quality_reports(
                    state,
                    validation,
                    versions,
                    design_paths,
                ),
                "summary_for_user": (
                    f"Design V{start_version + len(versions) - len(existing_versions) - 1} đã pass hybrid validator: "
                    f"0 blocker, {len(validation.warnings)} warning; sync status={last_sync.get('status')}. "
                    "Run đang chờ anh review design trước khi sang Phase 3."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _write_experiment_report(self, state: dict[str, object]) -> None:
        run_id = str(state.get("run_id") or "")
        experiment = _dict_value(state.get("experiment"))
        trajectory = experiment.get("trajectory") if isinstance(experiment.get("trajectory"), list) else []
        payload = {
            "version": "design_consensus_experiment_v1",
            "run_id": run_id,
            "source_run_id": experiment.get("source_run_id"),
            "source_ref": state.get("source_ref"),
            "status": state.get("status"),
            "stop_reason": _experiment_stop_reason(str(state.get("status") or "")),
            "llm": state.get("llm"),
            "validation_trials": experiment.get("validation_trials"),
            "quorum": experiment.get("quorum"),
            "repair_limit": experiment.get("repair_limit"),
            "sync_disabled": True,
            "trajectory": trajectory,
            "quality_reports": state.get("quality_reports", {}),
            "design_progressive_validation": state.get("design_progressive_validation"),
            "last_error": state.get("last_error"),
            "completed_at": utc_now_iso(),
        }
        json_path = self.store.write_json_artifact(run_id, "design_consensus_experiment.json", payload)
        markdown_path = self.store.artifact_path(run_id, "design_consensus_experiment.md")
        rows = [
            "| Version | Blockers | Warnings | Disputed | Valid trials | Consistency |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for item in trajectory:
            if not isinstance(item, dict):
                continue
            rows.append(
                "| "
                + " | ".join(
                    [
                        str(item.get("version") or ""),
                        str(item.get("blocking_count") or 0),
                        str(item.get("warning_count") or 0),
                        str(item.get("disputed_count") or 0),
                        f"{item.get('valid_trials', 0)}/{item.get('requested_trials', 0)}",
                        str(item.get("consistency_score") or 0),
                    ]
                )
                + " |"
            )
        markdown_path.write_text(
            "\n".join(
                [
                    "# Design Validator Consensus Experiment",
                    "",
                    f"- Experiment run: `{run_id}`",
                    f"- Source run: `{experiment.get('source_run_id', '')}`",
                    f"- Status: `{state.get('status', '')}`",
                    f"- Stop reason: `{payload['stop_reason']}`",
                    f"- Trials/quorum: `{experiment.get('validation_trials', '')}/{experiment.get('quorum', '')}`",
                    f"- Repair limit: `{experiment.get('repair_limit', '')}`",
                    "- External sync: `disabled`",
                    "",
                    *rows,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        latest = _string_dict(state.get("latest_artifacts"))
        latest["design_consensus_experiment"] = self.store.relative(json_path)
        latest["design_consensus_experiment_markdown"] = self.store.relative(markdown_path)
        state["latest_artifacts"] = latest

    def _traceability_attempt_recorder(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        design_version: int,
    ):
        """Return a sink that persists each raw trace attempt before later gates."""

        def record(attempt: dict[str, object]) -> None:
            refs = write_phase2_traceability_attempt(
                store=self.store,
                run_id=run_id,
                design_version=design_version,
                attempt=attempt,
            )
            history_value = state.get("traceability_diagnostics")
            history = list(history_value) if isinstance(history_value, list) else []
            history.append(
                {
                    "design_version": f"v{design_version}",
                    "diagram": attempt.get("diagram"),
                    "attempt": attempt.get("attempt"),
                    "finish_reason": attempt.get("finish_reason"),
                    "usage": attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {},
                    "parse_status": attempt.get("parse_status"),
                    "error": attempt.get("error"),
                    **refs,
                }
            )
            state["traceability_diagnostics"] = history[-12:]
            self.store.write_state(state)

        return record

    def _handle_error(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        error: HarnessError,
    ) -> dict[str, object]:
        requirement_error = _requirement_blocking_error(error, run_id=run_id)
        retryable_validator = error.error_code in {
            "design_semantic_review_failed",
            "design_llm_budget_exhausted",
            "validator_traceability_failed",
        }
        if requirement_error is not None or retryable_validator:
            effective = requirement_error or error
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_design", "get_status", "request_change", "cancel_run"],
                    "last_error": effective.to_response(),
                    "summary_for_user": f"Phase 2 bị chặn: {effective.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "status": "failed",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": error.to_response(),
                "summary_for_user": f"Phase 2 lỗi khi tạo hoặc kiểm tra design: {error.message}",
            }
        )
        self.store.write_state(state)
        if not error.run_id:
            raise HarnessError(error.error_code, error.message, run_id=run_id) from error
        raise error

    def _require_requirement_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        if str(state.get("status")) != "waiting_requirement_approval":
            raise HarnessError("checkpoint_not_current", f"Run is not waiting for requirement approval: {run_id}", run_id=run_id)
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError("no_pending_checkpoint", f"Run does not have a pending checkpoint: {run_id}", run_id=run_id)
        if checkpoint.get("type") != "requirement_approval":
            raise HarnessError("checkpoint_not_current", f"Pending checkpoint is not a requirement checkpoint: {run_id}", run_id=run_id)
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError("checkpoint_not_current", f"Checkpoint is not current: {checkpoint_id}", run_id=run_id)

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        return {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": state.get("latest_artifacts", {}),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": state.get("allowed_next_actions", []),
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "design_progressive_validation": state.get("design_progressive_validation"),
            "traceability_diagnostics": state.get("traceability_diagnostics", []),
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
            "requirement_contract_version": state.get("requirement_contract_version", "v2"),
            REPO_DISCOVERY_CONTRACT_STATE_KEY: state.get(REPO_DISCOVERY_CONTRACT_STATE_KEY),
            DESIGNER_CONTRACT_STATE_KEY: state.get(DESIGNER_CONTRACT_STATE_KEY),
            DESIGN_BUNDLE_STATE_KEY: state.get(DESIGN_BUNDLE_STATE_KEY),
            VERTICAL_TRACE_STATE_KEY: state.get(VERTICAL_TRACE_STATE_KEY),
            VERTICAL_TRACE_MODE_STATE_KEY: state.get(VERTICAL_TRACE_MODE_STATE_KEY),
            "designer_execution": state.get("designer_execution"),
        }


def _vertical_trace_repair_feedback(
    *,
    state: dict[str, object],
    approved: object,
    graph: dict[str, object],
) -> list[dict[str, object]]:
    """Preflight the persisted graph so enforce revalidation starts with exact L4 blockers."""

    if (
        str(state.get(VERTICAL_TRACE_STATE_KEY) or "") != VERTICAL_TRACE_GRAPH_VERSION
        or str(state.get(VERTICAL_TRACE_MODE_STATE_KEY) or "") != "enforce"
    ):
        return []
    canonical_requirement = getattr(approved, "canonical_model", None)
    if canonical_requirement is None:
        return []
    trace_graph = build_vertical_trace_graph(
        requirement_model=canonical_requirement,
        design_graph=graph,
    )
    result = validate_vertical_trace_coverage(trace_graph, mode="enforce")
    return [item.to_dict() for item in result.blocking_findings]


def _quality_reports(
    state: dict[str, object],
    validation: DesignValidationResult,
    versions: list[str],
    paths: dict[str, str],
) -> dict[str, object]:
    gate_stage = str(validation.llm_review.get("gate_stage") or validation.rule_review.get("gate_stage") or "")
    traceability_ran = validation.traceability.mode != "not_run_preflight_blocked"
    summary = (
        f"{len(validation.blocking_findings)} blocker(s), {len(validation.warnings)} warning(s); "
        f"gate={gate_stage or 'legacy'}; "
        f"traceability={'not_run' if not traceability_ran else ('passed' if validation.traceability.ok else 'failed')}."
    )
    return {
        **_dict_value(state.get("quality_reports")),
        "design_traceability": {
            "ok": validation.traceability.ok if traceability_ran else False,
            "ran": traceability_ran,
            "mode": validation.traceability.mode,
            "versions": versions,
            "summary": _traceability_summary(validation.traceability.missing_links),
            "report": paths["design_quality_report"],
        },
        "design_validation": {
            "ok": validation.ok,
            "mode": validation.mode,
            "gate_stage": gate_stage,
            "versions": versions,
            "blocking_count": len(validation.blocking_findings),
            "warning_count": len(validation.warnings),
            "disputed_count": len(validation.disputed_findings),
            "inconclusive": validation.inconclusive,
            "requested_trials": validation.semantic_consensus.get("requested_trials", 1),
            "valid_trials": validation.semantic_consensus.get("valid_trials", 1),
            "quorum": validation.semantic_consensus.get("quorum", 1),
            "consistency_score": validation.semantic_consensus.get("consistency_score", 1.0),
            "llm_calls": validation.semantic_consensus.get("llm_calls", {}),
            "summary": summary,
            "report": paths["design_validation_report"],
        },
    }


def _vertical_snapshot_binding(
    *,
    state: dict[str, object],
    approved: object,
    landscape: dict[str, object],
    workspace: object,
) -> dict[str, object]:
    requirement_payload = approved.canonical_model.to_dict()
    requirement_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            requirement_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    profile = _dict_value(state.get("profile_snapshot"))
    artifacts = _string_dict(state.get("latest_artifacts"))
    return {
        "requirement_contract_version": approved.contract_version,
        "requirement_revision": approved.canonical_model.revision,
        "requirement_artifact_ref": approved.approved_artifacts.get("requirement_model"),
        "requirement_digest": requirement_digest,
        "repo_landscape_artifact_ref": artifacts.get("repo_landscape"),
        "repo_landscape_digest": landscape.get("artifact_digest"),
        "project_profile_id": profile.get("project_profile_id"),
        "project_profile_schema": profile.get("project_profile_schema"),
        "language_profile_id": profile.get("language_profile_id"),
        "language_profile_schema": profile.get("language_profile_schema"),
        "target_manifest_digest": profile.get("target_manifest_digest"),
        "workspace_ref": "run_workspace_v1",
        "base_sha": getattr(workspace, "base_sha", ""),
    }


def _traceability_not_run(canonical_model: object) -> TraceabilityResult:
    return TraceabilityResult(
        requirement_graph=build_requirement_graph_v2(canonical_model),
        class_trace=[],
        sequence_trace=[],
        missing_links={"class_diagram": [], "sequence_diagram": []},
        mode="not_run_preflight_blocked",
    )


def _designer_requirement_packet(
    *,
    state: dict[str, object],
    requirement_packet: str,
    run_id: str,
) -> str:
    """Attach run-frozen component ownership rules only to Designer input."""

    directive = _profile_component_routing_directive(state=state, run_id=run_id)
    if not directive:
        return requirement_packet
    return f"{requirement_packet.rstrip()}\n\n{directive}"


def _profile_component_routing_directive(*, state: dict[str, object], run_id: str) -> str:
    snapshot = read_profile_snapshot(state, run_id=run_id)
    if snapshot is None:
        return ""
    project, language = snapshot.resolve_profiles(run_id=run_id)
    policy = build_planning_path_policy(project=project, language=language)
    component_ids = ", ".join(f"`<<{rule.component_id}>>`" for rule in policy.component_rules)
    return "\n".join(
        [
            "# Project Component Routing Contract (Designer-only instruction)",
            f"Project profile: `{policy.project_profile_id}`; language profile: `{policy.language_profile_id}`.",
            (
                "Every class, interface, abstract class, and enum declaration in the PlantUML class diagram "
                f"must declare exactly one ownership stereotype from: {component_ids}."
            ),
            "The PlantUML declaration keyword still defines the symbol kind; the stereotype defines component ownership.",
            "Use `<<web>>` for views/pages/forms/UI components.",
            "Use `<<api>>` for controllers/services/use cases/validators/gateways/adapters/guards.",
            "Use `<<contracts>>` for DTOs/requests/responses/results and shared domain models.",
            "Use `<<database>>` for repositories/entities/records/stores.",
            "Do not invent a component and do not leave ownership implicit.",
        ]
    )


def _profile_component_routing_findings(
    *,
    state: dict[str, object],
    class_graph: dict[str, object],
    run_id: str,
) -> list[DesignFinding]:
    """Fail Phase 2 before approval when Phase 3 cannot route a design symbol."""

    snapshot = read_profile_snapshot(state, run_id=run_id)
    if snapshot is None:
        return []
    project, language = snapshot.resolve_profiles(run_id=run_id)
    policy = build_planning_path_policy(project=project, language=language)
    failures: list[tuple[str, HarnessError]] = []
    nodes = class_graph.get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        try:
            policy.infer_component(node=node, class_name=class_name, run_id=run_id)
        except HarnessError as exc:
            if exc.error_code not in DESIGN_ROUTING_ERROR_CODES:
                raise
            failures.append((class_name, exc))
    if not failures:
        return []

    component_ids = ", ".join(rule.component_id for rule in policy.component_rules)
    class_names = ", ".join(name for name, _ in failures)
    return [
        DesignFinding(
            finding_id="RULE-PROFILE-COMPONENT-ROUTING",
            category="project_component_routing",
            severity="blocking",
            message=f"Design symbols do not have unambiguous project-component ownership: {class_names}.",
            expected_change=(
                "Add or correct exactly one supported component stereotype on every listed declaration "
                f"({component_ids}); do not use a default/fallback component."
            ),
            diagram_ids=["class_diagram"],
            evidence=[
                {
                    "source": "class_diagram",
                    "ref": class_name,
                    "excerpt": f"{exc.error_code}: {exc.message}",
                }
                for class_name, exc in failures
            ],
            subject_ref=policy.project_profile_id,
            origin="deterministic",
        )
    ]


def _build_progressive_claims(traceability: TraceabilityResult) -> list[dict[str, object]]:
    links: dict[str, set[str]] = {}
    for trace_items in (traceability.class_trace, traceability.sequence_trace):
        for item in trace_items:
            requirement_id = str(item.get("requirement") or "")
            for link in item.get("links", []) if isinstance(item.get("links"), list) else []:
                if isinstance(link, dict) and str(link.get("target") or ""):
                    links.setdefault(requirement_id, set()).add(str(link["target"]))
    claims: list[dict[str, object]] = []
    requirements = traceability.requirement_graph.get("requirements", [])
    for item in requirements if isinstance(requirements, list) else []:
        if not isinstance(item, dict):
            continue
        requirement_id = str(item.get("id") or "")
        if requirement_id:
            claims.append(
                {
                    "claim_id": f"CLAIM-{requirement_id}",
                    "requirement_id": requirement_id,
                    "section": str(item.get("section") or ""),
                    "content": str(item.get("content") or ""),
                    "allowed_design_refs": sorted(links.get(requirement_id, set())),
                }
            )
    return claims


def _candidate_findings(candidates: list[SemanticCandidate]) -> list[DesignFinding]:
    return [
        DesignFinding(
            finding_id=item.candidate_id,
            category=item.category,
            severity="blocking",
            message=item.message,
            expected_change=item.expected_change,
            requirement_ids=[item.requirement_id],
            diagram_ids=[],
            evidence=[{"source": "design", "ref": ref, "excerpt": ref} for ref in item.design_refs],
            subject_ref=item.claim_id,
            support_count=item.support_count,
            trial_ids=item.trial_ids,
            origin="semantic",
        )
        for item in candidates
    ]


def _prioritize_candidates(candidates: list[SemanticCandidate]) -> list[SemanticCandidate]:
    priority = {
        "terminal_state": 0,
        "behavior_order": 1,
        "data_consistency": 2,
        "cross_diagram_contract": 3,
        "transaction_boundary": 4,
        "unsupported_assumption": 5,
    }
    return sorted(candidates, key=lambda item: (priority.get(item.category, 99), item.key))


def _progressive_validation_result(
    *,
    traceability: TraceabilityResult,
    blockers: list[DesignFinding],
    disputed: list[DesignFinding],
    gate_stage: str,
    details: dict[str, object],
    repair_iteration: int,
    inconclusive: bool,
    llm_calls: dict[str, object],
) -> DesignValidationResult:
    semantic_consensus = {
        key: details[key]
        for key in (
            "requested_trials",
            "valid_trials",
            "quorum",
            "confirmed_count",
            "disputed_count",
            "complete",
            "attempt_count",
            "max_attempts",
            "invalid_trial_count",
            "invalid_trials",
        )
        if key in details
    }
    semantic_consensus["llm_calls"] = llm_calls
    return DesignValidationResult(
        traceability=traceability,
        blocking_findings=blockers,
        warnings=[],
        rule_review={
            "ok": not blockers and gate_stage != "deterministic_preflight",
            "gate_stage": gate_stage,
            "findings": [item.to_dict() for item in blockers if item.origin == "deterministic"],
        },
        llm_review={"gate_stage": gate_stage, **details},
        repair_iterations=repair_iteration,
        mode="design_validation_progressive_v1",
        semantic_consensus=semantic_consensus,
        disputed_findings=disputed,
        inconclusive=inconclusive,
    )


def _bundle_design_findings(items: list[dict[str, object]]) -> list[DesignFinding]:
    return [
        DesignFinding(
            finding_id=str(item.get("id") or "BUNDLE"),
            category=str(item.get("category") or "surface_contract_missing"),
            severity="blocking",
            message=str(item.get("message") or "System Design bundle contract is incomplete."),
            expected_change=str(item.get("expected_change") or "Complete the structured surface contract."),
            requirement_ids=[str(value) for value in item.get("requirement_ids", []) if str(value)] if isinstance(item.get("requirement_ids"), list) else [],
            diagram_ids=[str(value) for value in item.get("diagram_ids", []) if str(value)] if isinstance(item.get("diagram_ids"), list) else [],
            evidence=[dict(value) for value in item.get("evidence", []) if isinstance(value, dict)] if isinstance(item.get("evidence"), list) else [],
            subject_ref=str(item.get("subject_ref") or ""),
            origin="deterministic",
        )
        for item in items
    ]


def _dict_items(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _traceability_summary(missing_links: dict[str, list[dict[str, str]]]) -> str:
    total = len(missing_links.get("class_diagram", [])) + len(missing_links.get("sequence_diagram", []))
    return "không phát hiện missing link class/sequence." if total == 0 else f"còn {total} missing link class/sequence cần review."


def _requirement_blocking_error(error: HarnessError, *, run_id: str) -> HarnessError | None:
    if error.error_code == "validator_traceability_failed" and "traceable requirement" in error.message.lower():
        return HarnessError("requirement_traceability_blocked", error.message, run_id=run_id)
    return None


def _analysis_for_version(*, base_analysis: str, version: int, previous_quality_report: str) -> str:
    if version == 1:
        return base_analysis
    return (
        f"{base_analysis.rstrip()}\n\n## Repair Context\n\n"
        f"- Design version: v{version}\n"
        f"- Previous quality report: `{previous_quality_report or 'revalidation'}`\n"
    )


def _repair_state_fingerprint(
    *,
    validation: DesignValidationResult,
) -> str:
    signatures = []
    for finding in validation.blocking_findings:
        signatures.append(finding.canonical_key)
    content = "\n".join(sorted(signatures))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _experiment_stop_reason(status: str) -> str:
    return {
        "design_experiment_converged_strong": "zero_consensus_blockers_without_disputed_blockers",
        "design_experiment_converged_by_consensus": "zero_consensus_blockers_with_minority_blocker_observations",
        "design_experiment_stalled": "same_blocker_set_repeated_consecutively",
        "design_experiment_oscillating": "previous_blocker_set_reappeared",
        "design_experiment_exhausted": "repair_limit_reached",
        "design_experiment_inconclusive": "one_or_more_semantic_trials_failed",
        "design_experiment_failed": "workflow_error",
    }.get(status, status or "unknown")


def _design_versions(state: dict[str, object]) -> list[str]:
    quality = _dict_value(_dict_value(state.get("quality_reports")).get("design_validation"))
    versions = quality.get("versions")
    return [str(item) for item in versions] if isinstance(versions, list) else []


def _next_design_version(artifact_dir: Path) -> int:
    versions = []
    for path in artifact_dir.glob("design_artifact_v*.md"):
        suffix = path.stem.rsplit("_v", 1)[-1]
        if suffix.isdigit():
            versions.append(int(suffix))
    return max(versions, default=0) + 1


def _required_artifact(artifacts: dict[str, str], key: str, *, run_id: str) -> str:
    value = str(artifacts.get(key) or "").strip()
    if not value:
        raise HarnessError("design_artifact_not_found", f"Run is missing artifact `{key}`.", run_id=run_id)
    return value


def _artifact_ref(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]
