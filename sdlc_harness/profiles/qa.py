"""Policy sinh và ghi QA test được suy ra từ project/language profile.

Policy này không chứa command thực thi. Nó chỉ khóa framework, component root
và test suffix mà QA được phép dùng; Verification Runner vẫn sở hữu argv/cwd,
environment và timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.errors import HarnessError
from .planning import PlanningPathPolicy, planning_path_policy_from_snapshot

if TYPE_CHECKING:
    from .snapshot import ProfileSnapshotV1


QA_TEST_POLICY_VERSION = "qa_test_policy_v1"


@dataclass(frozen=True)
class QaComponentTestRule:
    """Root và suffix test do một component sở hữu."""

    component_id: str
    test_root: str
    test_suffix: str


@dataclass(frozen=True)
class QaTestPolicy:
    """Contract bất biến cho QA unit/acceptance test của một profiled run."""

    project_profile_id: str
    language_profile_id: str
    policy_version: str
    framework: str
    component_rules: tuple[QaComponentTestRule, ...]
    planning_policy: PlanningPathPolicy

    def metadata(self) -> dict[str, object]:
        return {
            "project_profile_id": self.project_profile_id,
            "language_profile_id": self.language_profile_id,
            "policy_version": self.policy_version,
            "framework": self.framework,
            "components": [
                {
                    "component_id": rule.component_id,
                    "test_root": rule.test_root,
                    "test_suffix": rule.test_suffix,
                }
                for rule in self.component_rules
            ],
        }

    def prompt_context(self) -> dict[str, object]:
        return self.metadata()

    def normalize_test_path(self, value: object) -> str:
        """Trả canonical co-located unit-test path hoặc chuỗi rỗng."""

        path = self.planning_policy.normalize_editable_path(value)
        if not path or not self.planning_policy.is_test_path(path):
            return ""
        component_id = self.planning_policy.component_for_path(path)
        for rule in self.component_rules:
            if (
                rule.component_id == component_id
                and path.startswith(f"{rule.test_root}/")
                and path.endswith(rule.test_suffix)
            ):
                return path
        return ""


def build_qa_test_policy(*, planning_policy: PlanningPathPolicy) -> QaTestPolicy:
    """Tạo QA policy cho profile hiện được A3 hỗ trợ."""

    if (
        planning_policy.project_profile_id != "typescript_fullstack_v1"
        or planning_policy.language_profile_id != "typescript_v1"
    ):
        raise HarnessError(
            "qa_test_policy_not_supported",
            "QA test policy is not supported for the selected project/language profile.",
        )
    rules = tuple(
        QaComponentTestRule(
            component_id=rule.component_id,
            test_root=rule.source_root,
            test_suffix=rule.test_suffix,
        )
        for rule in planning_policy.component_rules
    )
    return QaTestPolicy(
        project_profile_id=planning_policy.project_profile_id,
        language_profile_id=planning_policy.language_profile_id,
        policy_version=QA_TEST_POLICY_VERSION,
        framework="vitest",
        component_rules=rules,
        planning_policy=planning_policy,
    )


def qa_test_policy_from_snapshot(
    snapshot: ProfileSnapshotV1,
    *,
    run_id: str | None = None,
) -> QaTestPolicy:
    """Resolve QA policy từ frozen snapshot đã bind workspace."""

    planning_policy = planning_path_policy_from_snapshot(snapshot, run_id=run_id)
    try:
        return build_qa_test_policy(planning_policy=planning_policy)
    except HarnessError as exc:
        if run_id and exc.run_id is None:
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        raise
