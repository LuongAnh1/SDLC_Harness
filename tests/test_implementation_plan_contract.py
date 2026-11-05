import json
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_harness.agents.developer.planner import (
    DeveloperPlanAgent,
    _complete_phase3_json,
    _apply_design_adjustments,
    _apply_design_adjustments_to_steps,
    _canonical_proposed_files,
    _design_adjustments,
    _design_mappings,
    _ensure_executable_test_files,
    _signature_patch_for_repair,
)
from sdlc_harness.agents.developer.work_packet import (
    _build_execution_units,
    _derive_cross_package_dependencies,
    _signature_ids_for_targets,
    build_developer_work_packet,
    refresh_developer_work_packet,
    upgrade_developer_dependency_scope,
)
from sdlc_harness.agents.validator.plan_review import (
    ImplementationPlanReview,
    ImplementationPlanValidator,
    _llm_review_prompt,
)
from sdlc_harness.core.config import LlmCallProfile
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.interfaces.cli import build_parser
from sdlc_harness.profiles import (
    build_planning_path_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.workflows.phase3 import _next_implementation_plan_version


class _ProfileCaptureLlm:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.settings = SimpleNamespace(
            llm_structured_max_tokens=8192,
            phase3_llm_profiles={
                "developer_plan": LlmCallProfile("plan-model", "high", 49152, 420),
                "developer_repair": LlmCallProfile("repair-model", "max", 65536, 600),
                "plan_validator": LlmCallProfile("validator-model", "high", 32768, 300),
            },
        )

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        return LlmCompletion(
            content=json.dumps(
                {
                    "ok": True,
                    "summary": "accepted",
                    "coverage_issues": [],
                    "requirement_coverage_issues": [],
                    "traceability_issues": [],
                    "implementation_quality_issues": [],
                    "test_issues": [],
                    "scope_issues": [],
                    "developer_feedback": "",
                }
            )
        )


def test_phase3_developer_and_validator_calls_use_individual_profiles() -> None:
    client = _ProfileCaptureLlm()

    _complete_phase3_json(
        client,
        system="developer",
        prompt="plan",
        profile_name="developer_plan",
    )
    _complete_phase3_json(
        client,
        system="repair",
        prompt="repair plan",
        profile_name="developer_repair",
    )
    ImplementationPlanValidator(client).review_llm(
        structured_plan={},
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph={},
        sequence_graph={},
        rule_review=ImplementationPlanReview(ok=True, mode="rules"),
    )

    assert [
        (call["model"], call["reasoning_effort"], call["max_tokens"], call["timeout_seconds"])
        for call in client.calls
    ] == [
        ("plan-model", "high", 49152, 420),
        ("repair-model", "max", 65536, 600),
        ("validator-model", "high", 32768, 300),
    ]
    assert all(call["json_mode"] is True for call in client.calls)


def test_execution_unit_signatures_are_scoped_by_path_and_symbol() -> None:
    signatures = [
        {
            "contract_id": "SIG-CREATE",
            "source_path": "packages/db/src/cultural-heritage.ts",
            "qualified_name": "createCulturalHeritage",
        },
        {
            "contract_id": "SIG-QUERY",
            "source_path": "packages/db/src/cultural-heritage.ts",
            "qualified_name": "queryCulturalHeritageByIdentity",
        },
    ]

    selected = _signature_ids_for_targets(
        signatures=signatures,
        targets=[
            {
                "path": "packages/db/src/cultural-heritage.ts",
                "symbol": "createCulturalHeritage",
            }
        ],
    )

    assert selected == ["SIG-CREATE"]


def test_execution_unit_adds_shared_boundary_dependency_targets_as_read_context() -> None:
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    provider_path = "apps/api/src/cultural-heritage-verification.ts"
    contract_path = "packages/contracts/src/cultural-heritage.ts"
    consumer_path = "apps/web/src/CulturalHeritageInsertionForm.tsx"

    units = _build_execution_units(
        structured_plan={
            "implementation_signature_contract": {"entries": []},
            "work_units": [
                {
                    "unit_id": "WU-PROVIDER",
                    "order": 1,
                    "depends_on": [],
                    "design_edge_ids": ["EDGE-400"],
                    "planned_code_targets": [
                        {"path": provider_path, "symbol": "verifyCulturalHeritageData"},
                        {"path": contract_path, "symbol": "CulturalHeritageContract"},
                    ],
                    "test_intents": [],
                },
                {
                    "unit_id": "WU-CONSUMER",
                    "order": 2,
                    "depends_on": ["WU-PROVIDER"],
                    "design_edge_ids": ["EDGE-400"],
                    "planned_code_targets": [
                        {"path": consumer_path, "symbol": "CulturalHeritageInsertionForm"},
                    ],
                    "test_intents": [],
                },
            ],
        },
        allowed_paths=[provider_path, contract_path, consumer_path],
        planning_policy=planning_policy,
        verification_policy=verification_policy,
        run_id="run_boundary_context",
    )

    consumer = next(unit for unit in units if unit["unit_id"] == "WU-CONSUMER")
    assert consumer["write_paths"] == [consumer_path]
    assert consumer["context_paths"] == [consumer_path, provider_path, contract_path]
    assert consumer["contract_anchor_paths"] == [provider_path, contract_path]


def test_cross_package_symbol_dependency_emits_tool_owned_contract(tmp_path: Path) -> None:
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    api_manifest = tmp_path / "apps/api/package.json"
    db_manifest = tmp_path / "packages/db/package.json"
    api_manifest.parent.mkdir(parents=True)
    db_manifest.parent.mkdir(parents=True)
    api_manifest.write_text(
        json.dumps({"name": "@example/gateway", "version": "3.0.0", "dependencies": {}}),
        encoding="utf-8",
    )
    db_manifest.write_text(
        json.dumps({"name": "@example/storage", "version": "2.4.1"}),
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"private": True, "packageManager": "npm@10.9.0"}),
        encoding="utf-8",
    )
    units = [
        {
            "unit_id": "WU-STORAGE",
            "component_id": "database",
            "depends_on": [],
            "design_edge_ids": ["EDGE-DIRECT"],
            "planned_code_targets": [
                {"path": "packages/db/src/widget.ts", "symbol": "persistWidget"}
            ],
        },
        {
            "unit_id": "WU-GATEWAY",
            "component_id": "api",
            "depends_on": ["WU-STORAGE"],
            "design_edge_ids": ["EDGE-DIRECT"],
            "planned_code_targets": [
                {"path": "apps/api/src/widget.ts", "symbol": "createWidgetRoute"}
            ],
            "boundary_contracts": [{"provider_operation": "persistWidget"}],
        },
    ]

    requirements = _derive_cross_package_dependencies(
        units=units,
        target_repo_path=tmp_path,
        planning_policy=planning_policy,
        run_id="run_package_scope",
    )

    assert len(requirements) == 1
    requirement = requirements[0]
    assert str(requirement["dependency_id"]).startswith("DEP-")
    assert requirement["required_by_unit_ids"] == ["WU-GATEWAY"]
    assert requirement["package_manager"] == "npm"
    assert requirement["lockfile_path"] == "package-lock.json"
    assert requirement["consumer_manifest_path"] == "apps/api/package.json"
    assert requirement["consumer_package_name"] == "@example/gateway"
    assert requirement["dependency_name"] == "@example/storage"
    assert requirement["dependency_spec"] == "2.4.1"
    assert requirement["provider_manifest_path"] == "packages/db/package.json"
    assert requirement["provider_symbols"] == ["persistWidget"]


