"""Đồng bộ checkpoint và kết quả Harness sang công cụ cộng tác.

Runner chọn mock/real adapter; Atlassian cập nhật Jira/Confluence và GitHub xử
lý trạng thái code review theo contract. Package chỉ phản chiếu kết quả đã có,
không tạo requirement, design, code hay quyết định checkpoint được duyệt.
"""

from .atlassian import (
    real_sync_code_review_checkpoint,
    real_sync_design_checkpoint,
    real_sync_implementation_plan_checkpoint,
    real_sync_requirement_checkpoint,
)
from .mock import (
    mock_sync_code_review_checkpoint,
    mock_sync_design_checkpoint,
    mock_sync_implementation_plan_checkpoint,
    mock_sync_requirement_checkpoint,
)
from .runner import (
    sync_code_review_checkpoint,
    sync_design_checkpoint,
    sync_implementation_plan_checkpoint,
    sync_requirement_checkpoint,
)

__all__ = [
    "mock_sync_code_review_checkpoint",
    "mock_sync_design_checkpoint",
    "mock_sync_implementation_plan_checkpoint",
    "mock_sync_requirement_checkpoint",
    "real_sync_code_review_checkpoint",
    "real_sync_design_checkpoint",
    "real_sync_implementation_plan_checkpoint",
    "real_sync_requirement_checkpoint",
    "sync_code_review_checkpoint",
    "sync_design_checkpoint",
    "sync_implementation_plan_checkpoint",
    "sync_requirement_checkpoint",
]
