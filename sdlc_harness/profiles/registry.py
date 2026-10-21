"""Khai báo các profile có sẵn.

Registry chủ ý chỉ chứa định danh và policy của repository. Các ánh xạ
argv/cwd/env có thể thực thi thuộc về một lát Verification Runner sau này.
"""

from __future__ import annotations

from .model import ComponentProfile, LanguageProfile, ProfileRegistry, ProjectProfile


_TYPESCRIPT = LanguageProfile(
    profile_id="typescript_v1",
    schema_version="language_profile_v1",
    source_suffixes=(".ts", ".tsx"),
    test_suffixes=(".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"),
    prompt_directive_id="typescript_prompt_v1",
    symbol_extractor_id="typescript_compiler_ast_v1",
    diagnostic_classifier_id="typescript_diagnostics_v1",
)

_TYPESCRIPT_FULLSTACK = ProjectProfile(
    profile_id="typescript_fullstack_v1",
    schema_version="project_profile_v1",
    language_profile_id=_TYPESCRIPT.profile_id,
    target_manifest_schema="target_profile_manifest_v1",
    components=(
        ComponentProfile(component_id="web", editable_roots=("apps/web/src",)),
        ComponentProfile(component_id="api", editable_roots=("apps/api/src",)),
        ComponentProfile(component_id="contracts", editable_roots=("packages/contracts/src",)),
        ComponentProfile(
            component_id="database",
            editable_roots=("packages/db/src", "packages/db/prisma"),
        ),
        ComponentProfile(component_id="e2e", editable_roots=("tests/e2e",)),
    ),
    protected_paths=(
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
    required_paths=(
        ".sdlc/target-profile.json",
        "package.json",
        "package-lock.json",
        "compose.yaml",
        "apps/web",
        "apps/api",
        "packages/contracts",
        "packages/db",
        "tests/e2e",
    ),
    verification_stage_ids=(
        "dependency_setup",
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
)

_REGISTRY = ProfileRegistry(
    language_profiles=(_TYPESCRIPT,),
    project_profiles=(_TYPESCRIPT_FULLSTACK,),
)


def get_language_profile(profile_id: str) -> LanguageProfile:
    """Resolve one built-in language profile or fail closed."""

    return _REGISTRY.language(profile_id)


def get_project_profile(profile_id: str) -> ProjectProfile:
    """Resolve one built-in project profile or fail closed."""

    return _REGISTRY.project(profile_id)


def list_project_profile_ids() -> tuple[str, ...]:
    """List supported project profile IDs in deterministic registry order."""

    return _REGISTRY.project_ids()