def test_dependency_scope_upgrade_does_not_open_manifest_for_http_boundary(tmp_path: Path) -> None:
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    for path, manifest in (
        ("apps/web/package.json", {"name": "@example/web", "version": "1.0.0"}),
        ("apps/api/package.json", {"name": "@example/api", "version": "1.0.0"}),
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest), encoding="utf-8")
    packet = {
        "execution_units": [
            {
                "unit_id": "WU-API",
                "component_id": "api",
                "depends_on": [],
                "design_edge_ids": ["EDGE-HTTP"],
                "planned_code_targets": [
                    {"path": "apps/api/src/widget.ts", "symbol": "widgetRoute"}
                ],
                "write_paths": ["apps/api/src/widget.ts"],
            },
            {
                "unit_id": "WU-WEB",
                "component_id": "web",
                "depends_on": ["WU-API"],
                "design_edge_ids": ["EDGE-HTTP"],
                "planned_code_targets": [
                    {"path": "apps/web/src/Widget.tsx", "symbol": "Widget"}
                ],
                "boundary_contracts": [{"provider_operation": "GET /widgets/{id}"}],
                "write_paths": ["apps/web/src/Widget.tsx"],
            },
        ],
        "proposed_files": [],
        "current_files": [],
        "output_contract": {
            "allowed_paths": ["apps/api/src/widget.ts", "apps/web/src/Widget.tsx"]
        },
    }

    upgraded, changed = upgrade_developer_dependency_scope(
        packet=packet,
        target_repo_path=tmp_path,
        planning_policy=planning_policy,
        run_id="run_http_scope",
    )

    assert changed is False
    assert upgraded is packet


