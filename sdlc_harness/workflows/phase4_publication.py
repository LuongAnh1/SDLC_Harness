"""Phase 4 publication cycle."""

from __future__ import annotations

from pathlib import Path

from ..artifacts.writers import (
    write_phase4_github_code_review,
    write_phase4_github_code_review_approval,
    write_phase4_github_pr_body,
)
from ..core.errors import HarnessError
from ..storage.runtime import utc_now_iso
from ..sync.github import (
    GitHubPublishError,
    github_repository_from_remote,
)
from ..sync.runner import sync_code_review_checkpoint
from ..workspaces import RunWorkspaceRecord
from .phase3 import _dict_value, _issue_key_from_state, _read_json_artifact, _string_dict
from .phase4 import (
    ALLOWED_CODE_REVIEW_APPROVED_ACTIONS,
    ALLOWED_CODE_REVIEW_PUBLISH_ACTIONS,
    ALLOWED_CODE_REVIEW_READY_ACTIONS,
    ALLOWED_CODE_REVIEW_SYNC_ACTIONS,
    CODE_TRACE_APPROVED_CONTEXT_LIMITS,
    _code_review_pr_body,
    _code_review_publication_paths,
    _code_trace_policy_for_state,
    _planning_policy_for_state,
    _positive_int_or_none,
    _required_phase4_artifact,
)


class PhaseFourPublicationCycle:
    """Own this Phase 4 transition cycle; composed by PhaseFourWorkflow."""

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
                planning_policy=_planning_policy_for_state(state, run_id=run_id),
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
        class_graph_ref = _required_phase4_artifact(artifact_paths, "class_diagram_graph", run_id=run_id)
        context["class_diagram_graph_data"] = _read_json_artifact(
            self._existing_artifact_path(class_graph_ref, run_id=run_id),
            run_id=run_id,
        )
        ledger_ref = str(artifact_paths.get("implementation_validation_ledger") or "").strip()
        if ledger_ref:
            context["prior_implementation_validation_ledger"] = _read_json_artifact(
                self._existing_artifact_path(ledger_ref, run_id=run_id),
                run_id=run_id,
            )
        return context

    def _validate_code_trace_snapshot(
        self,
        *,
        state: dict[str, object],
        artifact_paths: dict[str, str],
        run_id: str,
        target_path: Path,
        work_packet: dict[str, object],
        verification_evidence: dict[str, object] | None = None,
    ) -> object:
        approved_context = self._code_trace_approved_context(
            artifact_paths=artifact_paths,
            run_id=run_id,
        )
        verification = (
            _dict_value(verification_evidence)
            if verification_evidence is not None
            else _dict_value(_dict_value(state.get("quality_reports")).get("verification"))
        )
        return self.code_trace_validator.validate(
            target_path=target_path,
            work_packet=work_packet,
            approved_context=approved_context,
            verification_evidence=verification,
            previous_ledger=_dict_value(approved_context.get("prior_implementation_validation_ledger")),
            code_trace_policy=_code_trace_policy_for_state(state, run_id=run_id),
        )

    def _write_implementation_validation_ledger(
        self,
        *,
        run_id: str,
        trace_result: object,
        version: int,
    ) -> str:
        ledger = getattr(trace_result, "ledger", {})
        payload = dict(ledger) if isinstance(ledger, dict) else {}
        path = self.store.write_json_artifact(
            run_id,
            f"implementation_validation_ledger_v{version}.json",
            payload,
        )
        return self.store.relative(path)

