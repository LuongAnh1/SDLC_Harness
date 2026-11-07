"""Kiểm contract directive ngôn ngữ và prompt đang hoạt động ở Phase 3."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from sdlc_harness.agents.developer.planner import (
    _component_plan_prompt,
    _developer_plan_prompt,
)
from sdlc_harness.agents.validator.plan_review import (
    ImplementationPlanReview,
    _llm_review_prompt,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.fake import fake_completion
from sdlc_harness.repo_context.codegraph import _context_query
from sdlc_harness.profiles import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    build_planning_path_policy,
    build_verification_stage_policy,
    build_profile_snapshot,
    get_language_profile,
    get_project_profile,
    get_prompt_directive,
    prompt_directive_from_snapshot,
    resolve_language_prompt_directive,
)


_JAVA_ASSUMPTIONS = (".java", "src/main/java", "src/test/java", "maven", "gradle", "junit")


def _typescript_prompt_context():
    language = get_language_profile("typescript_v1")
    project = get_project_profile("typescript_fullstack_v1")
    return (
        build_planning_path_policy(project=project, language=language),
        resolve_language_prompt_directive(language),
        build_verification_stage_policy(project=project),
    )


def _developer_prompt(*, repair_feedback: str = "") -> str:
    policy, directive, verification_policy = _typescript_prompt_context()
    return _developer_plan_prompt(
        run_id="run_prompt_contract",
        source_ref="jira_issue:SDLC-1",
        issue_key="SDLC-1",
        requirement_packet="Delete one approved record.",
        design_artifact="Approved service and repository design.",
        requirement_design_trace={},
        class_graph={"nodes": []},
        sequence_graph={"sequenceDiagrams": []},
        repo_context={},
        base_mappings=[],
        base_test_intents=[],
        target_repo_path="target",
        target_repo_remote="origin",
        target_base_branch="main",
        planned_branch="feature/run_prompt_contract",
        target_files=[],
        test_command="",
        repair_feedback=repair_feedback,
        previous_review={"ok": False} if repair_feedback else {},
        canonical_requirement=None,
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
    )


def test_prompt_directive_registry_resolves_snapshot_and_fails_closed() -> None:
    directive = get_prompt_directive("typescript_prompt_v1")
    snapshot = build_profile_snapshot(
        project_profile_id="typescript_fullstack_v1",
        target_remote="https://example.invalid/target.git",
        target_base_branch="main",
    )

    assert prompt_directive_from_snapshot(snapshot) == directive

    with pytest.raises(HarnessError) as unknown:
        get_prompt_directive("unknown_prompt_v1")
    assert unknown.value.error_code == "prompt_directive_not_supported"

    wrong_language = replace(get_language_profile("typescript_v1"), profile_id="other_language_v1")
    with pytest.raises(HarnessError) as mismatch:
        resolve_language_prompt_directive(wrong_language)
    assert mismatch.value.error_code == "prompt_directive_mismatch"


@pytest.mark.parametrize("repair_feedback", ("", "Keep the repair inside the approved targets."))
def test_active_typescript_developer_prompt_has_no_java_assumptions(repair_feedback: str) -> None:
    prompt = _developer_prompt(repair_feedback=repair_feedback)
    lowered = prompt.lower()

    assert '"prompt_directive_id": "typescript_prompt_v1"' in prompt
    assert "TypeScript" in prompt
    assert "apps/web/src/Example.tsx" in prompt
    assert "apps/web/src/Example.test.tsx" in prompt
    assert '"policy_version": "verification_stage_policy_v1"' in prompt
    assert '"stage_id": "unit"' in prompt
    assert '"command"' not in prompt
    assert "test_command" not in prompt
    assert "powershell" not in lowered
    assert "scripts/test.ps1" not in lowered
    assert all(token not in lowered for token in _JAVA_ASSUMPTIONS)


def _v4_review_prompt(structured_plan: dict[str, object]) -> str:
    policy, directive, verification_policy = _typescript_prompt_context()
    return _llm_review_prompt(
        structured_plan={"version": COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION, **structured_plan},
        requirement_packet="Delete one approved record.",
        design_artifact="Approved service and repository design.",
        requirement_design_trace={"class_trace": [{"note": "UNIQUE-LEGACY-TRACE"}]},
        class_graph={"nodes": [{"note": "UNIQUE-CLASS"}]},
        sequence_graph={"sequenceDiagrams": [{"note": "UNIQUE-SEQUENCE"}]},
        rule_review=ImplementationPlanReview(ok=True, mode="implementation_plan_rule_review_v4"),
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
    )


def test_active_typescript_validator_prompt_uses_same_directive() -> None:
    prompt = _v4_review_prompt({"design_mappings": []})
    lowered = prompt.lower()

    assert '"prompt_directive_id": "typescript_prompt_v1"' in prompt
    assert "TypeScript" in prompt
    assert '"policy_version": "verification_stage_policy_v1"' in prompt
    assert "Work-unit identity, membership, dependency, order, runtime_bindings, requirement_refs" in prompt
    assert "Do not invent a new business component or work unit" in prompt
    assert "A `typecheck` intent targets a non-test source" in prompt
    assert "Do not re-audit that mapping item by item" in prompt
    assert "do not require a compose service or stub for it" in prompt
    for legacy in ("UNIQUE-LEGACY-TRACE", "UNIQUE-CLASS", "UNIQUE-SEQUENCE", "Class graph JSON"):
        assert legacy not in prompt
    assert all(token not in lowered for token in _JAVA_ASSUMPTIONS)


def test_component_plan_prompt_keeps_signature_identity_harness_owned() -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    prompt = _component_plan_prompt(
        run_id="run_signature_targets",
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        units=[],
        vertical_design_graph={"nodes": [], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        signature_targets=[
            {
                "target_path": "apps/web/src/CulturalHeritageInsertionForm.tsx",
                "target_symbol": "CulturalHeritageInsertionForm",
                "symbol_kind": "function",
                "owner_kind": "",
                "design_element_ids": [
                    "method:CulturalHeritageInsertionForm.submit",
                    "method:CulturalHeritageInsertionForm.confirm",
                ],
            }
        ],
        repair_feedback="",
        previous_review={},
    )

    assert '"target_path": "Harness-owned source path"' in prompt
    assert '"target_symbol": "Harness-owned function/component or Owner.method"' in prompt
    assert "Multiple legacy Design operations may trace to one function/component target" in prompt
    assert "Never emit design_element_id" in prompt
    assert "`packages/contracts/src/...` target" in prompt
    assert "Harness assigns repo-profile" in prompt
    # The old "map focus/responsive to tests or report non-executable" rules contradicted the
    # stage capabilities; obligations now route such items to unverified_obligations.
    assert "A UI objective is only executable" not in prompt
    assert "non-executable" not in prompt
    assert "CSS class names without owned style rules" in prompt
    assert "static\n  markup does not cover interaction or focus" in prompt
    assert "This is a repair patch" not in prompt
    assert "in a repair, only\n  for targets of the units you update" in prompt
    assert "owner of required auth and input mappings" in prompt
    assert "apps/web/src/CulturalHeritageInsertionForm.tsx" in prompt
    assert "Legacy Class graph" not in prompt
    assert "Legacy Sequence graph" not in prompt

    repair_prompt = _component_plan_prompt(
        run_id="run_signature_targets",
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        units=[],
        vertical_design_graph={"nodes": [], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        signature_targets=[],
        repair_feedback="Repair WU-1.",
        previous_review={"blocking_issues": ["missing test"]},
    )
    assert "This is a repair patch" in repair_prompt


def test_fake_developer_plan_follows_language_directive() -> None:
    payload = json.loads(fake_completion(_developer_prompt()))
    encoded = json.dumps(payload, ensure_ascii=False).lower()

    assert ".ts" in encoded
    assert ".java" not in encoded
    assert "src/main/java" not in encoded
    assert "src/test/java" not in encoded
    assert "command" not in encoded
    assert [item["stage_id"] for item in payload["test_intents"]] == ["unit", "typecheck"]


def test_codegraph_context_prioritizes_runtime_wiring_terms() -> None:
    query = _context_query(
        issue_key="SDLC-1",
        target_files=["apps/api/src/health.ts"],
        class_graph={"nodes": [{"className": "LegacyHealth"}]},
        sequence_graph={"sequenceDiagrams": []},
        vertical_design_graph={
            "nodes": [
                {
                    "runtime_binding": {
                        "target": {
                            "path": "apps/api/src/cultural-heritage-delete.ts",
                            "symbol": "registerDeleteCulturalHeritageRoute",
                        },
                        "attachment": {
                            "owner_path": "apps/api/src/app.ts",
                            "owner_symbol": "createApp",
                        },
                    }
                }
            ],
            "edges": [
                {
                    "boundary_contract": {
                        "consumer_operation": "deleteSelectedHeritage",
                        "provider_operation": "DELETE /cultural-heritage/{id}",
                    }
                }
            ],
        },
    )

    assert query.index("cultural-heritage-delete") < query.index("LegacyHealth")
    assert "registerDeleteCulturalHeritageRoute" in query
    assert "createApp" in query
    assert "DELETE /cultural-heritage/{id}" in query


def test_component_plan_prompt_carries_unit_contracts_only_in_the_design_slice() -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    marker = "UNIQUE-SURFACE-CONTRACT-TEXT"
    node = {"id": "VDN-1", "surface_contract": {"note": marker}}
    edge = {"id": "VDE-1", "from": "VDN-1", "to": "VDN-1", "boundary_contract": {"note": "UNIQUE-BOUNDARY"}}
    prompt = _component_plan_prompt(
        run_id="run_dedupe",
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        units=[
            {
                "unit_id": "WU-1",
                "design_node_ids": ["VDN-1"],
                "design_edge_ids": ["VDE-1"],
                "surface_contracts": [{"design_node_id": "VDN-1", "surface_contract": node["surface_contract"]}],
                "boundary_contracts": [{"design_edge_id": "VDE-1", "note": "UNIQUE-BOUNDARY"}],
            }
        ],
        vertical_design_graph={"nodes": [node], "edges": [edge]},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        signature_targets=[],
        repair_feedback="",
        previous_review={},
    )

    assert prompt.count(marker) == 1
    assert prompt.count("UNIQUE-BOUNDARY") == 1
    assert "ui_stylesheets" in prompt
    assert "stage_capabilities" in prompt
    assert "stays unverified until such a stage" in prompt


def test_validator_prompt_omits_v4_projections_and_unit_contract_copies() -> None:
    prompt = _v4_review_prompt(
        {
            "work_units": [
                {
                    "unit_id": "WU-1",
                    "planned_code_targets": [{"path": "apps/web/src/Screen.tsx"}],
                    "surface_contracts": [{"note": "UNIQUE-SURFACE"}],
                    "boundary_contracts": [{"note": "UNIQUE-BOUNDARY"}],
                    "ui_obligations": [{"id": "VDN-1:state.ready", "summary": "UNIQUE-OBLIGATION"}],
                }
            ],
            "design_mappings": [{"note": "UNIQUE-MAPPING"}],
            "implementation_steps": [{"note": "UNIQUE-STEP"}],
            "proposed_files": [{"note": "UNIQUE-FILE"}],
            "test_intents": [{"note": "UNIQUE-TEST"}],
            "implementation_signature_contract": {"entries": [{"note": "UNIQUE-SIGNATURE"}]},
        }
    )

    for omitted in (
        "UNIQUE-SURFACE",
        "UNIQUE-BOUNDARY",
        "UNIQUE-OBLIGATION",
        "UNIQUE-MAPPING",
        "UNIQUE-STEP",
        "UNIQUE-FILE",
        "UNIQUE-TEST",
    ):
        assert omitted not in prompt
    assert "UNIQUE-SIGNATURE" in prompt
    assert "apps/web/src/Screen.tsx" in prompt
    assert "stage_capabilities" in prompt
