"""Workflow Phase 4: implementation plan -> code/test/trace/QA -> Draft PR.

Module này điều phối Phase 4 sau khi implementation plan đã được human duyệt:
đóng gói DeveloperWorkPacket, cho Developer.act ghi code vào run worktree, chạy
Verification Runner, rồi bắt Validator kiểm trace requirement/design sang
code/test. Nếu Validator thấy gap nằm trong code/test, workflow ghi feedback cho
Developer.repair, refresh snapshot từ target repo thật, chạy lại runner +
Validator và chỉ pass khi trace đạt `implementation_traced`. Sau đó QA sinh
acceptance tests; nếu test fail do code/test, workflow có thể giao feedback lại
Developer.repair rồi chạy lại Runner -> Validator -> QA.

Khi QA pass, lát GitHub publication chỉ stage các file có trong work packet,
QA-generated tests hoặc verification support path đã được tham chiếu. Commit,
push và Draft PR được lưu thành artifact riêng; bước kế tiếp sync evidence sang
Jira/Confluence và mở checkpoint `code_review_ready` cho human review.

Mọi action đọc/sửa code resolve `run_workspace_v1.json` và giữ per-run lock.
Canonical target repo chỉ còn dùng để fetch/quản lý Git worktree, không bị checkout
hay ghi code bởi workflow Phase 4.

`revalidate_implementation` là đường vận hành riêng cho run từng bị block vì
repair limit: nó kiểm lại code hiện tại bằng logic Validator mới mà không tự
gọi Developer sửa thêm.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from ..agents.developer import (
    DeveloperActAgent,
    DeveloperActLlmError,
    DeveloperActResult,
    DeveloperExecutionError,
    DeveloperToolLoopExecutor,
    build_developer_work_packet,
    repair_scope_mismatch_retry_allowed,
    refresh_developer_work_packet,
)
from ..agents.tester import QaGeneratedTestSuite, QaTestResult, QaTesterAgent, QaTesterLlmError
from ..agents.validator import CodeTraceValidator, build_requirement_graph_v2
from ..artifacts import read_frozen_requirement_input
from ..artifacts.writers import (
    write_phase4_code_change_report,
    write_phase4_code_trace_report,
    write_phase4_developer_llm_metadata,
    write_phase4_developer_raw_response,
    write_phase4_developer_work_packet,
    write_phase4_effective_design_contract,
    write_phase4_github_code_review,
    write_phase4_github_code_review_approval,
    write_phase4_github_pr_body,
    write_phase4_qa_execution_report,
    write_phase4_qa_generated_tests,
    write_phase4_qa_raw_response,
    write_phase4_qa_repair_feedback,
    write_phase4_qa_test_report,
    write_phase4_repair_feedback,
    write_phase4_test_report,
)
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..repo_context import CodeGraphContextProvider
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync.github import (
    GitHubCodeReviewApprover,
    GitHubCodeReviewPublisher,
    GitHubPublishError,
    github_repository_from_remote,
)
from ..sync.runner import sync_code_review_checkpoint
from ..verification import VerificationResult, VerificationRunner
from ..workspaces import RunWorkspaceManager, RunWorkspaceRecord
from .phase3 import _dict_value, _issue_key_from_state, _read_json_artifact, _string_dict, _target_source_files

ALLOWED_IMPLEMENTATION_READY_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_WRITTEN_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_TESTED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_TRACED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS = [
    "continue_run",
    "revalidate_implementation",
    "get_status",
    "cancel_run",
]
ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS = ["revalidate_implementation", "get_status", "cancel_run"]
ALLOWED_QA_REPAIR_NEEDED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_QA_TESTED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_CODE_REVIEW_PUBLISH_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_CODE_REVIEW_SYNC_ACTIONS = ["get_status", "sync_run", "cancel_run"]
ALLOWED_CODE_REVIEW_READY_ACTIONS = ["approve_checkpoint", "get_status", "sync_run", "cancel_run"]
ALLOWED_CODE_REVIEW_APPROVED_ACTIONS = ["get_status", "sync_run", "cancel_run"]
IMPLEMENTATION_AUTOMATION_STATUSES = {
    "implementation_written",
    "implementation_tested",
    "implementation_repair_needed",
}

REVALIDATABLE_IMPLEMENTATION_ERROR_CODES = {
    "implementation_repair_limit_exceeded",
    "test_failed",
    "test_skipped",
    "validator_evidence_or_mapping_issue",
    "implementation_traceability_llm_failed",
}
CODE_TRACE_APPROVED_CONTEXT_LIMITS = {
    "requirement_packet": 12000,
    "design_artifact": 20000,
    "implementation_plan": 20000,
}

MAX_QA_REQUIREMENT_CHARS = 6000
MAX_QA_VERIFICATION_CHARS = 4000
MAX_QA_CODE_FILE_CHARS = 5000
MAX_QA_CODE_TOTAL_CHARS = 24000
MAX_QA_TRACE_SAMPLE_EDGES = 20
MAX_VERIFICATION_DIAGNOSTIC_CHARS = 6000


class PhaseFourWorkflow:
    """Điều phối Phase 4 từ plan đã duyệt tới QA và publish code review."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.repo_context_provider = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")
        self.llm_client = LlmClient(settings)
        self.developer = DeveloperActAgent(self.llm_client)
        self.developer_executor = DeveloperToolLoopExecutor(
            settings=settings,
            store=self.store,
            llm_client=self.llm_client,
        )
        self.verification_runner = VerificationRunner()
        self.code_trace_validator = CodeTraceValidator(self.llm_client)
        self.qa_tester = QaTesterAgent(self.llm_client)
        self.github_publisher = GitHubCodeReviewPublisher()
        self.github_approver = GitHubCodeReviewApprover()
        self.workspace_manager = RunWorkspaceManager(settings)

    def approve_code_review_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Duyệt Code Review bằng Draft -> Ready; merge vẫn do human."""

        state = self.store.read_state(run_id)
        self._require_code_review_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        with self.workspace_manager.locked_workspace(run_id) as workspace:
            state = self.store.read_state(run_id)
            self._require_code_review_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
            artifact_paths = _string_dict(state.get("latest_artifacts"))
            github_report_path = _required_phase4_artifact(
                artifact_paths,
                "github_code_review",
                run_id=run_id,
            )
            github_report = _read_json_artifact(
                self._existing_artifact_path(github_report_path, run_id=run_id),
                run_id=run_id,
            )
            published_pr = _dict_value(github_report.get("pull_request"))
            pr_number = workspace.pr_number
            if (
                pr_number is None
                or published_pr.get("number") != pr_number
                or str(published_pr.get("url") or "") != workspace.pr_url
            ):
                raise HarnessError(
                    "github_pr_identity_mismatch",
                    "Published PR identity does not match the run workspace.",
                    run_id=run_id,
                )
            try:
                approval = self.github_approver.approve(
                    target_path=Path(workspace.worktree_path),
                    repository=github_repository_from_remote(workspace.remote),
                    pr_number=pr_number,
                    expected_branch=workspace.branch,
                    expected_head_sha=workspace.head_sha,
                    expected_base_branch=self.settings.target_base_branch,
                    run_id=run_id,
                )
            except HarnessError as exc:
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "last_error": exc.to_response(),
                        "summary_for_user": (
                            f"Chưa thể chuyển Draft PR sang Ready cho run {run_id}: {exc.message}"
                        ),
                    }
                )
                self.store.write_state(state)
                raise

            approval_path = write_phase4_github_code_review_approval(
                store=self.store,
                run_id=run_id,
                report=approval,
            )
            artifact_paths["github_code_review_approval"] = approval_path
            quality_reports = _dict_value(state.get("quality_reports"))
            approved_pr = _dict_value(approval.get("pull_request"))
            quality_reports["code_review_approval"] = {
                "ok": True,
                "status": "ready_for_review",
                "operation": approval.get("operation"),
                "pr_url": approved_pr.get("url"),
                "draft": approved_pr.get("isDraft"),
                "approved_at": approval.get("approved_at"),
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review_approval",
                    "status": "code_review_approval_syncing",
                    "latest_artifacts": artifact_paths,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_SYNC_ACTIONS,
                    "last_error": None,
                    "summary_for_user": (
                        f"PR của run {run_id} đã Ready for review; đang đồng bộ trạng thái approval."
                    ),
                }
            )
            self.store.write_state(state)
            return self._sync_code_review_locked(run_id=run_id, review_status="code_review_approved")

    def approve_implementation_plan_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Duyệt implementation plan trong workspace đã được Phase 3 khóa."""

        state = self.store.read_state(run_id)
        self._require_implementation_plan_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        with self.workspace_manager.locked_workspace(run_id):
            return self._approve_implementation_plan_checkpoint_locked(
                run_id=run_id,
                checkpoint_id=checkpoint_id,
            )

    def _approve_implementation_plan_checkpoint_locked(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Sinh DeveloperWorkPacket khi caller đang giữ per-run lock."""

        state = self.store.read_state(run_id)
        self._require_implementation_plan_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        implementation_plan_path = _required_phase4_artifact(
            artifact_paths,
            "implementation_plan",
            run_id=run_id,
        )
        structured_plan_path = _required_phase4_artifact(
            artifact_paths,
            "implementation_plan_structured",
            run_id=run_id,
        )
        review_path = _required_phase4_artifact(
            artifact_paths,
            "implementation_plan_review",
            run_id=run_id,
        )
        # Các artifact design/trace vẫn được kiểm tra ở đây để đảm bảo packet
        # không bị tách khỏi graph đã được Validator dùng khi lập plan.
        for key in (
            "design_artifact",
            "requirement_design_trace",
            "class_diagram_graph",
            "sequence_diagram_graph",
        ):
            _required_phase4_artifact(artifact_paths, key, run_id=run_id)

        structured_plan = _read_json_artifact(
            self._existing_artifact_path(structured_plan_path, run_id=run_id),
            run_id=run_id,
        )
        review = _read_json_artifact(self._existing_artifact_path(review_path, run_id=run_id), run_id=run_id)
        if review.get("ok") is not True:
            raise HarnessError(
                "implementation_plan_review_not_passed",
                "Implementation plan review is not marked ok; Phase 4 cannot start.",
                run_id=run_id,
            )

        design_adjustments = _dict_items(structured_plan.get("design_adjustments"))
        unresolved_adjustments = [
            item
            for item in design_adjustments
            if item.get("action") == "unresolved" or item.get("status") == "conflict"
        ]
        if unresolved_adjustments:
            raise HarnessError(
                "design_repo_conflict_unresolved",
                "Implementation plan contains an unresolved design/repository conflict.",
                run_id=run_id,
            )
        effective_design_contract = {
            "mode": "approved_design_with_phase3_adjustments_v1",
            "run_id": run_id,
            "checkpoint_id": checkpoint_id or f"checkpoint_implementation_plan_{run_id}",
            "source_design_artifacts": {
                key: artifact_paths[key]
                for key in (
                    "design_artifact",
                    "requirement_design_trace",
                    "class_diagram_graph",
                    "sequence_diagram_graph",
                )
            },
            "approved_adjustments": [
                {**item, "status": "approved"}
                for item in design_adjustments
                if item.get("action") == "omit"
            ],
        }
        effective_design_contract_path = write_phase4_effective_design_contract(
            store=self.store,
            run_id=run_id,
            contract=effective_design_contract,
            version=1,
        )

        canonical_requirements, requirement_contract, requirement_coverage = self._require_v2_phase4_input(
            state=state,
            artifact_paths=artifact_paths,
            structured_plan=structured_plan,
            review=review,
            run_id=run_id,
        )

        try:
            workspace = self.workspace_manager.resolve(run_id)
            target_info = self._workspace_target_info(run_id=run_id, allow_dirty=False)
            packet = build_developer_work_packet(
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                issue_key=_issue_key_from_state(state),
                structured_plan=structured_plan,
                target_repo_path=target_info.path,
                run_workspace=workspace.to_dict(),
                approved_plan_refs={
                    "implementation_plan": implementation_plan_path,
                    "implementation_plan_structured": structured_plan_path,
                    "implementation_plan_review": review_path,
                    "effective_design_contract": effective_design_contract_path,
                },
                canonical_requirements=canonical_requirements,
                requirement_contract=requirement_contract,
                requirement_coverage=requirement_coverage,
                effective_design_contract={
                    **effective_design_contract,
                    "artifact_path": effective_design_contract_path,
                },
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_preparing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chuẩn bị DeveloperWorkPacket: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        packet_path = write_phase4_developer_work_packet(
            store=self.store,
            run_id=run_id,
            packet=packet,
            version=1,
        )
        latest_artifacts = {
            **artifact_paths,
            "effective_design_contract": effective_design_contract_path,
            "developer_work_packet": packet_path,
        }
        approved_artifacts = {
            **_dict_value(state.get("approved_artifacts")),
            "implementation_plan": implementation_plan_path,
            "implementation_plan_structured": structured_plan_path,
            "implementation_plan_review": review_path,
            "effective_design_contract": effective_design_contract_path,
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_preparing",
                "status": "implementation_ready",
                "latest_artifacts": latest_artifacts,
                "approved_artifacts": approved_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_READY_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Đã duyệt implementation plan cho run {run_id} và chuẩn bị DeveloperWorkPacket V1. "
                    f"Target repo `{target_info.path}` sạch, packet có {len(packet['proposed_files'])} file mục tiêu. "
                    "Workspace/branch của run đã được khóa; slice này chưa sửa code và chưa mở PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _require_v2_phase4_input(
        self,
        *,
        state: dict[str, object],
        artifact_paths: dict[str, str],
        structured_plan: dict[str, object],
        review: dict[str, object],
        run_id: str,
    ) -> tuple[list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
        """Validate the frozen V2 contract and Phase 3 coverage before any Phase 4 mutation."""

        try:
            frozen = read_frozen_requirement_input(store=self.store, state=state, run_id=run_id)
            graph = build_requirement_graph_v2(frozen.canonical_model)
            contract = _dict_value(structured_plan.get("requirement_contract"))
            expected_refs = frozen.approved_artifacts
            actual_refs = _string_dict(contract.get("artifact_refs"))
            if (
                contract.get("version") != "v2"
                or contract.get("revision") != frozen.canonical_model.revision
                or any(actual_refs.get(key) != value for key, value in expected_refs.items())
            ):
                raise HarnessError(
                    "requirement_model_v2_invalid",
                    "Implementation plan requirement contract does not match the frozen canonical revision.",
                )
            if review.get("ok") is not True or review.get("requirement_contract_version") != "v2":
                raise HarnessError(
                    "implementation_plan_review_not_passed",
                    "Implementation plan review is not an approved Requirement V2 review.",
                )
            trace_ref = _required_phase4_artifact(artifact_paths, "requirement_design_trace", run_id=run_id)
            trace = _read_json_artifact(self._existing_artifact_path(trace_ref, run_id=run_id), run_id=run_id)
            trace_inputs = _dict_value(trace.get("inputs"))
            if (
                trace.get("mode") != "traceability_v2"
                or trace.get("requirement_revision") != frozen.canonical_model.revision
                or trace_inputs.get("requirement_model") != expected_refs.get("requirement_model")
            ):
                raise HarnessError(
                    "requirement_model_v2_invalid",
                    "Requirement-design trace does not match the frozen canonical revision.",
                )
            canonical = _dict_items(graph.get("requirements"))
            coverage = _dict_items(structured_plan.get("requirement_coverage"))
            approved_design_ids = {
                str(item.get("design_element_id") or "").strip()
                for item in _dict_items(structured_plan.get("design_mappings"))
                if str(item.get("design_element_id") or "").strip()
            }
            referenced_design_ids = {
                design_id
                for item in coverage
                for design_id in _string_items(item.get("design_element_ids"))
            }
            if not referenced_design_ids or not referenced_design_ids.issubset(approved_design_ids):
                raise HarnessError(
                    "requirement_model_v2_invalid",
                    "Requirement coverage references missing or unapproved design element IDs.",
                )
            _require_exact_v2_coverage(
                canonical_requirements=canonical,
                coverage=coverage,
                trace=trace,
                run_id=run_id,
            )
            return canonical, contract, coverage
        except HarnessError as exc:
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"Requirement V2 Phase 4 input is invalid: {exc.message}",
                run_id=run_id,
            ) from exc

    def continue_run(self, *, run_id: str) -> dict[str, object]:
        """Chạy đúng một transition Phase 4; dùng cho test và thao tác nội bộ."""

        with self.workspace_manager.locked_workspace(run_id):
            return self._continue_run_locked(run_id=run_id)

    def continue_until_implementation_checkpoint(self, *, run_id: str) -> dict[str, object]:
        """Tự chạy Developer -> Verification -> Validator tới checkpoint hoặc trạng thái chặn."""

        # One implementation/repair slice can require Developer, Verification,
        # and Validator transitions before the next slice is known.
        max_transitions = max(8, self.settings.implementation_repair_limit * 3 + 5)
        with self.workspace_manager.locked_workspace(run_id):
            for _ in range(max_transitions):
                response = self._continue_run_locked(run_id=run_id)
                status = str(response.get("status") or "")
                if status not in IMPLEMENTATION_AUTOMATION_STATUSES:
                    return response
        raise HarnessError(
            "implementation_automation_limit_exceeded",
            f"Implementation automation exceeded {max_transitions} transitions for run {run_id}.",
            run_id=run_id,
        )

    def _continue_run_locked(self, *, run_id: str) -> dict[str, object]:
        """Dispatch một transition khi caller đang giữ per-run workspace lock."""

        state = self.store.read_state(run_id)
        status = str(state.get("status"))
        if status == "implementation_ready":
            return self._continue_developer_act(state=state, run_id=run_id)
        if status in {"implementation_executing", "implementation_paused"}:
            execution = _dict_value(state.get("developer_execution"))
            if str(state.get("current_phase")) == "qa_repair" or execution.get("mode") == "qa_repair":
                return self._continue_qa_repair(state=state, run_id=run_id)
            if execution.get("mode") == "implementation_repair" or str(state.get("current_phase")) == "implementation_repair":
                return self._continue_implementation_repair(state=state, run_id=run_id)
            return self._continue_developer_act(state=state, run_id=run_id)
        if status == "blocked" and self._legacy_developer_retry_allowed(state):
            return self._continue_developer_act(state=state, run_id=run_id)
        if status == "blocked" and repair_scope_mismatch_retry_allowed(state):
            execution = _dict_value(state.get("developer_execution"))
            if str(state.get("current_phase")) == "qa_repair" or execution.get("mode") == "qa_repair":
                return self._continue_qa_repair(state=state, run_id=run_id)
            return self._continue_implementation_repair(state=state, run_id=run_id)
        if status == "blocked" and self._legacy_verification_repair_allowed(state):
            prepared = self._prepare_legacy_verification_repair(state=state, run_id=run_id)
            if str(prepared.get("status")) == "implementation_repair_needed":
                return self._continue_implementation_repair(state=prepared, run_id=run_id)
            return self._status_response(prepared, created_new_run=False)
        if status == "implementation_written":
            return self._continue_verification(state=state, run_id=run_id)
        if status == "implementation_tested":
            return self._continue_code_trace(state=state, run_id=run_id)
        if status == "implementation_traced":
            return self._continue_qa_test(state=state, run_id=run_id)
        if status == "implementation_repair_needed":
            return self._continue_implementation_repair(state=state, run_id=run_id)
        if status == "qa_repair_needed":
            return self._continue_qa_repair(state=state, run_id=run_id)
        if status in {"qa_tested", "code_review_publish_failed"}:
            return self._continue_code_review_publish(state=state, run_id=run_id)
        if status == "code_review_published":
            return self._continue_code_review_sync(state=state, run_id=run_id)
        raise HarnessError(
            "run_not_continuable",
            f"Run is not ready for the next Phase 4 step: {run_id}",
            run_id=run_id,
        )


    def _continue_code_review_publish(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Commit/push code đã qua QA và tạo hoặc cập nhật Draft PR."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        report_path = ""
        workspace: RunWorkspaceRecord | None = None
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            qa_generated_path = _required_phase4_artifact(artifact_paths, "qa_generated_tests", run_id=run_id)
            for key in (
                "code_change_report",
                "test_report",
                "requirement_design_code_trace",
                "qa_test_execution",
                "qa_test_report",
            ):
                _required_phase4_artifact(artifact_paths, key, run_id=run_id)
            qa_quality = _dict_value(_dict_value(state.get("quality_reports")).get("qa_acceptance"))
            if qa_quality.get("ok") is not True:
                raise HarnessError(
                    "github_publish_qa_not_passed",
                    "Draft PR requires a passed QA acceptance report.",
                    run_id=run_id,
                )

            work_packet = _read_json_artifact(
                self._existing_artifact_path(packet_path, run_id=run_id),
                run_id=run_id,
            )
            qa_generated = _read_json_artifact(
                self._existing_artifact_path(qa_generated_path, run_id=run_id),
                run_id=run_id,
            )
            workspace = self.workspace_manager.resolve(run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            target_repository = _dict_value(work_packet.get("target_repository"))
            branch = str(target_repository.get("planned_branch") or "").strip()
            if not branch:
                raise HarnessError(
                    "github_publish_branch_missing",
                    "DeveloperWorkPacket has no planned branch for GitHub publication.",
                    run_id=run_id,
                )
            base_branch = str(target_repository.get("base_branch") or self.settings.target_base_branch).strip()
            issue_key = str(work_packet.get("issue_key") or _issue_key_from_state(state)).strip()
            repository = github_repository_from_remote(self.settings.target_repo_remote)
            publication_paths = _code_review_publication_paths(
                work_packet=work_packet,
                qa_generated=qa_generated,
                run_id=run_id,
            )
            pr_title = f"[{issue_key}] Implement approved use case"
            pr_body = _code_review_pr_body(
                state=state,
                run_id=run_id,
                issue_key=issue_key,
                artifact_paths=artifact_paths,
                qa_generated=qa_generated,
            )
            body_path = write_phase4_github_pr_body(store=self.store, run_id=run_id, body=pr_body)
            latest_artifacts["github_pr_body"] = body_path
            result = self.github_publisher.publish(
                target_path=target_info.path,
                expected_remote=self.settings.target_repo_remote,
                repository=repository,
                branch=branch,
                base_branch=base_branch,
                issue_key=issue_key,
                run_id=run_id,
                publication_paths=publication_paths,
                pr_title=pr_title,
                pr_body_path=self.store.repo_root / body_path,
            )
            report = result.to_dict()
            report_path = write_phase4_github_code_review(store=self.store, run_id=run_id, report=report)
            latest_artifacts["github_code_review"] = report_path
            pull_request = _dict_value(report.get("pull_request"))
            workspace = self.workspace_manager.refresh_locked(
                workspace,
                pr_number=_positive_int_or_none(pull_request.get("number")),
                pr_url=str(pull_request.get("url") or ""),
            )
            state["workspace"] = workspace.status_summary()
            quality_reports = {
                **_dict_value(state.get("quality_reports")),
                "code_review_publication": {
                    "ok": True,
                    "status": "published",
                    "commit_sha": _dict_value(report.get("commit")).get("sha"),
                    "pr_url": pull_request.get("url"),
                    "draft": pull_request.get("isDraft"),
                },
            }
        except GitHubPublishError as exc:
            if exc.partial_result:
                partial_pr = _dict_value(exc.partial_result.get("pull_request"))
                if workspace is not None and _dict_value(exc.partial_result.get("commit")).get("sha"):
                    workspace = self.workspace_manager.refresh_locked(
                        workspace,
                        pr_number=_positive_int_or_none(partial_pr.get("number")),
                        pr_url=str(partial_pr.get("url") or "") or None,
                    )
                    state["workspace"] = workspace.status_summary()
                report_path = write_phase4_github_code_review(
                    store=self.store,
                    run_id=run_id,
                    report={**exc.partial_result, "status": "partial_failed", "error": exc.to_response()},
                )
                latest_artifacts["github_code_review"] = report_path
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review_publishing",
                    "status": "code_review_publish_failed",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_PUBLISH_ACTIONS,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"GitHub code-review publication lỗi cho run {run_id}: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review_publishing",
                    "status": "code_review_publish_failed",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_PUBLISH_ACTIONS,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Không thể chuẩn bị GitHub code review cho run {run_id}: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "code_review_publishing",
                "status": "code_review_published",
                "latest_artifacts": latest_artifacts,
                "quality_reports": quality_reports,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_CODE_REVIEW_PUBLISH_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Đã commit, push branch và publish Draft PR cho run {run_id}: "
                    f"{pull_request.get('url', '')}. Chưa sync checkpoint code review về Jira/Confluence."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_code_review_sync(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Mở checkpoint Code Review bằng cách sync evidence PR sang tool cộng tác."""

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "code_review_syncing",
                "status": "code_review_syncing",
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_CODE_REVIEW_SYNC_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Đang sync checkpoint Code Review cho run {run_id}.",
            }
        )
        self.store.write_state(state)
        return self._sync_code_review_locked(run_id=run_id)

    def sync_code_review(self, *, run_id: str) -> dict[str, object]:
        """Sync hoặc re-sync checkpoint Code Review; không approve hay sửa artifact."""

        with self.workspace_manager.locked_workspace(run_id):
            return self._sync_code_review_locked(run_id=run_id)

    def _sync_code_review_locked(
        self,
        *,
        run_id: str,
        review_status: str | None = None,
    ) -> dict[str, object]:
        """Sync Code Review khi caller đang giữ per-run lock."""

        state = self.store.read_state(run_id)
        status = str(state.get("status") or "")
        if status not in {
            "code_review_published",
            "code_review_syncing",
            "code_review_sync_failed",
            "code_review_ready",
            "code_review_approval_syncing",
            "code_review_approval_sync_failed",
            "code_review_approved",
        }:
            raise HarnessError(
                "sync_not_ready",
                f"Run is not ready for code review sync: {run_id}",
                run_id=run_id,
            )
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        if review_status is None:
            review_status = (
                "code_review_approved"
                if status in {
                    "code_review_approval_syncing",
                    "code_review_approval_sync_failed",
                    "code_review_approved",
                }
                else "code_review_ready"
            )
        if review_status not in {"code_review_ready", "code_review_approved"}:
            raise HarnessError(
                "code_review_status_invalid",
                f"Unsupported Code Review sync status: {review_status}",
                run_id=run_id,
            )
        for key in (
            "github_code_review",
            "test_report",
            "requirement_design_code_trace",
            "qa_test_execution",
            "qa_test_report",
        ):
            _required_phase4_artifact(artifact_paths, key, run_id=run_id)
        if review_status == "code_review_approved":
            _required_phase4_artifact(artifact_paths, "github_code_review_approval", run_id=run_id)

        last_sync = sync_code_review_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=artifact_paths,
            review_status=review_status,
        )
        sync_ok = str(last_sync.get("status") or "") in {"synced", "mock_synced"}
        github_report_path = artifact_paths["github_code_review"]
        github_report = _read_json_artifact(
            self._existing_artifact_path(github_report_path, run_id=run_id),
            run_id=run_id,
        )
        pull_request = _dict_value(github_report.get("pull_request"))
        if sync_ok and review_status == "code_review_approved":
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review_approved",
                    "status": "code_review_approved",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_APPROVED_ACTIONS,
                    "last_sync": last_sync,
                    "last_error": None,
                    "summary_for_user": (
                        f"Code Review của run {run_id} đã được duyệt. "
                        f"PR đã Ready for review: {pull_request.get('url', '')}. "
                        "Harness không merge; anh tiếp tục review và merge trực tiếp trên GitHub."
                    ),
                }
            )
        elif sync_ok:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review",
                    "status": "code_review_ready",
                    "pending_checkpoint": {
                        "checkpoint_id": f"checkpoint_code_review_{run_id}",
                        "type": "code_review",
                        "artifact_ref": "github_code_review_v1",
                        "artifact_path": github_report_path,
                        "pull_request_url": str(pull_request.get("url") or ""),
                    },
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_READY_ACTIONS,
                    "last_sync": last_sync,
                    "last_error": None,
                    "summary_for_user": (
                        f"Checkpoint Code Review đã sẵn sàng cho run {run_id}. "
                        f"Draft PR: {pull_request.get('url', '')}. "
                        "Anh có thể approve checkpoint để chuyển Draft PR thành Ready for review."
                    ),
                }
            )
        elif review_status == "code_review_approved":
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review_approval",
                    "status": "code_review_approval_sync_failed",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_SYNC_ACTIONS,
                    "last_sync": last_sync,
                    "last_error": {
                        "ok": False,
                        "error_code": "code_review_approval_sync_failed",
                        "message": f"Code Review approval sync status={last_sync.get('status')}",
                        "run_id": run_id,
                    },
                    "summary_for_user": (
                        f"PR của run {run_id} đã Ready nhưng Jira/Confluence chưa sync đủ; "
                        f"sync status={last_sync.get('status')}. Dùng sync-run để retry."
                    ),
                }
            )
        else:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "code_review_syncing",
                    "status": "code_review_sync_failed",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_CODE_REVIEW_SYNC_ACTIONS,
                    "last_sync": last_sync,
                    "last_error": {
                        "ok": False,
                        "error_code": "code_review_sync_failed",
                        "message": f"Code Review sync status={last_sync.get('status')}",
                        "run_id": run_id,
                    },
                    "summary_for_user": (
                        f"Checkpoint Code Review chưa sync đủ cho run {run_id}; "
                        f"sync status={last_sync.get('status')}. Evidence GitHub/QA/Validator vẫn được giữ để retry."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _code_trace_approved_context(
        self,
        *,
        artifact_paths: dict[str, str],
        run_id: str,
    ) -> dict[str, object]:
        """Load bounded approved artifacts so Validator findings stay grounded."""

        context: dict[str, object] = {}
        for key, limit in CODE_TRACE_APPROVED_CONTEXT_LIMITS.items():
            artifact_ref = _required_phase4_artifact(artifact_paths, key, run_id=run_id)
            artifact_path = self._existing_artifact_path(artifact_ref, run_id=run_id)
            content = artifact_path.read_text(encoding="utf-8", errors="replace")
            context[key] = {
                "artifact_ref": key,
                "artifact_path": artifact_ref,
                "content": content[:limit],
                "content_truncated": len(content) > limit,
            }

        feedback_ref = str(artifact_paths.get("implementation_repair_feedback") or "").strip()
        if feedback_ref:
            feedback_path = self._existing_artifact_path(feedback_ref, run_id=run_id)
            context["prior_validator_feedback"] = _read_json_artifact(feedback_path, run_id=run_id)
        return context

    def revalidate_implementation(self, *, run_id: str) -> dict[str, object]:
        """Re-run Verification and Validator for a blocked or disputed Validator result."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            last_error = _dict_value(state.get("last_error"))
            blocked_revalidation = (
                str(state.get("status")) == "blocked"
                and last_error.get("error_code") in REVALIDATABLE_IMPLEMENTATION_ERROR_CODES
            )
            repair_feedback_revalidation = str(state.get("status") or "") == "implementation_repair_needed"
            if not blocked_revalidation and not repair_feedback_revalidation:
                raise HarnessError(
                    "implementation_revalidate_not_allowed",
                    (
                        "Implementation revalidate is only allowed for a revalidatable Phase 4 block or "
                        f"pending Validator repair feedback: {run_id}"
                    ),
                    run_id=run_id,
                )
            return self._revalidate_current_implementation(state=state, run_id=run_id)

    def _continue_developer_act(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        if self.settings.developer_execution_mode == "tool_loop":
            return self._continue_developer_tool_loop(state=state, run_id=run_id)
        return self._continue_developer_act_legacy(state=state, run_id=run_id)

    def _continue_developer_tool_loop(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run or resume budget-aware Developer actions with per-file checkpoints."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(
                self._existing_artifact_path(packet_path, run_id=run_id),
                run_id=run_id,
            )
            resuming = str(state.get("status") or "") in {"implementation_executing", "implementation_paused"}
            resuming = resuming or bool(artifact_paths.get("developer_execution_journal"))
            target_info = self._workspace_target_info(
                run_id=run_id,
                work_packet=work_packet,
                allow_dirty=resuming,
            )
            branch = target_info.current_branch

            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "implementation_executing",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": None,
                    "developer_execution": {
                        **_dict_value(state.get("developer_execution")),
                        "mode": "act",
                        "status": "running",
                    },
                    "summary_for_user": f"Developer tool loop đang triển khai code cho run {run_id}.",
                }
            )
            self.store.write_state(state)

            def record_progress(progress: dict[str, object], manifest_path: str, journal_path: str) -> None:
                latest_artifacts.update(
                    {
                        "developer_execution_manifest": manifest_path,
                        "developer_execution_journal": journal_path,
                    }
                )
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_writing",
                        "status": "implementation_executing",
                        "latest_artifacts": dict(latest_artifacts),
                        "allowed_next_actions": ["get_status", "cancel_run"],
                        "developer_execution": {"mode": "act", **progress},
                        "summary_for_user": (
                            f"Developer tool loop đã hoàn thành {progress.get('completed_items', 0)}/"
                            f"{progress.get('total_items', 0)} execution task cho run {run_id}."
                        ),
                    }
                )
                self.store.write_state(state)

            result = self.developer_executor.execute(
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_info.path,
                on_progress=record_progress,
            )
            latest_artifacts.update(
                {
                    "developer_execution_manifest": result.manifest_path,
                    "developer_execution_journal": result.journal_path,
                }
            )
            report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": branch,
                "summary": result.summary,
                "files": result.files,
                "execution_mode": "tool_loop",
                "execution_manifest": result.manifest_path,
                "execution_journal": result.journal_path,
                "progress": result.progress,
            }
            report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=report,
                version=1,
            )
        except DeveloperExecutionError as exc:
            if exc.manifest_path:
                latest_artifacts["developer_execution_manifest"] = exc.manifest_path
            if exc.journal_path:
                latest_artifacts["developer_execution_journal"] = exc.journal_path
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS if paused else ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "developer_execution": {"mode": "act", **exc.progress},
                    "summary_for_user": (
                        f"Developer tool loop đã tạm dừng cho run {run_id}: {exc.message}"
                        if paused
                        else f"Developer tool loop bị chặn cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chuẩn bị Developer tool loop: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except Exception as exc:  # Last-resort state recovery for executor defects.
            error = HarnessError(
                "developer_execution_internal_error",
                f"Unexpected Developer executor failure ({type(exc).__name__}): {exc}",
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "implementation_paused",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
                    "last_error": error.to_response(),
                    "developer_execution": {
                        **_dict_value(state.get("developer_execution")),
                        "mode": "act",
                        "status": "paused",
                    },
                    "summary_for_user": (
                        f"Developer tool loop đã tạm dừng an toàn cho run {run_id} vì lỗi executor nội bộ."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts["code_change_report"] = report_path
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "developer_execution": {
                "ok": True,
                "mode": "tool_loop",
                **result.progress,
                "manifest": result.manifest_path,
                "journal": result.journal_path,
            },
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_writing",
                "status": "implementation_written",
                "latest_artifacts": latest_artifacts,
                "quality_reports": quality_reports,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_WRITTEN_ACTIONS,
                "last_error": None,
                "developer_execution": {"mode": "act", **result.progress},
                "summary_for_user": (
                    f"Developer tool loop đã hoàn thành {result.progress.get('completed_items', 0)}/"
                    f"{result.progress.get('total_items', 0)} execution task cho run {run_id} trên branch `{branch}`. "
                    "Code chưa commit và chưa mở PR; bước tiếp theo là Verification Runner."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_developer_act_legacy(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.2: Developer.act sinh và apply code vào target repo."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet, allow_dirty=False)
            branch = target_info.current_branch
            result = self.developer.act(work_packet=work_packet)
            latest_artifacts = self._persist_developer_llm_artifacts(
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                action="act",
                version=1,
                value=result,
            )
            applied_files = _apply_developer_changes(
                result=result,
                work_packet=work_packet,
                target_path=target_info.path,
                run_id=run_id,
            )
            report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": branch,
                "summary": result.summary,
                "files": applied_files,
            }
            report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=report,
                version=1,
            )
        except HarnessError as exc:
            latest_artifacts = self._persist_developer_llm_artifacts(
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                action="act",
                version=1,
                value=exc,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Developer.act sinh/apply code: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        latest_artifacts = {
            **latest_artifacts,
            "code_change_report": report_path,
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_writing",
                "status": "implementation_written",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_WRITTEN_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Developer.act đã apply {len(applied_files)} file cho run {run_id} trên branch `{branch}`. "
                    "Slice 4.2 chưa chạy test, chưa commit và chưa mở PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.3: Verification Runner compile/unit test target repo."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        try:
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
            report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                **result.to_dict(),
            }
            report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=report,
                version=1,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chạy Verification Runner: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **artifact_paths,
            "test_report": report_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=result,
                report_path=report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=0,
            ),
        }
        if result.status == "passed":
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_tested",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TESTED_ACTIONS,
                    "last_error": None,
                    "summary_for_user": (
                        f"Verification Runner đã pass compile/unit test cho run {run_id} "
                        f"trên branch `{target_info.current_branch}`. Slice 4.3 chưa chạy code trace, "
                        "chưa commit và chưa mở PR."
                    ),
                }
            )
        else:
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=result,
                report_path=report_path,
                artifact_version=1,
                repair_iteration=0,
                current_phase="implementation_testing",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _prepare_legacy_verification_repair(
        self,
        *,
        state: dict[str, object],
        run_id: str,
    ) -> dict[str, object]:
        """Migrate a pre-repair-loop test failure into the current repair contract."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
        report_path = _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
        work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
        target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
        report_text = self._existing_artifact_path(report_path, run_id=run_id).read_text(
            encoding="utf-8",
            errors="replace",
        )
        result = _verification_result_from_markdown(report_text, cwd=str(target_info.path))
        artifact_version = _phase4_artifact_version(report_path, prefix="test_report_", default=1)
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=result,
                report_path=report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=_int_value(state.get("implementation_repair_iterations")),
            ),
        }
        self._apply_verification_failure_state(
            state=state,
            run_id=run_id,
            work_packet=work_packet,
            target_repo=str(target_info.path),
            branch=target_info.current_branch,
            result=result,
            report_path=report_path,
            artifact_version=artifact_version,
            repair_iteration=_int_value(state.get("implementation_repair_iterations")),
            current_phase="implementation_testing",
            latest_artifacts=artifact_paths,
            quality_reports=quality_reports,
        )
        self.store.write_state(state)
        return state

    def _apply_verification_failure_state(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
        target_repo: str,
        branch: str,
        result: VerificationResult,
        report_path: str,
        artifact_version: int,
        repair_iteration: int,
        current_phase: str,
        latest_artifacts: dict[str, str],
        quality_reports: dict[str, object],
    ) -> None:
        """Route actionable compile/test diagnostics to Developer.repair."""

        repairable = _is_repairable_verification_failure(result)
        quality_reports["verification"] = {
            **_dict_value(quality_reports.get("verification")),
            "repairable": repairable,
        }
        if repairable and repair_iteration < self.settings.implementation_repair_limit:
            feedback = _verification_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                result=result,
                report_path=report_path,
                target_repo=target_repo,
                branch=branch,
                feedback_version=artifact_version,
                repair_iteration=repair_iteration,
            )
            feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": feedback_path,
            }
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_source": "verification",
                "feedback_artifact": feedback_path,
                "repair_iterations": repair_iteration,
                "summary": result.reason,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_repair_needed",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": None,
                    "summary_for_user": (
                        f"Verification Runner chưa pass cho run {run_id}: {result.reason} "
                        f"Đã ghi feedback v{artifact_version} để Developer sửa code/test trong allowed_paths."
                    ),
                }
            )
            return

        if repairable:
            error_code = "implementation_repair_limit_exceeded"
            message = (
                f"Implementation repair limit reached after {repair_iteration} iteration(s); "
                f"latest verification result: {result.reason}"
            )
        else:
            error_code = "test_skipped" if result.status == "skipped" else "test_failed"
            message = result.reason
        quality_reports["implementation_repair"] = {
            "needed": repairable,
            "feedback_source": "verification",
            "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
            "repair_iterations": repair_iteration,
            "limit_exceeded": error_code == "implementation_repair_limit_exceeded",
            "summary": message,
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": current_phase,
                "status": "blocked",
                "latest_artifacts": latest_artifacts,
                "quality_reports": quality_reports,
                "pending_checkpoint": None,
                "allowed_next_actions": _implementation_blocked_actions(error_code),
                "implementation_repair_iterations": repair_iteration,
                "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                "summary_for_user": (
                    f"Verification Runner status={result.status} cho run {run_id}: {message}. "
                    "Workflow chưa được phép chạy Validator hoặc mở PR."
                ),
            }
        )

    def _continue_code_trace(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.4: Validator trace approved design sang code/unit test."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
            for key in (
                "design_artifact",
                "requirement_design_trace",
                "class_diagram_graph",
                "sequence_diagram_graph",
            ):
                _required_phase4_artifact(artifact_paths, key, run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            trace_result = self.code_trace_validator.validate(
                target_path=target_info.path,
                work_packet=work_packet,
                approved_context=self._code_trace_approved_context(
                    artifact_paths=artifact_paths,
                    run_id=run_id,
                ),
            )
            trace_payload = {
                **trace_result.to_dict(),
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
                version=1,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chạy Validator code trace: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **artifact_paths,
            "requirement_design_code_trace": trace_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if trace_result.ok:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_traced",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
                    "last_error": None,
                    "summary_for_user": (
                        f"Validator code trace đã pass cho run {run_id}: {trace_result.summary} "
                        "Slice 4.4 chưa chạy QA/Tester Agent, chưa commit và chưa mở PR."
                    ),
                }
            )
        elif _is_repairable_code_trace_failure(trace_result):
            feedback = _implementation_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                trace_result=trace_result,
                trace_path=trace_path,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=1,
                repair_iteration=0,
            )
            feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=1,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": feedback_path,
            }
            quality_reports = {
                **quality_reports,
                "implementation_repair": {
                    "needed": True,
                    "feedback_artifact": feedback_path,
                    "missing_count": len(trace_result.missing_links),
                        "repair_iterations": 0,
                    "summary": trace_result.summary,
                },
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_repair_needed",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
                    "last_error": None,
                    "implementation_repair_iterations": 0,
                    "summary_for_user": (
                        f"Validator code trace còn thiếu link có thể sửa ở code/test cho run {run_id}: "
                        f"{trace_result.summary} Đã ghi implementation_repair_feedback_v1.json. "
                        "Lần continue-run tiếp theo sẽ giao Developer.repair xử lý feedback này."
                    ),
                }
            )
        else:
            error_code = _nonrepairable_code_trace_error_code(trace_result)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(error_code),
                    "last_error": HarnessError(
                        error_code,
                        trace_result.summary,
                        run_id=run_id,
                    ).to_response(),
                    "summary_for_user": (
                        f"Validator code trace còn thiếu link cho run {run_id}: {trace_result.summary} "
                        "Workflow chưa được phép mở PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_qa_test(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run QA from an implementation that already passed code trace."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        qa_version = _next_qa_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            qa_run = self._run_qa_generated_tests(
                state=state,
                artifact_paths=artifact_paths,
                latest_artifacts=latest_artifacts,
                work_packet=work_packet,
                target_info=target_info,
                qa_version=qa_version,
            )
        except HarnessError as exc:
            return self._block_qa_error(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                qa_version=qa_version,
                exc=exc,
                summary_prefix="QA/Tester Agent bi chan khi sinh/chay acceptance tests",
            )

        return self._finalize_qa_outcome(
            state=state,
            run_id=run_id,
            work_packet=work_packet,
            target_info=target_info,
            qa_run=qa_run,
            qa_version=qa_version,
            qa_repair_iterations=_int_value(state.get("qa_repair_iterations")),
            repaired_from=None,
        )

    def _continue_qa_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run one large QA repair loop: Developer.repair -> runner -> Validator -> QA."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        previous_qa_iterations = _int_value(state.get("qa_repair_iterations"))
        repair_iteration = previous_qa_iterations + 1
        artifact_version = _next_phase4_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            feedback_path = _required_phase4_artifact(artifact_paths, "qa_repair_feedback", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            qa_feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_path, run_id=run_id),
                run_id=run_id,
            )
            if repair_iteration > self.settings.qa_repair_limit:
                raise HarnessError(
                    "qa_repair_limit_exceeded",
                    f"QA repair limit exceeded for run {run_id}: limit={self.settings.qa_repair_limit}.",
                    run_id=run_id,
                )
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            work_packet = refresh_developer_work_packet(
                packet=work_packet,
                target_repo_path=target_info.path,
                version=artifact_version,
                run_id=run_id,
            )
            refreshed_packet_path = write_phase4_developer_work_packet(
                store=self.store,
                run_id=run_id,
                packet=work_packet,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "developer_work_packet": refreshed_packet_path,
            }
            if self.settings.developer_execution_mode == "tool_loop":
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "qa_repair",
                        "status": "implementation_executing",
                        "latest_artifacts": latest_artifacts,
                        "pending_checkpoint": None,
                        "allowed_next_actions": ["get_status", "cancel_run"],
                        "developer_execution": {
                            "mode": "qa_repair",
                            "repair_iteration": repair_iteration,
                            "status": "running",
                        },
                        "last_error": None,
                        "summary_for_user": (
                            f"Developer tool loop đang xử lý QA repair iteration {repair_iteration} "
                            f"cho run {run_id}."
                        ),
                    }
                )
                self.store.write_state(state)

                def record_qa_repair_progress(
                    progress: dict[str, object],
                    manifest_path: str,
                    journal_path: str,
                ) -> None:
                    latest_artifacts.update(
                        {
                            "developer_qa_repair_execution_manifest": manifest_path,
                            "developer_qa_repair_execution_journal": journal_path,
                        }
                    )
                    state.update(
                        {
                            "updated_at": utc_now_iso(),
                            "current_phase": "qa_repair",
                            "status": "implementation_executing",
                            "latest_artifacts": dict(latest_artifacts),
                            "developer_execution": {
                                "mode": "qa_repair",
                                "repair_iteration": repair_iteration,
                                **progress,
                            },
                            "summary_for_user": (
                                f"Developer QA repair đã hoàn thành {progress.get('completed_items', 0)}/"
                                f"{progress.get('total_items', 0)} execution task cho run {run_id}."
                            ),
                        }
                    )
                    self.store.write_state(state)

                execution_result = self.developer_executor.execute(
                    run_id=run_id,
                    work_packet=work_packet,
                    target_path=target_info.path,
                    repair_feedback=qa_feedback,
                    repair_iteration=repair_iteration,
                    repair_scope="qa",
                    on_progress=record_qa_repair_progress,
                )
                latest_artifacts.update(
                    {
                        "developer_qa_repair_execution_manifest": execution_result.manifest_path,
                        "developer_qa_repair_execution_journal": execution_result.journal_path,
                    }
                )
                applied_files = execution_result.files
                developer_summary = execution_result.summary
                developer_raw_response = ""
            else:
                result = self.developer.repair(
                    work_packet=work_packet,
                    repair_feedback=qa_feedback,
                    repair_iteration=repair_iteration,
                )
                latest_artifacts = self._persist_developer_llm_artifacts(
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    action="qa_repair",
                    version=artifact_version,
                    value=result,
                )
                applied_files = _apply_developer_changes(
                    result=result,
                    work_packet=work_packet,
                    target_path=target_info.path,
                    run_id=run_id,
                )
                developer_summary = result.summary
                developer_raw_response = latest_artifacts.get("developer_repair_raw_response")
            change_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "summary": developer_summary,
                "files": applied_files,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                "developer_raw_response": developer_raw_response,
                "repair_source": "qa_acceptance",
            }
            change_report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=change_report,
                version=artifact_version,
            )
            verification_result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
            test_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                **verification_result.to_dict(),
            }
            test_report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=test_report,
                version=artifact_version,
            )
        except DeveloperExecutionError as exc:
            if exc.manifest_path:
                latest_artifacts["developer_qa_repair_execution_manifest"] = exc.manifest_path
            if exc.journal_path:
                latest_artifacts["developer_qa_repair_execution_journal"] = exc.journal_path
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS if paused else ["get_status", "cancel_run"],
                    "qa_repair_iterations": previous_qa_iterations,
                    "last_error": exc.to_response(),
                    "developer_execution": {
                        "mode": "qa_repair",
                        "repair_iteration": repair_iteration,
                        **exc.progress,
                    },
                    "summary_for_user": (
                        f"Developer QA repair đã tạm dừng cho run {run_id}: {exc.message}"
                        if paused
                        else f"Developer QA repair bị chặn cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            latest_artifacts = self._persist_developer_llm_artifacts(
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                action="qa_repair",
                version=artifact_version,
                value=exc,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": previous_qa_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi Developer.repair xu ly QA feedback: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "code_change_report": change_report_path,
            "test_report": test_report_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=verification_result,
                report_path=test_report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=_int_value(state.get("implementation_repair_iterations")),
            ),
        }
        if verification_result.status != "passed":
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=verification_result,
                report_path=test_report_path,
                artifact_version=artifact_version,
                repair_iteration=_int_value(state.get("implementation_repair_iterations")),
                current_phase="qa_repair",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
            state["qa_repair_iterations"] = repair_iteration
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self.code_trace_validator.validate(
                target_path=target_info.path,
                work_packet=work_packet,
                approved_context=self._code_trace_approved_context(
                    artifact_paths=latest_artifacts,
                    run_id=run_id,
                ),
            )
            trace_payload = {
                **trace_result.to_dict(),
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": repair_iteration,
                "repaired_from": feedback_path,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
                version=artifact_version,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": repair_iteration,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi Validator trace lai sau QA repair: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "requirement_design_code_trace": trace_path,
        }
        quality_reports = {
            **quality_reports,
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if not trace_result.ok:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": repair_iteration,
                    "last_error": HarnessError(
                        _nonrepairable_code_trace_error_code(trace_result),
                        trace_result.summary,
                        run_id=run_id,
                    ).to_response(),
                    "summary_for_user": (
                        f"Validator code trace khong pass sau QA repair iteration {repair_iteration}: "
                        f"{trace_result.summary} Workflow chua duoc phep chay QA lai."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        qa_version = _next_qa_artifact_version(self.store, run_id)
        try:
            qa_run = self._run_qa_generated_tests(
                state=state,
                artifact_paths=latest_artifacts,
                latest_artifacts=latest_artifacts,
                work_packet=work_packet,
                target_info=target_info,
                qa_version=qa_version,
                repaired_from=feedback_path,
            )
        except HarnessError as exc:
            return self._block_qa_error(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                qa_version=qa_version,
                exc=exc,
                summary_prefix="QA/Tester Agent bi chan khi chay lai sau Developer.repair",
                qa_repair_iterations=repair_iteration,
            )

        return self._finalize_qa_outcome(
            state=state,
            run_id=run_id,
            work_packet=work_packet,
            target_info=target_info,
            qa_run=qa_run,
            qa_version=qa_version,
            qa_repair_iterations=repair_iteration,
            repaired_from=feedback_path,
            quality_reports_override=quality_reports,
        )

    def _run_qa_generated_tests(
        self,
        *,
        state: dict[str, object],
        artifact_paths: dict[str, str],
        latest_artifacts: dict[str, str],
        work_packet: dict[str, object],
        target_info: object,
        qa_version: int,
        repaired_from: str | None = None,
    ) -> dict[str, object]:
        """Sinh/apply QA tests, chạy baseline và từng QA class bắt buộc, rồi ghi artifacts."""

        context = _qa_review_context(
            store=self.store,
            state=state,
            artifact_paths=artifact_paths,
            run_id=str(state.get("run_id") or ""),
            target_repo=str(target_info.path),
            branch=target_info.current_branch,
            work_packet=work_packet,
        )
        if repaired_from:
            context["repaired_from"] = repaired_from
        generated_tests = self.qa_tester.generate_tests(context=context)
        generated_payload = {
            **generated_tests.to_dict(),
            "run_id": state.get("run_id"),
            "source_ref": state.get("source_ref"),
            "issue_key": work_packet.get("issue_key"),
            "target_repo": str(target_info.path),
            "branch": target_info.current_branch,
            "repaired_from": repaired_from,
        }
        generated_path = write_phase4_qa_generated_tests(
            store=self.store,
            run_id=str(state.get("run_id") or ""),
            generated=generated_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_generated_tests": generated_path,
        }
        applied_tests = _apply_qa_generated_tests(
            generated=generated_tests,
            target_path=target_info.path,
            run_id=str(state.get("run_id") or ""),
        )
        execution = self.verification_runner.run(
            target_path=target_info.path,
            work_packet=work_packet,
            required_test_files=[str(item.get("path") or "") for item in applied_tests],
        )
        execution_payload = {
            "run_id": state.get("run_id"),
            "source_ref": state.get("source_ref"),
            "issue_key": work_packet.get("issue_key"),
            "target_repo": str(target_info.path),
            "branch": target_info.current_branch,
            "qa_generated_tests": generated_path,
            "applied_tests": applied_tests,
            "repaired_from": repaired_from,
            **execution.to_dict(),
        }
        execution_path = write_phase4_qa_execution_report(
            store=self.store,
            run_id=str(state.get("run_id") or ""),
            report=execution_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_test_execution": execution_path,
        }
        result = _qa_result_from_generated_execution(
            generated=generated_tests,
            execution=execution,
            execution_path=execution_path,
            generated_path=generated_path,
            applied_tests=applied_tests,
        )
        report_payload = {
            **result.to_dict(),
            "run_id": state.get("run_id"),
            "source_ref": state.get("source_ref"),
            "issue_key": work_packet.get("issue_key"),
            "target_repo": str(target_info.path),
            "branch": target_info.current_branch,
            "qa_generated_tests": generated_path,
            "qa_test_execution": execution_path,
            "execution_status": execution.status,
            "applied_tests": applied_tests,
            "repaired_from": repaired_from,
            "requirement_contract": work_packet.get("requirement_contract"),
            "requirement_ids": sorted(
                {
                    requirement_id
                    for scenario in generated_tests.scenarios
                    for requirement_id in _string_items(scenario.get("requirement_ids"))
                }
            ),
        }
        report_path = write_phase4_qa_test_report(
            store=self.store,
            run_id=str(state.get("run_id") or ""),
            report=report_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_test_report": report_path,
        }
        return {
            "latest_artifacts": latest_artifacts,
            "result": result,
            "applied_tests": applied_tests,
            "generated_path": generated_path,
            "execution_path": execution_path,
            "report_path": report_path,
        }

    def _finalize_qa_outcome(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
        target_info: object,
        qa_run: dict[str, object],
        qa_version: int,
        qa_repair_iterations: int,
        repaired_from: str | None,
        quality_reports_override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Move the run to qa_tested, qa_repair_needed, or blocked after QA execution."""

        latest_artifacts = _string_dict(qa_run.get("latest_artifacts"))
        result = qa_run["result"]
        if not isinstance(result, QaTestResult):
            raise HarnessError("qa_test_failed", "QA result object is invalid.", run_id=run_id)
        applied_tests = _dict_items(qa_run.get("applied_tests"))
        quality_reports = {
            **(quality_reports_override or _dict_value(state.get("quality_reports"))),
            "qa_acceptance": result.to_quality_report(),
        }
        if result.ok:
            if qa_repair_iterations:
                quality_reports["qa_repair"] = {
                    "needed": False,
                    "repair_iterations": qa_repair_iterations,
                    "repaired_from": repaired_from,
                    "summary": result.summary,
                }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_testing",
                    "status": "qa_tested",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_QA_TESTED_ACTIONS,
                    "qa_repair_iterations": qa_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"QA/Tester Agent da sinh {len(applied_tests)} file acceptance test va Verification Runner "
                        f"da pass cho run {run_id}: {result.summary} Chua commit va chua mo PR."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if result.status == "fail" and qa_repair_iterations < self.settings.qa_repair_limit:
            feedback = _qa_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                qa_result=result,
                qa_run=qa_run,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=qa_version,
                repair_iteration=qa_repair_iterations,
            )
            feedback_path = write_phase4_qa_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=qa_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "qa_repair_feedback": feedback_path,
            }
            quality_reports["qa_repair"] = {
                "needed": True,
                "feedback_artifact": feedback_path,
                "repair_iterations": qa_repair_iterations,
                "summary": result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "qa_repair_needed",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_QA_REPAIR_NEEDED_ACTIONS,
                    "qa_repair_iterations": qa_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"QA/Tester Agent fail cho run {run_id}: {result.summary} "
                        f"Da ghi qa_repair_feedback_v{qa_version}.json; continue-run se giao Developer.repair."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if result.status == "skipped":
            error_code = "qa_test_skipped"
        elif result.status == "fail":
            error_code = "qa_repair_limit_exceeded"
            quality_reports["qa_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("qa_repair_feedback"),
                "repair_iterations": qa_repair_iterations,
                "limit_exceeded": True,
                "summary": result.summary,
            }
        else:
            error_code = "qa_test_failed"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "qa_testing",
                "status": "blocked",
                "latest_artifacts": latest_artifacts,
                "quality_reports": quality_reports,
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "qa_repair_iterations": qa_repair_iterations,
                "last_error": HarnessError(error_code, result.summary, run_id=run_id).to_response(),
                "summary_for_user": (
                    f"QA/Tester Agent status={result.status} cho run {run_id}: {result.summary} "
                    "Workflow chua duoc phep mo PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _persist_developer_llm_artifacts(
        self,
        *,
        run_id: str,
        latest_artifacts: dict[str, str],
        action: str,
        version: int,
        value: DeveloperActResult | DeveloperActLlmError | HarnessError,
    ) -> dict[str, str]:
        """Giữ raw output và metadata kể cả khi Developer JSON parse thất bại."""

        if not isinstance(value, (DeveloperActResult, DeveloperActLlmError)):
            return latest_artifacts
        result = dict(latest_artifacts)
        artifact_prefix = "developer_act" if action == "act" else "developer_repair"
        raw_response = getattr(value, "raw_response", "")
        if raw_response:
            result[f"{artifact_prefix}_raw_response"] = write_phase4_developer_raw_response(
                store=self.store,
                run_id=run_id,
                action=action,
                raw_response=raw_response,
                version=version,
            )
        metadata = getattr(value, "llm_metadata", None)
        if isinstance(metadata, dict) and metadata:
            result[f"{artifact_prefix}_llm_metadata"] = write_phase4_developer_llm_metadata(
                store=self.store,
                run_id=run_id,
                action=action,
                metadata=metadata,
                version=version,
            )
        return result

    def _block_qa_error(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        qa_version: int,
        exc: HarnessError,
        summary_prefix: str,
        qa_repair_iterations: int | None = None,
    ) -> dict[str, object]:
        """Block a QA step and persist raw LLM output when available."""

        raw_response = getattr(exc, "raw_response", "")
        if isinstance(exc, QaTesterLlmError) and raw_response:
            latest_artifacts = {
                **latest_artifacts,
                "qa_tester_raw_response": write_phase4_qa_raw_response(
                    store=self.store,
                    run_id=run_id,
                    raw_response=raw_response,
                    version=qa_version,
                ),
            }
        update = {
            "updated_at": utc_now_iso(),
            "current_phase": "qa_testing",
            "status": "blocked",
            "latest_artifacts": latest_artifacts,
            "pending_checkpoint": None,
            "allowed_next_actions": ["get_status", "cancel_run"],
            "last_error": exc.to_response(),
            "summary_for_user": f"{summary_prefix}: {exc.message}",
        }
        if qa_repair_iterations is not None:
            update["qa_repair_iterations"] = qa_repair_iterations
        state.update(update)
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _revalidate_current_implementation(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Re-run Verification Runner and Validator without editing code."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        previous_iterations = _int_value(state.get("implementation_repair_iterations"))
        revalidated_from = str(_dict_value(state.get("last_error")).get("error_code") or "blocked")
        artifact_version = _next_phase4_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
            for key in (
                "design_artifact",
                "requirement_design_trace",
                "class_diagram_graph",
                "sequence_diagram_graph",
            ):
                _required_phase4_artifact(artifact_paths, key, run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            work_packet = refresh_developer_work_packet(
                packet=work_packet,
                target_repo_path=target_info.path,
                version=artifact_version,
                run_id=run_id,
            )
            refreshed_packet_path = write_phase4_developer_work_packet(
                store=self.store,
                run_id=run_id,
                packet=work_packet,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "developer_work_packet": refreshed_packet_path,
            }
            verification_result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
            test_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": previous_iterations,
                "revalidated_from": revalidated_from,
                "developer_work_packet": refreshed_packet_path,
                **verification_result.to_dict(),
            }
            test_report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=test_report,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "test_report": test_report_path,
            }
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi revalidate implementation: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=verification_result,
                report_path=test_report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=previous_iterations,
            ),
        }
        if verification_result.status != "passed":
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=verification_result,
                report_path=test_report_path,
                artifact_version=artifact_version,
                repair_iteration=previous_iterations,
                current_phase="implementation_repair",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self.code_trace_validator.validate(
                target_path=target_info.path,
                work_packet=work_packet,
                approved_context=self._code_trace_approved_context(
                    artifact_paths=latest_artifacts,
                    run_id=run_id,
                ),
            )
            trace_payload = {
                **trace_result.to_dict(),
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": previous_iterations,
                "revalidated_from": revalidated_from,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
                version=artifact_version,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi Validator revalidate implementation: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "requirement_design_code_trace": trace_path,
        }
        quality_reports = {
            **quality_reports,
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if trace_result.ok:
            quality_reports["implementation_repair"] = {
                "needed": False,
                "repair_iterations": previous_iterations,
                "revalidated": True,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_traced",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"Revalidate implementation da pass test va Validator code trace cho run {run_id}: "
                        f"{trace_result.summary} Workflow chua chay QA/Tester Agent, chua commit va chua mo PR."
                    ),
                }
            )
        elif _is_repairable_code_trace_failure(trace_result) and previous_iterations < self.settings.implementation_repair_limit:
            feedback = _implementation_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                trace_result=trace_result,
                trace_path=trace_path,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=artifact_version,
                repair_iteration=previous_iterations,
            )
            feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": feedback_path,
            }
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": feedback_path,
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": previous_iterations,
                "revalidated": True,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_repair_needed",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"Revalidate implementation cho run {run_id} da tao feedback moi v{artifact_version}: "
                        f"{trace_result.summary} Co the chay continue-run de Developer.repair tiep."
                    ),
                }
            )
        else:
            if _is_repairable_code_trace_failure(trace_result):
                error_code = "implementation_repair_limit_exceeded"
                message = (
                    f"Implementation repair limit is still reached after revalidation: "
                    f"iterations={previous_iterations}, limit={self.settings.implementation_repair_limit}. "
                    f"{trace_result.summary}"
                )
                allowed_actions = ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS
            else:
                error_code = _nonrepairable_code_trace_error_code(trace_result)
                message = trace_result.summary
                allowed_actions = _implementation_blocked_actions(error_code)
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": previous_iterations,
                "limit_exceeded": error_code == "implementation_repair_limit_exceeded",
                "revalidated": True,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": allowed_actions,
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Revalidate implementation van chua pass cho run {run_id}: {message} "
                        "Workflow chua duoc phep mo PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_implementation_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy một vòng Developer.repair rồi test và trace lại."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        artifact_version = _next_phase4_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            feedback_path = _required_phase4_artifact(
                artifact_paths,
                "implementation_repair_feedback",
                run_id=run_id,
            )
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            repair_feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_path, run_id=run_id),
                run_id=run_id,
            )
            previous_iterations = _int_value(state.get("implementation_repair_iterations"))
            repair_iteration = previous_iterations + 1
            if repair_iteration > self.settings.implementation_repair_limit:
                raise HarnessError(
                    "implementation_repair_limit_exceeded",
                    (
                        f"Implementation repair limit exceeded for run {run_id}: "
                        f"limit={self.settings.implementation_repair_limit}."
                    ),
                    run_id=run_id,
                )
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            work_packet = refresh_developer_work_packet(
                packet=work_packet,
                target_repo_path=target_info.path,
                version=artifact_version,
                run_id=run_id,
            )
            refreshed_packet_path = write_phase4_developer_work_packet(
                store=self.store,
                run_id=run_id,
                packet=work_packet,
                version=artifact_version,
            )
            latest_artifacts = {
                **artifact_paths,
                "developer_work_packet": refreshed_packet_path,
            }
            if self.settings.developer_execution_mode == "tool_loop":
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_repair",
                        "status": "implementation_executing",
                        "latest_artifacts": latest_artifacts,
                        "pending_checkpoint": None,
                        "allowed_next_actions": ["get_status", "cancel_run"],
                        "developer_execution": {
                            "mode": "implementation_repair",
                            "repair_iteration": repair_iteration,
                            "status": "running",
                        },
                        "last_error": None,
                        "summary_for_user": (
                            f"Developer tool loop đang repair implementation iteration {repair_iteration} "
                            f"cho run {run_id}."
                        ),
                    }
                )
                self.store.write_state(state)

                def record_repair_progress(progress: dict[str, object], manifest_path: str, journal_path: str) -> None:
                    latest_artifacts.update(
                        {
                            "developer_repair_execution_manifest": manifest_path,
                            "developer_repair_execution_journal": journal_path,
                        }
                    )
                    state.update(
                        {
                            "updated_at": utc_now_iso(),
                            "current_phase": "implementation_repair",
                            "status": "implementation_executing",
                            "latest_artifacts": dict(latest_artifacts),
                            "developer_execution": {
                                "mode": "implementation_repair",
                                "repair_iteration": repair_iteration,
                                **progress,
                            },
                            "summary_for_user": (
                                f"Developer repair đã hoàn thành {progress.get('completed_items', 0)}/"
                                f"{progress.get('total_items', 0)} execution task cho run {run_id}."
                            ),
                        }
                    )
                    self.store.write_state(state)

                execution_result = self.developer_executor.execute(
                    run_id=run_id,
                    work_packet=work_packet,
                    target_path=target_info.path,
                    repair_feedback=repair_feedback,
                    repair_iteration=repair_iteration,
                    repair_scope="implementation",
                    on_progress=record_repair_progress,
                )
                latest_artifacts.update(
                    {
                        "developer_repair_execution_manifest": execution_result.manifest_path,
                        "developer_repair_execution_journal": execution_result.journal_path,
                    }
                )
                applied_files = execution_result.files
                developer_summary = execution_result.summary
                developer_raw_response = ""
            else:
                result = self.developer.repair(
                    work_packet=work_packet,
                    repair_feedback=repair_feedback,
                    repair_iteration=repair_iteration,
                )
                latest_artifacts = self._persist_developer_llm_artifacts(
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    action="repair",
                    version=artifact_version,
                    value=result,
                )
                applied_files = _apply_developer_changes(
                    result=result,
                    work_packet=work_packet,
                    target_path=target_info.path,
                    run_id=run_id,
                )
                developer_summary = result.summary
                developer_raw_response = latest_artifacts.get("developer_repair_raw_response")
            change_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "summary": developer_summary,
                "files": applied_files,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                "developer_raw_response": developer_raw_response,
            }
            change_report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=change_report,
                version=artifact_version,
            )
            verification_result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
            test_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                **verification_result.to_dict(),
            }
            test_report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=test_report,
                version=artifact_version,
            )
        except DeveloperExecutionError as exc:
            if exc.manifest_path:
                latest_artifacts["developer_repair_execution_manifest"] = exc.manifest_path
            if exc.journal_path:
                latest_artifacts["developer_repair_execution_journal"] = exc.journal_path
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS if paused else ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "developer_execution": {
                        "mode": "implementation_repair",
                        "repair_iteration": locals().get("repair_iteration", 0),
                        **exc.progress,
                    },
                    "summary_for_user": (
                        f"Developer repair tool loop đã tạm dừng cho run {run_id}: {exc.message}"
                        if paused
                        else f"Developer repair tool loop bị chặn cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            latest_artifacts = self._persist_developer_llm_artifacts(
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                action="repair",
                version=artifact_version,
                value=exc,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS
                        if exc.error_code in REVALIDATABLE_IMPLEMENTATION_ERROR_CODES
                        else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Developer.repair xử lý feedback: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except Exception as exc:  # Last-resort state recovery for executor defects.
            error = HarnessError(
                "developer_execution_internal_error",
                f"Unexpected Developer executor failure ({type(exc).__name__}): {exc}",
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_paused",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
                    "last_error": error.to_response(),
                    "developer_execution": {
                        **_dict_value(state.get("developer_execution")),
                        "mode": "implementation_repair",
                        "repair_iteration": locals().get("repair_iteration", 0),
                        "status": "paused",
                    },
                    "summary_for_user": (
                        f"Developer repair đã tạm dừng an toàn cho run {run_id} vì lỗi executor nội bộ."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "code_change_report": change_report_path,
            "test_report": test_report_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=verification_result,
                report_path=test_report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=repair_iteration,
            ),
        }
        if verification_result.status != "passed":
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=verification_result,
                report_path=test_report_path,
                artifact_version=artifact_version,
                repair_iteration=repair_iteration,
                current_phase="implementation_repair",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self.code_trace_validator.validate(
                target_path=target_info.path,
                work_packet=work_packet,
                approved_context=self._code_trace_approved_context(
                    artifact_paths=latest_artifacts,
                    run_id=run_id,
                ),
            )
            trace_payload = {
                **trace_result.to_dict(),
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": repair_iteration,
                "repaired_from": feedback_path,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
                version=artifact_version,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Validator trace lại sau Developer.repair: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "requirement_design_code_trace": trace_path,
        }
        quality_reports = {
            **quality_reports,
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if trace_result.ok:
            quality_reports["implementation_repair"] = {
                "needed": False,
                "repair_iterations": repair_iteration,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_traced",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": None,
                    "summary_for_user": (
                        f"Developer.repair iteration {repair_iteration} đã pass test và Validator code trace "
                        f"cho run {run_id}: {trace_result.summary} Slice 4.6 chưa chạy QA/Tester Agent, "
                        "chưa commit và chưa mở PR."
                    ),
                }
            )
        elif _is_repairable_code_trace_failure(trace_result) and repair_iteration < self.settings.implementation_repair_limit:
            feedback = _implementation_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                trace_result=trace_result,
                trace_path=trace_path,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=artifact_version,
                repair_iteration=repair_iteration,
            )
            next_feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": next_feedback_path,
            }
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": next_feedback_path,
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": repair_iteration,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_repair_needed",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": None,
                    "summary_for_user": (
                        f"Developer.repair iteration {repair_iteration} đã chạy xong nhưng Validator vẫn còn "
                        f"missing link có thể sửa: {trace_result.summary} Đã ghi feedback v{artifact_version} "
                        "để chạy tiếp một vòng repair khác."
                    ),
                }
            )
        else:
            if _is_repairable_code_trace_failure(trace_result):
                error_code = "implementation_repair_limit_exceeded"
                message = (
                    f"Implementation repair limit reached after {repair_iteration} iteration(s): "
                    f"{trace_result.summary}"
                )
            else:
                error_code = _nonrepairable_code_trace_error_code(trace_result)
                message = trace_result.summary
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": repair_iteration,
                "limit_exceeded": error_code == "implementation_repair_limit_exceeded",
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(error_code),
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Validator code trace vẫn chưa pass sau Developer.repair iteration {repair_iteration}: "
                        f"{message} Workflow chưa được phép mở PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _require_code_review_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        """Chỉ cho phép human approve đúng checkpoint Code Review hiện hành."""

        if str(state.get("status")) != "code_review_ready":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for Code Review approval: {run_id}",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "code_review":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not a Code Review checkpoint: {run_id}",
                run_id=run_id,
            )
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError(
                "checkpoint_not_current",
                f"Checkpoint is not current: {checkpoint_id}",
                run_id=run_id,
            )

    def _require_implementation_plan_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        if str(state.get("status")) != "waiting_implementation_plan_review":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for implementation plan review: {run_id}",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "implementation_plan_review":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not an implementation plan checkpoint: {run_id}",
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
                "approved_implementation_plan_not_found",
                f"Approved Phase 4 input artifact does not exist: {relative_path}",
                run_id=run_id,
            )
        return path

    def _workspace_target_info(
        self,
        *,
        run_id: str,
        work_packet: dict[str, object] | None = None,
        allow_dirty: bool = True,
    ) -> "_TargetRepoInfo":
        """Resolve workspace đã khóa và đối chiếu packet với identity của run."""

        workspace = self.workspace_manager.resolve(run_id)
        if work_packet is not None:
            _require_work_packet_workspace(work_packet=work_packet, workspace=workspace, run_id=run_id)
        target_path = Path(workspace.worktree_path)
        self.repo_context_provider.require_index(target_path)
        if not allow_dirty:
            completed = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(target_path),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            dirty = _dirty_status_without_codegraph(completed.stdout)
            if dirty:
                raise HarnessError(
                    "workspace_dirty",
                    "Run workspace has uncommitted changes before the next write action.",
                    run_id=run_id,
                )
        return _TargetRepoInfo(
            path=target_path,
            remote=workspace.remote,
            current_branch=workspace.branch,
            source_files=_target_source_files(target_path),
        )

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        allowed_next_actions = list(state.get("allowed_next_actions", []))
        if self._legacy_developer_retry_allowed(state) and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        if self._legacy_verification_repair_allowed(state) and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        if repair_scope_mismatch_retry_allowed(state) and "continue_run" not in allowed_next_actions:
            allowed_next_actions.insert(0, "continue_run")
        if (
            str(state.get("status") or "") == "implementation_repair_needed"
            and "revalidate_implementation" not in allowed_next_actions
        ):
            allowed_next_actions.insert(1 if allowed_next_actions else 0, "revalidate_implementation")
        return {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": state.get("latest_artifacts", {}),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": allowed_next_actions,
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "implementation_repair_iterations": state.get("implementation_repair_iterations", 0),
            "qa_repair_iterations": state.get("qa_repair_iterations", 0),
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
            "requirement_contract_version": state.get("requirement_contract_version", "v2"),
            "developer_execution": state.get("developer_execution", {}),
        }

    def _legacy_developer_retry_allowed(self, state: dict[str, object]) -> bool:
        """Expose one safe migration path for pre-tool-loop Developer truncation failures."""

        if self.settings.developer_execution_mode != "tool_loop" or str(state.get("status") or "") != "blocked":
            return False
        error_code = str(_dict_value(state.get("last_error")).get("error_code") or "")
        artifacts = _string_dict(state.get("latest_artifacts"))
        return (
            error_code == "developer_act_failed"
            and bool(artifacts.get("developer_work_packet"))
            and not bool(artifacts.get("code_change_report"))
        )

    @staticmethod
    def _legacy_verification_repair_allowed(state: dict[str, object]) -> bool:
        """Expose continue-run for test failures created before verification repair existed."""

        if str(state.get("status") or "") != "blocked":
            return False
        error_code = str(_dict_value(state.get("last_error")).get("error_code") or "")
        artifacts = _string_dict(state.get("latest_artifacts"))
        quality_reports = _dict_value(state.get("quality_reports"))
        return (
            error_code == "test_failed"
            and bool(artifacts.get("developer_work_packet"))
            and bool(artifacts.get("code_change_report"))
            and bool(artifacts.get("test_report"))
            and not bool(artifacts.get("implementation_repair_feedback"))
            and not bool(quality_reports.get("verification"))
        )


class _TargetRepoInfo:
    def __init__(self, *, path: Path, remote: str, current_branch: str, source_files: list[str]):
        self.path = path
        self.remote = remote
        self.current_branch = current_branch
        self.source_files = source_files


def _dirty_status_without_codegraph(status: str) -> str:
    lines: list[str] = []
    for line in status.splitlines():
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path == ".codegraph" or path.startswith(".codegraph/") or path.startswith(".codegraph\\"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _require_work_packet_workspace(
    *,
    work_packet: dict[str, object],
    workspace: RunWorkspaceRecord,
    run_id: str,
) -> None:
    """Fail closed nếu packet cũ/tampered không còn trỏ đúng workspace của run."""

    packet_workspace = _dict_value(work_packet.get("run_workspace"))
    repository = _dict_value(work_packet.get("target_repository"))
    expected = {
        "workspace_mode": workspace.workspace_mode,
        "branch": workspace.branch,
        "base_sha": workspace.base_sha,
        "remote": workspace.remote,
    }
    actual = {
        "workspace_mode": str(packet_workspace.get("workspace_mode") or ""),
        "branch": str(packet_workspace.get("branch") or repository.get("planned_branch") or ""),
        "base_sha": str(packet_workspace.get("base_sha") or repository.get("base_sha") or ""),
        "remote": str(packet_workspace.get("remote") or repository.get("remote") or ""),
    }
    if actual != expected:
        raise HarnessError(
            "run_workspace_mismatch",
            "DeveloperWorkPacket does not match the owning run workspace.",
            run_id=run_id,
        )
    local_path = Path(str(repository.get("local_path") or "")).resolve()
    if local_path != Path(workspace.worktree_path).resolve():
        raise HarnessError(
            "run_workspace_mismatch",
            "DeveloperWorkPacket local_path does not match the owning worktree.",
            run_id=run_id,
        )


def _positive_int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _qa_review_context(
    *,
    store: RuntimeStore,
    state: dict[str, object],
    artifact_paths: dict[str, str],
    run_id: str,
    target_repo: str,
    branch: str,
    work_packet: dict[str, object],
) -> dict[str, object]:
    """Đóng gói context QA ngắn, có metadata truncation để tránh prompt quá dài."""

    requirement_path = _required_phase4_artifact(artifact_paths, "requirement_packet", run_id=run_id)
    verification_path = _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
    trace_path = _required_phase4_artifact(artifact_paths, "requirement_design_code_trace", run_id=run_id)
    design_path = _required_phase4_artifact(artifact_paths, "design_artifact", run_id=run_id)
    implementation_plan_path = _required_phase4_artifact(artifact_paths, "implementation_plan", run_id=run_id)
    requirement = _bounded_artifact_section(
        store=store,
        relative_path=requirement_path,
        label="Accepted requirement/use-case packet",
        limit=MAX_QA_REQUIREMENT_CHARS,
    )
    verification = _bounded_artifact_section(
        store=store,
        relative_path=verification_path,
        label="Verification Runner report",
        limit=MAX_QA_VERIFICATION_CHARS,
    )
    trace_report = _read_json_artifact(store.repo_root / trace_path, run_id=run_id)
    code_under_test = _qa_code_under_test(
        target_repo=Path(target_repo),
        work_packet=work_packet,
        run_id=run_id,
    )
    traceability = _qa_traceability_summary(trace_report=trace_report, trace_path=trace_path)
    requirement_contract = _dict_value(work_packet.get("requirement_contract"))
    v2 = requirement_contract.get("version") == "v2"
    canonical_requirements = _dict_items(work_packet.get("canonical_requirements")) if v2 else []
    accepted_requirement: dict[str, object] = requirement
    if v2:
        accepted_requirement = {
            "label": "Canonical Requirement V2 stable-ID set",
            "requirement_contract": requirement_contract,
            "requirements": canonical_requirements,
            "requirement_coverage": _dict_items(work_packet.get("requirement_coverage")),
            "requirement_packet_reference": requirement,
        }
    chars_sent = (
        int(requirement.get("chars_sent") or 0)
        + int(verification.get("chars_sent") or 0)
        + int(code_under_test.get("chars_sent") or 0)
    )
    chars_total = (
        int(requirement.get("chars_total") or 0)
        + int(verification.get("chars_total") or 0)
        + int(code_under_test.get("chars_total") or 0)
    )
    return {
        "task": "qa_acceptance_review_v1",
        "packet_mode": "trace_dev_style_compact_qa_packet_v2" if v2 else "trace_dev_style_compact_qa_packet_v1",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "target_repo": target_repo,
        "branch": branch,
        "work_packet_summary": {
            "implementation_steps": work_packet.get("implementation_steps"),
            "proposed_files": work_packet.get("proposed_files"),
            "test_intents": work_packet.get("test_intents"),
        },
        "accepted_requirement": accepted_requirement,
        "requirement_contract": requirement_contract,
        "canonical_requirements": canonical_requirements,
        "requirement_coverage": _dict_items(work_packet.get("requirement_coverage")) if v2 else [],
        "code_under_test": code_under_test,
        "verification": verification,
        "traceability": traceability,
        "artifact_references": {
            "design_artifact": design_path,
            "implementation_plan": implementation_plan_path,
            "requirement_design_code_trace": trace_path,
        },
        "context_budget": {
            "packet_mode": "trace_dev_style_compact_qa_packet_v2" if v2 else "trace_dev_style_compact_qa_packet_v1",
            "section_count": 3,
            "code_file_count": len(_dict_items(code_under_test.get("files"))),
            "chars_sent": chars_sent,
            "chars_total": chars_total,
            "any_truncated": (
                bool(requirement.get("content_truncated"))
                or bool(verification.get("content_truncated"))
                or bool(code_under_test.get("any_truncated"))
            ),
            "requirement_limit_chars": MAX_QA_REQUIREMENT_CHARS,
            "verification_limit_chars": MAX_QA_VERIFICATION_CHARS,
            "code_file_limit_chars": MAX_QA_CODE_FILE_CHARS,
            "code_total_limit_chars": MAX_QA_CODE_TOTAL_CHARS,
            "omitted_full_artifacts": ["design_artifact", "implementation_plan", "requirement_design_code_trace"],
        },
        "guardrails": [
            "QA/Tester generates acceptance/system tests from current code and test evidence.",
            "Full design, implementation plan, and trace report are references only; use summaries unless a future slice fetches them.",
            "QA/Tester must not request requirement/design edits in this slice.",
            "QA-generated test fail/skipped blocks the run; Developer QA repair loop is a later slice.",
        ],
    }


def _bounded_artifact_section(
    *,
    store: RuntimeStore,
    relative_path: str,
    label: str,
    limit: int,
) -> dict[str, object]:
    path = (store.repo_root / relative_path).resolve()
    if not path.exists():
        raise HarnessError(
            "approved_implementation_plan_not_found",
            f"QA input artifact does not exist: {relative_path}",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    excerpt = text[:limit]
    return {
        "label": label,
        "path": relative_path,
        "content": excerpt,
        "content_truncated": len(excerpt) < len(text),
        "chars_total": len(text),
        "chars_sent": len(excerpt),
    }


def _qa_code_under_test(
    *,
    target_repo: Path,
    work_packet: dict[str, object],
    run_id: str,
) -> dict[str, object]:
    """Lay source/test code hien tai tu target repo, khong dung snapshot cu trong packet."""

    files: list[dict[str, object]] = []
    chars_sent = 0
    chars_total = 0
    for rel_path in _qa_code_paths(work_packet=work_packet, run_id=run_id):
        target_file = (target_repo / rel_path).resolve()
        try:
            target_file.relative_to(target_repo.resolve())
        except ValueError as exc:
            raise HarnessError(
                "developer_act_unsafe_path",
                f"QA code path escapes target repo: {rel_path}",
                run_id=run_id,
            ) from exc
        if not target_file.exists():
            files.append(
                {
                    "path": rel_path,
                    "role": _qa_code_role(rel_path),
                    "exists": False,
                    "content": "",
                    "content_truncated": False,
                    "chars_total": 0,
                    "chars_sent": 0,
                }
            )
            continue
        text = target_file.read_text(encoding="utf-8", errors="replace")
        chars_total += len(text)
        remaining_total = max(MAX_QA_CODE_TOTAL_CHARS - chars_sent, 0)
        excerpt_limit = min(MAX_QA_CODE_FILE_CHARS, remaining_total)
        excerpt = text[:excerpt_limit]
        chars_sent += len(excerpt)
        files.append(
            {
                "path": rel_path,
                "role": _qa_code_role(rel_path),
                "exists": True,
                "content": excerpt,
                "content_truncated": len(excerpt) < len(text),
                "chars_total": len(text),
                "chars_sent": len(excerpt),
            }
        )
    return {
        "source": "target_repo_allowed_paths",
        "files": files,
        "file_count": len(files),
        "chars_sent": chars_sent,
        "chars_total": chars_total,
        "any_truncated": any(bool(item.get("content_truncated")) for item in files),
    }


def _qa_code_paths(*, work_packet: dict[str, object], run_id: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for rel_path in _allowed_paths(work_packet):
        if rel_path not in seen:
            paths.append(rel_path)
            seen.add(rel_path)
    if paths:
        return paths
    for item in _dict_items(work_packet.get("proposed_files")) + _dict_items(work_packet.get("current_files")):
        rel_path = _normalize_code_path(str(item.get("path") or ""), run_id=run_id)
        if rel_path not in seen:
            paths.append(rel_path)
            seen.add(rel_path)
    return paths


def _qa_code_role(rel_path: str) -> str:
    return "test" if rel_path.startswith("src/test/java/") else "production"


def _qa_traceability_summary(*, trace_report: dict[str, object], trace_path: str) -> dict[str, object]:
    edges = _dict_items(trace_report.get("graph_edges"))
    missing_links = _dict_items(trace_report.get("missing_links"))
    risks = _string_items(trace_report.get("risks"))
    llm_review = _dict_value(trace_report.get("llm_review"))
    review_context = _dict_value(llm_review.get("review_context"))
    return {
        "path": trace_path,
        "ok": bool(trace_report.get("ok")),
        "confidence": trace_report.get("confidence"),
        "summary": trace_report.get("summary"),
        "graph_edge_count": len(edges),
        "sample_graph_edges": edges[:MAX_QA_TRACE_SAMPLE_EDGES],
        "missing_link_count": len(missing_links),
        "sample_missing_links": missing_links[:MAX_QA_TRACE_SAMPLE_EDGES],
        "risks": risks[:MAX_QA_TRACE_SAMPLE_EDGES],
        "review_context": {
            "code_snippet_count": review_context.get("code_snippet_count"),
            "code_snippets_total_chars": review_context.get("code_snippets_total_chars"),
            "code_snippets_truncated": review_context.get("code_snippets_truncated"),
        },
    }


def _apply_qa_generated_tests(
    *,
    generated: QaGeneratedTestSuite,
    target_path: Path,
    run_id: str,
) -> list[dict[str, object]]:
    """Apply only QA-generated test files under src/test/java/."""

    applied: list[dict[str, object]] = []
    target_root = target_path.resolve()
    for item in generated.test_files:
        rel_path = _normalize_qa_test_path(str(item.get("path") or ""), run_id=run_id)
        target_file = (target_root / rel_path).resolve()
        try:
            target_file.relative_to(target_root)
        except ValueError as exc:
            raise HarnessError(
                "qa_test_unsafe_path",
                f"QA-generated test path escapes target repo: {rel_path}",
                run_id=run_id,
            ) from exc
        content = str(item.get("content") or "").strip()
        if not content:
            raise HarnessError(
                "qa_test_empty_content",
                f"QA-generated test file has empty content: {rel_path}",
                run_id=run_id,
            )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content.rstrip() + "\n", encoding="utf-8")
        applied.append(
            {
                "path": rel_path,
                "operation": "create_or_update",
                "scenarios": _string_items(item.get("scenarios")),
            }
        )
    if not applied:
        raise HarnessError("qa_test_files_missing", "QA did not generate any test file.", run_id=run_id)
    return applied


def _normalize_qa_test_path(raw_path: str, *, run_id: str) -> str:
    rel_path = _normalize_code_path(raw_path, run_id=run_id)
    if not rel_path.startswith("src/test/java/") or not rel_path.endswith(".java"):
        raise HarnessError(
            "qa_test_out_of_scope",
            f"QA-generated tests can only write Java tests under src/test/java/: {raw_path}",
            run_id=run_id,
        )
    return rel_path


def _qa_result_from_generated_execution(
    *,
    generated: QaGeneratedTestSuite,
    execution: object,
    execution_path: str,
    generated_path: str,
    applied_tests: list[dict[str, object]],
) -> QaTestResult:
    status = _qa_status_from_verification(str(getattr(execution, "status", "")))
    reason = str(getattr(execution, "reason", "") or "")
    scenarios: list[dict[str, object]] = []
    for item in generated.scenarios:
        scenarios.append(
            {
                "name": str(item.get("name") or ""),
                "result": status,
                "evidence": reason or str(item.get("expected_result") or ""),
                "risk": str(item.get("risk") or ""),
                "requirement_ids": _string_items(item.get("requirement_ids")),
            }
        )
    if not scenarios:
        scenarios = [
            {
                "name": "QA-generated acceptance tests",
                "result": status,
                "evidence": reason,
                "risk": "",
                "requirement_ids": [],
            }
        ]
    if status == "pass":
        summary = f"QA-generated acceptance tests passed: {reason}"
    elif status == "skipped":
        summary = f"QA-generated acceptance tests were skipped: {reason}"
    else:
        summary = f"QA-generated acceptance tests failed: {reason}"
    evidence = [
        f"Generated tests artifact: {generated_path}",
        f"QA execution artifact: {execution_path}",
        f"Applied QA test files: {len(applied_tests)}",
        f"Verification Runner status: {getattr(execution, 'status', '')}",
    ]
    risks = list(generated.risks)
    if status != "pass":
        risks.append("QA-generated acceptance test execution did not pass.")
    review_context = {
        **(generated.generation_context or {}),
        "generated_test_file_count": len(generated.test_files),
        "applied_test_file_count": len(applied_tests),
        "execution_status": getattr(execution, "status", ""),
    }
    return QaTestResult(
        status=status,
        summary=summary,
        scenarios=scenarios,
        evidence=evidence,
        risks=risks,
        raw_response=generated.raw_response,
        review_context=review_context,
        requirement_contract=generated.requirement_contract,
    )


def _qa_status_from_verification(status: str) -> str:
    if status == "passed":
        return "pass"
    if status == "skipped":
        return "skipped"
    return "fail"

def _qa_repair_feedback(
    *,
    run_id: str,
    state: dict[str, object],
    work_packet: dict[str, object],
    qa_result: QaTestResult,
    qa_run: dict[str, object],
    target_repo: str,
    branch: str,
    feedback_version: int,
    repair_iteration: int,
) -> dict[str, object]:
    """Build machine-readable QA feedback for Developer.repair."""

    output_contract = _dict_value(work_packet.get("output_contract"))
    failing_scenarios = [
        item for item in qa_result.scenarios if str(item.get("result") or "").strip().lower() != "pass"
    ]
    if not failing_scenarios:
        failing_scenarios = list(qa_result.scenarios)
    return {
        "version": f"qa_repair_feedback_v{feedback_version}",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "status": "qa_repair_needed",
        "repair_iteration": repair_iteration,
        "repair_scope": "code_or_test",
        "severity": "repair_required",
        "summary": qa_result.summary,
        "required_action": (
            "Developer must repair code/test behavior within allowed_paths so the QA-generated "
            "acceptance tests pass. Requirement, approved design, implementation plan, branch, "
            "and tool-sync records must not be edited in this slice."
        ),
        "target_repo": target_repo,
        "branch": branch,
        "qa_artifacts": {
            "qa_generated_tests": qa_run.get("generated_path"),
            "qa_test_execution": qa_run.get("execution_path"),
            "qa_test_report": qa_run.get("report_path"),
        },
        "failing_scenarios": failing_scenarios,
        "evidence": qa_result.evidence,
        "risks": qa_result.risks,
        "applied_tests": qa_run.get("applied_tests", []),
        "developer_constraints": {
            "allowed_paths": output_contract.get("allowed_paths", []),
            "changed_files_only": output_contract.get("changed_files_only"),
            "format": output_contract.get("format"),
        },
    }


def _code_review_publication_paths(
    *,
    work_packet: dict[str, object],
    qa_generated: dict[str, object],
    run_id: str,
) -> list[str]:
    """Lập manifest file từ evidence của Developer, QA và Verification Runner."""

    paths = {_normalize_publication_path(path, run_id=run_id) for path in _allowed_paths(work_packet)}
    for test_file in _dict_items(qa_generated.get("test_files")):
        paths.add(_normalize_publication_path(str(test_file.get("path") or ""), run_id=run_id))
    for value in _nested_strings(work_packet):
        for match in re.finditer(r"(?<![A-Za-z0-9_.-])(scripts/[A-Za-z0-9_./-]+\.(?:ps1|sh|bat|cmd))", value):
            paths.add(_normalize_publication_path(match.group(1), run_id=run_id))
    clean = sorted(path for path in paths if path)
    if not clean:
        raise HarnessError(
            "github_publish_manifest_empty",
            "No Developer, QA, or verification paths were found for GitHub publication.",
            run_id=run_id,
        )
    return clean


def _code_review_pr_body(
    *,
    state: dict[str, object],
    run_id: str,
    issue_key: str,
    artifact_paths: dict[str, str],
    qa_generated: dict[str, object],
) -> str:
    """Tạo PR body ngắn, trace được về tool và quality gates đã pass."""

    quality = _dict_value(state.get("quality_reports"))
    trace = _dict_value(quality.get("implementation_traceability"))
    qa = _dict_value(quality.get("qa_acceptance"))
    jira_url = _sync_target_url(state, "jira")
    confluence_url = _sync_target_url(state, "confluence")
    scenarios = _dict_items(qa_generated.get("scenarios"))
    risks = _string_items(qa_generated.get("risks"))
    trace_stats = _dict_value(trace.get("stats"))
    contract_version = str(trace.get("requirement_contract_version") or "v2")
    requirement_revision = trace.get("requirement_revision", 0)
    covered_ids = _string_items(qa.get("requirement_ids"))
    lines = [
        "## SDLC Harness Run",
        "",
        f"- Run: `{run_id}`",
        f"- Jira issue: [{issue_key}]({jira_url})" if jira_url else f"- Jira issue: `{issue_key}`",
        f"- Confluence checkpoint: [Implementation plan]({confluence_url})"
        if confluence_url
        else "- Confluence checkpoint: unavailable",
        "",
        "## Implementation Evidence",
        "",
        f"- Code change report: `{artifact_paths.get('code_change_report', '')}`",
        f"- Verification report: `{artifact_paths.get('test_report', '')}`",
        f"- Requirement/design/code trace: `{artifact_paths.get('requirement_design_code_trace', '')}`",
        f"- QA execution: `{artifact_paths.get('qa_test_execution', '')}`",
        f"- QA report: `{artifact_paths.get('qa_test_report', '')}`",
        "",
        "## Quality Gates",
        "",
        f"- Requirement contract: `{contract_version}` revision `{requirement_revision}`",
        (
            "- Canonical requirement coverage: "
            f"{trace_stats.get('requirements_covered', 0)}/{trace_stats.get('requirement_count', 0)}"
        ),
        "- Compile/unit verification: passed",
        (
            "- Validator traceability: passed "
            f"({trace.get('stats', {}).get('mapping_count', 0) if isinstance(trace.get('stats'), dict) else 0} mappings, "
            f"confidence={trace.get('llm_confidence', '')})"
        ),
        f"- QA acceptance/system tests: passed ({qa.get('scenario_count', len(scenarios))} scenarios)",
        "",
        "## QA Scenarios",
        "",
    ]
    if scenarios:
        lines.extend(
            (
                f"- {str(item.get('name') or '').strip()} "
                f"[{', '.join(_string_items(item.get('requirement_ids')))}]"
            ).rstrip()
            for item in scenarios[:12]
        )
    else:
        lines.append("- See QA report artifact.")
    if covered_ids:
        lines.extend(["", "## Stable Requirement IDs", "", *[f"- `{item}`" for item in covered_ids]])
    lines.extend(["", "## Known Risks", ""])
    if risks:
        lines.extend(f"- {risk}" for risk in risks[:10])
    else:
        lines.append("- No additional QA risk was recorded.")
    lines.extend(["", "This Draft PR was published by SDLC_Harness after QA passed."])
    return "\n".join(lines)


def _sync_target_url(state: dict[str, object], tool: str) -> str:
    last_sync = _dict_value(state.get("last_sync"))
    for target in _dict_items(last_sync.get("targets")):
        if str(target.get("tool") or "") == tool and str(target.get("status") or "") == "synced":
            return str(target.get("url") or "").strip()
    return ""


def _nested_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_nested_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_nested_strings(item))
        return result
    return []


def _normalize_publication_path(raw_path: str, *, run_id: str) -> str:
    path = raw_path.strip().replace("\\", "/").lstrip("/")
    if (
        not path
        or path == ".."
        or path.startswith("../")
        or "/../" in path
        or path.startswith(".git/")
        or re.match(r"^[A-Za-z]:", path)
    ):
        raise HarnessError(
            "github_publish_unsafe_path",
            f"Unsafe publication path: {raw_path}",
            run_id=run_id,
        )
    return path

def _required_phase4_artifact(artifact_paths: dict[str, str], key: str, *, run_id: str) -> str:
    value = artifact_paths.get(key, "")
    if not value:
        raise HarnessError(
            "approved_implementation_plan_not_found",
            f"Run does not have an approved Phase 4 input artifact `{key}`: {run_id}",
            run_id=run_id,
        )
    return value


def _apply_developer_changes(
    *,
    result: DeveloperActResult,
    work_packet: dict[str, object],
    target_path: Path,
    run_id: str,
) -> list[dict[str, object]]:
    allowed_paths = set(_allowed_paths(work_packet))
    if not allowed_paths:
        raise HarnessError("developer_act_allowed_paths_missing", "DeveloperWorkPacket has no allowed paths.")
    applied: list[dict[str, object]] = []
    requirement_contract = _dict_value(work_packet.get("requirement_contract"))
    v2 = requirement_contract.get("version") == "v2"
    assigned_by_file = _requirement_ids_by_file(work_packet) if v2 else {}
    canonical_ids = {
        str(item.get("id") or "").strip()
        for item in _dict_items(work_packet.get("canonical_requirements"))
        if str(item.get("id") or "").strip()
    }
    if v2:
        for item in result.files:
            rel_path = _normalize_code_path(str(item.get("path") or ""), run_id=run_id)
            covered_requirement_ids = _string_items(item.get("covered_requirement_ids"))
            invalid = set(covered_requirement_ids) - assigned_by_file.get(rel_path, set())
            invented = set(covered_requirement_ids) - canonical_ids
            if invalid or invented:
                raise HarnessError(
                    "developer_act_requirement_coverage_invalid",
                    (
                        f"Developer.act returned stable requirement IDs outside the approved coverage for {rel_path}: "
                        f"{sorted(invalid | invented)}"
                    ),
                    run_id=run_id,
                )
    for item in result.files:
        rel_path = _normalize_code_path(str(item.get("path") or ""), run_id=run_id)
        if rel_path not in allowed_paths:
            raise HarnessError(
                "developer_act_out_of_scope",
                f"Developer.act tried to modify a file outside allowed_paths: {rel_path}",
                run_id=run_id,
            )
        covered_requirement_ids = _string_items(item.get("covered_requirement_ids"))
        target_file = (target_path / rel_path).resolve()
        try:
            target_file.relative_to(target_path.resolve())
        except ValueError as exc:
            raise HarnessError(
                "developer_act_unsafe_path",
                f"Developer.act path escapes target repo: {rel_path}",
                run_id=run_id,
            ) from exc
        content = str(item.get("content") or "")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content.rstrip() + "\n", encoding="utf-8")
        applied.append(
            {
                "path": rel_path,
                "operation": str(item.get("operation") or ""),
                "reason": str(item.get("reason") or ""),
                "covered_design_elements": _string_items(item.get("covered_design_elements")),
                "covered_requirement_ids": covered_requirement_ids,
            }
        )
    if not applied:
        raise HarnessError("developer_act_failed", "Developer.act did not apply any file.")
    return applied


def _requirement_ids_by_file(work_packet: dict[str, object]) -> dict[str, set[str]]:
    grouped = _dict_value(work_packet.get("requirement_coverage_by_file"))
    result: dict[str, set[str]] = {}
    for raw_path, raw_items in grouped.items():
        path = _normalize_code_path(str(raw_path), run_id="")
        result[path] = {
            str(item.get("requirement_id") or "").strip()
            for item in _dict_items(raw_items)
            if str(item.get("requirement_id") or "").strip()
        }
    return result


def _require_exact_v2_coverage(
    *,
    canonical_requirements: list[dict[str, object]],
    coverage: list[dict[str, object]],
    trace: dict[str, object],
    run_id: str,
) -> None:
    expected = {
        str(item.get("id") or "").strip(): str(item.get("content") or "").strip()
        for item in canonical_requirements
        if str(item.get("id") or "").strip()
    }
    actual_ids = [str(item.get("requirement_id") or "").strip() for item in coverage]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise HarnessError(
            "requirement_v2_phase4_input_invalid",
            "Implementation plan must cover each canonical stable requirement ID exactly once.",
            run_id=run_id,
        )
    allowed_targets: dict[str, set[str]] = {key: set() for key in expected}
    for trace_key in ("class_trace", "sequence_trace"):
        trace_ids = [
            str(item.get("requirement") or "").strip()
            for item in _dict_items(trace.get(trace_key))
        ]
        if len(trace_ids) != len(set(trace_ids)) or set(trace_ids) != set(expected):
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"{trace_key} must contain each canonical stable requirement ID exactly once.",
                run_id=run_id,
            )
        for item in _dict_items(trace.get(trace_key)):
            requirement_id = str(item.get("requirement") or "").strip()
            for link in _dict_items(item.get("links")):
                target = str(link.get("target") or "").strip()
                if target:
                    allowed_targets[requirement_id].add(target)
    for item in coverage:
        requirement_id = str(item.get("requirement_id") or "").strip()
        if str(item.get("content") or "").strip() != expected[requirement_id]:
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"Requirement coverage content does not match canonical ID {requirement_id}.",
                run_id=run_id,
            )
        if set(_string_items(item.get("design_targets"))) != allowed_targets[requirement_id]:
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"Requirement coverage design targets do not match approved trace for {requirement_id}.",
                run_id=run_id,
            )
        if not _string_items(item.get("design_element_ids")):
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"Requirement {requirement_id} has no approved design element.",
                run_id=run_id,
            )
        if not _string_items(item.get("planned_code_targets")):
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"Requirement {requirement_id} has no planned code target.",
                run_id=run_id,
            )
        if not _string_items(item.get("test_intents")):
            raise HarnessError(
                "requirement_v2_phase4_input_invalid",
                f"Requirement {requirement_id} has no test intent.",
                run_id=run_id,
            )


def _allowed_paths(work_packet: dict[str, object]) -> list[str]:
    output_contract = _dict_value(work_packet.get("output_contract"))
    values = output_contract.get("allowed_paths")
    if not isinstance(values, list):
        return []
    return [_normalize_code_path(str(value), run_id="") for value in values if str(value).strip()]


def _normalize_code_path(raw_path: str, *, run_id: str) -> str:
    path = raw_path.strip().replace("\\", "/").lstrip("/")
    if (
        not path
        or path.startswith("../")
        or "/../" in path
        or path == ".."
        or not (path.startswith("src/main/java/") or path.startswith("src/test/java/"))
    ):
        raise HarnessError(
            "developer_act_unsafe_path",
            f"Unsafe or unsupported Developer.act path: {raw_path}",
            run_id=run_id or None,
        )
    return path


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _next_phase4_artifact_version(store: RuntimeStore, run_id: str) -> int:
    """Chon version Phase 4 ke tiep ma khong ghi de artifact cu."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    prefixes = (
        "developer_work_packet_",
        "code_change_report_",
        "test_report_",
        "requirement_design_code_trace_",
        "implementation_repair_feedback_",
        "developer_repair_raw_response_",
    )
    max_version = 0
    if not artifact_dir.exists():
        return 1
    for path in artifact_dir.iterdir():
        name = path.name
        for prefix in prefixes:
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix) :]
            if not tail.startswith("v"):
                continue
            digits = []
            for char in tail[1:]:
                if not char.isdigit():
                    break
                digits.append(char)
            if digits:
                max_version = max(max_version, int("".join(digits)))
    return max_version + 1