def test_execution_unit_preserves_approved_ui_experience_contract() -> None:
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    source_path = "apps/web/src/CulturalHeritageInsertionForm.tsx"
    experience = {
        "schema_version": "ui_experience_contract_v1",
        "component_structure": ["Form, confirmation dialog, and result feedback."],
        "feedback": [{"trigger": "submit", "response": "Show pending then success or error."}],
        "accessibility": ["Keep labels and keyboard focus visible."],
        "responsive": ["Avoid horizontal overflow on small screens."],
        "focus_flow": ["Move focus into the dialog and restore it on close."],
        "guidance_refs": ["uiux:sha256:guidance"],
    }

    units = _build_execution_units(
        structured_plan={
            "implementation_signature_contract": {"entries": []},
            "work_units": [
                {
                    "unit_id": "WU-UI",
                    "order": 1,
                    "scenario_id": "SC-1",
                    "component_id": "web",
                    "surfaces": ["ui"],
                    "depends_on": [],
                    "design_node_ids": ["VDN-UI"],
                    "design_edge_ids": [],
                    "planned_code_targets": [
                        {"path": source_path, "symbol": "CulturalHeritageInsertionForm"}
                    ],
                    "surface_contracts": [
                        {
                            "design_node_id": "VDN-UI",
                            "surface": "ui",
                            "surface_contract": {
                                "schema_version": "design_surface_contract_v1",
                                "surface": "ui",
                                "experience_contract": experience,
                            },
                        }
                    ],
                    "test_intents": [],
                }
            ],
        },
        allowed_paths=[source_path],
        planning_policy=planning_policy,
        verification_policy=verification_policy,
        run_id="run_ui_contract",
    )

    assert units[0]["surface_contracts"][0]["surface_contract"]["experience_contract"] == experience


def _graphs():
    class_graph = {
        "nodes": [
            {"className": "InsertionFormView", "methods": [{"name": "displayForm"}]},
            {"className": "InsertCulturalHeritageController", "methods": [{"name": "activateInsertion"}]},
            {"className": "IdempotencyGuard", "methods": [{"name": "isDuplicate"}]},
            {"className": "ETourPort", "methods": [{"name": "insert"}]},
        ]
    }
    sequence_graph = {
        "sequenceDiagrams": [
            {
                "id": "insert-flow",
                "nodes": [
                    {"id": "AO", "label": "Agency Operator"},
                    {"id": "View", "label": "InsertionFormView"},
                    {"id": "Ctrl", "label": "InsertCulturalHeritageController"},
                    {"id": "Guard", "label": "IdempotencyGuard"},
                    {"id": "Port", "label": "ETourPort"},
                ],
                "messages": [
                    {"from": "View", "to": "Ctrl", "message": "activateInsertion()", "type": "synchronous", "arrow": "->"},
                    {"from": "Guard", "to": "Ctrl", "message": "false", "type": "return", "arrow": "-->"},
                    {"from": "Port", "to": "Ctrl", "message": "ConnectionException", "type": "lost_or_exception", "arrow": "--x"},
                    {"from": "AO", "to": "View", "message": "submit(data)", "type": "synchronous", "arrow": "->"},
                ],
            }
        ]
    }
    return class_graph, sequence_graph


