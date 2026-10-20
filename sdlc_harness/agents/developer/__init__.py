"""Lập kế hoạch triển khai và thực thi thay đổi source trong Phase 3/4.

Planner tạo implementation plan; work packet đóng băng ngữ cảnh được phép dùng;
actor và tool-loop thực hiện hoặc sửa code trong workspace của run. Giới hạn
đường dẫn, profile và verification vẫn do contract/workflow kiểm soát.
"""
from .actor import DeveloperActAgent, DeveloperActLlmError, DeveloperActResult
from .execution import (
    DeveloperExecutionError,
    DeveloperExecutionResult,
    DeveloperToolLoopExecutor,
    repair_scope_mismatch_retry_allowed,
)
from .planner import DeveloperPlanAgent, ImplementationPlanDraft
from .work_packet import (
    build_developer_work_packet,
    refresh_developer_work_packet,
    validate_developer_work_packet_path_policy,
)

__all__ = [
    "DeveloperActAgent",
    "DeveloperActLlmError",
    "DeveloperActResult",
    "DeveloperExecutionError",
    "DeveloperExecutionResult",
    "DeveloperToolLoopExecutor",
    "repair_scope_mismatch_retry_allowed",
    "DeveloperPlanAgent",
    "ImplementationPlanDraft",
    "build_developer_work_packet",
    "refresh_developer_work_packet",
    "validate_developer_work_packet_path_policy",
]

