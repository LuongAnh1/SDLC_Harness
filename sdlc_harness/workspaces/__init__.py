"""Tạo và quản lý Git workspace cô lập cho từng workflow run.

Package xác định branch/worktree identity, lock và metadata để Phase 4 sửa đúng
target repo mà không giẫm lên run khác. Nó không merge PR và không quyết định
nội dung source cần thay đổi.
"""

from .manager import RunWorkspaceManager, WorkspaceFileLock
from .model import RunWorkspaceRecord, branch_name_for_run, source_slug_for_workspace

__all__ = [
    "RunWorkspaceManager",
    "RunWorkspaceRecord",
    "WorkspaceFileLock",
    "branch_name_for_run",
    "source_slug_for_workspace",
]
