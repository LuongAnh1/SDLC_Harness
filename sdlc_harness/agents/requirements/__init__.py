"""Chuẩn hóa, làm rõ và kiểm tra requirement trong Phase 1.

Package cung cấp model V2, refiner, clarification, validation, projection và
renderer. Nó biến source snapshot thành requirement có cấu trúc; việc nạp Jira
hay fixture thuộc ``sources``, còn checkpoint và chuyển phase thuộc workflow.
"""

from .model_v2 import (
    DecisionStatus,
    QuestionStatus,
    RequirementModelV2,
    RequirementStatus,
    ValidationStatus,
)
from .gap_report import (
    GAP_REPORT_VERSION,
    GAP_TYPES,
    MISSING_EVIDENCE_CODES,
    SOURCE_UNAVAILABLE_CODES,
    build_requirement_gap_report,
    gap_report_summary,
    validate_requirement_gap_report,
)
from .delta_v1 import (
    DELTA_SCHEMA_VERSION,
    MAX_DELTA_OPERATIONS,
    RequirementDeltaApplication,
    apply_requirement_delta,
    requirement_candidate_hash,
)
from .clarification_v2 import (
    ClarificationAttempt,
    CurrentRequirementQuestion,
    RequirementClarificationResult,
    RequirementClarificationUpdater,
    RequirementQuestionSelection,
    clarification_submission_hash,
    find_clarification_submission,
    normalize_clarification_answer,
    select_current_requirement_question,
)
from .refiner_v2 import (
    RequirementModelV2Attempt,
    RequirementModelV2RefinementResult,
    RequirementModelV2Refiner,
)
from .projection_v2 import (
    RequirementProjectionV2Report,
    require_valid_requirement_v2_projections,
    validate_requirement_v2_projections,
)
from .renderer_v2 import render_formal_use_case_v2, render_requirement_packet_v2
from .source_tools import RequirementSourceCatalog
from .validator_v2 import (
    HARD_BLOCKING_CATEGORIES,
    RequirementModelV2Issue,
    RequirementModelV2ValidationReport,
    RequirementModelV2ValidationResult,
    normalize_and_validate_requirement_model_v2,
)

__all__ = [
    "ClarificationAttempt",
    "CurrentRequirementQuestion",
    "DecisionStatus",
    "DELTA_SCHEMA_VERSION",
    "GAP_REPORT_VERSION",
    "GAP_TYPES",
    "HARD_BLOCKING_CATEGORIES",
    "MISSING_EVIDENCE_CODES",
    "MAX_DELTA_OPERATIONS",
    "QuestionStatus",
    "RequirementClarificationResult",
    "RequirementClarificationUpdater",
    "RequirementDeltaApplication",
    "RequirementModelV2",
    "RequirementModelV2Attempt",
    "RequirementModelV2Issue",
    "RequirementModelV2RefinementResult",
    "RequirementModelV2Refiner",
    "RequirementModelV2ValidationReport",
    "RequirementModelV2ValidationResult",
    "RequirementProjectionV2Report",
    "RequirementQuestionSelection",
    "RequirementStatus",
    "RequirementSourceCatalog",
    "SOURCE_UNAVAILABLE_CODES",
    "ValidationStatus",
    "clarification_submission_hash",
    "build_requirement_gap_report",
    "apply_requirement_delta",
    "find_clarification_submission",
    "gap_report_summary",
    "normalize_and_validate_requirement_model_v2",
    "normalize_clarification_answer",
    "require_valid_requirement_v2_projections",
    "render_formal_use_case_v2",
    "render_requirement_packet_v2",
    "requirement_candidate_hash",
    "select_current_requirement_question",
    "validate_requirement_v2_projections",
    "validate_requirement_gap_report",
]
