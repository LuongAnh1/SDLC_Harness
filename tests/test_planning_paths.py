import json

import pytest

from sdlc_harness.agents.developer import DeveloperPlanAgent
from sdlc_harness.agents.validator import ImplementationPlanValidator
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
        "apps/web/src/Page.test.tsx",
        "apps/api/src/Service.ts",
        "packages/db/prisma/schema.prisma",
        "packages/db/prisma/migrations/001_init.sql",
        "tests/e2e/delete.spec.ts",
    }
    rejected = {
        "apps/api/src/Service.tsx",
        "apps/web/src/Page.ts",
        "tests/e2e/delete.test.ts",
        "package.json",
        "../escape.ts",
        "apps\\api\\src\\Service.ts",
        "src/main/java/Service.java",
    }
    assert all(policy.normalize_editable_path(path) == path for path in accepted)
    assert all(policy.normalize_editable_path(path) == "" for path in rejected)


def test_profiled_planner_keeps_deterministic_targets_when_llm_returns_java_paths():
    class_graph, sequence_graph = _graphs()
    policy = _policy()
    verification_policy = _verification_policy()
    draft = DeveloperPlanAgent(_HostilePlanningLlm()).plan(
        run_id="run_profiled",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_repo_path=".",
        target_repo_remote="remote",
        target_base_branch="main",
        planned_branch="feature/profiled",
        target_files=[],
        planning_policy=policy,
        verification_policy=verification_policy,
    )

    assert draft.structured["planning_profile"] == policy.metadata()
    assert draft.structured["verification_stage_ids"] == list(verification_policy.stage_ids)
    assert all("command" not in item for item in draft.structured["test_intents"])
    mappings = draft.structured["design_mappings"]
    assert not any(".java" in str(item["planned_code_target"]) for item in mappings)
    service = next(item for item in mappings if item["design_element_id"] == "class:DeleteHeritageService")
    assert service["planned_code_target"] == "apps/api/src/DeleteHeritageService.ts"
    assert service["component_id"] == "api"
    assert not any(item["design_element_id"] == "class:InventedService" for item in mappings)

    proposed = draft.structured["proposed_files"]
    paths = {item["path"] for item in proposed}
    assert "src/main/java/DeleteHeritageService.java" not in paths
    assert "package.json" not in paths
    assert "apps/api/src/DeleteHeritageService.ts" in paths
    assert "packages/db/src/HeritageRepository.test.ts" in paths
    assert all(item["component_id"] == policy.component_for_path(item["path"]) for item in proposed)
    assert draft.structured["implementation_steps"][0]["files"] == [
        "apps/api/src/DeleteHeritageService.ts"
    ]


def test_profiled_validator_rejects_wrong_profile_path_and_component():
    class_graph, sequence_graph = _graphs()
    policy = _policy()
    verification_policy = _verification_policy()
    draft = DeveloperPlanAgent(_ProfiledPlanningLlm()).plan(
        run_id="run_profiled",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_repo_path=".",
        target_repo_remote="remote",
        target_base_branch="main",
        planned_branch="feature/profiled",
        target_files=[],
        planning_policy=policy,
        verification_policy=verification_policy,
    )
    tampered = dict(draft.structured)
    tampered["design_mappings"] = [dict(item) for item in draft.structured["design_mappings"]]
    tampered["design_mappings"][0]["planned_code_target"] = "src/main/java/HeritagePage.java"
    tampered["design_mappings"][1]["component_id"] = "database"
    tampered["proposed_files"] = [*draft.structured["proposed_files"], {"path": "package.json"}]

    review = ImplementationPlanValidator().review_rules(
        structured_plan=tampered,
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        requirement_contract_version="v1",
        planning_policy=policy,
        verification_policy=verification_policy,
    )

    assert not review.ok
    assert any("outside approved planning roots" in issue for issue in review.executability_issues)
    assert any("Mapping component does not match" in issue for issue in review.executability_issues)


def test_profiled_validator_accepts_prisma_and_e2e_files_with_correct_owners():
    class_graph, sequence_graph = _graphs()
    policy = _policy()
    verification_policy = _verification_policy()
    draft = DeveloperPlanAgent(_ProfiledPlanningLlm()).plan(
        run_id="run_profiled",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_repo_path=".",
        target_repo_remote="remote",
        target_base_branch="main",
        planned_branch="feature/profiled",
        target_files=[],
        planning_policy=policy,
        verification_policy=verification_policy,
    )
    structured = dict(draft.structured)
    structured["proposed_files"] = [
        *draft.structured["proposed_files"],
        {"path": "packages/db/prisma/schema.prisma", "component_id": "database"},
        {"path": "packages/db/prisma/migrations/001_delete.sql", "component_id": "database"},
        {"path": "tests/e2e/delete-heritage.spec.ts", "component_id": "e2e"},
    ]
    structured["implementation_steps"] = [
        *draft.structured["implementation_steps"],
        {
            "order": 3,
            "title": "Schema and browser coverage",
            "description": "Apply the approved persistence and E2E changes.",
            "files": [
                "packages/db/prisma/schema.prisma",
                "packages/db/prisma/migrations/001_delete.sql",
                "tests/e2e/delete-heritage.spec.ts",
            ],
            "design_refs": [],
        },
    ]

    review = ImplementationPlanValidator().review_rules(
        structured_plan=structured,
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        requirement_contract_version="v1",
        planning_policy=policy,
        verification_policy=verification_policy,
    )

    assert review.ok, review.issues


@pytest.mark.parametrize(
    ("mutation", "expected_issue"),
    (
        ("raw_command", "must not contain execution field"),
        ("missing_stage", "missing stage_id"),
        ("unknown_stage", "unsupported verification stage"),
        ("reordered_stages", "do not match the frozen project profile"),
        ("wrong_version", "version must be"),
    ),
)
def test_profiled_validator_fails_closed_for_invalid_verification_contract(
    mutation: str,
    expected_issue: str,
):
    class_graph, sequence_graph = _graphs()
    policy = _policy()
    verification_policy = _verification_policy()
    draft = DeveloperPlanAgent(_ProfiledPlanningLlm()).plan(
        run_id="run_profiled",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_repo_path=".",
        target_repo_remote="remote",
        target_base_branch="main",
        planned_branch="feature/profiled",
        target_files=[],
        planning_policy=policy,
        verification_policy=verification_policy,
    )
    structured = json.loads(json.dumps(draft.structured))
    if mutation == "raw_command":
        structured["test_intents"][0]["command"] = "npm test"
    elif mutation == "missing_stage":
        structured["test_intents"][0].pop("stage_id")
    elif mutation == "unknown_stage":
        structured["test_intents"][0]["stage_id"] = "llm_invented_stage"
    elif mutation == "reordered_stages":
        structured["verification_stage_ids"].reverse()
    else:
        structured["version"] = "implementation_plan_structured_v1"

    review = ImplementationPlanValidator().review_rules(
        structured_plan=structured,
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        requirement_contract_version="v1",
        planning_policy=policy,
        verification_policy=verification_policy,
    )

    assert not review.ok
    assert any(expected_issue in issue for issue in review.executability_issues)