def test_profiled_work_packet_uses_v2_and_revalidates_execution_contract(tmp_path: Path):
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    source_path = "apps/api/src/ProfileBoundary.ts"
    plan = {
        "version": "implementation_plan_structured_v2",
        "planning_profile": planning_policy.metadata(),
        "target_repository": {},
        "implementation_steps": [{"order": 1, "files": [source_path]}],
        "proposed_files": [{"path": source_path}],
        "design_mappings": [],
        "verification_stage_ids": list(verification_policy.stage_ids),
        "test_intents": [{"stage_id": "unit"}],
    }

    packet = build_developer_work_packet(
        run_id="run_profiled_packet",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        structured_plan=plan,
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={},
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )

    assert packet["version"] == "developer_work_packet_v1"
    assert packet["schema_version"] == "developer_work_packet_profiled_v1"
    assert packet["output_contract"]["path_policy"] == {
        "version": "editable_path_policy_v1",
        "project_profile_id": "typescript_fullstack_v1",
        "language_profile_id": "typescript_v1",
        "planning_policy_version": "planning_path_policy_v1",
        "component_roots": {
            "web": ["apps/web/src"],
            "api": ["apps/api/src"],
            "contracts": ["packages/contracts/src"],
            "database": ["packages/db/src", "packages/db/prisma"],
            "e2e": ["tests/e2e"],
        },
        "protected_paths": list(project.protected_paths),
    }
    tampered = json.loads(json.dumps(plan))
    tampered["test_intents"][0]["command"] = "npm test"
    with pytest.raises(HarnessError) as context:
        build_developer_work_packet(
            run_id="run_profiled_packet",
            source_ref="fixture:profiled",
            issue_key="SDLC-1",
            structured_plan=tampered,
            target_repo_path=tmp_path,
            run_workspace={},
            approved_plan_refs={},
            planning_policy=planning_policy,
            verification_policy=verification_policy,
        )
    assert context.value.error_code == "implementation_plan_profile_invalid"


def test_profiled_work_packet_accepts_each_editable_component_root(tmp_path: Path):
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    paths = [
        "apps/web/src/ProfilePage.tsx",
        "apps/api/src/ProfileController.ts",
        "packages/contracts/src/ProfileResponse.ts",
        "packages/db/src/ProfileRepository.test.ts",
        "packages/db/prisma/profile.prisma",
        "tests/e2e/profile.spec.ts",
    ]
    packet = build_developer_work_packet(
        run_id="run_profiled_roots",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        structured_plan={
            "version": "implementation_plan_structured_v2",
            "planning_profile": planning_policy.metadata(),
            "target_repository": {},
            "implementation_steps": [{"order": 1, "files": paths}],
            "proposed_files": [{"path": path} for path in paths],
            "design_mappings": [],
            "verification_stage_ids": list(verification_policy.stage_ids),
            "test_intents": [{"stage_id": "unit"}],
        },
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={},
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )

    assert packet["output_contract"]["allowed_paths"] == paths


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "package.json",
        "apps/api/package.json",
        "apps/other/src/Outside.ts",
        "apps/api/src/Wrong.java",
        "apps/api/src/../Escape.ts",
        "/apps/api/src/Absolute.ts",
        "C:/repo/apps/api/src/Absolute.ts",
        "apps\\api\\src\\Backslash.ts",
    ],
)
def test_profiled_work_packet_refresh_rejects_paths_outside_frozen_policy(
    tmp_path: Path,
    unsafe_path: str,
):
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    safe_path = "apps/api/src/ProfileBoundary.ts"
    packet = build_developer_work_packet(
        run_id="run_profiled_refresh",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        structured_plan={
            "version": "implementation_plan_structured_v2",
            "planning_profile": planning_policy.metadata(),
            "target_repository": {},
            "implementation_steps": [{"order": 1, "files": [safe_path]}],
            "proposed_files": [{"path": safe_path}],
            "design_mappings": [],
            "verification_stage_ids": list(verification_policy.stage_ids),
            "test_intents": [{"stage_id": "unit"}],
        },
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={},
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )
    packet["output_contract"]["allowed_paths"] = [unsafe_path]

    with pytest.raises(HarnessError) as context:
        refresh_developer_work_packet(
            packet=packet,
            target_repo_path=tmp_path,
            version=2,
            run_id="run_profiled_refresh",
            planning_policy=planning_policy,
        )

    assert context.value.error_code == "developer_work_packet_unsafe_path"


