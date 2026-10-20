"""Kiểm tra traceability và contract xuyên suốt các phase.

Validator đối chiếu requirement với design, implementation plan và code để tìm
missing link hoặc blocker. Các kiểm tra deterministic được kết hợp với semantic
review khi phù hợp; package không tự sửa artifact hay quyết định approval.
"""
from .code_trace import CodeTraceResult, CodeTraceValidator
from .design_validation import DesignFinding, DesignValidationResult, DesignValidator
from .traceability import (
    TraceabilityResult,
    ValidatorAgent,
    build_requirement_graph,
    build_requirement_graph_v2,
    compute_missing_links,
)
from .plan_review import ImplementationPlanReview, ImplementationPlanValidator

__all__ = [
    "CodeTraceResult",
    "CodeTraceValidator",
    "DesignFinding",
    "DesignValidationResult",
    "DesignValidator",
    "ImplementationPlanReview",
    "ImplementationPlanValidator",
    "TraceabilityResult",
    "ValidatorAgent",
    "build_requirement_graph",
    "build_requirement_graph_v2",
    "compute_missing_links",
]
