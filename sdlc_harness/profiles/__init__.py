"""Mô tả target project bằng profile thay vì hard-code một ngôn ngữ.

Package này sở hữu contract project/language/component, manifest khai báo của
target repo và snapshot bất biến theo từng run. Từ snapshot, workflow dựng các
policy cho prompt, đường dẫn lập kế hoạch và stage verification. Package chỉ
mô tả và kiểm tra policy; việc chạy command thực tế thuộc ``verification``.
"""

from .model import ComponentProfile, LanguageProfile, ProfileRegistry, ProjectProfile
from .manifest import (
    TARGET_PROFILE_MANIFEST_PATH,
    ResolvedTargetManifest,
    TargetProfileManifestV1,
    load_and_validate_target_manifest,
)
from .registry import get_language_profile, get_project_profile, list_project_profile_ids
from .planning import (
    PLANNING_PATH_POLICY_VERSION,
    ComponentPlanningRule,
    PlanningPathPolicy,
    build_planning_path_policy,
    planning_path_policy_from_snapshot,
)
from .prompting import (
    LanguagePromptDirective,
    get_prompt_directive,
    prompt_directive_from_snapshot,
    resolve_language_prompt_directive,
)
from .snapshot import (
    PROFILE_SNAPSHOT_STATE_KEY,
    PROFILE_SNAPSHOT_VERSION,
    ProfileSnapshotV1,
    build_profile_snapshot,
    profile_status_metadata,
    read_profile_snapshot,
)
from .verification import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    PROFILED_IMPLEMENTATION_PLAN_VERSION,
    VERIFICATION_STAGE_POLICY_VERSION,
    VerificationStagePolicy,
    build_verification_stage_policy,
    is_profiled_work_packet_schema,
    profiled_execution_field_paths,
    profiled_plan_contract_issues,
    verification_stage_policy_from_snapshot,
)
from .signature import (
    IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION,
    build_implementation_signature_contract,
    implementation_signature_contract_digest,
    implementation_signature_contract_issues,
    normalize_typescript_type,
    signature_shape,
)
from .qa import (
    QA_TEST_POLICY_VERSION,
    QaComponentTestRule,
    QaTestPolicy,
    build_qa_test_policy,
    qa_test_policy_from_snapshot,
)
from .code_trace import (
    CODE_TRACE_POLICY_VERSION,
    CodeTracePolicy,
    build_code_trace_policy,
    code_trace_policy_from_snapshot,
)
from .retirement import (
    LEGACY_RETIRED_ALLOWED_COMMANDS,
    public_allowed_actions,
    public_run_summary,
    require_project_profile_for_start,
    require_run_command_allowed,
)

__all__ = [
    "ComponentProfile",
    "LanguageProfile",
    "LanguagePromptDirective",
    "ProfileRegistry",
    "ProjectProfile",
    "ComponentPlanningRule",
    "PlanningPathPolicy",
    "PLANNING_PATH_POLICY_VERSION",
    "ResolvedTargetManifest",
    "TargetProfileManifestV1",
    "TARGET_PROFILE_MANIFEST_PATH",
    "PROFILE_SNAPSHOT_STATE_KEY",
    "PROFILE_SNAPSHOT_VERSION",
    "ProfileSnapshotV1",
    "COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION",
    "LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION",
    "LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION",
    "PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION",
    "PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION",
    "PROFILED_IMPLEMENTATION_PLAN_VERSION",
    "IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION",
    "VerificationStagePolicy",
    "VERIFICATION_STAGE_POLICY_VERSION",
    "QA_TEST_POLICY_VERSION",
    "QaComponentTestRule",
    "QaTestPolicy",
    "CODE_TRACE_POLICY_VERSION",
    "CodeTracePolicy",
    "build_profile_snapshot",
    "build_planning_path_policy",
    "build_verification_stage_policy",
    "build_implementation_signature_contract",
    "build_qa_test_policy",
    "build_code_trace_policy",
    "get_language_profile",
    "get_prompt_directive",
    "get_project_profile",
    "list_project_profile_ids",
    "load_and_validate_target_manifest",
    "profile_status_metadata",
    "profiled_execution_field_paths",
    "profiled_plan_contract_issues",
    "implementation_signature_contract_issues",
    "implementation_signature_contract_digest",
    "is_profiled_work_packet_schema",
    "normalize_typescript_type",
    "signature_shape",
    "read_profile_snapshot",
    "planning_path_policy_from_snapshot",
    "prompt_directive_from_snapshot",
    "resolve_language_prompt_directive",
    "verification_stage_policy_from_snapshot",
    "qa_test_policy_from_snapshot",
    "code_trace_policy_from_snapshot",
    "LEGACY_RETIRED_ALLOWED_COMMANDS",
    "public_allowed_actions",
    "public_run_summary",
    "require_project_profile_for_start",
    "require_run_command_allowed",
]
