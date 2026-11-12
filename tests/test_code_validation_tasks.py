from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from sdlc_harness.agents.validator.code_contract import build_finding_ledger
from sdlc_harness.agents.validator.code_review_tasks import build_code_validation_review_plan
from sdlc_harness.agents.validator.code_trace import (
    CodeTraceValidator,
    _code_validation_recovery_prompt,
    _code_validation_task_prompt,
    _merge_prioritized_line_ranges,
    _task_code_snippets,
    _validate_code_validation_task_output,
)
from sdlc_harness.agents.validator.typescript_ast import TypeScriptAstEvidence
from sdlc_harness.llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.core.config import LlmCallProfile
from sdlc_harness.profiles import (
    build_code_trace_policy,
    build_planning_path_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.storage.runtime import RuntimeStore


class StaticTypeScriptExtractor:
    def extract(self, *, source_paths, policy, **_kwargs):
        return TypeScriptAstEvidence(
            extractor_id=policy.symbol_extractor_id,
            profile=policy.metadata(),
            files=tuple(
                {
                    "path": path,
                    "symbols": [
                        {
                            "kind": "class",
                            "name": "Sample",
                            "qualified_name": "Sample",
                            "start_line": 1,
                            "end_line": 1,
                        },
                        {
                            "kind": "method",
                            "name": "run",
                            "qualified_name": "Sample.run",
                            "start_line": 1,
                            "end_line": 1,
                        },
                    ],
                }
                for path in source_paths
            ),
        )


class TaskCodeTraceLlm:
    def __init__(self, responder=None) -> None:
        self.responder = responder or clean_review
        self.calls = 0
        self.prompts: list[str] = []
        self.options: list[dict[str, object]] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        return json.dumps(self.responder(json.loads(prompt), self.calls))

    def complete_with_metadata(self, *, system: str, prompt: str, **options) -> LlmCompletion:
        self.options.append(options)
        return LlmCompletion(content=self.complete(system=system, prompt=prompt))


class InvalidCodeTraceLlm:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return "not-json"


class ContractRecoveryCodeTraceLlm:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    def complete_with_metadata(self, *, system: str, prompt: str, **_options) -> LlmCompletion:
        self.calls += 1
        self.prompts.append(prompt)
        payload = json.loads(prompt)
        if self.calls == 1:
            result = blocking_review(payload)
            finding = result["findings"][0]
            finding.update(
                {
                    "code_refs": ["apps/api/src/Sample.ts#run"],
                    "test_refs": ["apps/api/src/Sample.test.ts"],
                    "evidence_ref": "code_snippets",
                    "evidence_quote": "return inventedBehavior();",
                }
            )
        else:
            result = payload["previous_output"]
            source = next(
                item
                for item in payload["relevant_evidence"]["code_snippets"]
                if item["path"] == "apps/api/src/Sample.ts"
            )
            result["findings"][0]["evidence_quote"] = source["content"].splitlines()[-1]
        return LlmCompletion(content=json.dumps(result))


def clean_review(payload: dict[str, object], _call: int = 1) -> dict[str, object]:
    contract = payload["task_contract"]
    return {
        "task_id": contract["task_id"],
        "reviewed_claim_ids": contract["authority_claim_ids"],
        "reviewed_design_ids": contract["required_reviewed_design_ids"],
        "reviewed_boundary_contract_ids": contract["required_reviewed_boundary_contract_ids"],
        "claim_assessments": [
            {"claim_id": claim_id, "verdict": "pass", "finding_ids": []}
            for claim_id in contract["authority_claim_ids"]
        ],
        "ok": True,
        "summary": "Task passed.",
        "findings": [],
        "observations": [],
    }


def blocking_review(payload: dict[str, object], _call: int = 1) -> dict[str, object]:
    result = clean_review(payload)
    contract = payload["task_contract"]
    claim = payload["authority_claims"][0]
    quote = claim["requirement_content"]
    result.update(
        {
            "ok": False,
            "findings": [
                {
                    "finding_id": f"F-{contract['task_id']}",
                    "category": "missing_behavior",
                    "severity": "blocker",
                    "blocking": True,
                    "scenario_ids": contract["scenario_ids"],
                    "claim_ids": [claim["claim_id"]],
                    "unit_ids": [contract["unit_ids"][0]],
                    "requirement_refs": [claim["requirement_ref"]] if claim["requirement_ref"] else [],
                    "design_refs": [],
                    "code_refs": [],
                    "test_refs": [],
                    "issue": "Approved behavior is missing.",
                    "expected_change": "Make the approved behavior observable and test it.",
                    "evidence_ref": claim["claim_id"],
                    "evidence_quote": quote,
                    "owner": "developer",
                    "recommended_action": "developer_repair",
                }
            ],
        }
    )
    result["claim_assessments"] = [
        {
            "claim_id": claim_id,
            "verdict": "finding" if claim_id == claim["claim_id"] else "pass",
            "finding_ids": [f"F-{contract['task_id']}"] if claim_id == claim["claim_id"] else [],
        }
        for claim_id in contract["authority_claim_ids"]
    ]
    return result


class CodeValidationTaskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target = Path(self.temp_dir.name)
        self.source_path = "apps/api/src/Sample.ts"
        self.test_path = "apps/api/src/Sample.test.ts"
        source = self.target / self.source_path
        test = self.target / self.test_path
        source.parent.mkdir(parents=True)
        source.write_text("export class Sample { run(): void {} }\n", encoding="utf-8")
        test.write_text("import { it } from 'vitest';\nit('runs', () => {});\n", encoding="utf-8")
        self.packet = {
            "run_id": "run-code-validation",
            "schema_version": "developer_work_packet_profiled_v1",
            "requirement_contract": {"version": "v2", "revision": 1},
            "canonical_requirements": [{"id": "REQ-1", "content": "Run the behavior."}],
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-1",
                    "content": "Run the behavior.",
                    "design_targets": ["Sample.run"],
                    "design_element_ids": ["method:Sample.run"],
                    "planned_code_targets": [f"{self.source_path}#run"],
                    "test_intents": ["Run SampleTest."],
                }
            ],
            "design_mappings_by_file": {
                self.source_path: [
                    {
                        "design_element_id": "method:Sample.run",
                        "planned_code_target": f"{self.source_path}#run",
                    }
                ]
            },
            "proposed_files": [{"path": self.test_path}],
            "execution_units": [
                {
                    "unit_id": "WU-1",
                    "scenario_id": "SC-1",
                    "depends_on": [],
                    "requirement_refs": ["REQ-1"],
                    "design_node_ids": ["method:Sample.run"],
                    "design_edge_ids": [],
                    "planned_code_targets": [{"path": self.source_path, "symbol": "run"}],
                    "test_target_paths": [self.test_path],
                }
            ],
        }
        project = get_project_profile("typescript_fullstack_v1")
        planning = build_planning_path_policy(
            project=project,
            language=get_language_profile(project.language_profile_id),
        )
        self.code_trace_policy = build_code_trace_policy(
            planning_policy=planning,
            symbol_extractor_id="typescript_compiler_ast_v1",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def validate(self, llm, **kwargs):
        return CodeTraceValidator(
            llm,
            llm_call_budget=6,
            symbol_extractor=StaticTypeScriptExtractor(),
        ).validate(
            target_path=self.target,
            work_packet=self.packet,
            code_trace_policy=self.code_trace_policy,
            **kwargs,
        )

    def test_clean_scenario_is_reviewed_once_without_consensus(self) -> None:
        llm = TaskCodeTraceLlm()

        result = self.validate(llm)

        self.assertTrue(result.ok)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(result.aggregation["completed_task_count"], 1)
        self.assertEqual(result.review_plan["mode"], "scenario_boundary_code_validation_v1")

    def test_review_plan_projects_units_as_compact_scope_index(self) -> None:
        first_unit = {
            "unit_id": "WU-A",
            "scenario_id": "SC-1",
            "design_node_ids": ["VDN-A"],
            "planned_code_targets": [{"path": "apps/api/src/a.ts"}],
        }
        second_unit = {
            "unit_id": "WU-B",
            "scenario_id": "SC-1",
            "title": "Implement B",
            "objective": "Verbose Plan prose must not become semantic authority.",
            "depends_on": ["WU-A", "WU-OUTSIDE"],
            "requirement_refs": ["REQ-1"],
            "acceptance_refs": ["AC-1"],
            "design_node_ids": ["VDN-B"],
            "design_edge_ids": ["VDE-AB"],
            "runtime_bindings": [{"target": "service:b", "attachment_wired_by": "WU-A"}],
            "planned_code_targets": [{"path": "apps/api/src/b.ts", "symbol": "run"}],
            "test_target_paths": ["tests/explicit.test.ts"],
            "test_intents": [
                {"stage_id": "unit", "target_paths": ["tests/unit.test.ts"]},
                {"stage_id": "typecheck", "target_paths": ["tests/typecheck.test.ts"]},
            ],
            "qa_test_intents": [
                {"stage_id": "browser", "target_paths": ["tests/browser.test.ts"]}
            ],
        }
        review_plan = build_code_validation_review_plan(
            work_packet={"execution_units": [first_unit, second_unit]},
            approved_context={
                "implementation_plan_structured_data": {
                    "work_units": [first_unit, second_unit]
                },
                "vertical_design_graph_data": {
                    "nodes": [
                        {"id": "VDN-A", "scenario_id": "SC-1"},
                        {"id": "VDN-B", "scenario_id": "SC-1"},
                    ],
                    "edges": [
                        {
                            "id": "VDE-AB",
                            "scenario_id": "SC-1",
                            "from": "VDN-A",
                            "to": "VDN-B",
                        }
                    ],
                },
            },
            validation_claims=[{"claim_id": "CODE-1", "requirement_ref": "REQ-1"}],
        )

        projected = next(
            unit
            for task in review_plan["tasks"]
            for unit in task["units"]
            if unit["unit_id"] == "WU-B"
        )
        self.assertEqual(
            set(projected),
            {
                "unit_id",
                "scenario_id",
                "title",
                "depends_on",
                "design_node_ids",
                "design_edge_ids",
                "runtime_bindings",
                "planned_code_targets",
                "test_paths",
            },
        )
        self.assertEqual(projected["depends_on"], ["WU-A"])
        self.assertEqual(projected["design_node_ids"], ["VDN-B"])
        self.assertEqual(projected["design_edge_ids"], ["VDE-AB"])
        self.assertEqual(
            projected["test_paths"],
            ["tests/browser.test.ts", "tests/explicit.test.ts", "tests/unit.test.ts"],
        )
        for field in ("objective", "requirement_refs", "acceptance_refs", "test_intents"):
            self.assertNotIn(field, projected)

    def test_code_validator_uses_its_high_thinking_profile(self) -> None:
        llm = TaskCodeTraceLlm()
        llm.settings = SimpleNamespace(
            llm_structured_max_tokens=4096,
            phase4_llm_profiles={
                "code_validator": LlmCallProfile(
                    model="deepseek-flash",
                    thinking_effort="high",
                    max_tokens=32768,
                    timeout_seconds=600,
                )
            },
        )

        result = self.validate(llm)

        self.assertTrue(result.ok)
        self.assertEqual(
            llm.options,
            [
                {
                    "json_mode": True,
                    "model": "deepseek-flash",
                    "reasoning_effort": "high",
                    "max_tokens": 32768,
                    "timeout_seconds": 600,
                }
            ],
        )

    def test_boundary_contract_ownership_is_explicit_per_task_kind(self) -> None:
        units = [
            {
                **self.packet["execution_units"][0],
                "unit_id": "WU-SC1",
                "scenario_id": "SC-1",
                "design_node_ids": ["VDN-SC1-A", "VDN-SC1-B"],
                "design_edge_ids": ["VDE-SC1-LOCAL", "VDE-SC1-TO-SC2"],
            },
            {
                **self.packet["execution_units"][0],
                "unit_id": "WU-SC2",
                "scenario_id": "SC-2",
                "depends_on": ["WU-SC1"],
                "design_node_ids": ["VDN-SC2"],
                "design_edge_ids": [],
            },
        ]
        review_plan = build_code_validation_review_plan(
            work_packet={**self.packet, "execution_units": units},
            approved_context={
                "implementation_plan_structured_data": {"work_units": units},
                "vertical_design_graph_data": {
                    "nodes": [
                        {"id": "VDN-SC1-A", "scenario_id": "SC-1"},
                        {"id": "VDN-SC1-B", "scenario_id": "SC-1"},
                        {"id": "VDN-SC2", "scenario_id": "SC-2"},
                    ],
                    "edges": [
                        {
                            "id": "VDE-SC1-LOCAL",
                            "scenario_id": "SC-1",
                            "from": "VDN-SC1-A",
                            "to": "VDN-SC1-B",
                            "boundary_contract": {"input_mapping": []},
                        },
                        {
                            "id": "VDE-SC1-TO-SC2",
                            "scenario_id": "SC-1",
                            "from": "VDN-SC1-B",
                            "to": "VDN-SC2",
                            "boundary_contract": {"input_mapping": []},
                        },
                    ],
                },
            },
            validation_claims=[],
        )
        tasks = {task["task_id"]: task for task in review_plan["tasks"]}

        self.assertEqual(tasks["scenario:SC-1"]["owned_boundary_contract_ids"], ["VDE-SC1-LOCAL"])
        self.assertEqual(tasks["scenario:SC-2"]["owned_boundary_contract_ids"], [])
        self.assertEqual(
            tasks["boundary:cross-scenario"]["owned_boundary_contract_ids"],
            ["VDE-SC1-TO-SC2"],
        )
        boundary_contracts = {
            item["id"]: item for item in tasks["boundary:cross-scenario"]["approved_design_contracts"]
        }
        self.assertNotIn("boundary_contract", boundary_contracts["VDE-SC1-LOCAL"])
        self.assertIn("boundary_contract", boundary_contracts["VDE-SC1-TO-SC2"])
        self.assertEqual(
            tasks["boundary:cross-scenario"]["scenario_owned_design_sections"],
            [
                {
                    "owner_task_id": "scenario:SC-1",
                    "field_path": "boundary_contract",
                    "design_ids": ["VDE-SC1-LOCAL"],
                }
            ],
        )

        prompt = _code_validation_task_prompt(
            task=tasks["scenario:SC-1"],
            code_snippets=[],
            trace_mappings=[],
            verification_evidence={},
            symbol_evidence={},
            scenario_results=[],
        )
        output = clean_review(json.loads(prompt))
        output["reviewed_boundary_contract_ids"] = []
        with self.assertRaisesRegex(ValueError, "exact complete owned boundary contract ID set"):
            _validate_code_validation_task_output(
                output=output,
                task=tasks["scenario:SC-1"],
                prompt=prompt,
            )

    def test_all_task_blockers_are_returned_without_voting(self) -> None:
        llm = TaskCodeTraceLlm(blocking_review)

        result = self.validate(llm)

        self.assertFalse(result.ok)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(result.missing_links), 1)
        self.assertEqual(result.missing_links[0]["task_id"], "scenario:SC-1")

    def test_twice_invalid_task_is_inconclusive_and_never_authorizes_repair(self) -> None:
        llm = InvalidCodeTraceLlm()

        result = self.validate(llm)

        self.assertFalse(result.ok)
        self.assertTrue(result.inconclusive)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(result.missing_links, [])

    def test_contract_retry_uses_previous_output_and_only_cited_evidence(self) -> None:
        llm = ContractRecoveryCodeTraceLlm()

        result = self.validate(llm)

        self.assertFalse(result.ok)
        self.assertEqual(llm.calls, 2)
        first, retry = (json.loads(prompt) for prompt in llm.prompts)
        self.assertEqual(retry["task"], "Code Validation Contract Recovery v1")
        self.assertEqual(
            retry["prompt_schema_version"],
            "code_validation_contract_recovery_prompt_v4",
        )
        self.assertIn("must cite exact evidence", retry["contract_error"])
        self.assertEqual(retry["previous_output"]["task_id"], first["task_contract"]["task_id"])
        self.assertEqual(
            {item["path"] for item in retry["relevant_evidence"]["code_snippets"]},
            {self.source_path, self.test_path},
        )
        self.assertEqual(
            retry["relevant_units"],
            [
                {
                    "unit_id": "WU-1",
                    "code_targets": [{"path": self.source_path, "symbol": "run"}],
                    "test_paths": [self.test_path],
                }
            ],
        )
        self.assertIn("Use relevant_units only to select exact unit_ids", retry["instruction"])
        self.assertEqual(
            retry["relevant_evidence"]["static_trace_evidence"]["test_files"],
            [self.test_path],
        )
        self.assertEqual(
            len(retry["relevant_evidence"]["static_trace_evidence"]["code_evidence"]),
            1,
        )
        self.assertTrue(llm.prompts[1].startswith('{\n"prompt_schema_version"'))
        self.assertNotIn("\n ", llm.prompts[1])
        self.assertLess(len(llm.prompts[1]), len(llm.prompts[0]))

    def test_contract_recovery_falls_back_to_legacy_test_intents(self) -> None:
        prompt = json.dumps(
            {
                "task_contract": {"unit_ids": ["WU-LEGACY"]},
                "expected_json_schema": {},
                "approved_units": [
                    {
                        "unit_id": "WU-LEGACY",
                        "test_intents": [
                            {
                                "stage_id": "unit",
                                "target_paths": ["tests/unit.test.ts"],
                            },
                            {
                                "stage_id": "typecheck",
                                "target_paths": ["tests/typecheck.test.ts"],
                            },
                        ],
                    }
                ],
            }
        )

        recovery = json.loads(
            _code_validation_recovery_prompt(
                prompt=prompt,
                previous_output={"findings": []},
                contract_error="invalid unit id",
            )
        )

        self.assertEqual(
            recovery["relevant_units"][0]["test_paths"],
            ["tests/unit.test.ts"],
        )

    def test_contract_valid_task_cache_avoids_a_second_provider_call(self) -> None:
        llm = TaskCodeTraceLlm()
        cache_path = self.target / "cache" / "code_validation_task_cache_v1.json"

        first = self.validate(llm, task_cache_path=cache_path)
        second = self.validate(llm, task_cache_path=cache_path)

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(second.cache["hits"], 1)

    def test_task_prompt_ignores_volatile_and_unrelated_verification_details(self) -> None:
        task = build_code_validation_review_plan(
            work_packet=self.packet,
            approved_context={"implementation_plan_structured_data": {"work_units": self.packet["execution_units"]}},
            validation_claims=[
                {
                    "claim_id": "CODE-REQ-1",
                    "requirement_ref": "REQ-1",
                    "requirement_content": "Run the behavior.",
                    "design_element_ids": ["method:Sample.run"],
                    "code_targets": [f"{self.source_path}#run"],
                }
            ],
        )["tasks"][0]
        common = {
            "status": "passed",
            "exit_code": 0,
            "executed_stage_ids": ["typecheck", "unit"],
            "selected_test_files": [self.test_path, "apps/api/src/Other.test.ts"],
            "verified_test_files": [self.test_path, "apps/api/src/Other.test.ts"],
        }
        first = _code_validation_task_prompt(
            task=task,
            code_snippets=[],
            trace_mappings=[],
            verification_evidence={
                **common,
                "duration_seconds": 1.2,
                "stdout": "first run",
                "stage_results": [
                    {
                        "stage_id": "unit",
                        "status": "passed",
                        "duration_seconds": 1.1,
                        "stdout": "first stage",
                        "selected_test_files": common["selected_test_files"],
                        "verified_test_files": common["verified_test_files"],
                    }
                ],
            },
            symbol_evidence={},
            scenario_results=[],
        )
        second = _code_validation_task_prompt(
            task=task,
            code_snippets=[],
            trace_mappings=[],
            verification_evidence={
                **common,
                "duration_seconds": 9.9,
                "stdout": "second run",
                "stage_results": [
                    {
                        "stage_id": "unit",
                        "status": "passed",
                        "duration_seconds": 9.8,
                        "stdout": "second stage",
                        "selected_test_files": common["selected_test_files"],
                        "verified_test_files": common["verified_test_files"],
                    }
                ],
            },
            symbol_evidence={},
            scenario_results=[],
        )

        self.assertEqual(first, second)
        evidence = json.loads(first)["verification_evidence"]
        self.assertEqual(evidence["selected_test_files"], [self.test_path])
        self.assertNotIn("stdout", evidence)

    def test_task_prompt_keeps_frozen_artifact_and_stage_boundaries(self) -> None:
        task = build_code_validation_review_plan(
            work_packet=self.packet,
            approved_context={"implementation_plan_structured_data": {"work_units": self.packet["execution_units"]}},
            validation_claims=[
                {
                    "claim_id": "CODE-REQ-1",
                    "requirement_ref": "REQ-1",
                    "requirement_content": "Run the behavior.",
                    "design_element_ids": ["method:Sample.run"],
                    "code_targets": [f"{self.source_path}#run"],
                }
            ],
        )["tasks"][0]

        payload = json.loads(
            _code_validation_task_prompt(
                task=task,
                code_snippets=[],
                trace_mappings=[],
                verification_evidence={"executed_stage_ids": ["typecheck", "unit"]},
                symbol_evidence={},
                scenario_results=[],
            )
        )
        rules = "\n".join(payload["validator_rules"])

        self.assertIn("semantic authority", rules)
        self.assertIn("Plan-only expectation", rules)
        self.assertIn("Developer owns implementation choice", rules)
        self.assertIn("Every required_reviewed_boundary_contract_id is owned here", rules)
        self.assertIn("same-scenario boundary_contract stays with the scenario task", rules)
        self.assertIn("separate endpoint tests are not integration proof", rules)
        self.assertIn("system/block_run", rules)
        self.assertIn("warning finding requires complete visible evidence", rules)
        self.assertNotIn("human/human_decision", rules)
        self.assertIn("deferred browser, DOM, visual", rules)
        self.assertIn("deferred stage", rules)
        self.assertIn("review_scope stop_condition", rules)
        contract = payload["task_contract"]
        self.assertFalse(contract["repair_policy"]["human_decision_available"])
        self.assertEqual(
            contract["repair_policy"]["validator_expected_change_mode"],
            "required_outcome_only",
        )
        self.assertIn(["system", "block_run"], contract["allowed_owner_action_pairs"])
        self.assertNotIn(["human", "human_decision"], contract["allowed_owner_action_pairs"])
        self.assertEqual(
            contract["review_scope"]["deferred_destination"],
            "observations only; Browser/UI QA owns the verdict",
        )

    def test_finding_requirement_refs_must_belong_to_its_selected_claims(self) -> None:
        task = build_code_validation_review_plan(
            work_packet=self.packet,
            approved_context={"implementation_plan_structured_data": {"work_units": self.packet["execution_units"]}},
            validation_claims=[
                {
                    "claim_id": "CODE-REQ-1",
                    "requirement_ref": "REQ-1",
                    "requirement_content": "Run the behavior.",
                    "design_element_ids": ["method:Sample.run"],
                    "code_targets": [f"{self.source_path}#run"],
                },
                {
                    "claim_id": "CODE-PRE-1",
                    "requirement_ref": "PRE-1",
                    "requirement_content": "The operator is logged in.",
                    "design_element_ids": ["method:Sample.run"],
                    "code_targets": [f"{self.source_path}#run"],
                },
            ],
        )["tasks"][0]
        prompt = _code_validation_task_prompt(
            task=task,
            code_snippets=[],
            trace_mappings=[],
            verification_evidence={},
            symbol_evidence={},
            scenario_results=[],
        )
        output = blocking_review(json.loads(prompt))
        output["reviewed_claim_ids"] = task["authority_claim_ids"]
        output["findings"][0]["requirement_refs"] = ["PRE-1"]

        with self.assertRaisesRegex(ValueError, "refs without a selected matching claim: PRE-1"):
            _validate_code_validation_task_output(output=output, task=task, prompt=prompt)

    def test_post_requirement_findings_reject_human_and_repair_owned_warnings(self) -> None:
        task = build_code_validation_review_plan(
            work_packet=self.packet,
            approved_context={"implementation_plan_structured_data": {"work_units": self.packet["execution_units"]}},
            validation_claims=[
                {
                    "claim_id": "CODE-REQ-1",
                    "requirement_ref": "REQ-1",
                    "requirement_content": "Run the behavior.",
                    "design_element_ids": ["method:Sample.run"],
                    "code_targets": [f"{self.source_path}#run"],
                }
            ],
        )["tasks"][0]
        prompt = _code_validation_task_prompt(
            task=task,
            code_snippets=[],
            trace_mappings=[],
            verification_evidence={},
            symbol_evidence={},
            scenario_results=[],
        )
        human_output = blocking_review(json.loads(prompt))
        human_output["findings"][0].update(
            {"owner": "human", "recommended_action": "human_decision"}
        )
        with self.assertRaisesRegex(ValueError, "invalid owner/action pair"):
            _validate_code_validation_task_output(output=human_output, task=task, prompt=prompt)

        warning_output = blocking_review(json.loads(prompt))
        warning_output.update({"ok": True})
        warning_output["findings"][0].update(
            {"severity": "warning", "blocking": False}
        )
        with self.assertRaisesRegex(ValueError, "must use none/none"):
            _validate_code_validation_task_output(output=warning_output, task=task, prompt=prompt)

        warning_output["findings"][0].update(
            {"owner": "none", "recommended_action": "none"}
        )
        with self.assertRaisesRegex(ValueError, "expected_change must be empty"):
            _validate_code_validation_task_output(output=warning_output, task=task, prompt=prompt)

        blocker_output = blocking_review(json.loads(prompt))
        blocker_output["findings"][0].update(
            {"owner": "none", "recommended_action": "none"}
        )
        with self.assertRaisesRegex(ValueError, "must have an actionable owner/action pair"):
            _validate_code_validation_task_output(output=blocker_output, task=task, prompt=prompt)

    def test_scenario_prompt_excludes_same_path_mappings_owned_by_other_scenarios(self) -> None:
        task = build_code_validation_review_plan(
            work_packet=self.packet,
            approved_context={"implementation_plan_structured_data": {"work_units": self.packet["execution_units"]}},
            validation_claims=[
                {
                    "claim_id": "CODE-REQ-1",
                    "requirement_ref": "REQ-1",
                    "requirement_content": "Run the behavior.",
                    "design_element_ids": ["method:Sample.run"],
                    "code_targets": [f"{self.source_path}#run"],
                }
            ],
        )["tasks"][0]
        prompt = _code_validation_task_prompt(
            task=task,
            code_snippets=[],
            trace_mappings=[
                {
                    "requirement_ref": "REQ-1",
                    "planned_code_targets": [f"{self.source_path}#run"],
                    "code_evidence": [
                        {
                            "planned_code_target": f"{self.source_path}#run",
                            "source_file": self.source_path,
                            "symbol": "run",
                        },
                        {
                            "planned_code_target": f"{self.source_path}#run",
                            "source_file": self.source_path,
                            "symbol": "run",
                        },
                    ],
                    "test_files": [self.test_path],
                    "test_covered": True,
                    "evidence_status": "covered",
                },
                {
                    "requirement_ref": "REQ-OTHER",
                    "planned_code_targets": [f"{self.source_path}#run"],
                    "code_evidence": [
                        {
                            "planned_code_target": f"{self.source_path}#run",
                            "source_file": self.source_path,
                            "symbol": "run",
                        }
                    ],
                    "test_files": [self.test_path],
                    "test_covered": True,
                    "evidence_status": "covered",
                },
            ],
            verification_evidence={
                "status": "passed",
                "stage_results": [
                    {
                        "stage_id": "unit",
                        "status": "passed",
                        "argv": ["npm", "run", "test:unit"],
                        "stdout": f"passed {self.test_path}",
                    }
                ],
            },
            symbol_evidence={},
            scenario_results=[],
        )

        payload = json.loads(prompt)
        self.assertEqual(
            [item["requirement_ref"] for item in payload["static_trace_mappings"]],
            ["REQ-1"],
        )
        mapping = payload["static_trace_mappings"][0]
        self.assertNotIn("code_evidence", mapping)
        self.assertNotIn("test_files", mapping)
        self.assertEqual(
            payload["static_trace_evidence"]["code_evidence"],
            [
                {
                    "planned_code_target": f"{self.source_path}#run",
                    "source_file": self.source_path,
                    "symbol": "run",
                }
            ],
        )
        self.assertEqual(payload["static_trace_evidence"]["test_files"], [self.test_path])
        self.assertEqual(payload["prompt_schema_version"], "code_validation_task_prompt_v4")
        self.assertTrue(prompt.startswith('{\n"prompt_schema_version"'))
        self.assertNotIn("\n ", prompt)
        contract = payload["task_contract"]
        self.assertEqual(contract["allowed_requirement_refs"], ["REQ-1"])
        self.assertEqual(contract["allowed_code_paths"], [self.source_path])
        self.assertEqual(contract["allowed_test_paths"], [self.test_path])
        self.assertEqual(
            payload["verification_evidence"]["observed_task_test_files"],
            [self.test_path],
        )
        self.assertEqual(
            payload["verification_evidence"]["stage_results"][0]["argv"],
            ["npm", "run", "test:unit"],
        )

    def test_boundary_prompt_exposes_only_structural_ownership_metadata(self) -> None:
        prompt = _code_validation_task_prompt(
            task={
                "task_id": "boundary:cross-scenario",
                "task_kind": "boundary",
                "scenario_ids": ["SC-1", "SC-2"],
                "unit_ids": ["WU-1", "WU-2"],
                "authority_claim_ids": ["BOUNDARY-1"],
                "design_ids": [],
                "requirement_refs": [],
                "code_targets": [],
                "test_paths": [],
                "claims": [
                    {
                        "claim_id": "BOUNDARY-1",
                        "claim_kind": "cross_scenario_dependency",
                        "unit_ids": ["WU-1", "WU-2"],
                        "design_element_ids": [],
                    }
                ],
                "approved_design_contracts": [],
                "scenario_owned_design_sections": [
                    {
                        "owner_task_id": "scenario:SC-1",
                        "field_path": "boundary_contract",
                        "design_ids": ["VDE-LOCAL"],
                    }
                ],
                "units": [
                    {"unit_id": "WU-1", "scenario_id": "SC-1"},
                    {"unit_id": "WU-2", "scenario_id": "SC-2"},
                ],
            },
            code_snippets=[],
            trace_mappings=[],
            verification_evidence={},
            symbol_evidence={},
            scenario_results=[
                {
                    "task_id": "scenario:SC-1",
                    "task_kind": "scenario",
                    "scenario_ids": ["SC-1"],
                    "reviewed_claim_ids": ["CODE-1"],
                    "reported_ok": False,
                    "summary": "Local behavior is broken.",
                    "blocking_findings": [
                        {
                            "finding_id": "LOCAL-1",
                            "claim_ids": ["CODE-1"],
                            "unit_ids": ["WU-1"],
                            "scenario_ids": ["SC-1"],
                            "design_refs": ["VDE-LOCAL"],
                            "code_refs": ["apps/web/src/example.ts#submit", "apps/api/src/example.ts#route"],
                            "test_refs": ["apps/web/src/example.test.ts#submit"],
                            "evidence_ref": "code_snippets",
                            "evidence_quote": "authorization: Bearer token",
                            "issue": "Copying this would duplicate the local review.",
                            "expected_change": "Repair the local behavior.",
                        }
                    ],
                }
            ],
        )

        payload = json.loads(prompt)
        self.assertEqual(payload["prompt_schema_version"], "code_validation_task_prompt_v6")
        self.assertNotIn("scenario_task_outcomes", payload)
        ownership = payload["ownership_exclusions"]
        self.assertEqual(
            ownership["scenario_claim_ownership"],
            [
                {
                    "task_id": "scenario:SC-1",
                    "scenario_ids": ["SC-1"],
                    "scenario_owned_claim_ids": ["CODE-1"],
                }
            ],
        )
        self.assertNotIn("LOCAL-1", json.dumps(ownership))
        self.assertNotIn("Copying this would duplicate", json.dumps(ownership))
        self.assertNotIn("Repair the local behavior", json.dumps(ownership))
        self.assertEqual(
            ownership["scenario_blocker_evidence_ownership"],
            [
                {
                    "owner_task_id": "scenario:SC-1",
                    "scenario_ids": ["SC-1"],
                    "claim_ids": ["CODE-1"],
                    "unit_ids": ["WU-1"],
                    "design_refs": ["VDE-LOCAL"],
                    "code_refs": [
                        "apps/web/src/example.ts#submit",
                        "apps/api/src/example.ts#route",
                    ],
                    "code_paths": ["apps/api/src/example.ts", "apps/web/src/example.ts"],
                    "test_paths": ["apps/web/src/example.test.ts"],
                    "evidence_ref": "code_snippets",
                    "evidence_quote": "authorization: Bearer token",
                }
            ],
        )
        self.assertEqual(
            ownership["scenario_owned_design_sections"],
            [
                {
                    "owner_task_id": "scenario:SC-1",
                    "field_path": "boundary_contract",
                    "design_ids": ["VDE-LOCAL"],
                }
            ],
        )
        self.assertEqual(
            payload["claim_review_matrix"],
            [
                {
                    "claim_id": "BOUNDARY-1",
                    "review_mode": "cross_scenario_dependency_interaction",
                    "unit_ids": ["WU-1", "WU-2"],
                    "scenario_ids": ["SC-1", "SC-2"],
                }
            ],
        )
        rules = "\n".join(payload["validator_rules"])
        self.assertIn("Use claim_review_matrix as the finding boundary", rules)
        self.assertIn("structural evidence fingerprints", rules)
        self.assertIn("producer and consumer belong to one scenario", rules)

    def test_test_snippet_budget_is_not_consumed_by_source_files(self) -> None:
        (self.target / self.source_path).write_text(
            "\n".join(f"export const source{index} = {index};" for index in range(2000)) + "\n",
            encoding="utf-8",
        )
        (self.target / self.test_path).write_text(
            "\n".join(f"it('case {index}', () => expect({index}).toBe({index}));" for index in range(1000))
            + "\n",
            encoding="utf-8",
        )
        snippets = _task_code_snippets(
            target_path=self.target,
            task={
                "code_targets": [{"path": self.source_path, "symbol": "Sample"}],
                "test_paths": [self.test_path],
            },
            ast_evidence=TypeScriptAstEvidence(
                extractor_id="test",
                profile={},
                files=(),
            ),
            fallback_test_paths=[],
        )

        test_snippet = next(item for item in snippets if item["path"] == self.test_path)
        self.assertGreater(test_snippet["chars_included"], 20000)
        self.assertLessEqual(test_snippet["chars_included"], 24000)
        self.assertTrue(test_snippet["content_truncated"])
        self.assertLess(test_snippet["line_ranges"][0]["end_line"], 1000)

    def test_test_snippet_under_24k_is_included_completely(self) -> None:
        content = "".join(f"case {index:04d}: retry assertion\n" for index in range(870))
        self.assertGreater(len(content), 23000)
        self.assertLess(len(content), 24000)
        (self.target / self.test_path).write_text(content, encoding="utf-8")

        snippets = _task_code_snippets(
            target_path=self.target,
            task={"code_targets": [], "test_paths": [self.test_path]},
            ast_evidence=TypeScriptAstEvidence(extractor_id="test", profile={}, files=()),
            fallback_test_paths=[],
        )

        test_snippet = snippets[0]
        self.assertEqual(test_snippet["chars_included"], len(content))
        self.assertFalse(test_snippet["content_truncated"])
        self.assertIn("case 0869: retry assertion", test_snippet["content"])

    def test_test_snippet_total_budget_remains_50k(self) -> None:
        paths = [f"apps/api/src/Sample{index}.test.ts" for index in range(3)]
        content = "".join(f"case {index:04d}: assertion\n" for index in range(1200))
        self.assertGreater(len(content), 24000)
        for path in paths:
            file_path = self.target / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        snippets = _task_code_snippets(
            target_path=self.target,
            task={"code_targets": [], "test_paths": paths},
            ast_evidence=TypeScriptAstEvidence(extractor_id="test", profile={}, files=()),
            fallback_test_paths=[],
        )

        self.assertEqual(len(snippets), 3)
        self.assertLessEqual(sum(len(item["content"]) for item in snippets), 50000)
        self.assertTrue(all(item["chars_included"] <= 24000 for item in snippets))
        self.assertTrue(snippets[-1]["content_truncated"])

    def test_source_snippet_includes_local_dependencies_used_by_target_symbol(self) -> None:
        source = self.target / self.source_path
        source.write_text(
            "export function helper(): boolean { return true; }\n"
            + "\n" * 20
            + "export function target(): boolean { return helper(); }\n",
            encoding="utf-8",
        )
        snippets = _task_code_snippets(
            target_path=self.target,
            task={"code_targets": [{"path": self.source_path, "symbol": "target"}], "test_paths": []},
            ast_evidence=TypeScriptAstEvidence(
                extractor_id="test",
                profile={},
                files=(
                    {
                        "path": self.source_path,
                        "symbols": [
                            {
                                "kind": "function",
                                "name": "helper",
                                "qualified_name": "helper",
                                "start_line": 1,
                                "end_line": 1,
                            },
                            {
                                "kind": "function",
                                "name": "target",
                                "qualified_name": "target",
                                "start_line": 22,
                                "end_line": 22,
                            },
                        ],
                    },
                ),
            ),
            fallback_test_paths=[],
        )

        self.assertIn("function target", snippets[0]["content"])
        self.assertIn("function helper", snippets[0]["content"])

    def test_prioritized_ranges_coalesce_a_bridge_without_duplicate_evidence(self) -> None:
        self.assertEqual(
            _merge_prioritized_line_ranges([(40, 60), (10, 30), (25, 45)]),
            [(10, 60)],
        )

    def test_deterministic_blocker_skips_all_semantic_calls(self) -> None:
        (self.target / self.test_path).unlink()
        llm = TaskCodeTraceLlm()

        result = self.validate(llm)

        self.assertFalse(result.ok)
        self.assertEqual(llm.calls, 0)
        self.assertTrue(result.aggregation["deterministic_preflight_blocked"])

    def test_task_input_and_output_can_be_reviewed_and_resumed(self) -> None:
        llm = TaskCodeTraceLlm()
        runtime_root = self.target / ".runtime"
        store = RuntimeStore(repo_root=self.target, runtime_root=runtime_root)
        decision: dict[str, object] = {"review_input": True, "review_output": True}

        def validator() -> CodeTraceValidator:
            return CodeTraceValidator(
                llm,
                llm_call_budget=6,
                symbol_extractor=StaticTypeScriptExtractor(),
                reviewable_client_factory=lambda _task, _prompt: ReviewableLlmClient(
                    llm,
                    store=store,
                    run_id="run-code-validation",
                    review=decision,
                    call_prefix="code-validation-scenario-SC-1",
                    execution_dir="code_validation_execution/step_review/test",
                    error_prefix="implementation_validation",
                    label="Code Validation scenario:SC-1",
                ),
            )

        with self.assertRaises(LlmStepReviewRequired) as input_pause:
            validator().validate(
                target_path=self.target,
                work_packet=self.packet,
                code_trace_policy=self.code_trace_policy,
            )
        self.assertEqual(input_pause.exception.progress["review_stage"], "input")

        decision.update(
            {
                "call_id": input_pause.exception.progress["call_id"],
                "decision": "approve",
            }
        )
        with self.assertRaises(LlmStepReviewRequired) as output_pause:
            validator().validate(
                target_path=self.target,
                work_packet=self.packet,
                code_trace_policy=self.code_trace_policy,
            )
        self.assertEqual(output_pause.exception.progress["review_stage"], "output")

        decision.update(
            {
                "call_id": output_pause.exception.progress["call_id"],
                "decision": "approve",
            }
        )
        result = validator().validate(
            target_path=self.target,
            work_packet=self.packet,
            code_trace_policy=self.code_trace_policy,
        )
        self.assertTrue(result.ok)
        self.assertEqual(llm.calls, 1)

    def test_approved_plan_restores_all_scenarios_from_scoped_repair_packet(self) -> None:
        units = [
            {"unit_id": "WU-1", "scenario_id": "SC-1", "depends_on": [], "requirement_refs": ["REQ-1"]},
            {"unit_id": "WU-2", "scenario_id": "SC-2", "depends_on": ["WU-1"], "requirement_refs": ["REQ-2"]},
            {"unit_id": "WU-3", "scenario_id": "SC-3", "depends_on": ["WU-2"], "requirement_refs": ["REQ-3"]},
        ]
        claims = [
            {"claim_id": f"CODE-{index}", "requirement_ref": f"REQ-{index}", "code_targets": []}
            for index in range(1, 4)
        ]

        plan = build_code_validation_review_plan(
            work_packet={"execution_units": [units[-1]]},
            approved_context={"implementation_plan_structured_data": {"work_units": units}},
            validation_claims=claims,
        )

        self.assertEqual(
            [task["task_id"] for task in plan["tasks"]],
            ["scenario:SC-1", "scenario:SC-2", "scenario:SC-3", "boundary:cross-scenario"],
        )

    def test_cross_scenario_design_edge_is_owned_only_by_boundary_with_endpoint_closure(self) -> None:
        units = [
            {
                "unit_id": "WU-1",
                "scenario_id": "SC-1",
                "depends_on": [],
                "requirement_refs": ["REQ-1"],
                "design_node_ids": ["VDN-1"],
                "design_edge_ids": ["VDE-CROSS"],
            },
            {
                "unit_id": "WU-2",
                "scenario_id": "SC-2",
                "depends_on": ["WU-1"],
                "requirement_refs": ["REQ-2"],
                "design_node_ids": ["VDN-2"],
                "design_edge_ids": ["VDE-CROSS"],
                "runtime_bindings": [
                    {"design_node_id": "VDN-2", "attachment_wired_by": "WU-1"}
                ],
            },
        ]
        design_graph = {
            "nodes": [
                {"id": "VDN-1", "scenario_id": "SC-1", "kind": "business_rule"},
                {"id": "VDN-2", "scenario_id": "SC-2", "kind": "ui_interaction"},
            ],
            "edges": [
                {
                    "id": "VDE-CROSS",
                    "scenario_id": "SC-2",
                    "kind": "depends_on",
                    "from": "VDN-2",
                    "to": "VDN-1",
                }
            ],
        }
        plan = build_code_validation_review_plan(
            work_packet={"execution_units": units},
            approved_context={
                "implementation_plan_structured_data": {"work_units": units},
                "vertical_design_graph_data": design_graph,
            },
            validation_claims=[
                {
                    "claim_id": "CODE-1",
                    "requirement_ref": "REQ-1",
                    "design_element_ids": ["class:legacy-label", "VDN-1", "VDE-CROSS"],
                },
                {
                    "claim_id": "CODE-2",
                    "requirement_ref": "REQ-2",
                    "design_element_ids": ["VDN-2", "VDE-CROSS"],
                },
            ],
        )

        scenario_one = next(item for item in plan["tasks"] if item["task_id"] == "scenario:SC-1")
        scenario_two = next(item for item in plan["tasks"] if item["task_id"] == "scenario:SC-2")
        boundary = next(item for item in plan["tasks"] if item["task_kind"] == "boundary")
        self.assertEqual(scenario_one["design_ids"], ["VDN-1"])
        self.assertEqual(scenario_two["design_ids"], ["VDN-2"])
        self.assertEqual(scenario_one["claims"][0]["design_element_ids"], ["VDN-1"])
        self.assertNotIn("design_edge_ids", scenario_one["units"][0])
        self.assertNotIn("depends_on", scenario_two["units"][0])
        self.assertNotIn("attachment_wired_by", scenario_two["units"][0]["runtime_bindings"][0])
        self.assertEqual(
            set(boundary["design_ids"]),
            {"VDN-1", "VDN-2", "VDE-CROSS"},
        )
        boundary_two = next(unit for unit in boundary["units"] if unit["unit_id"] == "WU-2")
        self.assertEqual(boundary_two["depends_on"], ["WU-1"])
        self.assertEqual(boundary_two["runtime_bindings"][0]["attachment_wired_by"], "WU-1")

    def test_scenario_gets_compact_context_only_contracts_for_exact_shared_runtime_targets(self) -> None:
        def node(node_id: str, scenario_id: str, path: str, symbol: str) -> dict[str, object]:
            return {
                "id": node_id,
                "scenario_id": scenario_id,
                "kind": "business_rule",
                "name": node_id,
                "contract": f"Behavior owned by {scenario_id}.",
                "runtime_binding": {
                    "runtime_kind": "service",
                    "target": {"component_id": "api", "path": path, "symbol": symbol},
                    "attachment": {"owner_path": "apps/api/src/app.ts"},
                },
                "surface_contract": {
                    "schema_version": "design_surface_contract_v1",
                    "surface": "business_rule",
                    "decision": f"Decision for {scenario_id}.",
                    "experience_contract": {
                        "runtime_states": {"error": {"presentation": f"{scenario_id} error feedback"}},
                        "focus_flow": ["browser-only focus detail"],
                    },
                },
                "source_observation_refs": ["repo:large-context"],
            }

        units = [
            {
                "unit_id": "WU-1",
                "scenario_id": "SC-1",
                "requirement_refs": ["REQ-1"],
                "design_node_ids": ["VDN-1A", "VDN-1B"],
                "design_edge_ids": [],
            },
            {
                "unit_id": "WU-3",
                "scenario_id": "SC-3",
                "requirement_refs": ["REQ-3"],
                "design_node_ids": ["VDN-3A", "VDN-3B", "VDN-3-OTHER"],
                "design_edge_ids": ["VDE-3-SHARED", "VDE-3-OTHER"],
            },
        ]
        design_graph = {
            "nodes": [
                node("VDN-1A", "SC-1", "apps/api/src/shared.ts", "run"),
                node("VDN-1B", "SC-1", "apps/api/src/shared.ts", "save"),
                node("VDN-3A", "SC-3", "apps/api/src/shared.ts", "run"),
                node("VDN-3B", "SC-3", "apps/api/src/shared.ts", "save"),
                node("VDN-3-OTHER", "SC-3", "apps/api/src/shared.ts", "other"),
            ],
            "edges": [
                {
                    "id": "VDE-3-SHARED",
                    "scenario_id": "SC-3",
                    "kind": "calls",
                    "from": "VDN-3A",
                    "to": "VDN-3B",
                    "boundary_contract": {"provider_operation": "save"},
                },
                {
                    "id": "VDE-3-OTHER",
                    "scenario_id": "SC-3",
                    "kind": "calls",
                    "from": "VDN-3A",
                    "to": "VDN-3-OTHER",
                },
            ],
        }
        plan = build_code_validation_review_plan(
            work_packet={"execution_units": units},
            approved_context={
                "implementation_plan_structured_data": {"work_units": units},
                "vertical_design_graph_data": design_graph,
            },
            validation_claims=[
                {"claim_id": "CODE-1", "requirement_ref": "REQ-1"},
                {"claim_id": "CODE-3", "requirement_ref": "REQ-3"},
            ],
        )

        scenario_one = next(item for item in plan["tasks"] if item["task_id"] == "scenario:SC-1")
        self.assertEqual(scenario_one["design_ids"], ["VDN-1A", "VDN-1B"])
        self.assertEqual(
            scenario_one["context_only_design_ids"],
            ["VDE-3-SHARED", "VDN-3A", "VDN-3B"],
        )
        context = {item["id"]: item for item in scenario_one["context_only_design_contracts"]}
        self.assertNotIn("VDN-3-OTHER", context)
        self.assertNotIn("VDE-3-OTHER", context)
        self.assertNotIn("source_observation_refs", context["VDN-3A"])
        # Shared-code behavior in the experience contract stays visible; browser-deferred fields do not.
        self.assertEqual(
            context["VDN-3A"]["surface_contract"]["experience_contract"],
            {"runtime_states": {"error": {"presentation": "SC-3 error feedback"}}},
        )

        prompt = json.loads(
            _code_validation_task_prompt(
                task=scenario_one,
                code_snippets=[],
                trace_mappings=[],
                verification_evidence={},
                symbol_evidence={},
                scenario_results=[],
            )
        )
        self.assertEqual(
            prompt["task_contract"]["context_only_design_ids"],
            ["VDE-3-SHARED", "VDN-3A", "VDN-3B"],
        )
        self.assertEqual(prompt["related_design_context"]["role"], "explanatory_only")
        self.assertEqual(
            prompt["expected_json_schema"]["findings"][0]["design_refs"],
            "subset of required_reviewed_design_ids",
        )
        rules = "\n".join(prompt["validator_rules"])
        self.assertIn("Check it before calling shared-code behavior invented", rules)
        self.assertIn("Never review its scenarios", rules)
        self.assertIn("do not block the run and do not require code to break one approved contract", rules)
        self.assertEqual(
            prompt["task_contract"]["repair_policy"]["out_of_scope_or_unsatisfiable_requirement"], "system/block_run"
        )

    def test_code_review_projects_browser_ui_fields_out_of_current_authority_and_units(self) -> None:
        unit = {
            "unit_id": "WU-UI",
            "scenario_id": "SC-1",
            "requirement_refs": ["REQ-1"],
            "design_node_ids": ["VDN-UI"],
            "surface_contracts": [{"design_node_id": "VDN-UI", "duplicate": True}],
            "boundary_contracts": [{"design_edge_id": "VDE-UI", "duplicate": True}],
            "qa_test_intents": [{"stage_id": "browser", "intent": "Deferred browser check"}],
            "test_intents": [{"stage_id": "unit", "intent": "Current behavior check"}],
        }
        design_graph = {
            "nodes": [
                {
                    "id": "VDN-UI",
                    "scenario_id": "SC-1",
                    "kind": "ui_interaction",
                    "evidence_reason": "GAP: stale construction-time rationale",
                    "evidence_refs": ["repo:old"],
                    "source_observation_refs": ["repo:observation"],
                    "semantic_key": "old-semantic-key",
                    "disposition": "accepted",
                    "surface_contract": {
                        "surface": "ui",
                        "states": ["ready"],
                        "experience_contract": {
                            "component_structure": ["Current component structure"],
                            "feedback": ["Current feedback"],
                            "accessibility": ["Static ARIA contract"],
                            "focus_flow": ["Browser focus flow"],
                            "focus_policies": ["Browser focus policy"],
                            "responsive": ["Responsive rule"],
                            "visual_contract": {"layout": ["Visual layout"]},
                            "guidance_refs": ["uiux:guide"],
                            "repo_evidence_refs": ["repo:evidence"],
                        },
                    },
                }
            ],
            "edges": [],
        }
        task = build_code_validation_review_plan(
            work_packet={"execution_units": [unit]},
            approved_context={
                "implementation_plan_structured_data": {"work_units": [unit]},
                "vertical_design_graph_data": design_graph,
            },
            validation_claims=[{"claim_id": "CODE-1", "requirement_ref": "REQ-1"}],
        )["tasks"][0]

        experience = task["approved_design_contracts"][0]["surface_contract"]["experience_contract"]
        projected_contract = task["approved_design_contracts"][0]
        self.assertEqual(projected_contract["id"], "VDN-UI")
        self.assertEqual(projected_contract["kind"], "ui_interaction")
        self.assertIn("surface_contract", projected_contract)
        for field in (
            "evidence_reason",
            "evidence_refs",
            "source_observation_refs",
            "semantic_key",
            "disposition",
        ):
            self.assertNotIn(field, projected_contract)
        self.assertEqual(experience["component_structure"], ["Current component structure"])
        self.assertEqual(experience["feedback"], ["Current feedback"])
        self.assertNotIn("accessibility", experience)
        self.assertNotIn("focus_flow", experience)
        self.assertNotIn("focus_policies", experience)
        self.assertNotIn("responsive", experience)
        self.assertNotIn("visual_contract", experience)
        self.assertEqual(
            task["deferred_design_sections"],
            [
                {
                    "design_id": "VDN-UI",
                    "stage": "browser_ui_qa",
                    "field_paths": [
                        "surface_contract.experience_contract.accessibility",
                        "surface_contract.experience_contract.focus_flow",
                        "surface_contract.experience_contract.focus_policies",
                        "surface_contract.experience_contract.guidance_refs",
                        "surface_contract.experience_contract.repo_evidence_refs",
                        "surface_contract.experience_contract.responsive",
                        "surface_contract.experience_contract.visual_contract",
                    ],
                }
            ],
        )
        projected_unit = task["units"][0]
        self.assertNotIn("surface_contracts", projected_unit)
        self.assertNotIn("boundary_contracts", projected_unit)
        self.assertNotIn("qa_test_intents", projected_unit)
        self.assertNotIn("test_intents", projected_unit)
        self.assertEqual(projected_unit["test_paths"], [])

        prompt = json.loads(
            _code_validation_task_prompt(
                task=task,
                code_snippets=[],
                trace_mappings=[],
                verification_evidence={},
                symbol_evidence={},
                scenario_results=[],
            )
        )
        self.assertEqual(prompt["deferred_review_context"]["stage"], "browser_ui_qa")
        self.assertIn("Do not infer their content", prompt["deferred_review_context"]["policy"])

    def test_ledger_records_resolved_and_regressed_findings(self) -> None:
        finding = {"type": "llm_semantic_gap", "message": "missing", "canonical_key": "stable-key"}
        first = build_finding_ledger(
            previous=None,
            snapshot_id="snapshot-1",
            active_findings=[finding],
            disputed_findings=[],
            rejected_findings=[],
        )
        resolved = build_finding_ledger(
            previous=first,
            snapshot_id="snapshot-2",
            active_findings=[],
            disputed_findings=[],
            rejected_findings=[],
        )
        regressed = build_finding_ledger(
            previous=resolved,
            snapshot_id="snapshot-3",
            active_findings=[finding],
            disputed_findings=[],
            rejected_findings=[],
        )

        self.assertEqual(resolved["entries"][0]["status"], "resolved")
        self.assertEqual(regressed["entries"][0]["status"], "regressed")


if __name__ == "__main__":
    unittest.main()
