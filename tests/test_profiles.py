"""Kiểm thử contract của registry profile ngôn ngữ và dự án."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
import json
import unittest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import (
    ComponentProfile,
    LanguageProfile,
    PROFILE_SNAPSHOT_VERSION,
    ProfileRegistry,
    ProfileSnapshotV1,
    ProjectProfile,
    build_profile_snapshot,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
    list_project_profile_ids,
    profile_status_metadata,
    public_allowed_actions,
    public_run_summary,
    read_profile_snapshot,
)


def _language() -> LanguageProfile:
    return LanguageProfile(
        profile_id="typescript_v1",
        schema_version="language_profile_v1",
        source_suffixes=(".ts", ".tsx"),
        test_suffixes=(".test.ts", ".spec.ts"),
        prompt_directive_id="typescript_prompt_v1",
        symbol_extractor_id="typescript_compiler_ast_v1",
        diagnostic_classifier_id="typescript_diagnostics_v1",
    )


def _project(**changes: object) -> ProjectProfile:
    profile = ProjectProfile(
        profile_id="typescript_fullstack_v1",
        schema_version="project_profile_v1",
        language_profile_id="typescript_v1",
        target_manifest_schema="target_profile_manifest_v1",
        components=(ComponentProfile(component_id="web", editable_roots=("apps/web/src",)),),
        protected_paths=("package.json", ".sdlc"),
        required_paths=("package.json", ".sdlc/target-profile.json"),
        verification_stage_ids=("typecheck", "unit"),
    )
    return replace(profile, **changes)


class ProfileRegistryTests(unittest.TestCase):
    def test_builtin_typescript_fullstack_profile_is_complete(self) -> None:
        language = get_language_profile("typescript_v1")
        project = get_project_profile("typescript_fullstack_v1")

        self.assertEqual(list_project_profile_ids(), ("typescript_fullstack_v1",))
        self.assertEqual(project.language_profile_id, language.profile_id)
        self.assertEqual(language.source_suffixes, (".ts", ".tsx"))
        self.assertEqual(
            language.test_suffixes,
            (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"),
        )
        self.assertEqual(
            {component.component_id for component in project.components},
            {"web", "api", "contracts", "database", "e2e"},
        )
        self.assertEqual(
            project.editable_roots,
            (
                "apps/web/src",
                "apps/api/src",
                "packages/contracts/src",
                "packages/db/src",
                "packages/db/prisma",
                "tests/e2e",
            ),
        )
        self.assertEqual(project.verification_stage_ids[-1], "cleanup")
        self.assertEqual(
            build_verification_stage_policy(project=project).stage_ids,
            ("typecheck", "unit"),
        )
        self.assertIn(".sdlc/target-profile.json", project.required_paths)
        self.assertEqual(
            project.protected_paths,
            (
                ".sdlc",
                ".github",
                "package.json",
                "package-lock.json",
                "compose.yaml",
                "tsconfig.json",
                "tsconfig.base.json",
                "vitest.config.ts",
                "playwright.config.ts",
                "apps/web/package.json",
                "apps/web/tsconfig.json",
                "apps/web/vite.config.ts",
                "apps/api/package.json",
                "apps/api/tsconfig.json",
                "packages/contracts/package.json",
                "packages/contracts/tsconfig.json",
                "packages/db/package.json",
                "packages/db/prisma7.config.ts",
                "packages/db/tsconfig.json",
            ),
        )

    def test_profiles_and_nested_collections_are_immutable(self) -> None:
        roots = ["apps/web/src"]
        component = ComponentProfile(component_id="web", editable_roots=roots)  # type: ignore[arg-type]
        roots.append("apps/api/src")

        self.assertEqual(component.editable_roots, ("apps/web/src",))
        with self.assertRaises(FrozenInstanceError):
            setattr(component, "component_id", "api")

    def test_unknown_profiles_fail_closed_with_stable_codes(self) -> None:
        with self.assertRaises(HarnessError) as language_ctx:
            get_language_profile("unknown_v1")
        self.assertEqual(language_ctx.exception.error_code, "language_profile_not_supported")

        with self.assertRaises(HarnessError) as project_ctx:
            get_project_profile("unknown_v1")
        self.assertEqual(project_ctx.exception.error_code, "project_profile_not_supported")

    def test_unknown_contract_versions_fail_closed(self) -> None:
        cases = (
            lambda: replace(_language(), schema_version="language_profile_v2"),
            lambda: _project(schema_version="project_profile_v2"),
            lambda: _project(target_manifest_schema="target_profile_manifest_v2"),
        )
        for build in cases:
            with self.subTest(build=build), self.assertRaises(HarnessError) as ctx:
                build()
            self.assertEqual(ctx.exception.error_code, "profile_registry_invalid")

    def test_invalid_repository_paths_are_rejected(self) -> None:
        invalid_paths = (
            "/apps/web/src",
            "C:/apps/web/src",
            "apps\\web\\src",
            "apps/../api",
            "apps/./web",
            "apps//web",
            "apps/web/",
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(HarnessError) as ctx:
                ComponentProfile(component_id="web", editable_roots=(path,))
            self.assertEqual(ctx.exception.error_code, "profile_registry_invalid")

    def test_duplicate_and_overlapping_contract_values_are_rejected(self) -> None:
        cases = (
            lambda: ComponentProfile(component_id="web", editable_roots=("apps/web", "apps/web")),
            lambda: ComponentProfile(component_id="web", editable_roots=("apps/web", "apps/web/src")),
            lambda: _project(verification_stage_ids=("unit", "unit")),
            lambda: _project(
                components=(
                    ComponentProfile(component_id="web", editable_roots=("apps/web/src",)),
                    ComponentProfile(component_id="web", editable_roots=("apps/other/src",)),
                )
            ),
            lambda: _project(
                components=(
                    ComponentProfile(component_id="web", editable_roots=("apps/web",)),
                    ComponentProfile(component_id="api", editable_roots=("apps/web/src",)),
                )
            ),
            lambda: _project(
                components=(ComponentProfile(component_id="web", editable_roots=("apps/web",)),),
                protected_paths=("apps/web/package.json",),
            ),
        )
        for build in cases:
            with self.subTest(build=build), self.assertRaises(HarnessError) as ctx:
                build()
            self.assertEqual(ctx.exception.error_code, "profile_registry_invalid")

    def test_registry_rejects_duplicates_and_missing_language_reference(self) -> None:
        language = _language()
        cases = (
            lambda: ProfileRegistry(
                language_profiles=(language, language),
                project_profiles=(_project(),),
            ),
            lambda: ProfileRegistry(
                language_profiles=(language,),
                project_profiles=(_project(), _project()),
            ),
            lambda: ProfileRegistry(
                language_profiles=(language,),
                project_profiles=(_project(language_profile_id="missing_v1"),),
            ),
        )
        for build in cases:
            with self.subTest(build=build), self.assertRaises(HarnessError) as ctx:
                build()
            self.assertEqual(ctx.exception.error_code, "profile_registry_invalid")

    def test_contract_contains_stage_ids_but_no_executable_commands_or_java_assumptions(self) -> None:
        payload = json.dumps(
            {
                "language": asdict(get_language_profile("typescript_v1")),
                "project": asdict(get_project_profile("typescript_fullstack_v1")),
            },
            ensure_ascii=False,
        ).lower()

        for forbidden_key in ('"command"', '"commands"', '"argv"', '"shell"'):
            self.assertNotIn(forbidden_key, payload)
        for java_assumption in ("src/main/java", "src/test/java", "javac", "scripts/test.ps1"):
            self.assertNotIn(java_assumption, payload)

    def test_profile_snapshot_round_trips_and_allows_future_additive_fields(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        )
        payload: dict[str, object] = {**snapshot.to_dict(), "future_workspace_binding": {"base_sha": "abc"}}

        restored = ProfileSnapshotV1.from_dict(payload)
        project, language = restored.resolve_profiles()

        self.assertEqual(restored, snapshot)
        self.assertEqual(restored.version, PROFILE_SNAPSHOT_VERSION)
        self.assertEqual(project.profile_id, "typescript_fullstack_v1")
        self.assertEqual(language.profile_id, "typescript_v1")
        self.assertEqual(read_profile_snapshot({"profile_snapshot": payload}), snapshot)

    def test_profile_snapshot_rejects_invalid_shape_and_unknown_version(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        ).to_dict()
        cases: tuple[object, ...] = (
            [],
            {},
            {**snapshot, "version": "profile_snapshot_v2"},
            {**snapshot, "target_remote": ""},
            {**snapshot, "target_base_branch": " main "},
        )

        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(HarnessError) as context:
                ProfileSnapshotV1.from_dict(payload)
            self.assertEqual(context.exception.error_code, "profile_snapshot_invalid")

    def test_profile_snapshot_rejects_registry_mismatch(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        )
        cases = (
            replace(snapshot, project_profile_id="removed_profile_v1"),
            replace(snapshot, project_profile_schema="project_profile_v2"),
            replace(snapshot, language_profile_id="removed_language_v1"),
            replace(snapshot, language_profile_schema="language_profile_v2"),
        )

        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(HarnessError) as context:
                candidate.resolve_profiles(run_id="run_profile")
            self.assertEqual(context.exception.error_code, "profile_snapshot_mismatch")
            self.assertEqual(context.exception.run_id, "run_profile")

    def test_profile_status_metadata_classifies_profiled_legacy_and_invalid_runs(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        )

        self.assertEqual(
            profile_status_metadata({"run_id": "run_profile", "profile_snapshot": snapshot.to_dict()}),
            {
                "classification": "profiled",
                "snapshot_version": "profile_snapshot_v1",
                "project_profile_id": "typescript_fullstack_v1",
                "language_profile_id": "typescript_v1",
            },
        )
        self.assertEqual(
            profile_status_metadata({"run_id": "run_legacy"})["classification"],
            "legacy_java_v1",
        )
        self.assertEqual(
            profile_status_metadata(
                {"run_id": "run_invalid", "profile_snapshot": {"version": "profile_snapshot_v2"}}
            )["classification"],
            "invalid",
        )

    def test_public_projection_treats_null_snapshot_as_legacy(self) -> None:
        state = {
            "run_id": "run_legacy",
            "profile_snapshot": None,
            "summary_for_user": "Approve this checkpoint.",
        }

        self.assertEqual(
            public_allowed_actions(state, ["approve_checkpoint", "sync_run", "get_status"]),
            ["sync_run", "get_status"],
        )
        self.assertIn("legacy_java_v1 đã retire", public_run_summary(state))

    def test_public_projection_respects_profile_metadata_without_snapshot_payload(self) -> None:
        state = {
            "run_id": "run_profile",
            "execution_profile": {"classification": "profiled"},
        }

        self.assertEqual(
            public_allowed_actions(state, ["approve_checkpoint", "continue_run", "sync_run"]),
            ["sync_run"],
        )
        self.assertIn("Vertical Design contract", str(public_run_summary(state, "Continue this run.")))


if __name__ == "__main__":
    unittest.main()
