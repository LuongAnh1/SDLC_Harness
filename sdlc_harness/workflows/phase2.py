"""Workflow Phase 2: requirement đã duyệt -> checkpoint thiết kế đã xác thực."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..agents.designer import (
    DESIGN_BUNDLE_STATE_KEY,
    DESIGNER_CONTRACT_STATE_KEY,
    GRAPH_VERSION_V1,
    SUPPORTED_GRAPH_VERSIONS,
    UIUX_CONTRACT_STATE_KEY,
    UIUX_CONTRACT_VERSION,
    UIUX_MODE_STATE_KEY,
    DesignerExecutionError,
    DesignerExecutionResult,
    DesignerToolLoopExecutor,
    build_deterministic_trace,
    design_bundle_findings,
    design_bundle_version_for,
    projection_manifest,
    render_design_projections,
    validate_vertical_design_graph,
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
    bounded_trace_view,
    build_requirement_graph_v2,
    build_vertical_trace_graph,
    validate_vertical_trace_coverage,
)
from ..artifacts import read_approved_requirement_input, read_frozen_requirement_input
from ..artifacts.writers import write_phase2_design_artifacts
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import BudgetedLlmClient, LlmClient
from ..llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from ..profiles import build_planning_path_policy, read_profile_snapshot
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint
from ..workspaces import RunWorkspaceManager
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

ALLOWED_DESIGN_PAUSED_ACTIONS = [
    "continue_run",
    "restart_design",
    "get_status",
    "cancel_run",
]

_PHASE2_ARTIFACT_KEYS = frozenset(
    {
        "designer_analysis",
        "design_artifact",
        "requirement_graph",
        "class_diagram_graph",
        "sequence_diagram_graph",
        "requirement_design_trace",
        "missing_design_links",
        "design_validation_report",
        "design_quality_report",
        "vertical_design_graph",
        "design_component_view",
        "design_surface_view",
        "vertical_trace_graph",
        "vertical_trace_validation",
        "design_projection_manifest",
        "design_finding_ledger",
    }
)

_PHASE2_QUALITY_KEYS = frozenset(
    {"design_validation", "design_vertical_graph", "design_bundle", "vertical_trace"}
)

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

    def __init__(
        self,
        settings: Settings,
        *,
        designer_step_review: dict[str, object] | None = None,
    ):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.repo_discovery = RepoDiscoveryWorkflow(settings)
        self.designer_step_review = dict(designer_step_review or {})
        self._reset_design_runtime()

    def _reset_design_runtime(self) -> None:
        self.llm = BudgetedLlmClient(
            LlmClient(self.settings),
            limit=self.settings.design_llm_call_budget,
        )
        self.design_validator = DesignValidator(self.llm)
        self.designer_tool_loop = DesignerToolLoopExecutor(
            settings=self.settings,
            store=self.store,
            llm_client=self.llm,
            step_review=self.designer_step_review,
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

        return self._run_vertical_design(
            state=state,
            approved=approved,
            start_version=1,
            existing_versions=[],
        )

    def revalidate_design(self, *, run_id: str) -> dict[str, object]:
        """Validate and, when needed, repair the latest Phase 2 design version."""

        self._reset_design_runtime()
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
        recoverable_plan_step_review = (
            status == "implementation_plan_step_review_required"
            and phase == "implementation_planning"
        )
        recoverable_plan_checkpoint = (
            status == "waiting_implementation_plan_review"
            and phase == "implementation_planning"
        )
        recoverable_vertical_design_block = (
            str(state.get(DESIGNER_CONTRACT_STATE_KEY) or "") in SUPPORTED_GRAPH_VERSIONS
            and status == "design_blocked"
            and phase.startswith("design_")
        )
        recoverable_paused_design = (
            status == "design_paused"
            and phase.startswith("design_")
            and bool(_string_dict(state.get("latest_artifacts")).get("vertical_design_graph"))
        )
        if not recoverable_trace_failure and not recoverable_routing_failure and not recoverable_plan_step_review and not recoverable_plan_checkpoint and not recoverable_vertical_design_block and not recoverable_paused_design and (
            phase != "design_review" or status not in {"waiting_design_approval", "blocked"}
        ):
            raise HarnessError(
                "design_revalidation_not_allowed",
                f"Run is not waiting on a design checkpoint: {run_id}",
                run_id=run_id,
            )
        if recoverable_paused_design:
            self._supersede_pending_design_step_review(state=state, run_id=run_id)
        if recoverable_plan_step_review:
            self._supersede_pending_plan_step_review(run_id=run_id)
        if recoverable_plan_checkpoint:
            self._supersede_pending_plan_step_review(
                run_id=run_id,
                include_approved=True,
            )
            latest_artifacts = _string_dict(state.get("latest_artifacts"))
            for key in (
                "implementation_plan",
                "implementation_plan_structured",
                "implementation_plan_review",
            ):
                latest_artifacts.pop(key, None)
            quality_reports = _dict_value(state.get("quality_reports"))
            quality_reports.pop("implementation_plan_review", None)
            state.update(
                {
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                }
            )
            self.store.write_state(state)
        approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
        versions = _design_versions(state)
        existing_graph = self._latest_vertical_graph(state, run_id=run_id, required=False)
        if existing_graph is None and recoverable_vertical_design_block:
            existing_graph = self._blocked_session_graph(state=state, run_id=run_id)
        if existing_graph is None:
            raise HarnessError(
                "vertical_design_graph_missing",
                "Run has no canonical or recoverable persisted vertical Design graph.",
                run_id=run_id,
            )
        start_version = _next_design_version(self.store.run_dir(run_id) / "artifacts")
        prior_execution = _dict_value(state.get("designer_execution"))
        session_ordinal = max(
            start_version,
            int(prior_execution.get("session_ordinal") or 0) + 1,
        )
        initial_repair_feedback = self._persisted_design_feedback(state=state, run_id=run_id)
        initial_repair_feedback.extend(design_bundle_findings(existing_graph))
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
            validate_existing_first=True,
        )

    def _supersede_pending_design_step_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
    ) -> None:
        """Keep the audit trail while discarding a paused call from stale validation policy."""

        execution = _dict_value(state.get("designer_execution"))
        pending = _dict_value(execution.get("pending_step_review"))
        if not pending:
            pending = _dict_value(execution.get("validator_step_review"))
        exchange_ref = str(pending.get("exchange_path") or "")
        if not exchange_ref:
            return
        exchange_path = Path(exchange_ref)
        resolved = (
            exchange_path.resolve()
            if exchange_path.is_absolute()
            else (self.store.repo_root / exchange_path).resolve()
        )
        try:
            resolved.relative_to(self.store.run_dir(run_id).resolve())
        except ValueError as exc:
            raise HarnessError(
                "design_step_review_corrupted",
                "Pending Design review exchange is outside the run directory.",
                run_id=run_id,
            ) from exc
        session_path = resolved.parent / "step_review_session_v1.json"
        try:
            exchange = json.loads(resolved.read_text(encoding="utf-8"))
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(
                "design_step_review_corrupted",
                "Pending Design review exchange cannot be superseded safely.",
                run_id=run_id,
            ) from exc
        if not isinstance(exchange, dict) or not isinstance(session, dict):
            raise HarnessError(
                "design_step_review_corrupted",
                "Pending Design review exchange has an invalid structure.",
                run_id=run_id,
            )
        call_id = str(exchange.get("call_id") or "")
        calls = [item for item in session.get("calls", []) if isinstance(item, dict)]
        matched = False
        for call in calls:
            if str(call.get("call_id") or "") != call_id:
                continue
            matched = True
            if str(call.get("status") or "") in {"input_pending", "pending"}:
                call["status"] = "superseded"
                call["superseded_at"] = utc_now_iso()
                call["superseded_reason"] = "design_revalidation_policy_changed"
                exchange = dict(call)
            break
        if not matched:
            raise HarnessError(
                "design_step_review_corrupted",
                "Pending Design review call is missing from its session.",
                run_id=run_id,
            )
        session["calls"] = calls
        session["updated_at"] = utc_now_iso()
        _write_json_atomic(resolved, exchange)
        _write_json_atomic(session_path, session)

    def _supersede_pending_plan_step_review(
        self,
        *,
        run_id: str,
        include_approved: bool = False,
    ) -> None:
        """Preserve audit while invalidating a plan response built from stale Design."""

        root = self.store.run_dir(run_id) / "implementation_plan_execution"
        session_path = root / "step_review_session_v1.json"
        if not session_path.exists():
            return
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(
                "implementation_plan_step_review_corrupted",
                "Cannot supersede the pending Phase 3 review because its session is unreadable.",
                run_id=run_id,
            ) from exc
        if not isinstance(session, dict):
            raise HarnessError(
                "implementation_plan_step_review_corrupted",
                "Cannot supersede the pending Phase 3 review because its session is invalid.",
                run_id=run_id,
            )
        calls = [item for item in session.get("calls", []) if isinstance(item, dict)]
        changed = False
        for call in calls:
            superseded_statuses = {"pending", "approved"} if include_approved else {"pending"}
            if str(call.get("status") or "") not in superseded_statuses:
                continue
            call["status"] = "superseded"
            call["superseded_at"] = utc_now_iso()
            call["superseded_reason"] = "upstream_design_revalidation"
            exchange_path = root / f"exchange_{str(call.get('call_id') or '')}.json"
            _write_json_atomic(exchange_path, call)
            changed = True
        if changed:
            session["calls"] = calls
            session["updated_at"] = utc_now_iso()
            _write_json_atomic(session_path, session)

    def continue_design(self, *, run_id: str) -> dict[str, object]:
        """Resume đúng persisted L3 Designer session sau transport/environment pause."""

        self._reset_design_runtime()
        state = self.store.read_state(run_id)
        if (
            str(state.get(DESIGNER_CONTRACT_STATE_KEY) or "") not in SUPPORTED_GRAPH_VERSIONS
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
            validate_existing_first=bool(_dict_value(execution.get("validator_step_review"))),
        )

    def restart_design(self, *, run_id: str) -> dict[str, object]:
        """Abandon a paused Designer session and restart from the approved Requirement."""

        self._reset_design_runtime()
        state = self.store.read_state(run_id)
        if str(state.get("status") or "") != "design_paused":
            raise HarnessError(
                "design_restart_not_allowed",
                f"Design can only restart from status 'design_paused', not {state.get('status')!r}.",
                run_id=run_id,
            )
        approved = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
        contract_version = str(self.settings.designer_contract_version)
        if contract_version not in SUPPORTED_GRAPH_VERSIONS:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Unsupported configured Designer contract: {contract_version}",
                run_id=run_id,
            )
        if self.settings.vertical_trace_mode not in VERTICAL_TRACE_MODES:
            raise HarnessError(
                "vertical_trace_mode_invalid",
                f"Unsupported configured vertical trace mode: {self.settings.vertical_trace_mode}",
                run_id=run_id,
            )

        previous_execution = _dict_value(state.get("designer_execution"))
        versions = _design_versions(state)
        start_version = _next_design_version(self.store.run_dir(run_id) / "artifacts")
        session_ordinal = max(start_version, int(previous_execution.get("session_ordinal") or 0) + 1)
        artifacts = _string_dict(state.get("latest_artifacts"))
        quality_reports = _dict_value(state.get("quality_reports"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_restarting",
                "status": "design_restarting",
                "latest_artifacts": {
                    key: value for key, value in artifacts.items() if key not in _PHASE2_ARTIFACT_KEYS
                },
                "quality_reports": {
                    key: value for key, value in quality_reports.items() if key not in _PHASE2_QUALITY_KEYS
                },
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": None,
                "designer_repair_feedback": [],
                "traceability_diagnostics": [],
                "summary_for_user": "Đang chạy lại Design từ Requirement đã duyệt bằng cấu hình hiện tại.",
                DESIGNER_CONTRACT_STATE_KEY: contract_version,
                UIUX_MODE_STATE_KEY: self.settings.uiux_skill_mode,
                UIUX_CONTRACT_STATE_KEY: (
                    UIUX_CONTRACT_VERSION if self.settings.uiux_skill_mode == "enforce" else ""
                ),
                VERTICAL_TRACE_STATE_KEY: VERTICAL_TRACE_GRAPH_VERSION,
                VERTICAL_TRACE_MODE_STATE_KEY: self.settings.vertical_trace_mode,
            }
        )
        state.pop(DESIGN_BUNDLE_STATE_KEY, None)
        self.store.write_state(state)
        return self._run_vertical_design(
            state=state,
            approved=approved,
            start_version=start_version,
            existing_versions=versions,
            existing_graph=None,
            session_ordinal=session_ordinal,
        )



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
        validate_existing_first: bool = False,
    ) -> dict[str, object]:
        """Run the repo-aware Designer loop and publish compatibility projections."""

        run_id = str(state.get("run_id") or "")
        design_contract_version = str(state.get(DESIGNER_CONTRACT_STATE_KEY) or GRAPH_VERSION_V1)
        if design_contract_version not in SUPPORTED_GRAPH_VERSIONS:
            raise HarnessError(
                "vertical_design_graph_invalid",
                f"Unsupported pinned Designer contract: {design_contract_version}",
                run_id=run_id,
            )
        uiux_mode = str(state.get(UIUX_MODE_STATE_KEY) or "off")
        uiux_contract_version = str(state.get(UIUX_CONTRACT_STATE_KEY) or "")
        if uiux_mode == "enforce" and uiux_contract_version != UIUX_CONTRACT_VERSION:
            raise HarnessError(
                "designer_uiux_contract_missing",
                "Run requires UI/UX guidance but has no supported pinned UI/UX contract.",
                run_id=run_id,
            )
        design_bundle_version = design_bundle_version_for(design_contract_version)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        landscape = self.repo_discovery.run(
            run_id=run_id,
            requirement_model=approved.canonical_model.to_dict(),
        )
        if landscape is None:
            return self._status_response(self.store.read_state(run_id), created_new_run=False)
        state = self.store.read_state(run_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
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
            if str(existing_graph.get("schema_version") or "") != design_contract_version:
                raise HarnessError(
                    "designer_input_drift",
                    "Persisted Design graph contract does not match the run-pinned contract.",
                    run_id=run_id,
                )
        versions = list(existing_versions)
        merged_artifacts = dict(artifact_paths)
        prior_digest = str((existing_graph or {}).get("artifact_digest") or "")
        repair_feedback = list(initial_repair_feedback or [])
        prior_blocker_keys = frozenset(
            str(item.get("canonical_key") or "")
            for item in repair_feedback
            if str(item.get("canonical_key") or "")
        )
        repair_stalled = False
        validation: DesignValidationResult | None = None
        trace_validation = None
        design_paths: dict[str, str] = {}
        graph = existing_graph
        base_ordinal = session_ordinal or start_version

        for repair_iteration in range(initial_repair_iteration, self.settings.design_repair_limit + 1):
            relative_iteration = repair_iteration - initial_repair_iteration
            version = start_version + relative_iteration
            validation_only_iteration = (
                validate_existing_first and relative_iteration == 0 and graph is not None
            )
            ordinal_offset = (
                max(0, relative_iteration - 1)
                if validate_existing_first and graph is not None
                else relative_iteration
            )
            ordinal = base_ordinal + ordinal_offset
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": (
                        "design_validating"
                        if validation_only_iteration
                        else ("design_investigating" if repair_iteration == 0 else "design_repairing")
                    ),
                    "status": (
                        "design_validating"
                        if validation_only_iteration
                        else ("design_investigating" if repair_iteration == 0 else "design_repairing")
                    ),
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": None,
                    "summary_for_user": (
                        "Design Validator đang kiểm tra lại vertical Design graph hiện có."
                        if validation_only_iteration
                        else (
                        "Designer đang khảo sát frozen repository evidence và dựng vertical Design graph."
                        if repair_iteration == 0
                        else f"Designer đang sửa partition bị chặn, vòng {repair_iteration}/{self.settings.design_repair_limit}."
                        )
                    ),
                    "designer_execution": {
                        "contract_version": design_contract_version,
                        "status": "running",
                        "session_ordinal": ordinal,
                        "design_version": version,
                        "repair_iteration": repair_iteration,
                    },
                    "designer_repair_feedback": [] if validation_only_iteration else repair_feedback,
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
                    "contract_version": design_contract_version,
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

            if validation_only_iteration:
                execution = DesignerExecutionResult(
                    graph=graph,
                    session_path="",
                    manifest_path="",
                    journal_path="",
                    delta_paths=[],
                    progress={
                        "status": "validating_existing",
                        "graph_revision": graph.get("revision"),
                        "graph_digest": graph.get("artifact_digest"),
                    },
                )
            else:
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
                    contract_version=design_contract_version,
                    uiux_mode=uiux_mode,
                    uiux_contract_version=uiux_contract_version,
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
                            ALLOWED_DESIGN_PAUSED_ACTIONS
                            if exc.retriable
                            else (
                                ["get_status", "cancel_run"]
                                if human_required
                                else ["revalidate_design", "get_status", "cancel_run"]
                            )
                        ),
                        "last_error": exc.to_response(),
                        "designer_execution": {
                            "contract_version": design_contract_version,
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
                        "contract_version": design_contract_version,
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
            bundle_findings = design_bundle_findings(graph, landscape=landscape)
            bundle_blockers = _bundle_design_findings(bundle_findings)
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
                validator = self.design_validator
                if bool(self.designer_step_review.get("enabled")):
                    validator = DesignValidator(
                        ReviewableLlmClient(
                            self.llm,
                            store=self.store,
                            run_id=run_id,
                            review=self.designer_step_review,
                            call_prefix="design-validator",
                            execution_dir=f"design_validator_execution/design_v{version}",
                            error_prefix="design_validator",
                            label="Design Validator",
                        )
                    )
                try:
                    validation = validator.review(
                        requirement_packet=approved.requirement_packet,
                        design_artifact=str(projections["design_markdown"]),
                        class_graph=projections["class_graph"],
                        sequence_graph=projections["sequence_graph"],
                        traceability=traceability,
                        canonical_requirement=approved.canonical_model,
                        vertical_trace=bounded_trace_view(trace_graph) if trace_graph is not None else None,
                        vertical_design_graph=graph,
                        repair_iterations=repair_iteration,
                        validation_trials=self.settings.design_validation_trials,
                    )
                except LlmStepReviewRequired as exc:
                    state = self.store.read_state(run_id)
                    state.update(
                        {
                            "updated_at": utc_now_iso(),
                            "current_phase": "design_validating",
                            "status": "design_paused",
                            "pending_checkpoint": None,
                            "allowed_next_actions": ALLOWED_DESIGN_PAUSED_ACTIONS,
                            "last_error": exc.to_response(),
                            "designer_execution": {
                                "contract_version": design_contract_version,
                                "status": "validator_review_required",
                                "session_ordinal": ordinal,
                                "design_version": version,
                                **execution.progress,
                                "repair_iteration": repair_iteration,
                                "validator_step_review": exc.progress,
                            },
                            "summary_for_user": (
                                f"Design Validator call {exc.progress['call_id']} requires operator review."
                            ),
                        }
                    )
                    self.store.write_state(state)
                    return self._status_response(state, created_new_run=False)
            validation_payload = validation.to_dict()
            validation_payload["version"] = "design_validation_report_v4_vertical_graph"
            validation_payload["mode"] = design_contract_version
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
            vertical_validation_summary["mode"] = design_contract_version
            quality_reports["design_validation"] = vertical_validation_summary
            quality_reports["design_vertical_graph"] = {
                "ok": validation.ok,
                "mode": design_contract_version,
                "graph_revision": graph.get("revision"),
                "graph_digest": graph.get("artifact_digest"),
                "graph": design_paths["vertical_design_graph"],
                "projection_manifest": design_paths["design_projection_manifest"],
                "finding_ledger": design_paths["design_finding_ledger"],
            }
            quality_reports["design_bundle"] = {
                "ok": validation.ok and not bundle_blockers,
                "contract_version": design_bundle_version,
                "component_count": len(_dict_items(projections["component_view"].get("components"))),
                "ui_count": len(_dict_items(projections["surface_view"].get("ui"))),
                "api_count": len(_dict_items(projections["surface_view"].get("api"))),
                "business_rule_count": len(_dict_items(projections["surface_view"].get("business_rules"))),
                "data_count": len(_dict_items(projections["surface_view"].get("data"))),
                **_runtime_design_summary(graph=graph, findings=bundle_findings),
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
                        "contract_version": design_contract_version,
                        "status": "validated" if validation.ok else "repair_needed",
                        "session_ordinal": ordinal,
                        "design_version": version,
                        **execution.progress,
                        "blocking_count": len(validation.blocking_findings),
                        "warning_count": len(validation.warnings),
                    },
                    "designer_repair_feedback": [
                        item.to_dict() for item in validation.blocking_findings
                    ],
                }
            )
            self.store.write_state(state)
            if validation.ok:
                break
            current_digest = str(graph.get("artifact_digest") or "")
            current_blocker_keys = frozenset(item.canonical_key for item in validation.blocking_findings)
            repair_stalled = (
                False
                if validation_only_iteration
                else _design_repair_stalled(
                    prior_digest=prior_digest,
                    current_digest=current_digest,
                    prior_blocker_keys=prior_blocker_keys,
                    current_blocker_keys=current_blocker_keys,
                )
            )
            if repair_iteration >= self.settings.design_repair_limit or repair_stalled:
                break
            prior_digest = current_digest
            prior_blocker_keys = current_blocker_keys
            repair_feedback = [item.to_dict() for item in validation.blocking_findings]

        if validation is None or graph is None:
            raise HarnessError("designer_failed", "L3 Designer did not produce a validated graph.", run_id=run_id)
        if not validation.ok:
            error = HarnessError(
                "design_repair_stalled" if repair_stalled else "design_repair_exhausted",
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
                    "design_bundle_contract_version": design_bundle_version,
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
                DESIGN_BUNDLE_STATE_KEY: design_bundle_version,
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "designer_execution": {
                    **_dict_value(state.get("designer_execution")),
                    "contract_version": design_contract_version,
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

    def _persisted_design_feedback(
        self,
        *,
        state: dict[str, object],
        run_id: str,
    ) -> list[dict[str, object]]:
        feedback = _dict_items(state.get("designer_repair_feedback"))
        if feedback:
            return feedback
        ledger_ref = _string_dict(state.get("latest_artifacts")).get("design_finding_ledger", "")
        if not ledger_ref:
            return []
        ledger_path = self.store.artifact_path(run_id, Path(ledger_ref).name)
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(
                "design_finding_ledger_invalid",
                "Latest Design finding ledger cannot be read for revalidation.",
                run_id=run_id,
            ) from exc
        return _dict_items(ledger.get("blocking_findings")) if isinstance(ledger, dict) else []

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

    def _blocked_session_graph(
        self,
        *,
        state: dict[str, object],
        run_id: str,
    ) -> dict[str, object] | None:
        """Recover the last validated graph after invalid-action exhaustion."""

        execution = _dict_value(state.get("designer_execution"))
        version = int(execution.get("design_version") or 0)
        if version < 1:
            return None
        path = (
            self.store.run_dir(run_id)
            / "designer_execution"
            / f"design_v{version}"
            / "designer_loop_session_v1.json"
        )
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        graph = _dict_value(_dict_value(session).get("graph"))
        if not graph:
            return None
        return validate_vertical_design_graph(graph, expected_run_id=run_id)





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
            "traceability_diagnostics": state.get("traceability_diagnostics", []),
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
            "requirement_contract_version": state.get("requirement_contract_version", "v2"),
            REPO_DISCOVERY_CONTRACT_STATE_KEY: state.get(REPO_DISCOVERY_CONTRACT_STATE_KEY),
            DESIGNER_CONTRACT_STATE_KEY: state.get(DESIGNER_CONTRACT_STATE_KEY),
            UIUX_MODE_STATE_KEY: state.get(UIUX_MODE_STATE_KEY, "off"),
            UIUX_CONTRACT_STATE_KEY: state.get(UIUX_CONTRACT_STATE_KEY, ""),
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
    trace_edges = {
        str(item.get("id") or ""): item
        for item in _dict_items(trace_graph.get("edges"))
        if str(item.get("id") or "")
    }
    design_edges = {
        str(item.get("id") or ""): item
        for item in _dict_items(graph.get("edges"))
        if str(item.get("id") or "")
    }
    design_nodes = {
        str(item.get("id") or ""): item
        for item in _dict_items(graph.get("nodes"))
        if str(item.get("id") or "")
    }
    feedback: list[dict[str, object]] = []
    for finding in result.blocking_findings:
        item = finding.to_dict()
        trace_ref = str(item.get("subject_ref") or "")
        trace_edge = trace_edges.get(trace_ref, {})
        source_edge_id = str(trace_edge.get("source_edge_id") or "")
        design_edge = design_edges.get(source_edge_id, {})
        if design_edge:
            source_node = design_nodes.get(str(design_edge.get("from") or ""), {})
            item["trace_subject_ref"] = trace_ref
            item["subject_ref"] = source_edge_id
            item["partition"] = {
                "scenario_id": str(design_edge.get("scenario_id") or source_node.get("scenario_id") or ""),
                "component_id": str(source_node.get("component_id") or ""),
            }
            if str(item.get("category") or "") == "cross_scenario_design_edge":
                item["expected_change"] = (
                    f"Remove Design edge {source_edge_id}, or replace it only with an approved main-to-alternative/exception branch."
                )
        feedback.append(item)
    return feedback


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
        "repo_codegraph_snapshot_artifact_ref": artifacts.get("repo_codegraph_snapshot"),
        "repo_codegraph_snapshot_digest": state.get("repo_codegraph_snapshot_digest"),
        "designer_uiux_mode": state.get(UIUX_MODE_STATE_KEY, "off"),
        "designer_uiux_contract_version": state.get(UIUX_CONTRACT_STATE_KEY, ""),
        "project_profile_id": profile.get("project_profile_id"),
        "project_profile_schema": profile.get("project_profile_schema"),
        "language_profile_id": profile.get("language_profile_id"),
        "language_profile_schema": profile.get("language_profile_schema"),
        "target_manifest_digest": profile.get("target_manifest_digest"),
        "workspace_ref": "run_workspace_v1",
        "base_sha": getattr(workspace, "base_sha", ""),
    }




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


def _runtime_design_summary(
    *,
    graph: dict[str, object],
    findings: list[dict[str, object]],
) -> dict[str, object]:
    """Expose bounded V2 composition coverage without leaking source excerpts."""

    nodes = _dict_items(graph.get("nodes"))
    partitions = _dict_items(graph.get("partitions"))
    edges = _dict_items(graph.get("edges"))
    bound_nodes = [item for item in nodes if isinstance(item.get("runtime_binding"), dict)]
    observed_partitions = [
        item
        for item in partitions
        if isinstance(item.get("source_observation_refs"), list)
        and any(str(ref) for ref in item.get("source_observation_refs", []))
    ]
    scenarios = {str(item.get("scenario_id") or "") for item in partitions if str(item.get("scenario_id") or "")}
    disconnected = {
        str(_dict_value(item.get("partition")).get("scenario_id") or "")
        for item in findings
        if str(item.get("category") or "") == "runtime_path_disconnected"
    } - {""}
    return {
        "runtime_binding_coverage": {
            "bound": len(bound_nodes),
            "total": len(nodes),
        },
        "investigation_evidence_coverage": {
            "observed": len(observed_partitions),
            "total": len(partitions),
        },
        "runtime_path_coverage": {
            "connected": max(0, len(scenarios) - len(disconnected)),
            "required": len(scenarios),
        },
        "boundary_contract_count": sum(
            1 for item in edges if isinstance(item.get("boundary_contract"), dict)
        ),
    }


def _dict_items(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _design_repair_stalled(
    *,
    prior_digest: str,
    current_digest: str,
    prior_blocker_keys: frozenset[str],
    current_blocker_keys: frozenset[str],
) -> bool:
    return bool(
        (prior_digest and current_digest == prior_digest)
        or (prior_blocker_keys and current_blocker_keys == prior_blocker_keys)
    )


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _traceability_summary(missing_links: dict[str, list[dict[str, str]]]) -> str:
    total = len(missing_links.get("class_diagram", [])) + len(missing_links.get("sequence_diagram", []))
    return "không phát hiện missing link class/sequence." if total == 0 else f"còn {total} missing link class/sequence cần review."










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




def _artifact_ref(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