def _verification_attempt_count(store: RuntimeStore, run_id: str) -> int:
    """Count persisted official Verification Runner reports, independent of artifact gaps."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    if not artifact_dir.exists():
        return 0
    return sum(
        1
        for path in artifact_dir.iterdir()
        if path.is_file() and re.fullmatch(r"test_report_v\d+\.md", path.name)
    )


def _next_qa_artifact_version(store: RuntimeStore, run_id: str) -> int:
    """Chọn version QA report riêng để lần đầu luôn là v1."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    prefixes = (
        "qa_generated_tests_",
        "qa_test_execution_",
        "qa_test_report_",
        "qa_tester_raw_response_",
        "qa_repair_feedback_",
    )
    max_version = 0
    if not artifact_dir.exists():
        return 1
    for path in artifact_dir.iterdir():
        name = path.name
        for prefix in prefixes:
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix) :]
            if not tail.startswith("v"):
                continue
            digits = []
            for char in tail[1:]:
                if not char.isdigit():
                    break
                digits.append(char)
            if digits:
                max_version = max(max_version, int("".join(digits)))
    return max_version + 1


def _is_repairable_code_trace_failure(trace_result: object) -> bool:
    """Chỉ chuyển sang repair-needed khi thiếu link nằm ở code/test, không phải input graph."""

    missing_links = getattr(trace_result, "missing_links", [])
    if not isinstance(missing_links, list) or not missing_links:
        return False
    repairable_types = {"missing_source_file", "missing_test_file", "llm_semantic_gap"}
    for item in missing_links:
        if not isinstance(item, dict):
            return False
        link_type = str(item.get("type") or "").strip()
        owner = str(item.get("owner") or "developer").strip().lower()
        if owner != "developer":
            return False
        if link_type not in repairable_types:
            return False
    return True


