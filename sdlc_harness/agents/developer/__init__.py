"""Lập kế hoạch triển khai và thực thi thay đổi source trong Phase 3/4.

Planner tạo implementation plan; work packet đóng băng ngữ cảnh được phép dùng;
tool-loop thực hiện hoặc sửa code trong workspace của run. Giới hạn đường dẫn,
profile và verification vẫn do contract/workflow kiểm soát.
"""
from .execution import (
    DeveloperExecutionError,
    DeveloperExecutionResult,
    DeveloperToolLoopExecutor,
    developer_transport_retry_allowed,
    repair_scope_mismatch_retry_allowed,
)
from .planner import DeveloperPlanAgent, ImplementationPlanDraft
from .component_plan import (
    COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
    IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY,
    PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    build_component_work_units,
    canonicalize_work_unit_dependencies,
    component_plan_issues,
    component_plan_projections,
    merge_work_unit_updates,
    rebind_legacy_mappings_to_work_units,
    resolve_implementation_plan_contract,
)
from .work_packet import (
    build_developer_work_packet,
    refresh_developer_work_packet,
    validate_developer_work_packet_path_policy,
)

__all__ = [
    "DeveloperExecutionError",
    "DeveloperExecutionResult",
    "DeveloperToolLoopExecutor",
    "developer_transport_retry_allowed",
    "repair_scope_mismatch_retry_allowed",
    "DeveloperPlanAgent",
    "ImplementationPlanDraft",
    "COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION",
    "PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION",
    "IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY",
    "build_component_work_units",
    "canonicalize_work_unit_dependencies",
    "component_plan_issues",
    "component_plan_projections",
    "merge_work_unit_updates",
    "rebind_legacy_mappings_to_work_units",
    "resolve_implementation_plan_contract",
    "build_developer_work_packet",
    "refresh_developer_work_packet",
    "validate_developer_work_packet_path_policy",
]

