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


LEGACY_VERIFICATION_STAGE_POLICY_VERSION = "verification_stage_policy_v1"
VERIFICATION_STAGE_POLICY_VERSION = "verification_stage_policy_v2"
LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v2"
PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v3"
PREVIOUS_PROFILED_IMPLEMENTATION_PLAN_VERSION = PROFILED_IMPLEMENTATION_PLAN_VERSION
PREVIOUS_COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v4"
COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION = "implementation_plan_structured_v5"
LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION = "developer_work_packet_profiled_v1"
PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION = "developer_work_packet_profiled_v2"
PREVIOUS_UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION = "developer_work_packet_profiled_v3"
UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION = "developer_work_packet_profiled_v3"
_ACTIVE_STAGE_IDS_BY_POLICY = {
    LEGACY_VERIFICATION_STAGE_POLICY_VERSION: {
        "typescript_fullstack_v1": ("typecheck", "unit"),
    },
    VERIFICATION_STAGE_POLICY_VERSION: {
        "typescript_fullstack_v1": (
            "database_ready",
            "migrate_and_seed",
            "typecheck",
            "unit",
            "api_database_integration",
            "frontend_build",
            "service_health",
            "browser_e2e",
            "cleanup",
        ),
    },
}
DEVELOPER_INTENT_STAGE_OWNER = "developer_intent"
QA_INTENT_STAGE_OWNER = "qa_intent"
HARNESS_GLOBAL_STAGE_OWNER = "harness_global"
_STAGE_OWNERS_BY_POLICY = {
    LEGACY_VERIFICATION_STAGE_POLICY_VERSION: {
        "typescript_fullstack_v1": {
            "typecheck": DEVELOPER_INTENT_STAGE_OWNER,
            "unit": DEVELOPER_INTENT_STAGE_OWNER,
        },
    },
    VERIFICATION_STAGE_POLICY_VERSION: {
        "typescript_fullstack_v1": {
            "database_ready": HARNESS_GLOBAL_STAGE_OWNER,
            "migrate_and_seed": HARNESS_GLOBAL_STAGE_OWNER,
            "typecheck": DEVELOPER_INTENT_STAGE_OWNER,
            "unit": DEVELOPER_INTENT_STAGE_OWNER,
            "api_database_integration": HARNESS_GLOBAL_STAGE_OWNER,
            "frontend_build": HARNESS_GLOBAL_STAGE_OWNER,
            "service_health": HARNESS_GLOBAL_STAGE_OWNER,
            "browser_e2e": QA_INTENT_STAGE_OWNER,
            "cleanup": HARNESS_GLOBAL_STAGE_OWNER,
        },
    },
}
# What each active stage can observe; fixed by the profile's protected tsconfig/vitest config.
_STAGE_CAPABILITIES_BY_PROJECT = {
    "typescript_fullstack_v1": {
        "typecheck": "TypeScript compiler over the workspace: proves types, imports and exported "
        "signatures of .ts/.tsx sources; runs no code and does not read stylesheets.",
        "unit": "Vitest in a Node environment with no DOM and no browser: runs exported TypeScript "
        "functions and React rendered to static markup; cannot observe focus movement, keyboard "
        "events, live-region announcements, CSS or viewport-dependent layout.",
        "database_ready": "Starts an isolated PostgreSQL Compose project and waits for its healthcheck.",
        "migrate_and_seed": "Applies migrations to an empty database, seeds twice and checks deterministic data setup.",
        "api_database_integration": "Runs the target's database smoke/integration contract against the isolated database.",
        "frontend_build": "Builds the Vite frontend and therefore observes imports, stylesheets and production bundling.",
        "service_health": "Validates the protected web/API runtime bootstrap before browser execution.",
        "browser_e2e": "Runs Playwright Chromium and observes DOM interaction, focus, keyboard, CSS and viewport layout.",
        "cleanup": "Always tears down the isolated database resources, including after failure or timeout.",
    },
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
    stage_ownership: tuple[tuple[str, str], ...] = ()

    def owner_for_stage(self, stage_id: str) -> str:
        explicit = dict(self.stage_ownership)
        if stage_id in explicit:
            return explicit[stage_id]
        return str(
            _STAGE_OWNERS_BY_POLICY.get(self.policy_version, {})
            .get(self.project_profile_id, {})
            .get(stage_id, "")
        )

    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "project_profile_id": self.project_profile_id,
            "policy_version": self.policy_version,
            "stage_ids": list(self.stage_ids),
        }
        if self.stage_ownership:
            metadata["stage_ownership"] = {
                stage_id: self.owner_for_stage(stage_id) for stage_id in self.stage_ids
            }
        return metadata

    def prompt_context(self) -> dict[str, object]:
        capabilities = _STAGE_CAPABILITIES_BY_PROJECT.get(self.project_profile_id, {})
        return {
            **self.metadata(),
            "stage_capabilities": {
                stage_id: capabilities[stage_id] for stage_id in self.stage_ids if stage_id in capabilities
            },
        }


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
        PREVIOUS_COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
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
        elif verification_policy.owner_for_stage(stage_id) != DEVELOPER_INTENT_STAGE_OWNER:
            issues.append(
                f"Developer test intent cannot claim verification stage owned by "
                f"{verification_policy.owner_for_stage(stage_id)}: {stage_id}"
            )
    if plan_version != LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION:
        issues.extend(
            implementation_signature_contract_issues(
                structured_plan.get("implementation_signature_contract"),
                planning_policy=planning_policy,
                mappings=_dict_items(structured_plan.get("design_mappings")),
                class_graph=class_graph,
                enforce_design_method_arity=(
                    plan_version not in {
                        COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                        PREVIOUS_COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                    }
                ),
                reject_placeholder_only=(
                    plan_version in {
                        COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                        PREVIOUS_COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                    }
                ),
                bind_to_mapped_symbol=(
                    plan_version in {
                        COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                        PREVIOUS_COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                    }
                ),
            )
        )
    return issues


def is_profiled_work_packet_schema(value: object) -> bool:
    """Nhận diện cả packet hiện hành và packet profiled lịch sử."""

    return value in {
        UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
        PREVIOUS_UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
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

    return build_verification_stage_policy_version(
        project=project,
        policy_version=LEGACY_VERIFICATION_STAGE_POLICY_VERSION,
    )


def build_verification_stage_policy_version(
    *,
    project: ProjectProfile,
    policy_version: str,
) -> VerificationStagePolicy:
    """Build one explicitly pinned verification policy without changing historical runs."""

    policies = _ACTIVE_STAGE_IDS_BY_POLICY.get(policy_version)
    stage_ids = policies.get(project.profile_id) if policies else None
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
        policy_version=policy_version,
        stage_ids=stage_ids,
        stage_ownership=(
            tuple(
                _STAGE_OWNERS_BY_POLICY.get(policy_version, {})
                .get(project.profile_id, {})
                .items()
            )
            if policy_version == VERIFICATION_STAGE_POLICY_VERSION
            else ()
        ),
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
        return build_verification_stage_policy_version(
            project=project,
            policy_version=str(
                getattr(snapshot, "verification_policy_version", "")
                or LEGACY_VERIFICATION_STAGE_POLICY_VERSION
            ),
        )
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