def _nonrepairable_code_trace_error_code(trace_result: object) -> str:
    missing_links = getattr(trace_result, "missing_links", [])
    if not isinstance(missing_links, list):
        return "implementation_traceability_failed"
    for item in missing_links:
        if not isinstance(item, dict):
            continue
        link_type = str(item.get("type") or "").strip()
        owner = str(item.get("owner") or "").strip().lower()
        if link_type == "open_requirement_question" or owner == "human":
            return "requirement_decision_needed"
        if link_type in {"trace_mapping_issue", "evidence_incomplete"}:
            return "validator_evidence_or_mapping_issue"
    return "implementation_traceability_failed"


def _implementation_blocked_actions(error_code: str) -> list[str]:
    if error_code in REVALIDATABLE_IMPLEMENTATION_ERROR_CODES:
        return list(ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS)
    return ["get_status", "cancel_run"]


def _is_repairable_verification_failure(result: VerificationResult) -> bool:
    """Only route concrete code/test diagnostics into the Developer loop."""

    return (
        result.status == "failed"
        and not result.timed_out
        and bool(result.stdout.strip() or result.stderr.strip())
    )


def _verification_quality_report(
    *,
    result: VerificationResult,
    report_path: str,
    attempt: int,
    repair_iteration: int,
) -> dict[str, object]:
    return {
        "ok": result.status == "passed",
        "status": result.status,
        "reason": result.reason,
        "command": result.command,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "attempt": attempt,
        "repair_iteration": repair_iteration,
        "repairable": _is_repairable_verification_failure(result),
        "report": report_path,
    }


