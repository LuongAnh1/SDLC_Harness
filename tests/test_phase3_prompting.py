"""Kiểm contract directive ngôn ngữ và prompt đang hoạt động ở Phase 3."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from sdlc_harness.agents.developer.planner import (
    DeveloperPlanAgent,
    _component_repair_slice,
    _component_plan_prompt,
    _compact_review,
    _owned_source_files,
    _prior_scenario_plan_context,
)
from sdlc_harness.repo_context import codegraph as codegraph_module
from sdlc_harness.workflows.phase3 import _with_repo_target_facts
from sdlc_harness.agents.validator.plan_review import (
    ImplementationPlanReview,
    _llm_review_prompt,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.repo_context.codegraph import _context_query
from sdlc_harness.profiles import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    VERIFICATION_STAGE_POLICY_VERSION,
    build_planning_path_policy,
    build_verification_stage_policy,
    build_verification_stage_policy_version,
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
    assert '"policy_version": "verification_stage_policy_v2"' in prompt
    assert "Work-unit identity, membership, dependency, order, runtime_bindings, requirement_refs" in prompt
    assert "Do not invent a new business component or work unit" in prompt
    assert "A `typecheck` intent targets a non-test source" in prompt
    assert "do not re-audit that mapping item by item" in prompt
    assert "the\n  Plan has no browser tests or test data: never ask for them" in prompt
    assert "do not require a compose service or stub for it" in prompt
    # A Design contract narrower than its Requirement never lowers what the plan must satisfy.
    assert "a plan that follows only\n  the narrower contract is a blocker" in prompt
    assert "applies to every\n  case it lists (for example both edit and delete)" in prompt
    for legacy in ("UNIQUE-LEGACY-TRACE", "UNIQUE-CLASS", "UNIQUE-SEQUENCE", "Class graph JSON"):
        assert legacy not in prompt
    assert all(token not in lowered for token in _JAVA_ASSUMPTIONS)


def test_validator_prompt_lists_every_design_node_instead_of_a_cut_design_document() -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    design = {
        "nodes": [
            {"id": "VDN-edit", "scenario_id": "SC-1", "component_id": "api", "surface": "api",
             "disposition": "modify", "name": "Update endpoint (PUT /api/x/:id)"},
            {"id": "VDN-page", "scenario_id": "SC-3", "component_id": "web", "surface": "ui",
             "disposition": "reuse", "name": "Article page"},
        ],
        "edges": [],
        "surface_not_exercised": [{"scenario_id": "SC-6", "surface": "data"}],
    }
    prompt = _llm_review_prompt(
        structured_plan={
            "version": COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
            "inputs": {"repo_runtime_config": {"schema_version": "x"}, "repo_target_callers": [{"path": "UNIQUE-CALLER"}]},
            "work_units": [{"unit_id": "WU-1", "design_node_ids": ["VDN-edit"]}],
            "requirement_coverage": [
                {"requirement_id": "AC-3", "coverage_kind": "existing_code",
                 "existing_code_targets": ["apps/web/src/Page.tsx#ArticleMeta"]},
                {"requirement_id": "PRE-1", "coverage_kind": "precondition"},
            ],
        },
        requirement_packet="UNIQUE-REQUIREMENT-PACKET",
        design_artifact="UNIQUE-DESIGN-DOCUMENT",
        requirement_design_trace={},
        class_graph={},
        sequence_graph={},
        rule_review=ImplementationPlanReview(ok=True, mode="implementation_plan_rule_review_v4"),
        canonical_requirement={
            "schema_version": "requirement_model_v2",
            "acceptance_criteria": [{"id": "AC-3", "statement": "UNIQUE-AC"}],
            "provenance_ledger": {"note": "UNIQUE-PROVENANCE"},
            "evidence": [{"note": "UNIQUE-EVIDENCE"}],
        },
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        vertical_design_graph=design,
        vertical_trace_graph={"nodes": [], "edges": []},
    )

    assert "VDN-edit | SC-1 | api/api | modify | WU-1 | Update endpoint (PUT /api/x/:id)" in prompt
    assert "VDN-page | SC-3 | web/ui | reuse | no work unit | Article page" in prompt
    assert "Not exercised: SC-6 data" in prompt
    assert "AC-3: existing code apps/web/src/Page.tsx#ArticleMeta" in prompt
    assert "PRE-1: precondition" in prompt
    assert "never report it as\n  unplanned or missing" in prompt
    # The cut documents, provenance and Harness-only facts stay out; the Requirement itself stays.
    for absent in ("UNIQUE-REQUIREMENT-PACKET", "UNIQUE-DESIGN-DOCUMENT", "UNIQUE-PROVENANCE",
                   "UNIQUE-EVIDENCE", "UNIQUE-CALLER", "truncated for implementation plan review"):
        assert absent not in prompt
    assert "UNIQUE-AC" in prompt and '"repo_runtime_config"' in prompt


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

    structured_repair_prompt = _component_plan_prompt(
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
        repair_feedback="UNIQUE_FLATTENED_DUPLICATE",
        previous_review={
            "ok": False,
            "findings": [
                {
                    "severity": "blocking",
                    "issue": "UNIQUE_STRUCTURED_REPAIR_ISSUE",
                }
            ],
        },
    )
    assert "Blocking review findings:" in structured_repair_prompt
    assert structured_repair_prompt.count("UNIQUE_STRUCTURED_REPAIR_ISSUE") == 1
    assert "UNIQUE_FLATTENED_DUPLICATE" not in structured_repair_prompt


def test_sliced_planner_prior_context_keeps_only_compatibility_targets() -> None:
    context = _prior_scenario_plan_context(
        units=[
            {
                "unit_id": "WU-OLD",
                "scenario_id": "SC-1",
                "component_id": "web",
                "objective": "Keep the existing screen contract.",
                "depends_on": [],
                "planned_code_targets": [{"path": "apps/web/src/Screen.tsx", "symbol": "Screen"}],
                "code_entities": [{"entity": "apps/web/src/Screen.tsx#Screen"}],
                "test_intents": [
                    {
                        "stage_id": "unit",
                        "intent": "UNIQUE_VERBOSE_UNIT_INTENT",
                        "target_paths": ["apps/web/src/Screen.test.tsx"],
                        "obligation_refs": ["VDN-OLD:state.ready"],
                    }
                ],
                "qa_test_intents": [
                    {
                        "stage_id": "browser_e2e",
                        "intent": "UNIQUE_VERBOSE_QA_INTENT",
                        "target_paths": ["tests/e2e/sc-1.spec.ts"],
                        "data_setup": {
                            "mode": "synthetic",
                            "reason": "UNIQUE_VERBOSE_FIXTURE_REASON",
                            "requirements": ["large fixture payload"],
                        },
                    }
                ],
            },
            {
                "unit_id": "WU-CURRENT",
                "scenario_id": "SC-2",
                "component_id": "web",
                "depends_on": ["WU-OLD"],
                "planned_code_targets": [{"path": "apps/web/src/Screen.tsx", "symbol": "Screen"}],
                "code_entities": [{"entity": "apps/web/src/Screen.tsx#Screen"}],
            },
        ],
        current_units=[
            {
                "unit_id": "WU-CURRENT",
                "depends_on": ["WU-OLD"],
                "planned_code_targets": [{"path": "apps/web/src/Screen.tsx", "symbol": "Screen"}],
                "code_entities": [{"entity": "apps/web/src/Screen.tsx#Screen"}],
            }
        ],
        completed_unit_ids={"WU-OLD"},
        signature_entries=[
            {"target_path": "apps/web/src/Screen.tsx", "target_symbol": "Screen"}
        ],
    )

    encoded = json.dumps(context, ensure_ascii=False)
    assert "apps/web/src/Screen.test.tsx" in encoded
    assert "tests/e2e/sc-1.spec.ts" in encoded
    assert "implementation_signature_entries" in context
    assert "UNIQUE_VERBOSE_UNIT_INTENT" not in encoded
    assert "UNIQUE_VERBOSE_QA_INTENT" not in encoded
    assert "UNIQUE_VERBOSE_FIXTURE_REASON" not in encoded
    assert "test_intents" not in context["completed_units"][0]
    assert "qa_test_intents" not in context["completed_units"][0]


def test_component_repair_slice_limits_mutation_and_keeps_related_context() -> None:
    units = [
        {
            "unit_id": "WU-AAAAAAAAAAAA",
            "scenario_id": "SC-1",
            "depends_on": [],
            "planned_code_targets": [{"path": "apps/web/src/Panel.tsx", "symbol": "Panel"}],
            "code_entities": [{"entity": "apps/web/src/Panel.tsx#Panel"}],
            "objective": "UNIQUE_RELATED_SC1_CONTEXT",
        },
        {
            "unit_id": "WU-BBBBBBBBBBBB",
            "scenario_id": "SC-2",
            "depends_on": ["WU-AAAAAAAAAAAA"],
            "planned_code_targets": [{"path": "apps/web/src/Panel.tsx", "symbol": "Panel"}],
            "code_entities": [{"entity": "apps/web/src/Panel.tsx#Panel"}],
            "objective": "UNIQUE_MUTABLE_SC2_UNIT",
        },
        {
            "unit_id": "WU-CCCCCCCCCCCC",
            "scenario_id": "SC-3",
            "depends_on": [],
            "planned_code_targets": [{"path": "apps/api/src/Retry.ts", "symbol": "retry"}],
            "code_entities": [{"entity": "apps/api/src/Retry.ts#retry"}],
            "objective": "UNIQUE_UNRELATED_SC3_UNIT",
        },
    ]

    scoped, prior_context, allowed = _component_repair_slice(
        units=units,
        review_report={"affected_unit_ids": ["WU-BBBBBBBBBBBB"]},
        signature_entries=[
            {
                "source_path": "apps/web/src/Panel.tsx",
                "qualified_name": "Panel",
            }
        ],
    )

    assert [unit["unit_id"] for unit in scoped] == ["WU-BBBBBBBBBBBB"]
    assert scoped[0]["scenario_id"] == "SC-2"
    assert allowed == {"WU-BBBBBBBBBBBB"}
    assert [unit["unit_id"] for unit in prior_context["completed_units"]] == [
        "WU-AAAAAAAAAAAA"
    ]
    assert prior_context["implementation_signature_entries"][0]["source_path"] == (
        "apps/web/src/Panel.tsx"
    )
    encoded = json.dumps({"scoped": scoped, "context": prior_context})
    assert "UNIQUE_MUTABLE_SC2_UNIT" in encoded
    assert "UNIQUE_RELATED_SC1_CONTEXT" in encoded
    assert "UNIQUE_UNRELATED_SC3_UNIT" not in encoded


def test_component_repair_keeps_structured_blockers_and_drops_warnings() -> None:
    blocker = {
        "category": "test",
        "severity": "blocking",
        "unit_ids": ["WU-BBBBBBBBBBBB"],
        "refs": ["AC-6"],
        "issue": "Cancellation completion is not deterministic.",
        "expected_change": "Define the observable completion transition.",
    }
    warning = {**blocker, "severity": "warning", "issue": "Optional parameter."}

    compact = _compact_review(
        {
            "ok": False,
            "summary": "Contains a warning that repair must not see.",
            "llm_review": {"findings": [blocker, warning]},
        }
    )

    assert compact == {"ok": False, "findings": [blocker]}


def test_component_plan_prompt_uses_structured_slices_instead_of_human_summaries() -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    prompt = _component_plan_prompt(
        run_id="run_compact_prompt",
        requirement_packet="UNIQUE_REDUNDANT_REQUIREMENT_SUMMARY",
        design_artifact="UNIQUE_REDUNDANT_DESIGN_SUMMARY",
        units=[],
        vertical_design_graph={"nodes": [], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        signature_targets=[],
        repair_feedback="",
        previous_review={},
    )

    assert "UNIQUE_REDUNDANT_REQUIREMENT_SUMMARY" not in prompt
    assert "UNIQUE_REDUNDANT_DESIGN_SUMMARY" not in prompt
    assert "Non-data units must omit `migration_impact`" in prompt
    assert "use `[]` for" in prompt


def test_component_plan_contract_retry_includes_previous_candidate() -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    invalid_candidate = json.dumps(
        {
            "scenario_id": "SC-1",
            "summary": "UNIQUE_PREVIOUS_CANDIDATE",
            "repo_evidence": [],
            "work_unit_updates": [
                {
                    "unit_id": "WU-1",
                    "test_intents": [
                        {
                            "stage_id": "unit",
                            "intent": "Verify the flow.",
                            "acceptance_refs": ["AC-1"],
                            "target_paths": "apps/api/src/sc1.test.ts",
                            "obligation_refs": [],
                        }
                    ],
                }
            ],
            "implementation_signature_contract": {"entries": []},
        }
    )
    corrected_candidate = invalid_candidate.replace(
        '"target_paths": "apps/api/src/sc1.test.ts"',
        '"target_paths": ["apps/api/src/sc1.test.ts"]',
    )

    class RetryLlm:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.outputs = [invalid_candidate, corrected_candidate]

        def complete(self, *, system: str, prompt: str) -> str:
            del system
            self.prompts.append(prompt)
            return self.outputs.pop(0)

    llm = RetryLlm()
    payload = DeveloperPlanAgent(llm_client=llm)._component_plan_llm(
        run_id="run_retry_candidate",
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        units=[{"unit_id": "WU-1", "scenario_id": "SC-1"}],
        vertical_design_graph={"nodes": [], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        signature_targets=[],
        repair_feedback="",
        previous_review={},
        scenario_id="SC-1",
        allowed_unit_ids={"WU-1"},
        required_unit_ids={"WU-1"},
    )

    assert payload["summary"] == "UNIQUE_PREVIOUS_CANDIDATE"
    assert len(llm.prompts) == 2
    assert invalid_candidate in llm.prompts[1]
    assert "target_paths must be an array of strings" in llm.prompts[1]
    assert "Preserve every contract-valid field" in llm.prompts[1]
    assert "Do not omit, summarize, or regenerate unrelated fields" in llm.prompts[1]


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
    verification_policy = build_verification_stage_policy_version(
        project=get_project_profile("typescript_fullstack_v1"),
        policy_version=VERIFICATION_STAGE_POLICY_VERSION,
    )
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
    # Browser QA is active under this policy: Phase 4 owns browser tests, so no rule offers QA
    # intents, test data or unverified UI obligations.
    assert "Phase 4 Browser QA observes it; never claim a `unit` test proves it" in prompt
    assert "Do not plan browser tests or their test data here" in prompt
    assert "stays unverified until such a stage" not in prompt
    for absent in ("unverified_obligations", "qa_test_intents", "data_setup", "synthetic"):
        assert absent not in prompt
    assert "`runtime_input` obligation" in prompt
    assert "do not add or alter a" in prompt
    # Points 2-4: a contract gap is read explicitly, callers keep their responses, tests are not scope.
    assert "`Reading of <Design node or edge ID>: ...`" in prompt
    assert "do not re-implement it in a planned target" in prompt
    assert "test files are not `write_scope` targets" in prompt
    # A target keeps its own existing responses; a seeded owner that already wires it needs no change.
    assert "target keeps the responses its own existing code already produces unless" in prompt
    assert "state in the objective\n  that the owner needs no change and do not invent one" in prompt
    assert "never leave an existing test asserting replaced behavior" in prompt
    # Without repository facts the `unit` capability keeps the profile text.
    assert "Vitest in a Node environment with no DOM and no browser" in prompt


def test_unit_stage_capability_follows_the_repository_test_environment() -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    verification_policy = build_verification_stage_policy_version(
        project=get_project_profile("typescript_fullstack_v1"),
        policy_version=VERIFICATION_STAGE_POLICY_VERSION,
    )
    per_file = {"unit_environment": "node", "dom_capable": True, "dom_test_directive": "// @vitest-environment jsdom"}
    unit = verification_policy.prompt_context(per_file)["stage_capabilities"]["unit"]
    assert "A test file whose first line is `// @vitest-environment jsdom`" in unit
    assert "fires events and queries the DOM, including which element has focus" in unit
    assert "cannot observe CSS or viewport-dependent layout" in unit
    configured = verification_policy.prompt_context({"unit_environment": "jsdom", "dom_capable": True})
    assert configured["stage_capabilities"]["unit"].startswith("Vitest in a jsdom DOM environment")
    no_dom = verification_policy.prompt_context({"unit_environment": "node", "dom_capable": False})
    assert no_dom == verification_policy.prompt_context()

    prompt = _component_plan_prompt(
        run_id="run_dom",
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
        repair_feedback="",
        previous_review={},
        unit_test_environment=per_file,
    )
    assert "(the repository's own convention) runs in a DOM" in prompt
    assert "Vitest in a Node environment with no DOM and no browser" not in prompt
    # The Plan review reads the same fact from the plan inputs, so both judge one capability.
    review = _llm_review_prompt(
        structured_plan={"inputs": {"repo_unit_test_environment": per_file}},
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        requirement_design_trace={},
        class_graph={},
        sequence_graph={},
        rule_review=ImplementationPlanReview(ok=True, mode="implementation_plan_rule_review_v4"),
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
    )
    assert "(the repository's own convention) runs in a DOM" in review


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
    assert "verify that the plan keeps the production dependency external" in prompt
    assert "data_setup" not in prompt and "qa_test_intents" not in prompt
    assert "`Reading of <Design ID>: ...`" in prompt
    assert "validation middleware on the route) stays there" in prompt
    assert "keeps the responses its own existing code\n  already produces" in prompt
    assert "may need no change;\n  accept an objective that says so" in prompt
    assert "Approved Requirement and Design are immutable inputs" in prompt
    assert "Review the final merged repository state" in prompt
    assert "Earlier test intents remain part of that final state" in prompt
    assert "Review each listed contract once" in prompt
    assert "do not keep exploring alternatives" in prompt
    assert "require upstream Design change" not in prompt
    assert "Do not cap the number of findings" not in prompt


def test_component_plan_prompt_shows_the_owned_source_files_of_the_slice(tmp_path) -> None:
    policy, directive, verification_policy = _typescript_prompt_context()
    (tmp_path / "apps/api/src").mkdir(parents=True)
    (tmp_path / "apps/api/src/app.ts").write_text("export function createApp() {\n  return 1;\n}\n", encoding="utf-8")
    (tmp_path / "apps/api/src/big.ts").write_text(
        "".join(f"export const value{index} = {index};\n" for index in range(2000)), encoding="utf-8"
    )
    units = [
        {
            "unit_id": "WU-1",
            "runtime_bindings": [
                {
                    "target": {"path": "apps/api/src/guard.ts", "symbol": "ensureGuard"},
                    "attachment": {"owner_path": "apps/api/src/app.ts", "owner_symbol": "createApp"},
                }
            ],
        },
        {
            "unit_id": "WU-2",
            "runtime_bindings": [{"target": {"path": "apps/api/src/big.ts", "symbol": "value0"}}],
        },
    ]

    (tmp_path / "apps/api/src/routes").mkdir()
    (tmp_path / "apps/api/src/routes/values.ts").write_text(
        'router.get("/", validate, value0);\n', encoding="utf-8"
    )
    callers = {
        ("apps/api/src/big.ts", "value0"): [
            {"path": "apps/api/src/routes/values.ts", "line": 1},
            {"path": "apps/api/src/app.ts", "line": 2},  # already in write_scope
            {"path": "apps/api/src/big.test.ts", "line": 4},  # tests are not wiring evidence
        ]
    }

    (tmp_path / "apps/api/src/app.test.ts").write_text("expect(createApp()).toBe(1);\n", encoding="utf-8")
    units[0]["existing_test_paths"] = ["apps/api/src/app.test.ts"]

    owned = _owned_source_files(
        units=units, target_repo_path=str(tmp_path), planning_policy=policy, target_callers=callers
    )

    assert (
        "--- apps/api/src/app.test.ts (existing test of a write_scope file; 1 lines)\n"
        "   1\texpect(createApp()).toBe(1);"
    ) in owned
    # Write-scope files first, then their existing tests, then read-only callers.
    assert owned.index("big.ts (2000") < owned.index("app.test.ts (") < owned.index("routes/values.ts")
    assert "--- apps/api/src/app.ts (3 lines)\n   1\texport function createApp() {\n   2\t  return 1;" in owned
    assert "--- apps/api/src/guard.ts: does not exist yet; the work unit creates it." in owned
    # A later unit of the same code entity extends a file the first unit creates.
    later = [
        {
            **units[0],
            "unit_id": "WU-LATER",
            "existing_test_paths": [],
            "code_entities": [
                {"entity": "apps/api/src/guard.ts#ensureGuard", "unit_ids": ["WU-1", "WU-LATER"]}
            ],
        }
    ]
    assert (
        "--- apps/api/src/guard.ts: does not exist in the worktree yet; earlier unit WU-1 creates it."
        in _owned_source_files(units=later, target_repo_path=str(tmp_path), planning_policy=policy)
    )
    assert "--- apps/api/src/big.ts (2000 lines)" in owned
    assert "-2000 not shown]" in owned
    assert (
        "--- apps/api/src/routes/values.ts (read-only caller of value0 at line 1; 1 lines)\n"
        '   1\trouter.get("/", validate, value0);'
    ) in owned
    assert owned.count("apps/api/src/app.ts") == 1 and "big.test.ts" not in owned
    assert owned.index("big.ts (2000") < owned.index("routes/values.ts")
    assert len(owned) < 9400
    prompt = _component_plan_prompt(
        run_id="run_owned",
        requirement_packet="Approved requirement.",
        design_artifact="Approved design.",
        units=units,
        vertical_design_graph={"nodes": [], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
        repo_context={},
        planning_policy=policy,
        prompt_directive=directive,
        verification_policy=verification_policy,
        signature_targets=[],
        repair_feedback="",
        previous_review={},
        owned_sources=owned,
    )
    assert prompt.index("Vertical Trace slice:") < prompt.index(
        "Owned source files (current worktree text"
    ) < prompt.index("Repository context:")
    assert "   1\texport function createApp() {" in prompt


def test_codegraph_symbol_kinds_read_only_exact_path_and_name(tmp_path, monkeypatch) -> None:
    (tmp_path / ".codegraph").mkdir()
    calls: list[list[str]] = []

    def fake_command(*, target_path, arguments, timeout_seconds):
        calls.append(arguments)
        return json.dumps(
            [
                {"node": {"kind": "function", "name": "articlesUpdateValidator", "filePath": "apps/api/src/a.ts"}},
                {"node": {"kind": "class", "name": "articlesUpdate", "filePath": "apps/api/src/other.ts"}},
                {"node": {"kind": "function", "name": "articlesUpdate", "filePath": "apps/api/src/articlesUpdate.ts"}},
            ]
        )

    monkeypatch.setattr(codegraph_module, "_run_codegraph_command", fake_command)
    provider = codegraph_module.CodeGraphContextProvider()

    facts = provider.symbol_kinds(
        tmp_path,
        [("apps/api/src/articlesUpdate.ts", "articlesUpdate"), ("apps/api/src/New.ts", "New")],
    )

    assert facts == [{"path": "apps/api/src/articlesUpdate.ts", "symbol": "articlesUpdate", "kind": "function"}]
    assert calls[0][:2] == ["query", "New"] and calls[1][:2] == ["query", "articlesUpdate"]


def test_codegraph_target_callers_list_other_files_with_their_lines(tmp_path, monkeypatch) -> None:
    (tmp_path / ".codegraph").mkdir()

    def fake_command(*, target_path, arguments, timeout_seconds):
        assert arguments[:2] == ["callers", "articlesUpdate"]
        return json.dumps(
            {
                "symbol": "articlesUpdate",
                "callers": [
                    {"name": "PUT /:slug", "kind": "route", "filePath": "apps/api/src/routes/api/articles.ts", "startLine": 41},
                    {"name": "self", "kind": "function", "filePath": "apps/api/src/articlesUpdate.ts", "startLine": 3},
                ],
            }
        )

    monkeypatch.setattr(codegraph_module, "_run_codegraph_command", fake_command)

    facts = codegraph_module.CodeGraphContextProvider().target_callers(
        tmp_path, [("apps/api/src/articlesUpdate.ts", "articlesUpdate")]
    )

    assert facts == [
        {
            "path": "apps/api/src/articlesUpdate.ts",
            "symbol": "articlesUpdate",
            "callers": [{"path": "apps/api/src/routes/api/articles.ts", "line": 41}],
        }
    ]


def test_phase3_records_repo_facts_only_for_existing_targets(tmp_path) -> None:
    asked: dict[str, list[tuple[str, str]]] = {}

    class Provider:
        def symbol_kinds(self, target_path, targets):
            asked["kinds"] = list(targets)
            return [{"path": "apps/api/src/articlesUpdate.ts", "symbol": "articlesUpdate", "kind": "function"}]

        def target_callers(self, target_path, targets):
            asked["callers"] = list(targets)
            return []

        def target_tests(self, target_path, paths):
            asked["tests"] = list(paths)
            return [{"path": "apps/web/src/EditArticle.tsx", "tests": ["apps/web/src/EditArticle.test.tsx"]}]

    def node(path: str, symbol: str, owner: str = "") -> dict[str, object]:
        attachment = {"owner_path": owner} if owner else {}
        return {"runtime_binding": {"target": {"path": path, "symbol": symbol}, "attachment": attachment}}

    for path in ("apps/api/src/articlesUpdate.ts", "apps/web/src/EditArticle.tsx", "apps/web/src/App.tsx"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("export {};\n", encoding="utf-8")
    graph = {
        "nodes": [
            node("apps/api/src/articlesUpdate.ts", "articlesUpdate"),
            node("apps/api/src/articleAuthorGuard.ts", "ensureArticleAuthor"),  # new file
            node("apps/web/src/EditArticle.tsx", "EditArticle", owner="apps/web/src/App.tsx"),
        ]
    }

    inputs = _with_repo_target_facts({"a": 1}, provider=Provider(), target_path=tmp_path, vertical_design_graph=graph)

    # Only a .ts target named like its file can be a class owning legacy methods.
    assert asked["kinds"] == [("apps/api/src/articlesUpdate.ts", "articlesUpdate")]
    assert asked["callers"] == [
        ("apps/api/src/articlesUpdate.ts", "articlesUpdate"),
        ("apps/web/src/EditArticle.tsx", "EditArticle"),
    ]
    # Existing tests are looked up for every existing target and attachment-owner file.
    assert asked["tests"] == [
        "apps/api/src/articlesUpdate.ts",
        "apps/web/src/App.tsx",
        "apps/web/src/EditArticle.tsx",
    ]
    assert inputs == {
        "a": 1,
        "repo_symbol_kinds": [
            {"path": "apps/api/src/articlesUpdate.ts", "symbol": "articlesUpdate", "kind": "function"}
        ],
        "repo_target_callers": [],
        "repo_target_tests": [{"path": "apps/web/src/EditArticle.tsx", "tests": ["apps/web/src/EditArticle.test.tsx"]}],
    }
    assert _with_repo_target_facts(
        {"a": 1}, provider=Provider(), target_path=tmp_path, vertical_design_graph={"nodes": [graph["nodes"][1]]}
    ) == {"a": 1}


def test_codegraph_target_tests_map_each_file_to_the_tests_importing_it(tmp_path, monkeypatch) -> None:
    (tmp_path / ".codegraph").mkdir()

    def fake_command(*, target_path, arguments, timeout_seconds):
        assert arguments[0] == "affected" and arguments[-3:] == ["--depth", "1", "--json"]
        tests = ["apps/web/src/EditArticle.test.tsx"] if "EditArticle.tsx" in arguments[1] else []
        return json.dumps({"changedFiles": [arguments[1]], "affectedTests": tests})

    monkeypatch.setattr(codegraph_module, "_run_codegraph_command", fake_command)

    facts = codegraph_module.CodeGraphContextProvider().target_tests(
        tmp_path, ["apps/web/src/EditArticle.tsx", "apps/api/src/articlesUpdate.ts"]
    )

    assert facts == [{"path": "apps/web/src/EditArticle.tsx", "tests": ["apps/web/src/EditArticle.test.tsx"]}]
