"""Focused contract tests for component-aware Phase 3 plans."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from sdlc_harness.agents.designer import (
    DELTA_VERSION,
    RUNTIME_BINDING_VERSION,
    apply_designer_delta,
    new_vertical_design_graph,
)
from sdlc_harness.agents.developer import (
    COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
    build_component_work_units,
    canonicalize_work_unit_dependencies,
    component_plan_issues,
    component_plan_projections,
    merge_work_unit_updates,
    rebind_legacy_mappings_to_work_units,
    resolve_implementation_plan_contract,
)
from sdlc_harness.agents.developer.planner import _markdown_plan
from sdlc_harness.agents.requirements import RequirementModelV2
from sdlc_harness.agents.validator import build_requirement_graph_v2, build_vertical_trace_graph
from sdlc_harness.profiles import (
    PROFILED_IMPLEMENTATION_PLAN_VERSION,
    build_planning_path_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)


def _fixture() -> tuple[RequirementModelV2, dict[str, object], dict[str, object]]:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "requirement_v2" / "uc2.json").read_text(
            encoding="utf-8"
        )
    )
    model = RequirementModelV2.from_dict(payload)
    graph = new_vertical_design_graph(
        run_id=model.run_id,
        snapshot_binding={
            "requirement_digest": "sha256:req",
            "repo_landscape_digest": "sha256:repo",
            "workspace_ref": "run_workspace_v1",
            "base_sha": "abc123",
        },
    )
    scenario = model.scenario_contract["scenarios"][0]
    components = {"ui": "web", "api": "api", "business_rule": "api", "data": "database"}
    kinds = {
        "ui": "ui_interaction",
        "api": "api_contract",
        "business_rule": "business_rule",
        "data": "data_operation",
    }
    for surface, contract in scenario["surfaces"].items():
        if contract["status"] != "required":
            continue
        graph, _ = apply_designer_delta(
            graph,
            {
                "schema_version": DELTA_VERSION,
                "base_graph_digest": graph["artifact_digest"],
                "partition": {
                    "use_case_id": str(model.use_case["id"]),
                    "scenario_id": str(scenario["id"]),
                    "component_id": components[surface],
                },
                "operations": [
                    {"op": "set_partition_coverage", "coverage": {surface: "covered"}},
                    {
                        "op": "add_node",
                        "node": {
                            "temp_ref": f"tmp:{surface}",
                            "semantic_key": f"SC-1:{surface}",
                            "name": f"SC-1 {surface}",
                            "kind": kinds[surface],
                            "surface": surface,
                            "disposition": "add",
                            "requirement_refs": list(contract["requirement_refs"]),
                            "evidence_refs": [],
                            "evidence_reason": "Required behavior is new.",
                            "contract": f"Realize {surface} for SC-1.",
                        },
                    },
                ],
                "completion_claims": [],
                "unresolved_items": [],
            },
            valid_evidence_refs=set(),
        )
    return model, graph, build_vertical_trace_graph(requirement_model=model, design_graph=graph)


def _policy():
    project = get_project_profile("typescript_fullstack_v1")
    return (
        build_planning_path_policy(
            project=project,
            language=get_language_profile(project.language_profile_id),
        ),
        build_verification_stage_policy(project=project),
    )


def test_work_units_are_stable_grouped_and_validator_owned_fields_are_immutable() -> None:
    model, design, trace = _fixture()
    planning, verification = _policy()
    first = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    second = build_component_work_units(
        design_graph=deepcopy(design),
        trace_graph=deepcopy(trace),
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    assert first == second
    assert len(first) == 3
    assert len({(item["scenario_id"], item["component_id"]) for item in first}) == 3
    main_web = next(
        item
        for item in first
        if item["scenario_id"] == "SC-1" and item["component_id"] == "web"
    )
    assert main_web["acceptance_refs"] == ["AC-2"]

    rules = {item.component_id: item for item in planning.component_rules}
    updates = []
    for unit in first:
        rule = rules[str(unit["component_id"])]
        update: dict[str, object] = {
            "unit_id": unit["unit_id"],
            "planned_code_targets": [
                {
                    "path": f"{rule.source_root}/Planned{str(unit['component_id']).title()}{rule.source_suffix}",
                    "symbol": "plannedChange",
                }
            ],
            "test_intents": [
                {
                    "stage_id": "unit",
                    "intent": "Verify the approved component behavior.",
                    "acceptance_refs": unit["acceptance_refs"],
                    "target_paths": [
                        f"{rule.source_root}/Planned{str(unit['component_id']).title()}{rule.test_suffix}"
                    ],
                },
                {
                    "stage_id": "typecheck",
                    "intent": "Typecheck the approved component source.",
                    "acceptance_refs": unit["acceptance_refs"],
                    "target_paths": [
                        f"{rule.source_root}/Planned{str(unit['component_id']).title()}{rule.source_suffix}"
                    ],
                },
            ],
        }
        if unit["component_id"] == "api" and "api" in unit["surfaces"]:
            update["planned_code_targets"].append(
                {
                    "path": "packages/contracts/src/sc1.ts",
                    "symbol": "Sc1Contract",
                }
            )
        if "data" in unit["surfaces"]:
            update["migration_impact"] = {
                "status": "not_required",
                "reason": "The approved contract does not alter schema.",
                "targets": [],
            }
        updates.append(update)
    units = merge_work_unit_updates(units=first, updates=updates, planning_policy=planning)
    api_unit = next(item for item in units if item["component_id"] == "api")
    contract_paths = {
        str(item.get("path") or "") for item in api_unit["planned_code_targets"]
    }
    assert "packages/contracts/src/sc1.ts" in contract_paths
    assert "packages/contracts/src/index.ts" in contract_paths
    requirement_graph = build_requirement_graph_v2(model)
    projections = component_plan_projections(
        units=units,
        design_graph=design,
        requirement_graph=requirement_graph,
        legacy_mappings=[],
        planning_policy=planning,
    )
    contract_file = next(
        item for item in projections["proposed_files"]
        if item["path"] == "packages/contracts/src/sc1.ts"
    )
    assert contract_file["component_id"] == "contracts"
    inputs = {"vertical_design_digest": design["artifact_digest"]}
    structured = {
        "version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
        "inputs": inputs,
        "work_units": units,
        "verification_stage_ids": list(verification.stage_ids),
        **projections,
    }
    assert component_plan_issues(
        structured_plan=structured,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    ) == []

    missing_test_source = deepcopy(structured)
    web_unit = next(
        unit for unit in missing_test_source["work_units"] if unit["component_id"] == "web"
    )
    web_unit["test_intents"][0]["target_paths"] = [
        "apps/web/src/unplanned-client.test.ts"
    ]
    missing_test_source.update(
        component_plan_projections(
            units=missing_test_source["work_units"],
            design_graph=design,
            requirement_graph=requirement_graph,
            legacy_mappings=[],
            planning_policy=planning,
        )
    )
    issues = component_plan_issues(
        structured_plan=missing_test_source,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    )
    assert any(
        "without matching source target `apps/web/src/unplanned-client.ts`" in issue
        for issue in issues
    )

    missing_contract = deepcopy(structured)
    for unit in missing_contract["work_units"]:
        unit["planned_code_targets"] = [
            target
            for target in unit["planned_code_targets"]
            if not str(target.get("path") or "").startswith("packages/contracts/")
        ]
    missing_contract["work_units"] = canonicalize_work_unit_dependencies(
        missing_contract["work_units"]
    )
    missing_contract.update(
        component_plan_projections(
            units=missing_contract["work_units"],
            design_graph=design,
            requirement_graph=requirement_graph,
            legacy_mappings=[],
            planning_policy=planning,
        )
    )
    issues = component_plan_issues(
        structured_plan=missing_contract,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    )
    assert any("shared contract target" in issue for issue in issues)

    missing_contract_barrel = deepcopy(structured)
    for unit in missing_contract_barrel["work_units"]:
        unit["planned_code_targets"] = [
            target
            for target in unit["planned_code_targets"]
            if str(target.get("path") or "") != "packages/contracts/src/index.ts"
        ]
    missing_contract_barrel["work_units"] = canonicalize_work_unit_dependencies(
        missing_contract_barrel["work_units"]
    )
    missing_contract_barrel.update(
        component_plan_projections(
            units=missing_contract_barrel["work_units"],
            design_graph=design,
            requirement_graph=requirement_graph,
            legacy_mappings=[],
            planning_policy=planning,
        )
    )
    issues = component_plan_issues(
        structured_plan=missing_contract_barrel,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    )
    assert any(
        "shared contract modules must update `packages/contracts/src/index.ts`" in issue
        for issue in issues
    )

    tampered = deepcopy(structured)
    tampered["work_units"][0]["component_id"] = "database"
    issues = component_plan_issues(
        structured_plan=tampered,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    )
    assert any("immutable field `component_id`" in issue for issue in issues)

    invalid_typecheck = deepcopy(structured)
    invalid_typecheck["work_units"][0]["test_intents"][1]["target_paths"] = [
        invalid_typecheck["work_units"][0]["test_intents"][0]["target_paths"][0]
    ]
    issues = component_plan_issues(
        structured_plan=invalid_typecheck,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    )
    assert any("invalid test target path" in issue for issue in issues)

    stale_projection = deepcopy(structured)
    stale_projection["design_mappings"].append(
        {
            "design_element_id": "class:LegacyPlaceholder",
            "planned_code_target": "apps/api/src/Legacy Placeholder.ts",
        }
    )
    issues = component_plan_issues(
        structured_plan=stale_projection,
        expected_units=first,
        expected_inputs=inputs,
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
    )
    assert any("outside work-unit code targets" in issue for issue in issues)


def test_branch_units_depend_on_and_follow_primary_main_units() -> None:
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "requirement_v2" / "uc2.json").read_text(
            encoding="utf-8"
        )
    )
    model = RequirementModelV2.from_dict(payload)
    planning, _ = _policy()
    graph = new_vertical_design_graph(
        run_id=model.run_id,
        snapshot_binding={
            "requirement_digest": "sha256:req",
            "repo_landscape_digest": "sha256:repo",
            "workspace_ref": "run_workspace_v1",
            "base_sha": "abc123",
        },
    )
    main_scenario = next(item for item in model.scenario_contract["scenarios"] if item["id"] == "SC-1")
    branch_scenario = next(item for item in model.scenario_contract["scenarios"] if item["id"] == "SC-2")
    graph, main_refs = apply_designer_delta(
        graph,
        {
            "schema_version": DELTA_VERSION,
            "base_graph_digest": graph["artifact_digest"],
            "partition": {"use_case_id": model.use_case["id"], "scenario_id": "SC-1", "component_id": "web"},
            "operations": [
                {"op": "set_partition_coverage", "coverage": {"ui": "covered"}},
                {
                    "op": "add_node",
                    "node": {
                        "temp_ref": "tmp:main-ui",
                        "semantic_key": "SC-1:web:ui",
                        "name": "Main UI",
                        "kind": "ui_interaction",
                        "surface": "ui",
                        "disposition": "add",
                        "requirement_refs": main_scenario["surfaces"]["ui"]["requirement_refs"],
                        "evidence_refs": [],
                        "evidence_reason": "Main UI is new.",
                        "contract": "Render the main flow.",
                    },
                },
            ],
            "completion_claims": [],
            "unresolved_items": [],
        },
        valid_evidence_refs=set(),
    )
    graph, _ = apply_designer_delta(
        graph,
        {
            "schema_version": DELTA_VERSION,
            "base_graph_digest": graph["artifact_digest"],
            "partition": {"use_case_id": model.use_case["id"], "scenario_id": "SC-2", "component_id": "web"},
            "operations": [
                {"op": "set_partition_coverage", "coverage": {"ui": "covered"}},
                {
                    "op": "add_node",
                    "node": {
                        "temp_ref": "tmp:branch-ui",
                        "semantic_key": "SC-2:web:ui",
                        "name": "Cancel UI",
                        "kind": "ui_interaction",
                        "surface": "ui",
                        "disposition": "add",
                        "requirement_refs": branch_scenario["surfaces"]["ui"]["requirement_refs"],
                        "evidence_refs": [],
                        "evidence_reason": "Cancellation UI is new.",
                        "contract": "Cancel the main flow.",
                    },
                },
                {
                    "op": "add_edge",
                    "edge": {
                        "from": "tmp:branch-ui",
                        "to": main_refs["tmp:main-ui"],
                        "kind": "depends_on",
                        "label": "Cancellation depends on the main delete flow",
                        "scenario_id": "SC-2",
                        "branch_group": "",
                        "guard": "",
                        "terminal": False,
                    },
                },
            ],
            "completion_claims": [],
            "unresolved_items": [],
        },
        valid_evidence_refs=set(),
    )
    trace = build_vertical_trace_graph(requirement_model=model, design_graph=graph)
    units = build_component_work_units(
        design_graph=graph,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )

    main_unit = next(item for item in units if item["scenario_id"] == "SC-1")
    branch_unit = next(item for item in units if item["scenario_id"] == "SC-2")
    assert branch_unit["depends_on"] == [main_unit["unit_id"]]
    assert main_unit["order"] < branch_unit["order"]


def test_runtime_binding_seeds_target_and_composition_owner_into_phase3_unit() -> None:
    model, design, _ = _fixture()
    planning, verification = _policy()
    ui_node = next(item for item in design["nodes"] if item["surface"] == "ui")
    partition = next(
        item
        for item in design["partitions"]
        if item["scenario_id"] == ui_node["scenario_id"]
        and item["component_id"] == ui_node["component_id"]
    )
    design, _ = apply_designer_delta(
        design,
        {
            "schema_version": DELTA_VERSION,
            "base_graph_digest": design["artifact_digest"],
            "partition": {
                "use_case_id": partition["use_case_id"],
                "scenario_id": partition["scenario_id"],
                "component_id": partition["component_id"],
                "source_observation_refs": ["repo:apps/web/src/AppShell.tsx#L1@sha256:source"],
            },
            "operations": [
                {
                    "op": "update_node",
                    "node_id": ui_node["id"],
                    "changes": {
                        "evidence_refs": ["RLA-app-shell", "RLA-main-entry"],
                        "runtime_binding": {
                            "schema_version": RUNTIME_BINDING_VERSION,
                            "runtime_kind": "ui_component",
                            "target": {
                                "component_id": "web",
                                "path": "apps/web/src/CulturalHeritageDeleteScreen.tsx",
                                "symbol": "CulturalHeritageDeleteScreen",
                            },
                            "attachment": {
                                "relation": "rendered_by",
                                "owner_ref": "RLA-app-shell",
                                "owner_path": "apps/web/src/AppShell.tsx",
                                "owner_symbol": "AppShell",
                            },
                            "evidence_refs": ["RLA-app-shell"],
                        }
                    },
                }
            ],
            "completion_claims": [],
            "unresolved_items": [],
        },
        valid_evidence_refs={"RLA-app-shell", "RLA-main-entry"},
    )
    trace = build_vertical_trace_graph(requirement_model=model, design_graph=design)
    units = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={
            "anchors": [
                {
                    "anchor_id": "RLA-app-shell",
                    "component_id": "web",
                    "path": "apps/web/src/AppShell.tsx",
                    "symbol": "AppShell",
                },
                {
                    "anchor_id": "RLA-main-entry",
                    "component_id": "web",
                    "path": "apps/web/src/main.tsx",
                    "symbol": "main",
                },
            ]
        },
        planning_policy=planning,
    )
    web_unit = next(item for item in units if item["component_id"] == "web")
    targets = {
        (str(item.get("path") or ""), str(item.get("symbol") or ""))
        for item in web_unit["planned_code_targets"]
    }
    assert ("apps/web/src/CulturalHeritageDeleteScreen.tsx", "CulturalHeritageDeleteScreen") in targets
    assert ("apps/web/src/AppShell.tsx", "AppShell") in targets
    assert ("apps/web/src/main.tsx", "main") not in targets
    assert web_unit["repo_evidence_refs"] == ["RLA-app-shell", "RLA-main-entry"]
    assert web_unit["runtime_bindings"][0]["attachment"]["owner_symbol"] == "AppShell"

    mappings = component_plan_projections(
        units=units,
        design_graph=design,
        requirement_graph=build_requirement_graph_v2(model),
        legacy_mappings=[],
        planning_policy=planning,
    )["design_mappings"]
    ui_mapping = next(item for item in mappings if item["design_element_id"] == ui_node["id"])
    assert ui_mapping["planned_code_target"] == (
        "apps/web/src/CulturalHeritageDeleteScreen.tsx#CulturalHeritageDeleteScreen"
    )

    polluted_units = deepcopy(units)
    polluted_web = next(item for item in polluted_units if item["component_id"] == "web")
    polluted_web["planned_code_targets"].append(
        {"path": "apps/web/src/main.tsx", "symbol": "main", "action": "create_or_update"}
    )
    polluted_units = canonicalize_work_unit_dependencies(polluted_units)
    projections = component_plan_projections(
        units=polluted_units,
        design_graph=design,
        requirement_graph=build_requirement_graph_v2(model),
        legacy_mappings=[],
        planning_policy=planning,
    )
    issues = component_plan_issues(
        structured_plan={
            "version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
            "inputs": {},
            "work_units": polluted_units,
            **projections,
        },
        expected_units=units,
        expected_inputs={},
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
        design_graph=design,
        trace_graph=trace,
        repo_landscape={
            "anchors": [
                {
                    "anchor_id": "RLA-main-entry",
                    "component_id": "web",
                    "path": "apps/web/src/main.tsx",
                    "symbol": "main",
                }
            ]
        },
    )
    assert any("main.tsx#main` outside runtime wiring" in issue for issue in issues)
    assert any("no connected runtime work-unit path" in issue for issue in issues)


def test_legacy_projection_rebinds_to_real_work_unit_targets() -> None:
    _, design, trace = _fixture()
    planning, _ = _policy()
    skeleton = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    api_unit = next(item for item in skeleton if item["component_id"] == "api")
    units = merge_work_unit_updates(
        units=skeleton,
        updates=[
            {
                "unit_id": api_unit["unit_id"],
                "planned_code_targets": [
                    {"path": "apps/api/src/app.ts", "symbol": "createApp"},
                    {"path": "apps/api/src/sc1-api.ts", "symbol": "handleSc1Api"},
                ],
            }
        ],
        planning_policy=planning,
    )
    mappings = rebind_legacy_mappings_to_work_units(
        units=units,
        design_graph=design,
        legacy_mappings=[
            {
                "design_element_id": "class:SC-1 api",
                "design_element_type": "class",
                "planned_code_target": "apps/api/src/SC-1 api.ts",
                "component_id": "api",
            },
            {
                "design_element_id": "method:SC-1 api.execute",
                "design_element_type": "method",
                "planned_code_target": "apps/api/src/SC-1 api.ts#execute",
                "component_id": "api",
            },
            {
                "design_element_id": "sequence:SC-1:1",
                "design_element_type": "sequence_message",
                "planned_code_target": "apps/api/src/SC-1 api.ts#execute",
                "component_id": "api",
                "trace_policy": "direct_symbol",
            },
            {
                "design_element_id": "sequence:SC-1:2",
                "design_element_type": "sequence_message",
                "planned_code_target": "apps/api/src/SC-1 api.ts",
                "component_id": "api",
                "trace_policy": "return_behavior",
            },
        ],
        planning_policy=planning,
    )

    by_id = {item["design_element_id"]: item for item in mappings}
    assert by_id["class:SC-1 api"]["planned_code_target"] == "apps/api/src/sc1-api.ts"
    assert (
        by_id["method:SC-1 api.execute"]["planned_code_target"]
        == "apps/api/src/sc1-api.ts#handleSc1Api"
    )
    assert (
        by_id["sequence:SC-1:1"]["planned_code_target"]
        == "apps/api/src/sc1-api.ts#handleSc1Api"
    )
    assert by_id["sequence:SC-1:2"]["planned_code_target"] == "apps/api/src/sc1-api.ts"


def test_legacy_method_keeps_method_symbol_when_target_is_a_typescript_class() -> None:
    _, design, trace = _fixture()
    planning, _ = _policy()
    skeleton = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    api_unit = next(item for item in skeleton if item["component_id"] == "api")
    units = merge_work_unit_updates(
        units=skeleton,
        updates=[
            {
                "unit_id": api_unit["unit_id"],
                "planned_code_targets": [
                    {
                        "path": "apps/api/src/CulturalHeritage.ts",
                        "symbol": "CulturalHeritage",
                    }
                ],
            }
        ],
        planning_policy=planning,
    )
    mappings = rebind_legacy_mappings_to_work_units(
        units=units,
        design_graph=design,
        legacy_mappings=[
            {
                "design_element_id": "method:SC-1 api.execute",
                "design_element_type": "method",
                "planned_code_target": "apps/api/src/SC-1 api.ts#execute",
                "component_id": "api",
            }
        ],
        planning_policy=planning,
    )

    assert (
        mappings[0]["planned_code_target"]
        == "apps/api/src/CulturalHeritage.ts#CulturalHeritage.execute"
    )


def test_legacy_methods_bind_to_their_matching_concrete_targets() -> None:
    _, design, trace = _fixture()
    planning, _ = _policy()
    skeleton = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    api_unit = next(item for item in skeleton if item["component_id"] == "api")
    units = merge_work_unit_updates(
        units=skeleton,
        updates=[
            {
                "unit_id": api_unit["unit_id"],
                "planned_code_targets": [
                    {
                        "path": "apps/api/src/cultural-heritage.ts",
                        "symbol": "CulturalHeritageEndpoint.submitData",
                    },
                    {
                        "path": "apps/api/src/cultural-heritage.ts",
                        "symbol": "CulturalHeritageEndpoint.confirmOperation",
                    },
                ],
            }
        ],
        planning_policy=planning,
    )
    mappings = rebind_legacy_mappings_to_work_units(
        units=units,
        design_graph=design,
        legacy_mappings=[
            {
                "design_element_id": "method:SC-1 api.submitData",
                "design_element_type": "method",
                "planned_code_target": "apps/api/src/SC-1 api.ts#submitData",
                "component_id": "api",
            },
            {
                "design_element_id": "method:SC-1 api.confirmOperation",
                "design_element_type": "method",
                "planned_code_target": "apps/api/src/SC-1 api.ts#confirmOperation",
                "component_id": "api",
            },
        ],
        planning_policy=planning,
    )

    by_id = {item["design_element_id"]: item for item in mappings}
    assert by_id["method:SC-1 api.submitData"]["planned_code_target"].endswith(
        "#CulturalHeritageEndpoint.submitData"
    )
    assert by_id["method:SC-1 api.confirmOperation"]["planned_code_target"].endswith(
        "#CulturalHeritageEndpoint.confirmOperation"
    )


def test_legacy_rebind_prefers_primary_behavior_targets_over_supporting_targets() -> None:
    planning, _ = _policy()
    mappings = rebind_legacy_mappings_to_work_units(
        units=[
            {
                "unit_id": "WU-WEB",
                "component_id": "web",
                "design_node_ids": ["VDN-WEB"],
                "planned_code_targets": [
                    {
                        "path": "apps/web/src/CulturalHeritageDuplicateNotice.tsx",
                        "symbol": "CulturalHeritageDuplicateNotice",
                    },
                    {
                        "path": "apps/web/src/CulturalHeritageInsertionForm.tsx",
                        "symbol": "CulturalHeritageInsertionForm",
                    },
                ],
            },
            {
                "unit_id": "WU-API",
                "component_id": "api",
                "design_node_ids": ["VDN-API"],
                "planned_code_targets": [
                    {
                        "path": "apps/api/src/cultural-heritage-duplicate-rule.ts",
                        "symbol": "detectCulturalHeritageDuplicate",
                    },
                    {
                        "path": "apps/api/src/cultural-heritage-duplicate.ts",
                        "symbol": "rejectDuplicateCulturalHeritage",
                    },
                    {
                        "path": "packages/contracts/src/cultural-heritage.ts",
                        "symbol": "CulturalHeritageDuplicateCheckResult",
                    },
                ],
            },
        ],
        design_graph={
            "nodes": [
                {
                    "id": "VDN-WEB",
                    "name": "Cultural Heritage Duplicate Rejection Notice",
                },
                {
                    "id": "VDN-API",
                    "name": "Cultural Heritage Duplicate Rejection Endpoint",
                },
            ]
        },
        legacy_mappings=[
            {
                "design_element_id": "method:Cultural Heritage Duplicate Rejection Notice.execute",
                "design_element_type": "method",
                "planned_code_target": (
                    "apps/web/src/Cultural Heritage Duplicate Rejection Notice.tsx#execute"
                ),
                "component_id": "web",
            },
            {
                "design_element_id": "method:Cultural Heritage Duplicate Rejection Endpoint.execute",
                "design_element_type": "method",
                "planned_code_target": (
                    "apps/api/src/Cultural Heritage Duplicate Rejection Endpoint.ts#execute"
                ),
                "component_id": "api",
            },
        ],
        planning_policy=planning,
    )

    by_id = {item["design_element_id"]: item for item in mappings}
    assert by_id[
        "method:Cultural Heritage Duplicate Rejection Notice.execute"
    ]["planned_code_target"] == (
        "apps/web/src/CulturalHeritageDuplicateNotice.tsx#CulturalHeritageDuplicateNotice"
    )
    assert by_id[
        "method:Cultural Heritage Duplicate Rejection Endpoint.execute"
    ]["planned_code_target"] == (
        "apps/api/src/cultural-heritage-duplicate.ts#rejectDuplicateCulturalHeritage"
    )


def test_legacy_rebind_uses_signature_symbol_only_when_work_unit_owns_it() -> None:
    planning, _ = _policy()
    mappings = rebind_legacy_mappings_to_work_units(
        units=[
            {
                "unit_id": "WU-API",
                "component_id": "api",
                "design_node_ids": ["VDN-API"],
                "planned_code_targets": [
                    {
                        "path": "apps/api/src/cultural-heritage.ts",
                        "symbol": "submitCulturalHeritage",
                    },
                    {
                        "path": "apps/api/src/cultural-heritage.ts",
                        "symbol": "confirmCulturalHeritageInsertion",
                    },
                ],
            }
        ],
        design_graph={
            "nodes": [
                {
                    "id": "VDN-API",
                    "name": "Cultural Heritage Insertion Endpoint",
                }
            ]
        },
        legacy_mappings=[
            {
                "design_element_id": (
                    "method:Cultural Heritage Insertion Endpoint."
                    "insertionFormSubmitsCulturalHeritageDataToInsertionEndpoint"
                ),
                "design_element_type": "method",
                "planned_code_target": (
                    "apps/api/src/Cultural Heritage Insertion Endpoint.ts#execute"
                ),
                "component_id": "api",
            },
            {
                "design_element_id": (
                    "method:Cultural Heritage Insertion Endpoint."
                    "operatorConfirmsTheOperationFromTheInsertionForm"
                ),
                "design_element_type": "method",
                "planned_code_target": (
                    "apps/api/src/Cultural Heritage Insertion Endpoint.ts#execute"
                ),
                "component_id": "api",
            },
        ],
        planning_policy=planning,
        signature_entries=[
            {
                "design_element_id": (
                    "method:Cultural Heritage Insertion Endpoint."
                    "insertionFormSubmitsCulturalHeritageDataToInsertionEndpoint"
                ),
                "qualified_name": "submitCulturalHeritage",
            },
            {
                "design_element_id": (
                    "method:Cultural Heritage Insertion Endpoint."
                    "operatorConfirmsTheOperationFromTheInsertionForm"
                ),
                "qualified_name": "confirmCulturalHeritageInsertion",
            },
        ],
    )

    targets = {
        item["design_element_id"]: item["planned_code_target"] for item in mappings
    }
    assert targets[
        "method:Cultural Heritage Insertion Endpoint."
        "insertionFormSubmitsCulturalHeritageDataToInsertionEndpoint"
    ].endswith("#submitCulturalHeritage")
    assert targets[
        "method:Cultural Heritage Insertion Endpoint."
        "operatorConfirmsTheOperationFromTheInsertionForm"
    ].endswith("#confirmCulturalHeritageInsertion")


def test_repair_patch_preserves_required_migration_target_when_omitted() -> None:
    _, design, trace = _fixture()
    planning, _ = _policy()
    skeleton = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    data_unit = next(item for item in skeleton if item["component_id"] == "database")
    enriched = merge_work_unit_updates(
        units=skeleton,
        updates=[
            {
                "unit_id": data_unit["unit_id"],
                "migration_impact": {
                    "status": "required",
                    "reason": "The approved data model requires a schema change.",
                    "targets": [
                        "packages/db/prisma/migrations/20261027000100_change/migration.sql"
                    ],
                },
            }
        ],
        planning_policy=planning,
    )
    repaired = merge_work_unit_updates(
        units=enriched,
        updates=[
            {
                "unit_id": data_unit["unit_id"],
                "migration_impact": {
                    "status": "required",
                    "reason": "Keep the approved schema change.",
                },
            }
        ],
        planning_policy=planning,
    )

    current = next(item for item in repaired if item["unit_id"] == data_unit["unit_id"])
    assert current["migration_impact"]["targets"] == [
        "packages/db/prisma/migrations/20261027000100_change/migration.sql"
    ]


def test_shared_artifacts_add_harness_owned_serial_dependencies() -> None:
    units = [
        {
            "unit_id": "WU-A",
            "order": 1,
            "scenario_id": "SC-1",
            "component_id": "api",
            "depends_on": [],
            "planned_code_targets": [{"path": "apps/api/src/app.ts"}],
            "test_intents": [],
        },
        {
            "unit_id": "WU-B",
            "order": 2,
            "scenario_id": "SC-2",
            "component_id": "api",
            "depends_on": [],
            "planned_code_targets": [{"path": "apps/api/src/app.ts"}],
            "test_intents": [],
        },
        {
            "unit_id": "WU-C",
            "order": 3,
            "scenario_id": "SC-3",
            "component_id": "web",
            "depends_on": [],
            "planned_code_targets": [{"path": "apps/web/src/Form.tsx"}],
            "test_intents": [
                {"target_paths": ["apps/api/src/app.ts"]},
            ],
        },
    ]

    canonical = canonicalize_work_unit_dependencies(units)
    by_id = {item["unit_id"]: item for item in canonical}

    assert by_id["WU-B"]["depends_on"] == ["WU-A"]
    assert by_id["WU-C"]["depends_on"] == ["WU-B"]
    assert [item["order"] for item in canonical] == [1, 2, 3]


def test_required_migration_needs_concrete_sql_artifact() -> None:
    model, design, trace = _fixture()
    planning, verification = _policy()
    skeleton = build_component_work_units(
        design_graph=design,
        trace_graph=trace,
        repo_landscape={"anchors": []},
        planning_policy=planning,
    )
    data_unit = next(item for item in skeleton if item["component_id"] == "database")
    units = merge_work_unit_updates(
        units=skeleton,
        updates=[
            {
                "unit_id": data_unit["unit_id"],
                "planned_code_targets": [
                    {"path": "packages/db/prisma/schema.prisma", "symbol": "Heritage"}
                ],
                "test_intents": [
                    {
                        "stage_id": "unit",
                        "intent": "Verify persistence.",
                        "acceptance_refs": data_unit["acceptance_refs"],
                        "target_paths": ["packages/db/src/heritage.test.ts"],
                    }
                ],
                "migration_impact": {
                    "status": "required",
                    "reason": "The approved data model changes.",
                    "targets": ["packages/db/prisma/schema.prisma"],
                },
            }
        ],
        planning_policy=planning,
    )
    structured = {
        "version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
        "inputs": {},
        "work_units": canonicalize_work_unit_dependencies(units),
        "implementation_steps": [],
        "proposed_files": [],
        "design_mappings": [],
        "test_intents": [],
    }
    issues = component_plan_issues(
        structured_plan=structured,
        expected_units=skeleton,
        expected_inputs={},
        planning_policy=planning,
        verification_stage_ids=list(verification.stage_ids),
        design_graph=design,
        trace_graph=trace,
    )

    assert any("no concrete Prisma migration SQL target" in issue for issue in issues)
    assert not any("schema.prisma#Heritage` outside runtime wiring" in issue for issue in issues)


def test_markdown_verification_plan_renders_exact_targets() -> None:
    markdown = _markdown_plan(
        structured={
            "version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
            "verification_stage_ids": ["unit"],
            "test_intents": [
                {
                    "stage_id": "unit",
                    "intent": "Verify the approved behavior.",
                    "target_paths": ["apps/api/src/heritage.test.ts"],
                }
            ],
        },
        requirement_packet="",
        design_artifact="",
    )

    assert "- `apps/api/src/heritage.test.ts`: Verify the approved behavior." in markdown
    assert "- None:" not in markdown


def test_missing_contract_marker_keeps_historical_v3() -> None:
    assert resolve_implementation_plan_contract({}) == PROFILED_IMPLEMENTATION_PLAN_VERSION
    assert resolve_implementation_plan_contract(
        {"implementation_plan_contract_version": COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION}
    ) == COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION
