import json

import pytest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.fake import fake_completion
from sdlc_harness.profiles import (
    build_planning_path_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)


def _policy():
    project = get_project_profile("typescript_fullstack_v1")
    language = get_language_profile(project.language_profile_id)
    return build_planning_path_policy(project=project, language=language)


def _verification_policy():
    return build_verification_stage_policy(
        project=get_project_profile("typescript_fullstack_v1")
    )


def _graphs():
    return (
        {
            "nodes": [
                {
                    "className": "DeleteHeritagePage",
                    "stereotype": "web",
                    "methods": [{"name": "submitDeletion"}],
                },
                {
                    "className": "DeleteHeritageService",
                    "stereotype": "api",
                    "methods": [{"name": "deleteHeritage"}],
                },
                {
                    "className": "DeleteHeritageRequest",
                    "stereotype": "contracts",
                    "methods": [],
                },
                {
                    "className": "HeritageRepository",
                    "stereotype": "database",
                    "methods": [{"name": "delete"}],
                },
            ]
        },
        {
            "sequenceDiagrams": [
                {
                    "id": "delete-flow",
                    "nodes": [
                        {"id": "Page", "label": "DeleteHeritagePage"},
                        {"id": "Service", "label": "DeleteHeritageService"},
                        {"id": "Repository", "label": "HeritageRepository"},
                    ],
                    "messages": [
                        {
                            "from": "Page",
                            "to": "Service",
                            "message": "deleteHeritage()",
                            "type": "synchronous",
                        },
                        {
                            "from": "Service",
                            "to": "Repository",
                            "message": "delete()",
                            "type": "synchronous",
                        },
                    ],
                }
            ]
        },
    )


class _HostilePlanningLlm:
    def complete(self, **_kwargs):
        return json.dumps(
            {
                "summary": "Attempt to replace deterministic targets.",
                "repo_evidence": [],
                "implementation_steps": [
                    {
                        "order": 1,
                        "title": "Wrong paths",
                        "description": "Use paths that are not in the frozen profile.",
                        "files": ["src/main/java/DeleteHeritageService.java", "../escape.ts"],
                        "design_refs": ["class:DeleteHeritageService"],
                    }
                ],
                "proposed_files": [
                    {"path": "src/main/java/DeleteHeritageService.java", "action": "create"},
                    {"path": "package.json", "action": "update"},
                ],
                "design_mappings": [
                    {
                        "design_element_id": "class:DeleteHeritageService",
                        "planned_code_target": "src/main/java/DeleteHeritageService.java",
                        "component_id": "web",
                        "reason": "LLM-selected target",
                    },
                    {
                        "design_element_id": "class:InventedService",
                        "planned_code_target": "apps/api/src/InventedService.ts",
                    },
                ],
                "test_intents": [],
                "assumptions": [],
                "open_questions": [],
            }
        )


class _ProfiledPlanningLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        del system
        return fake_completion(prompt)


def test_policy_routes_typescript_components_and_test_suffixes():
    policy = _policy()
    cases = [
        ({"className": "HeritagePage", "stereotype": "web"}, "apps/web/src/HeritagePage.tsx", "web"),
        ({"className": "HeritageService"}, "apps/api/src/HeritageService.ts", "api"),
        ({"className": "HeritageRequest"}, "packages/contracts/src/HeritageRequest.ts", "contracts"),
        ({"className": "HeritageRepository"}, "packages/db/src/HeritageRepository.ts", "database"),
    ]
    for node, expected_path, expected_component in cases:
        assert policy.resolve_class_target(node=node, target_files=[]) == (
            expected_path,
            expected_component,
        )
    assert (
        policy.test_path_for_source("apps/web/src/HeritagePage.tsx", "HeritagePage")
        == "apps/web/src/HeritagePage.test.tsx"
    )
    assert (
        policy.test_path_for_source("packages/db/src/HeritageRepository.ts", "HeritageRepository")
        == "packages/db/src/HeritageRepository.test.ts"
    )
    assert policy.normalize_mapping_path("apps/web/src/health-client.ts") == (
        "apps/web/src/health-client.ts"
    )
    assert policy.test_path_for_source("apps/web/src/health-client.ts", "health-client") == (
        "apps/web/src/health-client.test.ts"
    )
    assert policy.source_path_for_test("apps/web/src/health-client.test.ts") == (
        "apps/web/src/health-client.ts"
    )


def test_policy_fails_closed_for_unclassified_conflicting_and_ambiguous_classes():
    policy = _policy()
    with pytest.raises(HarnessError, match="Update the Phase 2 design") as unclassified:
        policy.resolve_class_target(node={"className": "CulturalHeritage"}, target_files=[])
    assert unclassified.value.error_code == "implementation_target_unclassified"

    with pytest.raises(HarnessError) as conflict:
        policy.resolve_class_target(
            node={"className": "HeritagePage", "stereotype": "api"},
            target_files=[],
        )
    assert conflict.value.error_code == "implementation_target_conflict"

    with pytest.raises(HarnessError) as ambiguous:
        policy.resolve_class_target(
            node={"className": "SharedThing"},
            target_files=["apps/web/src/SharedThing.tsx", "apps/api/src/SharedThing.ts"],
        )
    assert ambiguous.value.error_code == "implementation_target_ambiguous"


def test_policy_prefers_unique_existing_symbol_and_uses_hint_to_disambiguate():
    policy = _policy()
    assert policy.resolve_class_target(
        node={"className": "SharedThing"},
        target_files=["packages/contracts/src/SharedThing.ts"],
    ) == ("packages/contracts/src/SharedThing.ts", "contracts")
    assert policy.resolve_class_target(
        node={"className": "SharedThing", "stereotype": "web"},
        target_files=["apps/web/src/SharedThing.tsx", "apps/api/src/SharedThing.ts"],
    ) == ("apps/web/src/SharedThing.tsx", "web")
    assert policy.symbol_owner("apps/web/src/SharedThing.test.tsx") == "SharedThing"


def test_policy_accepts_only_profile_owned_file_kinds():
    policy = _policy()
    accepted = {
        "apps/web/src/Page.tsx",
        "apps/web/src/Page.ts",
        "apps/web/src/Page.test.tsx",
        "apps/api/src/Service.ts",
        "packages/db/prisma/schema.prisma",
        "packages/db/prisma/migrations/001_init.sql",
        "tests/e2e/delete.spec.ts",
    }
    rejected = {
        "apps/api/src/Service.tsx",
        "apps/web/src/Page.jsx",
        "tests/e2e/delete.test.ts",
        "package.json",
        "../escape.ts",
        "apps\\api\\src\\Service.ts",
        "src/main/java/Service.java",
    }
    assert all(policy.normalize_editable_path(path) == path for path in accepted)
    assert all(policy.normalize_editable_path(path) == "" for path in rejected)


def test_policy_accepts_ui_stylesheets_as_non_source_web_targets():
    policy = _policy()
    for path in ("apps/web/src/Screen.css", "apps/web/src/screens/Screen.css"):
        assert policy.normalize_editable_path(path) == path
        assert policy.is_stylesheet_path(path)
        assert policy.component_for_path(path) == "web"
        assert policy.normalize_mapping_path(path) == ""
        assert not policy.is_test_path(path)
    for path in ("apps/api/src/Service.css", "packages/contracts/src/x.css", "Screen.css"):
        assert policy.normalize_editable_path(path) == ""
        assert not policy.is_stylesheet_path(path)
    assert policy.prompt_context()["special_roots"]["ui_stylesheets"] == {
        "root": "apps/web/src",
        "suffixes": [".css"],
    }


def test_verification_prompt_context_states_active_stage_capabilities():
    context = _verification_policy().prompt_context()
    assert set(context["stage_capabilities"]) == set(context["stage_ids"])
    assert "no DOM" in context["stage_capabilities"]["unit"]

