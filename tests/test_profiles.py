"""Kiểm thử contract của registry profile ngôn ngữ và dự án."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
import json
import unittest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import (
    ComponentProfile,
    LanguageProfile,
    ProfileRegistry,
    ProjectProfile,
    get_language_profile,
    get_project_profile,
    list_project_profile_ids,
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
        self.assertIn(".sdlc/target-profile.json", project.required_paths)
        self.assertIn("package-lock.json", project.protected_paths)

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


if __name__ == "__main__":
    unittest.main()
