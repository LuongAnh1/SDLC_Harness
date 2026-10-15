"""Nhận nuôi run Code Review cũ vào contract worktree-per-run.

Workflow này chỉ điều phối bằng chứng runtime, GitHub reader và workspace manager.
Nó không sinh lại artifact, không cập nhật PR và không sync Jira/Confluence.
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import Settings
from ..sync.github import GitHubPullRequestReader
from ..workspaces import RunWorkspaceManager


class WorkspaceAdoptionWorkflow:
    """Cung cấp contract CLI ổn định cho migration run legacy một lần."""

    def __init__(
        self,
        settings: Settings,
        *,
        pull_request_reader: GitHubPullRequestReader | None = None,
    ):
        self.manager = RunWorkspaceManager(settings)
        self.pull_request_reader = pull_request_reader or GitHubPullRequestReader()

    def adopt(self, *, run_id: str) -> dict[str, object]:
        """Adopt branch/PR hiện hữu và giữ nguyên trạng thái workflow của run."""

        def read_pull_request(repository: str, pr_number: int, target_path: Path) -> dict[str, object]:
            return self.pull_request_reader.read(
                target_path=target_path,
                repository=repository,
                pr_number=pr_number,
                run_id=run_id,
            )

        record, adopted = self.manager.adopt_legacy_run(
            run_id,
            pull_request_reader=read_pull_request,
        )
        state = self.manager.store.read_state(run_id)
        action = "đã nhận nuôi" if adopted else "đã có sẵn và được xác minh lại"
        return {
            "ok": True,
            "run_id": run_id,
            "adopted": adopted,
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "workspace": record.status_summary(),
            "pull_request": {
                "number": record.pr_number,
                "url": record.pr_url,
                "draft": True,
            },
            "summary_for_user": (
                f"Workspace legacy {action}; branch {record.branch} giữ nguyên HEAD "
                f"{record.head_sha} và run vẫn ở trạng thái {state.get('status')}."
            ),
        }