def test_profiled_work_packet_refresh_requires_matching_frozen_policy(tmp_path: Path):
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    source_path = "apps/api/src/ProfileBoundary.ts"
    packet = build_developer_work_packet(
        run_id="run_profiled_policy",
        source_ref="fixture:profiled",
        issue_key="SDLC-1",
        structured_plan={
            "version": "implementation_plan_structured_v2",
            "planning_profile": planning_policy.metadata(),
            "target_repository": {},
            "implementation_steps": [{"order": 1, "files": [source_path]}],
            "proposed_files": [{"path": source_path}],
            "design_mappings": [],
            "verification_stage_ids": list(verification_policy.stage_ids),
            "test_intents": [{"stage_id": "unit"}],
        },
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={},
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )

    with pytest.raises(HarnessError) as missing:
        refresh_developer_work_packet(
            packet=packet,
            target_repo_path=tmp_path,
            version=2,
            run_id="run_profiled_policy",
        )
    assert missing.value.error_code == "developer_work_packet_path_policy_missing"

    packet["output_contract"]["path_policy"]["planning_policy_version"] = "tampered"
    with pytest.raises(HarnessError) as mismatch:
        refresh_developer_work_packet(
            packet=packet,
            target_repo_path=tmp_path,
            version=2,
            run_id="run_profiled_policy",
            planning_policy=planning_policy,
        )
    assert mismatch.value.error_code == "developer_work_packet_path_policy_mismatch"


def test_immutable_final_field_proposes_explicit_design_adjustment(tmp_path: Path):
    class_graph = {
        "nodes": [
            {
                "className": "CulturalHeritage",
                "methods": [{"name": "setId"}, {"name": "setName"}, {"name": "getId"}],
            }
        ]
    }
    source = tmp_path / "src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "final class CulturalHeritage { private final String id; private final String name; }",
        encoding="utf-8",
    )
    adjustments = _design_adjustments(
        class_graph=class_graph,
        class_targets={"CulturalHeritage": "src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java"},
        target_repo_path=str(tmp_path),
        requirement_design_trace={},
    )
    assert {item["design_element_id"] for item in adjustments} == {
        "method:CulturalHeritage.setId",
        "method:CulturalHeritage.setName",
    }
    assert all(item["action"] == "omit" and item["requires_human_approval"] for item in adjustments)

    mappings = _apply_design_adjustments(
        [
            {
                "design_element_id": "method:CulturalHeritage.setId",
                "planned_code_target": "src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java#setId",
                "reason": "implement setId",
                "test_intent": "test setId",
            }
        ],
        adjustments,
    )
    steps = _apply_design_adjustments_to_steps(
        [
            {
                "order": 1,
                "title": "Implement setId",
                "description": "Make fields mutable and add setId(String).",
                "files": ["src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java"],
                "design_refs": ["method:CulturalHeritage.setId"],
            }
        ],
        adjustments,
    )
    assert mappings[0]["planned_code_target"].endswith("CulturalHeritage.java")
    assert mappings[0]["implementation_status"] == "waived_by_design_adjustment"
    assert "do not add `setId`" in steps[0]["description"]
    assert "method:CulturalHeritage.setId" not in steps[0]["design_refs"]


def test_validator_rejects_unresolved_design_repo_conflict():
    class_graph, sequence_graph = _graphs()
    mappings = _design_mappings(class_graph=class_graph, sequence_graph=sequence_graph, target_files=[])
    steps = [{"order": 1, "files": [], "design_refs": []}]
    proposed = _canonical_proposed_files(proposed_files=[], implementation_steps=steps, mappings=mappings)
    review = ImplementationPlanValidator().review_rules(
        structured_plan={
            "target_repository": {"local_path": "x", "remote": "r", "base_branch": "main", "planned_branch": "b"},
            "implementation_steps": steps,
            "proposed_files": proposed,
            "design_mappings": mappings,
            "design_adjustments": [{"design_element_id": "method:X.setId", "action": "unresolved", "status": "conflict"}],
            "test_intents": [{"command": "test"}],
        },
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
    )
    assert not review.ok
    assert any("unresolved" in issue for issue in review.executability_issues)


class _RepairLlm:
    def complete(self, **_kwargs):
        return json.dumps(
            {
                "summary": "repair",
                "repo_evidence": [],
                "implementation_steps": [],
                "proposed_files": [],
                "design_mappings": [],
                "test_intents": [],
            }
        )


def test_semantic_repair_cannot_overwrite_approved_signature_bindings():
    payload = {"implementation_signature_contract": {"entries": [{"design_element_id": "method:X.run"}]}}

    assert _signature_patch_for_repair(
        llm_payload=payload,
        developer_feedback="Add the missing API client adapter target.",
    ) == {}
    assert _signature_patch_for_repair(
        llm_payload=payload,
        developer_feedback="Approved methods share one implementation signature binding.",
    ) == payload["implementation_signature_contract"]


def test_repair_preserves_candidate_and_moves_review_to_history():
    class_graph, sequence_graph = _graphs()
    project = get_project_profile("typescript_fullstack_v1")
    planning_policy = build_planning_path_policy(
        project=project,
        language=get_language_profile(project.language_profile_id),
    )
    verification_policy = build_verification_stage_policy(project=project)
    base = DeveloperPlanAgent().plan(
        run_id="run_test",
        source_ref="source",
        issue_key="SDLC-2",
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_repo_path=".",
        target_repo_remote="remote",
        target_base_branch="main",
        planned_branch="branch",
        target_files=[],
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )
    repaired = DeveloperPlanAgent(_RepairLlm()).repair_plan(
        previous_draft=base,
        review_report={"ok": False, "issue_count": 1, "implementation_quality_issues": ["fix"]},
        developer_feedback="fix",
        iteration=1,
        run_id="run_test",
        source_ref="source",
        issue_key="SDLC-2",
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph=class_graph,
        sequence_graph=sequence_graph,
        target_repo_path=".",
        target_repo_remote="remote",
        target_base_branch="main",
        planned_branch="branch",
        target_files=[],
        planning_policy=planning_policy,
        verification_policy=verification_policy,
    )
    assert repaired.structured["design_mappings"] == base.structured["design_mappings"]
    assert repaired.structured["repair_history"][0]["feedback"] == "fix"
    assert "previous_review" not in repaired.structured
    assert "repair_feedback" not in repaired.structured


def test_review_prompt_excludes_history_and_coverage():
    prompt = _llm_review_prompt(
        structured_plan={"implementation_steps": [], "repair_history": [{"secret": "old"}], "requirement_coverage": [{"id": "x"}]},
        requirement_packet="req",
        design_artifact="design",
        requirement_design_trace={},
        class_graph={},
        sequence_graph={},
        rule_review=ImplementationPlanValidator().review_rules(
            structured_plan={"target_repository": {}, "design_mappings": [], "test_intents": []},
            requirement_design_trace={},
            class_graph={},
            sequence_graph={},
        ),
    )
    assert "repair_history" not in prompt
    assert '"requirement_coverage":' not in prompt
    assert "old" not in prompt


