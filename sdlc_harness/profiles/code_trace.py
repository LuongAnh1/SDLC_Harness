"""Policy Code Trace được suy ra từ immutable project/language profile.

Policy chỉ quyết định file nào được xem là source/test và extractor strategy nào
được phép dùng. Việc chạy Node/TypeScript Compiler thuộc tầng Validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.errors import HarnessError
from .planning import PlanningPathPolicy, planning_path_policy_from_snapshot

if TYPE_CHECKING:
    from .snapshot import ProfileSnapshotV1


CODE_TRACE_POLICY_VERSION = "code_trace_policy_v1"


@dataclass(frozen=True)
class CodeTracePolicy:
    """Contract bất biến cho discovery và symbol extraction của Code Trace."""

    project_profile_id: str
    language_profile_id: str
    policy_version: str
    symbol_extractor_id: str
    planning_policy: PlanningPathPolicy

    def metadata(self) -> dict[str, object]:
        return {
            "project_profile_id": self.project_profile_id,
            "language_profile_id": self.language_profile_id,
            "policy_version": self.policy_version,
            "symbol_extractor_id": self.symbol_extractor_id,
        }

    def normalize_source_path(self, value: object) -> str:
        """Trả canonical TypeScript production source path hoặc chuỗi rỗng."""

        return self.planning_policy.normalize_mapping_path(value)

    def normalize_test_path(self, value: object) -> str:
        """Trả co-located unit-test path; browser E2E chưa thuộc Chặng A."""

        path = self.planning_policy.normalize_editable_path(value)
        if not path or not self.planning_policy.is_test_path(path):
            return ""
        component_id = self.planning_policy.component_for_path(path)
        for rule in self.planning_policy.component_rules:
            if (
                rule.component_id == component_id
                and path.startswith(f"{rule.source_root}/")
                and path.endswith(rule.test_suffix)
            ):
                return path
        return ""


def build_code_trace_policy(
    *,
    planning_policy: PlanningPathPolicy,
    symbol_extractor_id: str,
) -> CodeTracePolicy:
    """Tạo Code Trace policy cho profile hiện được A3 hỗ trợ."""

    if (
        planning_policy.project_profile_id != "typescript_fullstack_v1"
        or planning_policy.language_profile_id != "typescript_v1"
        or symbol_extractor_id != "typescript_compiler_ast_v1"
    ):
        raise HarnessError(
            "code_trace_policy_not_supported",
            "Code Trace policy is not supported for the selected profile/extractor.",
        )
    return CodeTracePolicy(
        project_profile_id=planning_policy.project_profile_id,
        language_profile_id=planning_policy.language_profile_id,
        policy_version=CODE_TRACE_POLICY_VERSION,
        symbol_extractor_id=symbol_extractor_id,
        planning_policy=planning_policy,
    )


def code_trace_policy_from_snapshot(
    snapshot: ProfileSnapshotV1,
    *,
    run_id: str | None = None,
) -> CodeTracePolicy:
    """Resolve Code Trace policy từ frozen snapshot đã bind workspace."""

    planning_policy = planning_path_policy_from_snapshot(snapshot, run_id=run_id)
    _, language = snapshot.resolve_profiles(run_id=run_id)
    try:
        return build_code_trace_policy(
            planning_policy=planning_policy,
            symbol_extractor_id=language.symbol_extractor_id,
        )
    except HarnessError as exc:
        if run_id and exc.run_id is None:
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        raise