def _verification_repair_feedback(
    *,
    run_id: str,
    state: dict[str, object],
    work_packet: dict[str, object],
    result: VerificationResult,
    report_path: str,
    target_repo: str,
    branch: str,
    feedback_version: int,
    repair_iteration: int,
) -> dict[str, object]:
    """Package bounded Verification Runner diagnostics for Developer.repair."""

    output_contract = _dict_value(work_packet.get("output_contract"))
    return {
        "version": f"implementation_repair_feedback_v{feedback_version}",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "status": "implementation_repair_needed",
        "repair_iteration": repair_iteration,
        "repair_scope": "code_or_test",
        "feedback_source": "verification",
        "severity": "repair_required",
        "summary": result.reason,
        "required_action": (
            "Developer must use the code currently in the worktree and repair the reported compile/unit-test "
            "failure within allowed_paths. Make the smallest coherent cross-file fix needed for the reported "
            "Verification command to pass; the harness will rerun Verification afterward. Preserve existing test "
            "assertions unless approved requirements or design prove that the test is incorrect. Do not edit the "
            "requirement or approved design and do not work on unrelated design-completeness findings in this pass."
        ),
        "target_repo": target_repo,
        "branch": branch,
        "verification_artifact": report_path,
        "verification": {
            "status": result.status,
            "command": result.command,
            "exit_code": result.exit_code,
            "reason": result.reason,
            "timed_out": result.timed_out,
            "stdout": _bounded_diagnostic(result.stdout),
            "stderr": _bounded_diagnostic(result.stderr),
        },
        "missing_links": [],
        "developer_constraints": {
            "allowed_paths": output_contract.get("allowed_paths", []),
            "changed_files_only": output_contract.get("changed_files_only"),
            "format": output_contract.get("format"),
        },
    }


