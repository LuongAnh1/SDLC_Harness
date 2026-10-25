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
    resolve_clarification_answer,
    select_current_requirement_question,
)
from .clarification_session_v1 import (
    SESSION_QUESTION_LIMIT,
    SESSION_VERSION,
    close_clarification_session,
    record_session_answer,
    session_pending_ids,
    start_clarification_session,
)
from .coverage_v1 import (
    COVERAGE_CATEGORIES,
    RequirementCoverageResult,
    RequirementCoverageReviewer,
    build_requirement_quality_checklist,
)
from .clarification_policy_v1 import (
    DEFERRED_TO_DESIGN,
    HUMAN_REQUIRED,
    IRRELEVANT,
    POLICY_DEFAULT,
    SHADOW_ARTIFACT_KEY,
    SHADOW_ARTIFACT_PREFIX,
    SHADOW_POLICY_VERSION,
    SHADOW_SCHEMA_VERSION,
    build_clarification_policy_shadow,
    shadow_error_summary,
    shadow_quality_summary,
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
    "COVERAGE_CATEGORIES",
    "DEFERRED_TO_DESIGN",
    "DecisionStatus",
    "DELTA_SCHEMA_VERSION",
    "GAP_REPORT_VERSION",
    "GAP_TYPES",
    "HARD_BLOCKING_CATEGORIES",
    "HUMAN_REQUIRED",
    "IRRELEVANT",
    "MISSING_EVIDENCE_CODES",
    "MAX_DELTA_OPERATIONS",
    "QuestionStatus",
    "POLICY_DEFAULT",
    "RequirementClarificationResult",
    "RequirementClarificationUpdater",
    "RequirementCoverageResult",
    "RequirementCoverageReviewer",
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
    "SHADOW_ARTIFACT_KEY",
    "SHADOW_ARTIFACT_PREFIX",
    "SHADOW_POLICY_VERSION",
    "SHADOW_SCHEMA_VERSION",
    "SESSION_QUESTION_LIMIT",
    "SESSION_VERSION",
    "ValidationStatus",
    "clarification_submission_hash",
    "build_requirement_gap_report",
    "build_clarification_policy_shadow",
    "build_requirement_quality_checklist",
    "apply_requirement_delta",
    "find_clarification_submission",
    "close_clarification_session",
    "gap_report_summary",
    "normalize_and_validate_requirement_model_v2",
    "normalize_clarification_answer",
    "record_session_answer",
    "resolve_clarification_answer",
    "require_valid_requirement_v2_projections",
    "render_formal_use_case_v2",
    "render_requirement_packet_v2",
    "requirement_candidate_hash",
    "select_current_requirement_question",
    "session_pending_ids",
    "shadow_error_summary",
    "shadow_quality_summary",
    "start_clarification_session",
    "validate_requirement_v2_projections",
    "validate_requirement_gap_report",
]
