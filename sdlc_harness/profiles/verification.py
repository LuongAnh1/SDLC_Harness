"""Chính sách stage kiểm chứng không thực thi dành cho implementation plan.

Policy này chỉ mang định danh stage đã được review trong project profile. Việc
ánh xạ stage sang argv/cwd/env/timeout thuộc Verification Runner ở Chặng A3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.errors import HarnessError
from .model import ProjectProfile
from .planning import PlanningPathPolicy
from .signature import implementation_signature_contract_issues

if TYPE_CHECKING:
    from .snapshot import ProfileSnapshotV1


VERIFICATION_STAGE_POLICY_VERSION = "verification_stage_policy_v1"
LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v2"
PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v3"
PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION = PROFILED_IMPLEMENTATION_PLAN_VERSION
COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v4"
LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION = "developer_work_packet_profiled_v1"
PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION = "developer_work_packet_profiled_v2"
_ACTIVE_STAGE_IDS_BY_PROJECT = {
    # Chặng A chỉ chạy smoke suite. Catalog full-stack trong ProjectProfile vẫn
    # bất biến; Chặng B phải phát hành policy/profile version mới để mở rộng.
    "typescript_fullstack_v1": ("typecheck", "unit"),
}
_PROHIBITED_EXECUTION_FIELDS = {
    "command",
    "argv",
    "cwd",
    "env",
    "timeout",
    "timeout_seconds",
}


@dataclass(frozen=True)
class VerificationStagePolicy:
    """Danh sách stage bất biến mà Phase 3 được phép tham chiếu."""

    project_profile_id: str
    policy_version: str
    stage_ids: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "project_profile_id": self.project_profile_id,
            "policy_version": self.policy_version,
            "stage_ids": list(self.stage_ids),
        }

    def prompt_context(self) -> dict[str, object]:
        return self.metadata()


def profiled_plan_contract_issues(
    structured_plan: dict[str, object],
    *,
    planning_policy: PlanningPathPolicy,
    verification_policy: VerificationStagePolicy,
    class_graph: dict[str, object] | None = None,
    allow_legacy: bool = False,
) -> list[str]:
    """Kiểm contract profiled plan tại cả Validator và ranh giới Phase 4."""

    issues: list[str] = []
    if verification_policy.project_profile_id != planning_policy.project_profile_id:
        issues.append("Verification stages do not belong to the planning project profile.")
    plan_version = structured_plan.get("version")
    current = plan_version in {
        PROFILED_IMPLEMENTATION_PLAN_VERSION,
        COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    }
    compatible = plan_version == LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION
    if not current and not (allow_legacy and compatible):
        issues.append(
            "Profiled implementation plan version must be "
            f"`{PROFILED_IMPLEMENTATION_PLAN_VERSION}` or "
            f"`{COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION}`."
        )
    planning_profile = structured_plan.get("planning_profile")
    if not isinstance(planning_profile, dict) or planning_profile != planning_policy.metadata():
        issues.append("Structured plan planning_profile does not match the frozen run profile.")

    for field_path in profiled_execution_field_paths(structured_plan):
        issues.append(f"Profiled implementation plan must not contain execution field: {field_path}")

    actual_stage_ids = _string_items(structured_plan.get("verification_stage_ids"))
    expected_stage_ids = list(verification_policy.stage_ids)
    if actual_stage_ids != expected_stage_ids:
        issues.append("Implementation plan verification_stage_ids do not match the frozen project profile.")

    test_intents = _dict_items(structured_plan.get("test_intents"))
    if not test_intents:
        issues.append("Implementation plan has no test intent block.")
    allowed_stage_ids = set(verification_policy.stage_ids)
    for item in test_intents:
        stage_id = str(item.get("stage_id") or "").strip()
        if not stage_id:
            issues.append("A profiled test intent is missing stage_id.")
        elif stage_id not in allowed_stage_ids:
            issues.append(f"Test intent references an unsupported verification stage: {stage_id}")
    if plan_version != LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION:
        issues.extend(
            implementation_signature_contract_issues(
                structured_plan.get("implementation_signature_contract"),
                planning_policy=planning_policy,
                mappings=_dict_items(structured_plan.get("design_mappings")),
                class_graph=class_graph,
                enforce_design_method_arity=(
                    plan_version != COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
                ),
                reject_placeholder_only=(
                    plan_version == COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
                ),
                bind_to_mapped_symbol=(
                    plan_version == COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
                ),
            )
        )
    return issues


def is_profiled_work_packet_schema(value: object) -> bool:
    """Nhận diện cả packet hiện hành và packet profiled lịch sử."""

    return value in {
        PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
        LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    }


def profiled_execution_field_paths(value: object, *, path: str = "$") -> list[str]:
    """Tìm field thực thi bị cấm mà không diễn giải nội dung do LLM sinh."""

    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if str(key) in _PROHIBITED_EXECUTION_FIELDS:
                paths.append(child_path)
            paths.extend(profiled_execution_field_paths(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(profiled_execution_field_paths(item, path=f"{path}[{index}]"))
    return paths


def build_verification_stage_policy(*, project: ProjectProfile) -> VerificationStagePolicy:
    """Tạo policy chỉ chứa stage ID, không chứa dữ liệu có thể thực thi."""

    stage_ids = _ACTIVE_STAGE_IDS_BY_PROJECT.get(project.profile_id)
    if not stage_ids:
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Project profile has no active verification suite: {project.profile_id}",
        )
    unsupported = [stage_id for stage_id in stage_ids if stage_id not in project.verification_stage_ids]
    if unsupported:
        raise HarnessError(
            "verification_stage_policy_invalid",
            f"Active verification suite references stages outside the project catalog: {unsupported}",
        )
    return VerificationStagePolicy(
        project_profile_id=project.profile_id,
        policy_version=VERIFICATION_STAGE_POLICY_VERSION,
        stage_ids=stage_ids,
    )


def verification_stage_policy_from_snapshot(
    snapshot: ProfileSnapshotV1,
    *,
    run_id: str | None = None,
) -> VerificationStagePolicy:
    """Resolve ordered stage IDs từ frozen snapshot thay vì từ output LLM."""

    if not snapshot.is_workspace_bound:
        raise HarnessError(
            "profile_snapshot_mismatch",
            "Phase 3 verification planning requires a workspace-bound profile snapshot.",
            run_id=run_id,
        )
    project, _ = snapshot.resolve_profiles(run_id=run_id)
    try:
        return build_verification_stage_policy(project=project)
    except HarnessError as exc:
        if run_id and exc.run_id is None:
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        raise


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
