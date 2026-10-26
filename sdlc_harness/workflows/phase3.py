"""Workflow Phase 3: thiết kế đã duyệt -> checkpoint implementation plan.

Phase 3 chỉ bắt đầu sau khi design checkpoint đã được duyệt. Workflow tạo và
khóa Git worktree riêng cho run, đọc requirement/design/trace graph đã duyệt, tạo
implementation plan có cấu trúc và chạy Validator.review_plan hybrid. Phase này chưa
sửa source/test, chưa tạo commit/PR; branch/worktree đã được cấp phát cho run.
"""

from __future__ import annotations

# Smart Developer V1 đọc CodeGraph từ snapshot worktree riêng của run. Phase 3
# đã tạo branch/worktree nhưng chỉ đọc context để lập plan, chưa sửa code/commit/PR.

import hashlib
import json
from pathlib import Path
import re

from ..agents.developer import DeveloperPlanAgent
from ..agents.validator import (
    ImplementationPlanValidator,
    VERTICAL_TRACE_GRAPH_VERSION,
    VERTICAL_TRACE_MODE_STATE_KEY,
    VERTICAL_TRACE_STATE_KEY,
    build_requirement_graph_v2,
    build_vertical_trace_graph,
    validate_vertical_trace_graph,
    validate_vertical_trace_report,
)
from ..artifacts import CanonicalRequirementInput, read_frozen_requirement_input
from ..artifacts.writers import write_phase3_implementation_plan, write_phase3_plan_review_report
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..profiles import (
    planning_path_policy_from_snapshot,
    prompt_directive_from_snapshot,
    read_profile_snapshot,
    verification_stage_policy_from_snapshot,
)
from ..repo_context import CodeGraphContextProvider, discover_profile_source_files
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_implementation_plan_checkpoint
from ..workspaces import RunWorkspaceManager

