"""Dọn run workspace đã kết thúc mà không thay đổi workflow hay remote branch.

Workflow này nối runtime/GitHub evidence với RunWorkspaceManager. Nó chỉ cung cấp
CLI vận hành cục bộ; Niko và checkpoint Code Review chưa gọi hành vi cleanup này.
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import Settings
from ..sync.github import GitHubPullRequestReader, github_repository_from_remote
from ..workspaces import RunWorkspaceManager, RunWorkspaceRecord


class WorkspaceCleanupWorkflow:
    """Cung cấp JSON contract ổn định cho lifecycle cleanup của một run."""

    def __init__(
        self,
        settings: Settings,
        *,
        pull_request_reader: GitHubPullRequestReader | None = None,
    ):
        self.manager = RunWorkspaceManager(settings)
        self.pull_request_reader = pull_request_reader or GitHubPullRequestReader()

    def cleanup(self, *, run_id: str) -> dict[str, object]:
        """Cleanup workspace nếu PR đã merge hoặc run đã cancelled."""

        def read_pull_request(record: RunWorkspaceRecord) -> dict[str, object]:
            repository = github_repository_from_remote(record.remote)
            return self.pull_request_reader.read(
                target_path=Path(record.canonical_repo),
                repository=repository,
                pr_number=int(record.pr_number or 0),
                run_id=run_id,
                error_code="workspace_cleanup_failed",
            )

        record, cleaned = self.manager.cleanup_run(
            run_id,
            pull_request_reader=read_pull_request,
        )
        state = self.manager.store.read_state(run_id)
        action = "đã dọn" if cleaned else "đã được dọn trước đó"
        return {
            "ok": True,
            "run_id": run_id,
            "cleaned": cleaned,
            "cleanup_reason": record.cleanup_reason,
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "workspace": record.status_summary(),
            "pull_request": {
                "number": record.pr_number,
                "url": record.pr_url,
                "state_at_cleanup": record.pr_state_at_cleanup or None,
                "merged_at": record.pr_merged_at or None,
                "merge_commit_sha": record.merge_commit_sha or None,
            },
            "summary_for_user": (
                f"Workspace của {run_id} {action} an toàn theo lý do {record.cleanup_reason}; "
                "local worktree/branch đã được gỡ và remote branch không bị xóa."
            ),
        }
