"""Phase 4 publication cycle."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

from ..artifacts.writers import (
    write_phase4_github_code_review,
    write_phase4_github_code_review_approval,
    write_phase4_github_pr_body,
)
from ..core.errors import HarnessError
from ..llm.step_review import ReviewableLlmClient
from ..agents.validator import CodeTraceValidator
from ..storage.runtime import utc_now_iso
from ..sync.github import (
    GitHubPublishError,
    github_repository_from_remote,
)
from ..sync.runner import sync_code_review_checkpoint
from ..workspaces import RunWorkspaceRecord
from .phase3 import _dict_value, _issue_key_from_state, _read_json_artifact, _string_dict
from .phase4_browser import _worktree_fingerprint
from .phase4_closure import _next_closure_artifact_version, _write_closure_json
from .phase4 import (
    _dict_items,
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
from ..core import run_state


PUBLICATION_PREVIEW_CHECKPOINT_TYPE = "publication_preview"


def _code_validation_verification_evidence(value: object) -> dict[str, object]:
    evidence = _dict_value(value)
    nested = evidence.get("verification")
    return _dict_value(nested) if isinstance(nested, dict) else evidence


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

    def _code_review_publication_manifest(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
        qa_generated: dict[str, object],
    ) -> tuple[list[str], list[str]]:
        """(paths the Draft PR commits, changed Harness evidence that stays out of it)."""

        paths = _code_review_publication_paths(
            work_packet=work_packet,
            qa_generated=qa_generated,
            run_id=run_id,
            planning_policy=_planning_policy_for_state(state, run_id=run_id),
            extra_paths=[
                *self._runtime_closure_publication_paths(state=state, run_id=run_id),
                *self._browser_publication_paths(state=state, run_id=run_id),
            ],
        )
        added, evidence_only = self._documentation_publication_paths(
            state=state, run_id=run_id, work_packet=work_packet
        )
        if not added and not evidence_only:
            return paths, []
        return sorted((set(paths) | set(added)) - set(evidence_only)), sorted(evidence_only)

    def _publication_inputs(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Everything a Draft PR needs, shared by the publication preview and the real publish."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
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
        publication_paths, evidence_only_paths = self._code_review_publication_manifest(
            state=state, run_id=run_id, work_packet=work_packet, qa_generated=qa_generated
        )
        pr_title = f"[{issue_key}] Implement approved use case"
        pr_body = _code_review_pr_body(
            state=state,
            run_id=run_id,
            issue_key=issue_key,
            artifact_paths=artifact_paths,
            qa_generated=qa_generated,
        )
        pr_body = pr_body + "\n\n" + self._release_evidence_markdown(
            state=state, run_id=run_id, paths=publication_paths, evidence_only=evidence_only_paths
        )
        return {
            "target_info": target_info,
            "branch": branch,
            "base_branch": base_branch,
            "issue_key": issue_key,
            "repository": repository,
            "paths": publication_paths,
            "evidence_only": evidence_only_paths,
            "pr_title": pr_title,
            "pr_body": pr_body,
        }

    # ------------------------------------------------------------------ L5.5 publication preview (D7)

    def _publication_tree_matches_final_verification(self, state: dict[str, object], target_path: Path) -> bool:
        expected = str(run_state.final_verification(state).get("worktree_sha256") or "")
        return bool(expected) and _worktree_fingerprint(target_path).get("sha256") == expected

    def _block_publication(self, state: dict[str, object], *, run_id: str, error: HarnessError) -> dict[str, object]:
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "code_review_publishing",
                "status": "blocked",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": error.to_response(),
                "summary_for_user": f"Publication bị chặn cho run {run_id}: {error.message}",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_publication_preview(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Everything the Draft PR would contain, with the publisher's checks; nothing is committed or pushed."""

        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        try:
            prepared = self._publication_inputs(state=state, run_id=run_id)
            target_path = prepared["target_info"].path
            if not self._publication_tree_matches_final_verification(state, target_path):
                raise HarnessError(
                    "publication_tree_changed",
                    "The worktree changed after Final Verification; it must be verified again before publication.",
                    run_id=run_id,
                )
            preview = self.github_publisher.preview(
                target_path=target_path,
                expected_remote=self.settings.target_repo_remote,
                repository=prepared["repository"],
                branch=prepared["branch"],
                base_branch=prepared["base_branch"],
                run_id=run_id,
                publication_paths=prepared["paths"],
                evidence_only_paths=prepared["evidence_only"],
            )
        except HarnessError as exc:
            return self._block_publication(state, run_id=run_id, error=exc)
        body_ref = write_phase4_github_pr_body(store=self.store, run_id=run_id, body=prepared["pr_body"])
        version = _next_closure_artifact_version(self.store, run_id, "publication_preview")
        preview_ref = _write_closure_json(
            self.store,
            run_id,
            f"publication_preview_v{version}.json",
            {
                **preview,
                "run_id": run_id,
                "version": version,
                "pr_title": prepared["pr_title"],
                "pr_body": body_ref,
                "final_verification": run_state.final_verification(state).get("artifact"),
                "worktree_sha256": run_state.final_verification(state).get("worktree_sha256"),
            },
        )
        latest_artifacts.update({"github_pr_body": body_ref, "publication_preview": preview_ref})
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "code_review_publishing",
                "status": "publication_previewed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": f"checkpoint_publication_{run_id}_v{version}",
                    "type": PUBLICATION_PREVIEW_CHECKPOINT_TYPE,
                    "artifact_path": body_ref,
                    "preview_path": preview_ref,
                },
                "allowed_next_actions": ["approve_checkpoint", "get_status", "cancel_run"],
                "last_error": None,
                "summary_for_user": (
                    f"Bản xem trước Draft PR cho run {run_id}: {len(preview['staged_paths'])} file sẽ commit; "
                    "chưa commit/push gì. Đang chờ anh duyệt publish thật."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def approve_publication_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """The owner approves the previewed Draft PR; the next `continue_run` publishes it."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            checkpoint = _dict_value(state.get("pending_checkpoint"))
            current_id = str(checkpoint.get("checkpoint_id") or "")
            if (
                str(state.get("status") or "") != "publication_previewed"
                or checkpoint.get("type") != PUBLICATION_PREVIEW_CHECKPOINT_TYPE
                or (checkpoint_id and checkpoint_id != current_id)
            ):
                raise HarnessError("checkpoint_not_current", f"Run is not waiting for publication approval: {run_id}", run_id=run_id)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "status": "publication_approved",
                    "pending_checkpoint": None,
                    "publication": {"approved_checkpoint_id": current_id, "approved_at": utc_now_iso()},
                    "allowed_next_actions": ["continue_run", "get_status", "cancel_run"],
                    "last_error": None,
                    "summary_for_user": f"Anh đã duyệt publish Draft PR cho run {run_id}.",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

    def _continue_approved_publication(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        # Only the first attempt is guarded: after a commit the tree is clean and the retry path reuses the commit.
        try:
            target_path = self._publication_inputs(state=state, run_id=run_id)["target_info"].path
            if not self._publication_tree_matches_final_verification(state, target_path):
                raise HarnessError(
                    "publication_tree_changed",
                    "The worktree changed after the approved preview; nothing was published.",
                    run_id=run_id,
                )
        except HarnessError as exc:
            return self._block_publication(state, run_id=run_id, error=exc)
        return self._continue_code_review_publish(state=state, run_id=run_id)

    def _release_evidence_markdown(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        paths: list[str],
        evidence_only: list[str],
    ) -> str:
        """PR body release-evidence sections (owner D8); every line comes from an artifact."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))

        def read(key: str) -> dict[str, object]:
            ref = str(artifact_paths.get(key) or "")
            return _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id) if ref else {}

        lines = ["## Runtime Closure", ""]
        probes = _dict_items(read("runtime_closure_verification").get("probe_results"))
        if probes:
            lines.extend(["| Probe | Status |", "| --- | --- |"])
            lines.extend(f"| {item.get('probe_id')} | {item.get('status')} |" for item in probes)
        else:
            lines.append("- No runtime probe was recorded.")
        lines.append(f"- Contract: `{_dict_value(state.get('runtime_closure')).get('approved_contract', '')}`")
        polish = _dict_value(run_state.browser_stages(state).get("polish"))
        result_review = read("ui_polish_result_review")
        accepted = [*_dict_items(read("ui_polish_contract_review").get("warnings")), *_dict_items(result_review.get("warnings"))]
        lines.extend(
            [
                "",
                "## UI Polish",
                "",
                f"- Approved contract: `{polish.get('approved_contract', '')}`",
                f"- Result review: `{artifact_paths.get('ui_polish_result_review', '')}` "
                f"({len(_dict_items(result_review.get('blocking_findings')))} blockers, {len(accepted)} accepted warnings)",
                *[
                    f"  - {item.get('category', '')} {', '.join(str(unit) for unit in item.get('unit_ids') or [])}: "
                    f"{str(item.get('issue') or '').strip()[:300]}"
                    for item in accepted[:10]
                ],
            ]
        )
        documentation = _read_json_artifact(
            self._existing_artifact_path(str(run_state.documentation_closure(state).get("artifact") or ""), run_id=run_id),
            run_id=run_id,
        )
        context = _dict_value(documentation.get("context"))
        lines.extend(["", "## Browser QA", "", "| Scenario | Result | Satisfied | Not observable |", "| --- | --- | --- | --- |"])
        scenarios = _dict_items(context.get("scenarios"))
        lines.extend(
            f"| {item.get('scenario_id')} | {item.get('result')} | {item.get('satisfied', 0)} | {len(_dict_items(item.get('not_observable')))} |"
            for item in scenarios
        )
        for item in scenarios:
            for row in _dict_items(item.get("not_observable")):
                reason = str(row.get("reason") or "").strip()
                lines.append(f"- {item.get('scenario_id')} `{row.get('obligation_id')}` not observable" + (f": {reason}" if reason else ""))
        final = read("final_verification")
        command_count = sum(len(item.get("commands") or []) for item in _dict_items(context.get("commands")))
        env_names = ", ".join("`" + str(item.get("name")) + "`" for item in _dict_items(context.get("env"))) or "none"
        lines.extend(
            [
                "",
                "## Documentation Closure",
                "",
                "- `README.md` section written by the Harness from passed verification and approved by the owner "
                f"({command_count} commands).",
                f"- Environment: {env_names}",
                "",
                "## Final Verification",
                "",
                f"- F1 worktree unchanged since browser verification: {'passed' if _dict_value(final.get('f1')).get('ok') else 'failed'}",
                f"- F2 `{_dict_value(final.get('f2')).get('policy_version', '')}` on the published tree only "
                f"(no stand-in, no demo flag, no Harness specs): {'passed' if _dict_value(final.get('f2')).get('ok') else 'failed'}",
                f"- F3 documented commands: {'passed' if _dict_value(final.get('f3')).get('ok') else 'failed'}",
                f"- Report: `{artifact_paths.get('final_verification', '')}`",
            ]
        )
        added, _ = self._documentation_publication_paths(
            state=state, run_id=run_id, work_packet=read("developer_work_packet")
        )
        lines.extend(["", "## Files Outside The Developer Scope", ""])
        lines.extend(f"- `{path}`" for path in added if path in paths)
        lines.extend(f"- `{path}` stays out of this PR (run evidence)" for path in evidence_only)
        return "\n".join(lines)

    def _continue_code_review_publish(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Commit/push code đã qua QA và tạo hoặc cập nhật Draft PR."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        report_path = ""
        workspace: RunWorkspaceRecord | None = None
        try:
            prepared = self._publication_inputs(state=state, run_id=run_id)
            workspace = self.workspace_manager.resolve(run_id)
            target_info, branch, base_branch = prepared["target_info"], prepared["branch"], prepared["base_branch"]
            issue_key, repository = prepared["issue_key"], prepared["repository"]
            publication_paths, evidence_only_paths = prepared["paths"], prepared["evidence_only"]
            pr_title, pr_body = prepared["pr_title"], prepared["pr_body"]
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
                evidence_only_paths=evidence_only_paths,
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

        for context_key, artifact_key in (
            ("implementation_plan_structured_data", "implementation_plan_structured"),
            ("vertical_design_graph_data", "vertical_design_graph"),
            ("vertical_trace_graph_data", "vertical_trace_graph"),
        ):
            artifact_ref = _required_phase4_artifact(artifact_paths, artifact_key, run_id=run_id)
            context[context_key] = _read_json_artifact(
                self._existing_artifact_path(artifact_ref, run_id=run_id),
                run_id=run_id,
            )

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
        approved_closure_changes: dict[str, object] | None = None,
    ) -> object:
        approved_context = self._code_trace_approved_context(
            artifact_paths=artifact_paths,
            run_id=run_id,
        )
        if approved_closure_changes:
            # Only tasks whose sources the closure changed receive it; other prompts stay byte-identical.
            approved_context = {**approved_context, "approved_closure_changes": approved_closure_changes}
        verification = _code_validation_verification_evidence(
            verification_evidence
            if verification_evidence is not None
            else _dict_value(state.get("quality_reports")).get("verification")
        )
        validator = self.code_trace_validator
        if bool(self.code_validation_step_review.get("enabled")):
            def reviewable_client(task: dict[str, object], prompt: str) -> ReviewableLlmClient:
                task_id = str(task.get("task_id") or "task")
                safe_task_id = re.sub(r"[^A-Za-z0-9_-]+", "-", task_id).strip("-") or "task"
                prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
                return ReviewableLlmClient(
                    self.llm_client,
                    store=self.store,
                    run_id=run_id,
                    review=self.code_validation_step_review,
                    call_prefix=f"code-validation-{safe_task_id}",
                    execution_dir=f"code_validation_execution/step_review/{prompt_digest}",
                    error_prefix="implementation_validation",
                    label=f"Code Validation {task_id}",
                )

            validator = CodeTraceValidator(
                self.llm_client,
                llm_call_budget=self.code_trace_validator.llm_call_budget,
                symbol_extractor=self.code_trace_validator.symbol_extractor,
                reviewable_client_factory=reviewable_client,
            )
        return validator.validate(
            target_path=target_path,
            work_packet=work_packet,
            approved_context=approved_context,
            verification_evidence=verification,
            previous_ledger=_dict_value(approved_context.get("prior_implementation_validation_ledger")),
            code_trace_policy=_code_trace_policy_for_state(state, run_id=run_id),
            task_cache_path=(
                self.store.run_dir(run_id)
                / "code_validation_execution"
                / "code_validation_task_cache_v1.json"
            ),
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