ALLOWED_IMPLEMENTATION_PLAN_ACTIONS = [
    "approve_checkpoint",
    "revalidate_implementation_plan",
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


class PhaseThreeWorkflow:
    """Điều phối Phase 3 từ checkpoint design sang implementation plan."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        llm_client = LlmClient(settings)
        self.developer = DeveloperPlanAgent(llm_client)
        self.validator = ImplementationPlanValidator(llm_client)
        self.repo_context_provider = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")
        self.workspace_manager = RunWorkspaceManager(settings)

    def approve_design_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Tạo workspace rồi duyệt design để sinh implementation plan checkpoint."""

        state = self.store.read_state(run_id)
        self._require_design_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        self._read_phase3_inputs(state=state, run_id=run_id)
        try:
            self.workspace_manager.create_for_run(run_id)
        except HarnessError as exc:
            state = self.store.read_state(run_id)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_planning",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "request_change", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 3 bị chặn khi chuẩn bị run workspace: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        with self.workspace_manager.locked_workspace(run_id):
            return self._approve_design_checkpoint_locked(run_id=run_id, checkpoint_id=checkpoint_id)

    def revalidate_implementation_plan(self, *, run_id: str) -> dict[str, object]:
        """Create a new plan version from frozen Phase 1/2 inputs and review it again."""

        state = self.store.read_state(run_id)
        status = str(state.get("status") or "")
        if str(state.get("current_phase") or "") != "implementation_planning" or status not in {
            "waiting_implementation_plan_review",
            "blocked",
        }:
            raise HarnessError(
                "implementation_plan_revalidation_not_allowed",
                f"Run is not waiting on an implementation plan: {run_id}",
                run_id=run_id,
            )
        with self.workspace_manager.locked_workspace(run_id):
            return self._revalidate_implementation_plan_locked(run_id=run_id)

    def _revalidate_implementation_plan_locked(self, *, run_id: str) -> dict[str, object]:
        state = self.store.read_state(run_id)
        frozen_requirement, requirement_graph = self._read_phase3_inputs(state=state, run_id=run_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        design_path = _required_artifact(artifact_paths, "design_artifact", run_id=run_id)
        trace_path = _required_artifact(artifact_paths, "requirement_design_trace", run_id=run_id)
        class_graph_path = _required_artifact(artifact_paths, "class_diagram_graph", run_id=run_id)
        sequence_graph_path = _required_artifact(artifact_paths, "sequence_diagram_graph", run_id=run_id)
        design_artifact = self._existing_artifact_path(design_path, run_id=run_id).read_text(encoding="utf-8")
        requirement_design_trace = _read_json_artifact(
            self._existing_artifact_path(trace_path, run_id=run_id), run_id=run_id
        )
        class_graph = _read_json_artifact(
            self._existing_artifact_path(class_graph_path, run_id=run_id), run_id=run_id
        )
        sequence_graph = _read_json_artifact(
            self._existing_artifact_path(sequence_graph_path, run_id=run_id), run_id=run_id
        )
        workspace = self.workspace_manager.resolve(run_id)
        target_path = Path(workspace.worktree_path)
        target_files = _target_source_files(target_path, state=state, run_id=run_id)
        planning_policy, prompt_directive, verification_policy = _phase3_profile_context(
            state=state,
            run_id=run_id,
        )
        issue_key = _issue_key_from_state(state)
        self.repo_context_provider.prepare_index(target_path)
        repo_context = self.repo_context_provider.collect(
            target_path=target_path,
            target_files=target_files,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            issue_key=issue_key,
        ).to_dict()
        requirement_packet = frozen_requirement.requirement_packet
        plan_draft = self.developer.plan(
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            requirement_design_trace=requirement_design_trace,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            target_repo_path=str(target_path),
            target_repo_remote=workspace.remote,
            target_base_branch=self.settings.target_base_branch,
            planned_branch=workspace.branch,
            target_files=target_files,
            test_command="",
            repo_context=repo_context,
            workspace_mode=workspace.workspace_mode,
            workspace_ref=artifact_paths["run_workspace"],
            base_sha=workspace.base_sha,
            canonical_requirement=frozen_requirement.canonical_model,
            requirement_graph=requirement_graph,
            requirement_artifact_refs=frozen_requirement.approved_artifacts,
            planning_policy=planning_policy,
            prompt_directive=prompt_directive,
            verification_policy=verification_policy,
        )
        start_version = _next_implementation_plan_version(self.store.run_dir(run_id) / "artifacts")
        versions = list(
            _dict_value(_dict_value(state.get("quality_reports")).get("implementation_plan_review")).get(
                "versions", []
            )
        )
        merged_artifacts = dict(artifact_paths)
        quality_reports = _dict_value(state.get("quality_reports"))
        review = None
        review_report_path = ""
        plan_paths: dict[str, str] = {}
        previous_fingerprint = ""
        stalled = False
        repair_limit = self.settings.implementation_plan_repair_limit
        for repair_iteration in range(repair_limit + 1):
            version = start_version + repair_iteration
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_plan_revalidating",
                    "status": "implementation_plan_revalidating",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "summary_for_user": f"Đang revalidate implementation plan V{version}.",
                }
            )
            self.store.write_state(state)
            review = self.validator.review(
                structured_plan=plan_draft.structured,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                repair_iterations=repair_iteration,
                requirement_graph=requirement_graph,
                requirement_contract_version=frozen_requirement.contract_version,
                canonical_requirement=frozen_requirement.canonical_model.to_dict(),
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
            )
            plan_paths = write_phase3_implementation_plan(
                store=self.store,
                run_id=run_id,
                markdown=plan_draft.markdown,
                structured_plan=plan_draft.structured,
                version=version,
            )
            review_payload = _bind_plan_review_to_structured_plan(
                store=self.store,
                structured_plan_path=plan_paths["implementation_plan_structured"],
                structured_plan=plan_draft.structured,
                review_report=review.to_dict(),
            )
            review_report_path = write_phase3_plan_review_report(
                store=self.store, run_id=run_id, review_report=review_payload, version=version
            )
            version_label = f"v{version}"
            if version_label not in versions:
                versions.append(version_label)
            merged_artifacts = {
                **artifact_paths,
                **plan_paths,
                "implementation_plan_review": review_report_path,
            }
            quality_reports = {
                **quality_reports,
                "implementation_plan_review": {
                    **review_payload,
                    "versions": versions,
                    "report": review_report_path,
                },
            }
            state.update({"latest_artifacts": merged_artifacts, "quality_reports": quality_reports})
            self.store.write_state(state)
            if review.ok or repair_iteration >= repair_limit:
                break
            fingerprint = _plan_review_fingerprint(plan_draft.structured, review.issues)
            if fingerprint == previous_fingerprint:
                stalled = True
                break
            previous_fingerprint = fingerprint
            feedback = review.developer_feedback or "\n".join(review.issues)
            plan_draft = self.developer.repair_plan(
                previous_draft=plan_draft,
                review_report=review.to_dict(),
                developer_feedback=feedback,
                iteration=repair_iteration + 1,
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                issue_key=issue_key,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                target_repo_path=str(target_path),
                target_repo_remote=workspace.remote,
                target_base_branch=self.settings.target_base_branch,
                planned_branch=workspace.branch,
                target_files=target_files,
                test_command="",
                repo_context=repo_context,
                workspace_mode=workspace.workspace_mode,
                workspace_ref=artifact_paths["run_workspace"],
                base_sha=workspace.base_sha,
                canonical_requirement=frozen_requirement.canonical_model,
                requirement_graph=requirement_graph,
                requirement_artifact_refs=frozen_requirement.approved_artifacts,
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
            )
        if review is None or not review.ok:
            error_code = "implementation_plan_repair_stalled" if stalled else "implementation_plan_review_failed"
            error = HarnessError(
                error_code,
                f"Implementation plan revalidation stopped with {len(review.issues) if review else 0} issue(s).",
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_planning",
                    "status": "blocked",
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_implementation_plan", "get_status", "request_change", "cancel_run"],
                    "last_error": error.to_response(),
                    "quality_reports": quality_reports,
                    "summary_for_user": f"Revalidation chưa pass. Report: `{review_report_path}`.",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        last_sync = sync_implementation_plan_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
        checkpoint_id = f"checkpoint_implementation_plan_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "waiting_implementation_plan_review",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "implementation_plan_review",
                    "artifact_ref": _artifact_ref(plan_paths["implementation_plan"]),
                    "artifact_path": plan_paths["implementation_plan"],
                },
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_PLAN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "quality_reports": quality_reports,
                "summary_for_user": (
                    f"Implementation plan V{start_version + review.repair_iterations} đã pass revalidation "
                    f"và sync status={last_sync.get('status')}. Run đang chờ review implementation plan."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _approve_design_checkpoint_locked(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Thực thi Phase 3 trong khi caller giữ per-run workspace lock."""

        state = self.store.read_state(run_id)
        self._require_design_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        frozen_requirement, requirement_graph = self._read_phase3_inputs(state=state, run_id=run_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        requirement_path = frozen_requirement.approved_artifacts["requirement_packet"]
        design_path = _required_artifact(artifact_paths, "design_artifact", run_id=run_id)
        trace_path = _required_artifact(artifact_paths, "requirement_design_trace", run_id=run_id)
        class_graph_path = _required_artifact(artifact_paths, "class_diagram_graph", run_id=run_id)
        sequence_graph_path = _required_artifact(artifact_paths, "sequence_diagram_graph", run_id=run_id)
        design_abs_path = self._existing_artifact_path(design_path, run_id=run_id)
        trace_abs_path = self._existing_artifact_path(trace_path, run_id=run_id)
        class_graph_abs_path = self._existing_artifact_path(class_graph_path, run_id=run_id)
        sequence_graph_abs_path = self._existing_artifact_path(sequence_graph_path, run_id=run_id)

        workspace = self.workspace_manager.resolve(run_id)
        target_path = Path(workspace.worktree_path)
        target_files = _target_source_files(target_path, state=state, run_id=run_id)
        planning_policy, prompt_directive, verification_policy = _phase3_profile_context(
            state=state,
            run_id=run_id,
        )
        self.repo_context_provider.prepare_index(target_path)

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "implementation_planning",
                "pending_checkpoint": None,
                "last_error": None,
                "approved_artifacts": {
                    **_dict_value(state.get("approved_artifacts")),
                    "requirement_packet": requirement_path,
                    "design_artifact": design_path,
                    "requirement_design_trace": trace_path,
                    "class_diagram_graph": class_graph_path,
                    "sequence_diagram_graph": sequence_graph_path,
                    **_optional_artifacts(artifact_paths, "requirement_graph"),
                    **_optional_artifacts(artifact_paths, "vertical_design_graph"),
                    **_optional_artifacts(artifact_paths, "vertical_trace_graph"),
                    **_optional_artifacts(artifact_paths, "vertical_trace_validation"),
                },
                "allowed_next_actions": ["get_status", "cancel_run"],
                "summary_for_user": "Đã duyệt design checkpoint, đang tạo implementation plan Phase 3.",
            }
        )
        self.store.write_state(state)

        requirement_packet = frozen_requirement.requirement_packet
        design_artifact = design_abs_path.read_text(encoding="utf-8")
        requirement_design_trace = _read_json_artifact(trace_abs_path, run_id=run_id)
        class_graph = _read_json_artifact(class_graph_abs_path, run_id=run_id)
        sequence_graph = _read_json_artifact(sequence_graph_abs_path, run_id=run_id)
        issue_key = _issue_key_from_state(state)
        repo_context = self.repo_context_provider.collect(
            target_path=target_path,
            target_files=target_files,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            issue_key=issue_key,
        ).to_dict()
        try:
            plan_draft = self.developer.plan(
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                issue_key=issue_key,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                target_repo_path=str(target_path),
                target_repo_remote=workspace.remote,
                target_base_branch=self.settings.target_base_branch,
                planned_branch=workspace.branch,
                target_files=target_files,
                test_command="",
                repo_context=repo_context,
                workspace_mode=workspace.workspace_mode,
                workspace_ref=artifact_paths["run_workspace"],
                base_sha=workspace.base_sha,
                canonical_requirement=frozen_requirement.canonical_model,
                requirement_graph=requirement_graph,
                requirement_artifact_refs=frozen_requirement.approved_artifacts,
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
            )
        except HarnessError as exc:
            self._mark_phase3_failed(state, run_id=run_id, error=exc)
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        review = None
        plan_paths: dict[str, str] = {}
        review_report_path = ""
        merged_artifacts = dict(artifact_paths)
        quality_reports = _dict_value(state.get("quality_reports"))
        versions: list[str] = []
        previous_fingerprint = ""
        stalled = False
        repair_limit = self.settings.implementation_plan_repair_limit
        for repair_iteration in range(repair_limit + 1):
            version = repair_iteration + 1
            if repair_iteration == 0:
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_plan_validating",
                        "status": "implementation_plan_validating",
                        "summary_for_user": (
                            "Developer đã sinh implementation plan, Validator đang kiểm rule + LLM "
                            "trước khi mở checkpoint."
                        ),
                    }
                )
            else:
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_plan_repairing",
                        "status": "implementation_plan_repairing",
                        "summary_for_user": (
                            f"Validator còn issue, Developer đang repair implementation plan "
                            f"lần {repair_iteration}/{repair_limit}."
                        ),
                    }
                )
            self.store.write_state(state)

            review = self.validator.review(
                structured_plan=plan_draft.structured,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                repair_iterations=repair_iteration,
                requirement_graph=requirement_graph,
                requirement_contract_version=frozen_requirement.contract_version,
                canonical_requirement=frozen_requirement.canonical_model.to_dict(),
                planning_policy=planning_policy,
                prompt_directive=prompt_directive,
                verification_policy=verification_policy,
            )
            plan_paths = write_phase3_implementation_plan(
                store=self.store,
                run_id=run_id,
                markdown=plan_draft.markdown,
                structured_plan=plan_draft.structured,
                version=version,
            )
            review_payload = _bind_plan_review_to_structured_plan(
                store=self.store,
                structured_plan_path=plan_paths["implementation_plan_structured"],
                structured_plan=plan_draft.structured,
                review_report=review.to_dict(),
            )
            review_report_path = write_phase3_plan_review_report(
                store=self.store,
                run_id=run_id,
                review_report=review_payload,
                version=version,
            )
            versions.append(f"v{version}")
            merged_artifacts = {
                **artifact_paths,
                **plan_paths,
                "implementation_plan_review": review_report_path,
            }
            quality_reports = {
                **_dict_value(state.get("quality_reports")),
                "implementation_plan_review": {
                    **review_payload,
                    "versions": versions,
                    "report": review_report_path,
                },
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": merged_artifacts,
                    "quality_reports": quality_reports,
                }
            )
            self.store.write_state(state)
            if review.ok or repair_iteration >= repair_limit:
                break

            fingerprint = _plan_review_fingerprint(plan_draft.structured, review.issues)
            if fingerprint == previous_fingerprint:
                stalled = True
                break
            previous_fingerprint = fingerprint
            feedback = review.developer_feedback or "\n".join(review.issues)
            try:
                plan_draft = self.developer.repair_plan(
                    previous_draft=plan_draft,
                    review_report=review.to_dict(),
                    developer_feedback=feedback,
                    iteration=repair_iteration + 1,
                    run_id=run_id,
                    source_ref=str(state.get("source_ref") or ""),
                    issue_key=issue_key,
                    requirement_packet=requirement_packet,
                    design_artifact=design_artifact,
                    requirement_design_trace=requirement_design_trace,
                    class_graph=class_graph,
                    sequence_graph=sequence_graph,
                    target_repo_path=str(target_path),
                    target_repo_remote=workspace.remote,
                    target_base_branch=self.settings.target_base_branch,
                    planned_branch=workspace.branch,
                    target_files=target_files,
                    test_command="",
                    repo_context=repo_context,
                    workspace_mode=workspace.workspace_mode,
                    workspace_ref=artifact_paths["run_workspace"],
                    base_sha=workspace.base_sha,
                    canonical_requirement=frozen_requirement.canonical_model,
                    requirement_graph=requirement_graph,
                    requirement_artifact_refs=frozen_requirement.approved_artifacts,
                    planning_policy=planning_policy,
                    prompt_directive=prompt_directive,
                    verification_policy=verification_policy,
                )
            except HarnessError as exc:
                self._mark_phase3_failed(state, run_id=run_id, error=exc)
                raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        if review is None:
            raise HarnessError(
                "implementation_plan_review_failed",
                "Phase 3 did not produce an implementation plan review.",
                run_id=run_id,
            )
        if not review.ok:
            error_code = "implementation_plan_repair_stalled" if stalled else "implementation_plan_review_failed"
            error = HarnessError(
                error_code,
                (
                    f"Validator.review_plan found {len(review.issues)} issue(s) "
                    f"after {review.repair_iterations} repair iteration(s)."
                ),
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_planning",
                    "status": "blocked",
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_implementation_plan", "get_status", "request_change", "cancel_run"],
                    "last_error": error.to_response(),
                    "quality_reports": quality_reports,
                    "summary_for_user": (
                        "Phase 3 bị chặn vì implementation plan chưa qua Validator.review_plan hybrid. "
                        f"Report: `{review_report_path}`."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        last_sync = sync_implementation_plan_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
        checkpoint_id = f"checkpoint_implementation_plan_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "waiting_implementation_plan_review",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "implementation_plan_review",
                    "artifact_ref": _artifact_ref(plan_paths["implementation_plan"]),
                    "artifact_path": plan_paths["implementation_plan"],
                },
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_PLAN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "quality_reports": quality_reports,
                "summary_for_user": (
                    f"Đã duyệt design và tạo implementation plan cho run {run_id}. "
                    f"Validator.review_plan hybrid đã pass sau {review.repair_iterations} vòng repair, "
                    f"run workspace đã sẵn sàng trên branch `{workspace.branch}`, "
                    f"base `{workspace.base_sha[:12]}`, sync status={last_sync.get('status')}. "
                    "Run đang chờ review implementation plan trước khi slice tiếp theo tạo code/PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _read_phase3_inputs(
        self,
        *,
        state: dict[str, object],
        run_id: str,
    ) -> tuple[CanonicalRequirementInput, dict[str, object]]:
        """Read frozen Phase 1 input and validate the V2 Phase 2 handoff before mutation."""

        try:
            frozen = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
            artifact_paths = _string_dict(state.get("latest_artifacts"))
            graph_path = _required_artifact(artifact_paths, "requirement_graph", run_id=run_id)
            trace_path = _required_artifact(artifact_paths, "requirement_design_trace", run_id=run_id)
            saved_graph = _read_json_artifact(self._existing_artifact_path(graph_path, run_id=run_id), run_id=run_id)
            trace = _read_json_artifact(self._existing_artifact_path(trace_path, run_id=run_id), run_id=run_id)
            _require_v2_phase3_contract(
                canonical=frozen,
                canonical_graph=build_requirement_graph_v2(frozen.canonical_model),
                saved_graph=saved_graph,
                trace=trace,
                run_id=run_id,
            )
            if (
                str(state.get(VERTICAL_TRACE_STATE_KEY) or "") == VERTICAL_TRACE_GRAPH_VERSION
                and str(state.get(VERTICAL_TRACE_MODE_STATE_KEY) or "") == "enforce"
            ):
                design_graph_ref = _required_artifact(artifact_paths, "vertical_design_graph", run_id=run_id)
                vertical_trace_ref = _required_artifact(artifact_paths, "vertical_trace_graph", run_id=run_id)
                vertical_report_ref = _required_artifact(artifact_paths, "vertical_trace_validation", run_id=run_id)
                design_graph = _read_json_artifact(
                    self._existing_artifact_path(design_graph_ref, run_id=run_id),
                    run_id=run_id,
                )
                vertical_trace = _read_json_artifact(
                    self._existing_artifact_path(vertical_trace_ref, run_id=run_id),
                    run_id=run_id,
                )
                expected_trace = build_vertical_trace_graph(
                    requirement_model=frozen.canonical_model,
                    design_graph=design_graph,
                )
                validate_vertical_trace_graph(
                    vertical_trace,
                    expected_run_id=run_id,
                    expected_requirement_digest=str(expected_trace["binding"]["requirement_digest"]),
                    expected_design_digest=str(expected_trace["binding"]["design_graph_digest"]),
                )
                if vertical_trace.get("artifact_digest") != expected_trace.get("artifact_digest"):
                    raise HarnessError(
                        "vertical_trace_binding_stale",
                        "Vertical trace graph does not match the frozen Requirement and latest Design graph.",
                        run_id=run_id,
                    )
                vertical_report = _read_json_artifact(
                    self._existing_artifact_path(vertical_report_ref, run_id=run_id),
                    run_id=run_id,
                )
                validate_vertical_trace_report(
                    vertical_report,
                    graph=vertical_trace,
                    expected_mode="enforce",
                )
            return frozen, saved_graph
        except HarnessError as exc:
            raise HarnessError(
                "requirement_v2_phase3_input_invalid",
                f"Requirement V2 Phase 3 input is invalid: {exc.message}",
                run_id=run_id,
            ) from exc

    def _require_design_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        if str(state.get("status")) != "waiting_design_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for design approval: {run_id}",
                run_id=run_id,
            )
        quality_reports = _dict_value(state.get("quality_reports"))
        design_validation = _dict_value(quality_reports.get("design_validation"))
        latest_artifacts = _dict_value(state.get("latest_artifacts"))
        accepted_validation_modes = {
            "design_validation_hybrid_v2",
            "design_validation_progressive_v1",
            "vertical_design_graph_v1",
        }
        if (
            not bool(design_validation.get("ok"))
            or str(design_validation.get("mode") or "") not in accepted_validation_modes
            or not str(latest_artifacts.get("design_validation_report") or "").strip()
        ):
            raise HarnessError(
                "design_validation_required",
                "Design must pass the configured Design Validator before Phase 3 approval. Run revalidate-design first.",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "design_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not a design checkpoint: {run_id}",
                run_id=run_id,
            )
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError(
                "checkpoint_not_current",
                f"Checkpoint is not current: {checkpoint_id}",
                run_id=run_id,
            )

    def _existing_artifact_path(self, relative_path: str, *, run_id: str) -> Path:
        path = (self.store.repo_root / relative_path).resolve()
        if not path.exists():
            raise HarnessError(
                "approved_design_not_found",
                f"Approved artifact does not exist: {relative_path}",
                run_id=run_id,
            )
        return path

    def _mark_phase3_failed(self, state: dict[str, object], *, run_id: str, error: HarnessError) -> None:
        """Ghi rõ lỗi Phase 3 để run không kẹt với checkpoint design cũ."""

        if error.error_code in DESIGN_ROUTING_ERROR_CODES:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "design_review",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["revalidate_design", "get_status", "request_change", "cancel_run"],
                    "last_error": HarnessError(error.error_code, error.message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        "Design chưa đủ thông tin định tuyến component để lập implementation plan; "
                        "cần revalidate design trước khi thử lại Phase 3."
                    ),
                }
            )
            self.store.write_state(state)
            return

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "failed",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": HarnessError(error.error_code, error.message, run_id=run_id).to_response(),
                "summary_for_user": f"Phase 3 lỗi khi tạo implementation plan: {error.message}",
            }
        )
        self.store.write_state(state)

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
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
            "requirement_contract_version": state.get("requirement_contract_version", "v2"),
        }


def _target_source_files(
    target_path: Path,
    *,
    state: dict[str, object] | None = None,
    run_id: str = "",
) -> list[str]:
    snapshot = read_profile_snapshot(state or {}, run_id=run_id or None)
    if snapshot is None:
        raise HarnessError(
            "legacy_execution_profile_retired",
            "Phase 3 artifact generation requires an immutable project profile snapshot.",
            run_id=run_id or None,
        )
    return discover_profile_source_files(
        target_path=target_path,
        snapshot=snapshot,
        run_id=run_id,
    )


def _phase3_profile_context(*, state: dict[str, object], run_id: str):
    snapshot = read_profile_snapshot(state, run_id=run_id)
    if snapshot is None:
        raise HarnessError(
            "legacy_execution_profile_retired",
            "Phase 3 artifact generation requires an immutable project profile snapshot.",
            run_id=run_id,
        )
    return (
        planning_path_policy_from_snapshot(snapshot, run_id=run_id),
        prompt_directive_from_snapshot(snapshot, run_id=run_id),
        verification_stage_policy_from_snapshot(snapshot, run_id=run_id),
    )


def _next_implementation_plan_version(artifact_dir: Path) -> int:
    versions = []
    for path in artifact_dir.glob("implementation_plan_structured_v*.json"):
        match = re.search(r"_v(\d+)\.json$", path.name)
        if match:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1


def _plan_review_fingerprint(structured_plan: dict[str, object], issues: list[str]) -> str:
    payload = {
        "implementation_steps": structured_plan.get("implementation_steps", []),
        "proposed_files": structured_plan.get("proposed_files", []),
        "design_mappings": structured_plan.get("design_mappings", []),
        "implementation_signature_contract": structured_plan.get(
            "implementation_signature_contract", {}
        ),
        "verification_stage_ids": structured_plan.get("verification_stage_ids", []),
        "test_intents": structured_plan.get("test_intents", []),
        "issues": issues,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _bind_plan_review_to_structured_plan(
    *,
    store: RuntimeStore,
    structured_plan_path: str,
    structured_plan: dict[str, object],
    review_report: dict[str, object],
) -> dict[str, object]:
    """Bind review với đúng bytes/version của structured plan đã được ghi."""

    path = Path(structured_plan_path)
    if not path.is_absolute():
        path = store.repo_root / path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        **review_report,
        "implementation_plan_contract_version": str(structured_plan.get("version") or ""),
        "implementation_plan_sha256": f"sha256:{digest}",
    }


def _required_artifact(artifact_paths: dict[str, str], key: str, *, run_id: str) -> str:
    value = artifact_paths.get(key, "")
    if not value:
        raise HarnessError(
            "approved_design_not_found",
            f"Run does not have an approved artifact `{key}`: {run_id}",
            run_id=run_id,
        )
    return value


def _optional_artifacts(artifact_paths: dict[str, str], *keys: str) -> dict[str, str]:
    return {key: artifact_paths[key] for key in keys if artifact_paths.get(key)}


def _read_json_artifact(path: Path, *, run_id: str) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "approved_design_not_found",
            f"Approved JSON artifact is not valid JSON: {path.name}",
            run_id=run_id,
        ) from exc
    if not isinstance(parsed, dict):
        raise HarnessError(
            "approved_design_not_found",
            f"Approved JSON artifact must be an object: {path.name}",
            run_id=run_id,
        )
    return parsed


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _issue_key_from_state(state: dict[str, object]) -> str:
    source_ref = str(state.get("source_ref") or "")
    if source_ref.startswith("jira_issue:"):
        return source_ref.split(":", 1)[1]
    dataset = str(state.get("dataset") or "")
    use_case = str(state.get("use_case") or "")
    if dataset and use_case:
        return f"{dataset}-{use_case}"
    return use_case or "SDLC"


def _artifact_ref(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]


def _require_v2_phase3_contract(
    *,
    canonical: CanonicalRequirementInput,
    canonical_graph: dict[str, object],
    saved_graph: dict[str, object],
    trace: dict[str, object],
    run_id: str,
) -> None:
    revision = canonical.canonical_model.revision
    model_ref = canonical.approved_artifacts.get("requirement_model", "")
    if (
        saved_graph.get("source_graph_version") != "requirement_graph_v2"
        or saved_graph.get("requirement_contract_version") != "v2"
        or saved_graph.get("revision") != revision
        or saved_graph.get("requirements") != canonical_graph.get("requirements")
    ):
        raise HarnessError(
            "requirement_v2_phase3_input_invalid",
            "Saved requirement graph does not match the frozen canonical Requirement V2 model.",
            run_id=run_id,
        )
    trace_inputs = _dict_value(trace.get("inputs"))
    if (
        trace.get("mode") != "traceability_v2"
        or trace.get("requirement_contract_version") != "v2"
        or trace.get("requirement_revision") != revision
        or str(trace_inputs.get("requirement_model") or "") != model_ref
    ):
        raise HarnessError(
            "requirement_v2_phase3_input_invalid",
            "Requirement-design trace metadata does not match the frozen canonical revision.",
            run_id=run_id,
        )
    expected_ids = [
        str(item.get("id") or "")
        for item in _dict_list(canonical_graph.get("requirements"))
        if str(item.get("id") or "")
    ]
    for trace_key in ("class_trace", "sequence_trace"):
        actual_ids = [
            str(item.get("requirement") or "")
            for item in _dict_list(trace.get(trace_key))
            if str(item.get("requirement") or "")
        ]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
            raise HarnessError(
                "requirement_v2_phase3_input_invalid",
                f"{trace_key} does not contain each canonical stable requirement ID exactly once.",
                run_id=run_id,
            )


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
