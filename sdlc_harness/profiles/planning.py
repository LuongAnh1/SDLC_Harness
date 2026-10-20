"""Chính sách đường dẫn Phase 3 được khóa theo project/language profile.

Module này chỉ mô tả và kiểm tra target file. Nó không chứa shell command và
không đọc output LLM để quyết định topology của repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING

from ..core.errors import HarnessError
from .model import LanguageProfile, ProjectProfile

if TYPE_CHECKING:
    from .snapshot import ProfileSnapshotV1


PLANNING_PATH_POLICY_VERSION = "planning_path_policy_v1"
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[a-zA-Z]:")


@dataclass(frozen=True)
class ComponentPlanningRule:
    """Quy tắc source/test target tất định cho một component có code class."""

    component_id: str
    source_root: str
    source_suffix: str
    test_suffix: str
    stereotype_aliases: tuple[str, ...]
    class_name_suffixes: tuple[str, ...]


@dataclass(frozen=True)
class PlanningPathPolicy:
    """Policy bất biến dùng chung bởi Developer.plan và Validator.review_plan."""

    project_profile_id: str
    language_profile_id: str
    policy_version: str
    component_rules: tuple[ComponentPlanningRule, ...]
    component_roots: tuple[tuple[str, tuple[str, ...]], ...]
    protected_paths: tuple[str, ...]
    language_source_suffixes: tuple[str, ...]
    language_test_suffixes: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "project_profile_id": self.project_profile_id,
            "language_profile_id": self.language_profile_id,
            "policy_version": self.policy_version,
        }

    def prompt_context(self) -> dict[str, object]:
        return {
            **self.metadata(),
            "components": [
                {
                    "component_id": rule.component_id,
                    "source_root": rule.source_root,
                    "source_suffix": rule.source_suffix,
                    "test_suffix": rule.test_suffix,
                }
                for rule in self.component_rules
            ],
            "special_roots": {
                "database_schema_and_migrations": {
                    "root": "packages/db/prisma",
                    "suffixes": [".prisma", ".sql"],
                },
                "browser_e2e": {
                    "root": "tests/e2e",
                    "suffixes": [".spec.ts", ".spec.tsx"],
                },
            },
        }

    def resolve_class_target(
        self,
        *,
        node: dict[str, object],
        target_files: list[str],
        run_id: str | None = None,
    ) -> tuple[str, str]:
        """Resolve một class sang `(path, component_id)` hoặc fail closed."""

        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            raise HarnessError(
                "implementation_target_unclassified",
                "Design class is missing className/id and cannot be routed to a project component.",
                run_id=run_id,
            )

        existing = []
        for raw_path in target_files:
            path = self.normalize_mapping_path(raw_path)
            if path and self.symbol_owner(path) == class_name:
                existing.append((path, self.component_for_path(path, mapping_only=True)))
        existing = sorted(set(existing))
        if len(existing) == 1:
            return existing[0]

        if existing:
            try:
                inferred = self.infer_component(node=node, class_name=class_name, run_id=run_id)
            except HarnessError as exc:
                if exc.error_code != "implementation_target_unclassified":
                    raise
                candidates = ", ".join(path for path, _ in existing)
                raise HarnessError(
                    "implementation_target_ambiguous",
                    f"Design class `{class_name}` matches multiple source files: {candidates}",
                    run_id=run_id,
                ) from exc
            narrowed = [item for item in existing if item[1] == inferred]
            if len(narrowed) == 1:
                return narrowed[0]
            candidates = ", ".join(path for path, _ in existing)
            raise HarnessError(
                "implementation_target_ambiguous",
                f"Design class `{class_name}` matches multiple source files: {candidates}",
                run_id=run_id,
            )

        inferred = self.infer_component(node=node, class_name=class_name, run_id=run_id)
        rule = self.rule(inferred)
        return f"{rule.source_root}/{class_name}{rule.source_suffix}", inferred

    def infer_component(
        self,
        *,
        node: dict[str, object],
        class_name: str,
        run_id: str | None = None,
    ) -> str:
        explicit_values = [
            str(node.get(key) or "").strip()
            for key in ("component_id", "componentId", "component", "stereotype")
            if str(node.get(key) or "").strip()
        ]
        explicit_components = {
            component
            for value in explicit_values
            for component in [self._component_from_alias(value)]
            if component
        }
        if any(key in node and str(node.get(key) or "").strip() for key in ("component_id", "componentId")):
            raw_component = str(node.get("component_id") or node.get("componentId") or "").strip()
            if raw_component and not self._component_from_alias(raw_component):
                raise HarnessError(
                    "implementation_target_component_invalid",
                    f"Design class `{class_name}` declares unsupported component `{raw_component}`.",
                    run_id=run_id,
                )

        name_components = {
            rule.component_id
            for rule in self.component_rules
            if any(class_name.lower().endswith(suffix.lower()) for suffix in rule.class_name_suffixes)
        }
        if len(explicit_components) > 1 or len(name_components) > 1:
            raise HarnessError(
                "implementation_target_ambiguous",
                f"Design class `{class_name}` resolves to more than one project component.",
                run_id=run_id,
            )
        explicit = next(iter(explicit_components), "")
        named = next(iter(name_components), "")
        if explicit and named and explicit != named:
            raise HarnessError(
                "implementation_target_conflict",
                f"Design class `{class_name}` has conflicting component hints: `{explicit}` and `{named}`.",
                run_id=run_id,
            )
        component_id = explicit or named
        if not component_id:
            raise HarnessError(
                "implementation_target_unclassified",
                (
                    f"Design class `{class_name}` has no unique existing symbol, supported stereotype/component "
                    "hint, or recognized class-name suffix. Update the Phase 2 design before planning."
                ),
                run_id=run_id,
            )
        return component_id

    def normalize_editable_path(self, value: object) -> str:
        path = _canonical_path(value)
        if not path:
            return ""
        component_id, root = self._owned_root(path)
        if not component_id:
            return ""
        if root == "packages/db/prisma":
            return path if path.endswith((".prisma", ".sql")) else ""
        if component_id == "e2e":
            return path if path.endswith((".spec.ts", ".spec.tsx")) else ""
        rule = self.rule(component_id)
        suffixes = tuple(sorted((*self.language_test_suffixes, rule.source_suffix), key=len, reverse=True))
        return path if path.endswith(suffixes) else ""

    def normalize_mapping_path(self, value: object) -> str:
        path = self.normalize_editable_path(value)
        if not path or self.is_test_path(path):
            return ""
        component_id, root = self._owned_root(path)
        if component_id == "e2e" or root == "packages/db/prisma":
            return ""
        rule = self.rule(component_id)
        return path if path.endswith(rule.source_suffix) else ""

    def component_for_path(self, value: object, *, mapping_only: bool = False) -> str:
        path = self.normalize_mapping_path(value) if mapping_only else self.normalize_editable_path(value)
        if not path:
            return ""
        component_id, _ = self._owned_root(path)
        return component_id

    def is_test_path(self, value: object) -> bool:
        path = _canonical_path(value)
        if not path:
            return False
        return path.endswith(tuple(sorted(self.language_test_suffixes, key=len, reverse=True)))

    def test_path_for_source(self, source_path: str, test_stem: str) -> str:
        path = self.normalize_mapping_path(source_path)
        if not path:
            return ""
        component_id = self.component_for_path(path, mapping_only=True)
        rule = self.rule(component_id)
        parent = path.rsplit("/", 1)[0]
        return f"{parent}/{test_stem}{rule.test_suffix}"

    def symbol_owner(self, value: object) -> str:
        path = _canonical_path(value)
        if not path:
            return ""
        name = path.rsplit("/", 1)[-1]
        suffixes = tuple(
            sorted(
                (*self.language_test_suffixes, *self.language_source_suffixes, ".prisma", ".sql"),
                key=len,
                reverse=True,
            )
        )
        for suffix in suffixes:
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return PurePosixPath(name).stem

    def rule(self, component_id: str) -> ComponentPlanningRule:
        for rule in self.component_rules:
            if rule.component_id == component_id:
                return rule
        raise HarnessError(
            "implementation_target_component_invalid",
            f"Project component cannot own class source targets: {component_id}",
        )

    def _component_from_alias(self, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        for rule in self.component_rules:
            aliases = {rule.component_id, *rule.stereotype_aliases}
            if normalized in aliases:
                return rule.component_id
        return ""

    def _owned_root(self, path: str) -> tuple[str, str]:
        matches = [
            (component_id, root)
            for component_id, roots in self.component_roots
            for root in roots
            if path == root or path.startswith(f"{root}/")
        ]
        if not matches:
            return "", ""
        return max(matches, key=lambda item: len(item[1]))


def build_planning_path_policy(
    *,
    project: ProjectProfile,
    language: LanguageProfile,
) -> PlanningPathPolicy:
    """Build policy đã version hóa cho đúng cặp project/language profile."""

    if project.profile_id != "typescript_fullstack_v1" or language.profile_id != "typescript_v1":
        raise HarnessError(
            "planning_path_policy_not_supported",
            f"Planning path policy is not supported for {project.profile_id}/{language.profile_id}.",
        )
    if project.language_profile_id != language.profile_id:
        raise HarnessError(
            "planning_path_policy_mismatch",
            "Project and language profiles do not belong to the same planning policy.",
        )

    rules = (
        ComponentPlanningRule(
            component_id="web",
            source_root="apps/web/src",
            source_suffix=".tsx",
            test_suffix=".test.tsx",
            stereotype_aliases=("frontend", "ui", "presentation", "view", "page", "form"),
            class_name_suffixes=("View", "Page", "Component", "Form"),
        ),
        ComponentPlanningRule(
            component_id="api",
            source_root="apps/api/src",
            source_suffix=".ts",
            test_suffix=".test.ts",
            stereotype_aliases=("backend", "controller", "service", "usecase", "handler", "port", "guard", "boundary", "control"),
            class_name_suffixes=("Controller", "Service", "UseCase", "Handler", "Port", "Guard", "Boundary"),
        ),
        ComponentPlanningRule(
            component_id="contracts",
            source_root="packages/contracts/src",
            source_suffix=".ts",
            test_suffix=".test.ts",
            stereotype_aliases=("contract", "dto", "request", "response", "schema", "model"),
            class_name_suffixes=("Dto", "DTO", "Request", "Response", "Contract", "Schema", "Model"),
        ),
        ComponentPlanningRule(
            component_id="database",
            source_root="packages/db/src",
            source_suffix=".ts",
            test_suffix=".test.ts",
            stereotype_aliases=("db", "persistence", "repository", "entity", "record", "store"),
            class_name_suffixes=("Repository", "Entity", "Record", "Store", "Persistence"),
        ),
    )
    roots = tuple((component.component_id, component.editable_roots) for component in project.components)
    available_roots = {root for _, component_roots in roots for root in component_roots}
    missing_roots = [rule.source_root for rule in rules if rule.source_root not in available_roots]
    if missing_roots or "packages/db/prisma" not in available_roots or "tests/e2e" not in available_roots:
        raise HarnessError(
            "planning_path_policy_mismatch",
            f"Project profile is missing planning roots: {sorted(missing_roots)}",
        )
    return PlanningPathPolicy(
        project_profile_id=project.profile_id,
        language_profile_id=language.profile_id,
        policy_version=PLANNING_PATH_POLICY_VERSION,
        component_rules=rules,
        component_roots=roots,
        protected_paths=project.protected_paths,
        language_source_suffixes=language.source_suffixes,
        language_test_suffixes=language.test_suffixes,
    )


def planning_path_policy_from_snapshot(
    snapshot: ProfileSnapshotV1,
    *,
    run_id: str | None = None,
) -> PlanningPathPolicy:
    """Resolve policy từ frozen snapshot, không nhận identity từ plan/LLM."""

    if not snapshot.is_workspace_bound:
        raise HarnessError(
            "profile_snapshot_mismatch",
            "Phase 3 planning paths require a workspace-bound profile snapshot.",
            run_id=run_id,
        )
    project, language = snapshot.resolve_profiles(run_id=run_id)
    try:
        return build_planning_path_policy(project=project, language=language)
    except HarnessError as exc:
        if run_id and exc.run_id is None:
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        raise


def _canonical_path(value: object) -> str:
    raw_value = str(value or "")
    if not raw_value or raw_value != raw_value.strip() or "\\" in raw_value:
        return ""
    path = raw_value
    if (
        path.startswith("/")
        or path.endswith("/")
        or "//" in path
        or _WINDOWS_DRIVE_PATTERN.match(path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        return ""
    candidate = PurePosixPath(path)
    return path if not candidate.is_absolute() and str(candidate) == path else ""