def _bounded_diagnostic(value: str, *, limit: int = MAX_VERIFICATION_DIAGNOSTIC_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    edge = max(1, limit // 2)
    return text[:edge].rstrip() + "\n... diagnostic truncated ...\n" + text[-edge:].lstrip()


def _verification_result_from_markdown(markdown: str, *, cwd: str) -> VerificationResult:
    """Recover the structured fields needed to migrate a legacy failed test report."""

    status = _markdown_backtick_value(markdown, "Status") or "failed"
    command = _markdown_backtick_value(markdown, "Command")
    exit_code_text = _markdown_backtick_value(markdown, "Exit code")
    try:
        exit_code = int(exit_code_text) if exit_code_text else None
    except ValueError:
        exit_code = None
    duration_text = _markdown_backtick_value(markdown, "Duration").split(" ", 1)[0]
    try:
        duration_seconds = float(duration_text) if duration_text else 0.0
    except ValueError:
        duration_seconds = 0.0
    reason_match = re.search(r"(?m)^- Reason:\s*(.*)$", markdown)
    reason = reason_match.group(1).strip() if reason_match else "Legacy Verification Runner failure."
    return VerificationResult(
        status=status,
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        stdout=_markdown_code_section(markdown, "Stdout"),
        stderr=_markdown_code_section(markdown, "Stderr"),
        reason=reason,
        timed_out="timed out" in reason.casefold(),
    )


def _markdown_backtick_value(markdown: str, label: str) -> str:
    match = re.search(rf"(?m)^- {re.escape(label)}:\s*`([^`]*)`", markdown)
    return match.group(1).strip() if match else ""


def _markdown_code_section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*\n\s*```text\s*\n(.*?)\n```",
        markdown,
    )
    return match.group(1).rstrip() if match else ""


def _phase4_artifact_version(path: str, *, prefix: str, default: int) -> int:
    name = Path(path).name
    match = re.match(rf"{re.escape(prefix)}v(\d+)", name)
    return int(match.group(1)) if match else default


def _implementation_repair_feedback(
    *,
    run_id: str,
    state: dict[str, object],
    work_packet: dict[str, object],
    trace_result: object,
    trace_path: str,
    target_repo: str,
    branch: str,
    feedback_version: int,
    repair_iteration: int,
) -> dict[str, object]:
    """Đóng gói feedback Validator theo schema ổn định cho Developer repair slice sau."""

    llm_review = getattr(trace_result, "llm_review", {})
    if not isinstance(llm_review, dict):
        llm_review = {}
    missing_links = getattr(trace_result, "missing_links", [])
    if not isinstance(missing_links, list):
        missing_links = []
    actionable_messages = [
        str(item.get("message") or "").strip()
        for item in missing_links
        if isinstance(item, dict) and str(item.get("message") or "").strip()
    ]
    developer_summary = f"Validator reported {len(missing_links)} actionable blocking link(s)."
    if actionable_messages:
        developer_summary += " " + " | ".join(actionable_messages)
    output_contract = _dict_value(work_packet.get("output_contract"))
    return {
        "version": f"implementation_repair_feedback_v{feedback_version}",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "status": "implementation_repair_needed",
        "repair_iteration": repair_iteration,
        "repair_scope": "code_or_test",
        "feedback_source": "validator_code_trace",
        "severity": "repair_required",
        "summary": developer_summary,
        "required_action": (
            "Verification has passed. Developer must choose and implement one coherent bounded slice of the missing "
            "files, symbols, behaviors, tests, or trace links reported by Validator, using only allowed_paths and "
            "the current worktree. Other missing links may remain for a later iteration. The harness will rerun "
            "Verification and then Validator against the accumulated worktree. Do not edit the requirement or "
            "approved design."
        ),
        "target_repo": target_repo,
        "branch": branch,
        "trace_artifact": trace_path,
        "missing_links": missing_links,
        "llm_review": {
            "ok": llm_review.get("ok"),
            "confidence": llm_review.get("confidence"),
            "summary": developer_summary,
            "risks": [],
        },
        "developer_constraints": {
            "allowed_paths": output_contract.get("allowed_paths", []),
            "changed_files_only": output_contract.get("changed_files_only"),
            "format": output_contract.get("format"),
        },
    }




