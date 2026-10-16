import json
from pathlib import Path

from sdlc_harness.agents.developer.planner import (
    DeveloperPlanAgent,
    _apply_design_adjustments,
    _apply_design_adjustments_to_steps,
    _canonical_proposed_files,
    _design_adjustments,
    _design_mappings,
    _ensure_executable_test_files,
)
from sdlc_harness.agents.developer.work_packet import build_developer_work_packet
from sdlc_harness.agents.validator.plan_review import ImplementationPlanValidator, _llm_review_prompt
from sdlc_harness.interfaces.cli import build_parser
from sdlc_harness.workflows.phase3 import _next_implementation_plan_version


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


def test_sequence_mapping_uses_message_semantics_without_pseudo_symbols():
    class_graph, sequence_graph = _graphs()
    mappings = _design_mappings(class_graph=class_graph, sequence_graph=sequence_graph, target_files=[])
    sequence = [item for item in mappings if item["design_element_type"] == "sequence_message"]

    assert sequence[0]["planned_code_target"].endswith("InsertCulturalHeritageController.java#activateInsertion")
    assert sequence[0]["trace_policy"] == "direct_symbol"
    assert sequence[1]["planned_code_target"].endswith("IdempotencyGuard.java")
    assert "#false" not in sequence[1]["planned_code_target"]
    assert sequence[1]["trace_policy"] == "return_behavior"
    assert sequence[2]["planned_code_target"].endswith("ETourPort.java")
    assert "#ConnectionException" not in sequence[2]["planned_code_target"]
    assert sequence[2]["trace_policy"] == "exception_path"
    assert sequence[3]["planned_code_target"].endswith("InsertionFormView.java")
    assert sequence[3]["trace_policy"] == "boundary_interaction"


def test_canonical_files_include_step_tests_and_mapping_files(tmp_path: Path):
    class_graph, sequence_graph = _graphs()
    mappings = _design_mappings(class_graph=class_graph, sequence_graph=sequence_graph, target_files=[])
    test_path = "src/test/java/com/sdlc/etour/heritage/InsertFlowTest.java"
    steps = [{"order": 1, "title": "Test flow", "files": [test_path], "design_refs": []}]
    proposed = _canonical_proposed_files(proposed_files=[], implementation_steps=steps, mappings=mappings)
    paths = {item["path"] for item in proposed}
    assert test_path in paths
    assert "src/main/java/com/sdlc/etour/heritage/InsertCulturalHeritageController.java" in paths
    proposed = _ensure_executable_test_files(proposed_files=proposed, mappings=mappings)
    paths = {item["path"] for item in proposed}
    assert "src/test/java/com/sdlc/etour/heritage/InsertCulturalHeritageFlowTest.java" in paths

    packet = build_developer_work_packet(
        run_id="run_test",
        source_ref="dataset_use_case:eTour:UC2",
        issue_key="SDLC-2",
        structured_plan={
            "target_repository": {},
            "implementation_steps": steps,
            "proposed_files": proposed,
            "design_mappings": mappings,
            "test_intents": [{"command": "test"}],
        },
        target_repo_path=tmp_path,
        run_workspace={},
        approved_plan_refs={},
    )
    assert test_path in packet["output_contract"]["allowed_paths"]


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


def test_repair_preserves_candidate_and_moves_review_to_history():
    class_graph, sequence_graph = _graphs()
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
        test_command="test",
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
        test_command="test",
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
