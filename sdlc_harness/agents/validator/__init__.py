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
from .vertical_trace import (
    VERTICAL_TRACE_MODE_STATE_KEY,
    VERTICAL_TRACE_GRAPH_VERSION,
    VERTICAL_TRACE_MODES,
    VERTICAL_TRACE_STATE_KEY,
    VERTICAL_TRACE_VALIDATION_VERSION,
    VerticalTraceValidationResult,
    bounded_trace_view,
    build_vertical_trace_graph,
    validate_vertical_trace_coverage,
    validate_vertical_trace_graph,
    validate_vertical_trace_report,
)
from .plan_review import ImplementationPlanReview, ImplementationPlanValidator
from .typescript_ast import TypeScriptAstEvidence, TypeScriptAstExtractor

__all__ = [
    "CodeTraceResult",
    "CodeTraceValidator",
    "DesignFinding",
    "DesignValidationResult",
    "DesignValidator",
    "ImplementationPlanReview",
    "ImplementationPlanValidator",
    "TraceabilityResult",
    "TypeScriptAstEvidence",
    "TypeScriptAstExtractor",
    "ValidatorAgent",
    "build_requirement_graph",
    "build_requirement_graph_v2",
    "compute_missing_links",
    "VERTICAL_TRACE_GRAPH_VERSION",
    "VERTICAL_TRACE_MODE_STATE_KEY",
    "VERTICAL_TRACE_MODES",
    "VERTICAL_TRACE_STATE_KEY",
    "VERTICAL_TRACE_VALIDATION_VERSION",
    "VerticalTraceValidationResult",
    "bounded_trace_view",
    "build_vertical_trace_graph",
    "validate_vertical_trace_coverage",
    "validate_vertical_trace_graph",
    "validate_vertical_trace_report",
]