def test_revalidation_version_and_cli_contract(tmp_path: Path):
    (tmp_path / "implementation_plan_structured_v1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "implementation_plan_structured_v6.json").write_text("{}", encoding="utf-8")
    assert _next_implementation_plan_version(tmp_path) == 7
    args = build_parser().parse_args(
        [
            "revalidate-implementation-plan",
            "--run-id",
            "run_20261016_072017_ae0bac6b",
            "--llm-mode",
            "fake",
            "--implementation-plan-repair-limit",
            "5",
            "--sync-mode",
            "mock",
        ]
    )
    assert args.command == "revalidate-implementation-plan"
    assert args.implementation_plan_repair_limit == "5"


def test_v4_review_repairs_from_blocking_issues_only_and_fails_closed_on_truncation(monkeypatch) -> None:
    from sdlc_harness.profiles import COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION

    class _Answer:
        settings = None

        def __init__(self, completion: LlmCompletion) -> None:
            self.completion = completion

        def complete_with_metadata(self, **_: object) -> LlmCompletion:
            return self.completion

    def review(completion: LlmCompletion) -> ImplementationPlanReview:
        validator = ImplementationPlanValidator(_Answer(completion))
        monkeypatch.setattr(validator, "review_rules", lambda **_: ImplementationPlanReview(ok=True, mode="rules"))
        return validator.review(
            structured_plan={
                "version": COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
                "work_units": [
                    {"unit_id": "WU-AAAAAAAAAAAA", "design_node_ids": ["VDN-1"], "design_edge_ids": []}
                ],
            },
            requirement_packet="requirement",
            design_artifact="design",
            requirement_design_trace={},
            class_graph={},
            sequence_graph={},
            canonical_requirement={"acceptance_criteria": [{"id": "AC-1"}]},
            vertical_design_graph={"nodes": [{"id": "VDN-1"}], "edges": []},
            vertical_trace_graph={"nodes": [], "edges": []},
        )

    blocked = review(
        LlmCompletion(
            content=json.dumps(
                {
                    "summary": "Warning: an ETOUR stub would help local runs.",
                    "reviewed_design_ids": ["VDN-1"],
                    "findings": [
                        {
                            "category": "test",
                            "severity": "blocking",
                            "unit_ids": ["WU-AAAAAAAAAAAA"],
                            "refs": ["AC-1", "VDN-1"],
                            "issue": "The 409 outcome is untested.",
                            "expected_change": "Add a unit intent for the 409 outcome.",
                        },
                        {
                            "category": "implementation_quality",
                            "severity": "warning",
                            "unit_ids": ["WU-AAAAAAAAAAAA"],
                            "refs": ["VDN-1"],
                            "issue": "A local ETOUR stub could help operators.",
                            "expected_change": "No plan repair is required.",
                        },
                    ],
                }
            ),
            finish_reason="stop",
        )
    )
    assert blocked.affected_unit_ids == ["WU-AAAAAAAAAAAA"]
    assert blocked.developer_feedback == (
        "[test] Units: WU-AAAAAAAAAAAA. Refs: AC-1, VDN-1. "
        "Issue: The 409 outcome is untested. Expected: Add a unit intent for the 409 outcome."
    )
    assert len(blocked.test_issues) == 1
    assert blocked.implementation_quality_issues == []
    assert not blocked.inconclusive

    truncated = review(LlmCompletion(content="", finish_reason="length"))
    assert truncated.inconclusive and not truncated.ok and truncated.issues == []
    assert "finish_reason=length" in truncated.summary
    assert truncated.to_dict()["inconclusive"] is True


def test_v4_review_retries_contract_errors_once_and_rejects_unknown_ids(monkeypatch) -> None:
    from sdlc_harness.profiles import COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION

    class _Answers:
        settings = None

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.answers = [
                {
                    "summary": "bad id",
                    "reviewed_design_ids": [],
                    "findings": [
                        {
                            "category": "test",
                            "severity": "blocking",
                            "unit_ids": ["WU-BBBBBBBBBBBB"],
                            "refs": ["VDN-1"],
                            "issue": "Missing test.",
                            "expected_change": "Add the test.",
                        }
                    ],
                },
                {"summary": "corrected", "reviewed_design_ids": ["VDN-1"], "findings": []},
            ]

        def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
            self.calls.append(dict(kwargs))
            return LlmCompletion(content=json.dumps(self.answers.pop(0)), finish_reason="stop")

    client = _Answers()
    validator = ImplementationPlanValidator(client)
    monkeypatch.setattr(validator, "review_rules", lambda **_: ImplementationPlanReview(ok=True, mode="rules"))
    review = validator.review(
        structured_plan={
            "version": COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
            "work_units": [
                {"unit_id": "WU-AAAAAAAAAAAA", "design_node_ids": ["VDN-1"], "design_edge_ids": []}
            ],
        },
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph={},
        sequence_graph={},
        vertical_design_graph={"nodes": [{"id": "VDN-1"}], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
    )

    assert review.ok and not review.inconclusive
    assert len(client.calls) == 2
    assert "unknown ids: WU-BBBBBBBBBBBB" in str(client.calls[1]["prompt"])
    assert "`reviewed_design_ids` is missing ids: VDN-1" in str(client.calls[1]["prompt"])


def test_v4_review_is_inconclusive_after_two_contract_errors(monkeypatch) -> None:
    from sdlc_harness.profiles import COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION

    invalid = json.dumps({"summary": "invalid", "findings": [{"category": "test"}]})

    class _InvalidAnswers:
        settings = None

        def __init__(self) -> None:
            self.calls = 0

        def complete_with_metadata(self, **_: object) -> LlmCompletion:
            self.calls += 1
            return LlmCompletion(content=invalid, finish_reason="stop")

    client = _InvalidAnswers()
    validator = ImplementationPlanValidator(client)
    monkeypatch.setattr(validator, "review_rules", lambda **_: ImplementationPlanReview(ok=True, mode="rules"))
    review = validator.review(
        structured_plan={
            "version": COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
            "work_units": [{"unit_id": "WU-AAAAAAAAAAAA"}],
        },
        requirement_packet="requirement",
        design_artifact="design",
        requirement_design_trace={},
        class_graph={},
        sequence_graph={},
        vertical_design_graph={"nodes": [{"id": "VDN-1"}], "edges": []},
        vertical_trace_graph={"nodes": [], "edges": []},
    )

    assert client.calls == 2
    assert review.inconclusive and not review.ok and review.issues == []
    assert "contract is invalid" in review.summary


def test_v4_review_prompt_carries_write_scope_and_only_later_rounds_carry_memory() -> None:
    from sdlc_harness.profiles import COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
    from sdlc_harness.workflows.phase3 import _previous_review_round

    project = get_project_profile("typescript_fullstack_v1")
    planning = build_planning_path_policy(project=project, language=get_language_profile(project.language_profile_id))
    unit = {
        "unit_id": "WU-1",
        "surfaces": ["ui"],
        "design_node_ids": ["VDN-1"],
        "design_edge_ids": ["VDE-1"],
        "runtime_bindings": [
            {"target": {"path": "apps/web/src/Screen.tsx", "symbol": "Screen"}, "attachment": {"owner_path": "apps/web/src/AppShell.tsx"}}
        ],
    }
    plan = {"version": COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION, "work_units": [unit]}

    def prompt(previous_round: dict[str, object] | None) -> str:
        return _llm_review_prompt(
            structured_plan=plan,
            requirement_packet="req",
            design_artifact="design",
            requirement_design_trace={},
            class_graph={},
            sequence_graph={},
            rule_review=ImplementationPlanReview(ok=True, mode="rules"),
            planning_policy=planning,
            vertical_design_graph={
                "nodes": [
                    {
                        "id": "VDN-1",
                        "scenario_id": "SC-1",
                        "name": "Screen",
                        "contract": "Submit the confirmed deletion.",
                        "surface_contract": {"transitions": [{"action": "confirm", "to": "pending"}]},
                    }
                ],
                "edges": [
                    {
                        "id": "VDE-1",
                        "scenario_id": "SC-1",
                        "from": "VDN-1",
                        "to": "VDN-2",
                        "label": "submit deletion",
                        "boundary_contract": {"provider_operation": "DELETE /items/{id}"},
                    }
                ],
            },
            previous_round=previous_round,
        )

    first = prompt(None)
    assert '"write_scope":{"files":["apps/web/src/AppShell.tsx","apps/web/src/Screen.tsx"]' in first
    assert '"findings": [' in first
    assert '"reviewed_design_ids": ["every exact VDN/VDE id from all work units"]' in first
    assert '"severity": "blocking|warning"' in first
    assert "never abbreviate or invent ids" in first
    assert "Do not cap the number of findings" in first
    assert "from a reducer transition or final-state assertion alone" in first
    assert "Do not return `ok` or `developer_feedback`" in first
    assert '"approved_design_contracts":{"nodes":[{"id":"VDN-1"' in first
    assert '"boundary_contract":{"provider_operation":"DELETE /items/{id}"}' in first
    assert "Vertical Design/Trace JSON" not in first
    assert "Previous review round" not in first

    repaired = {**plan, "work_units": [{**unit, "objective": "changed"}]}
    previous = _previous_review_round(plan, repaired, ImplementationPlanReview(ok=False, mode="m", test_issues=["WU-1 misses 409."]))
    assert previous == {"blocking_issues": ["WU-1 misses 409."], "changed_unit_ids": ["WU-1"], "changed_plan_fields": []}
    later = prompt(previous)
    assert "Previous review round (check it first)" in later and "WU-1 misses 409." in later
