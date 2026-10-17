from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.validator.code_contract import (
    build_finding_ledger,
    deterministic_contract_findings,
    prepare_semantic_trial,
)
from sdlc_harness.agents.validator.code_trace import CodeTraceValidator


class SequenceCodeTraceLlm:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return json.dumps(response)


class InvalidCodeTraceLlm:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return "not-json"


def _review(*missing_links: dict[str, object]) -> dict[str, object]:
    return {
        "ok": not missing_links,
        "confidence": 0.9,
        "summary": "review",
        "graph_edges": [],
        "missing_links": list(missing_links),
        "risks": [],
    }


def _finding(message: str = "Approved behavior is missing.") -> dict[str, object]:
    return {
        "type": "missing_behavior",
        "blocking": True,
        "owner": "developer",
        "recommended_action": "developer_repair",
        "message": message,
        "requirement_ref": "REQ-1",
        "design_element_id": "method:Sample.run",
        "code_ref": "src/main/java/Sample.java#run",
        "evidence_ref": "approved_design",
        "evidence_quote": "Run the behavior.",
    }


class CodeValidationConsensusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.target = Path(self.temp_dir.name)
        source = self.target / "src/main/java/Sample.java"
        test = self.target / "src/test/java/SampleTest.java"
        source.parent.mkdir(parents=True)
        test.parent.mkdir(parents=True)
        source.write_text("public class Sample { public void run() {} }\n", encoding="utf-8")
        test.write_text("public class SampleTest { public static void main(String[] args) {} }\n", encoding="utf-8")
        self.packet = {
            "requirement_contract": {"version": "v2", "revision": 1},
            "canonical_requirements": [{"id": "REQ-1", "content": "Run the behavior."}],
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-1",
                    "content": "Run the behavior.",
                    "design_targets": ["Sample.run"],
                    "design_element_ids": ["method:Sample.run"],
                    "planned_code_targets": ["src/main/java/Sample.java#run"],
                    "test_intents": ["Run SampleTest."],
                }
            ],
            "design_mappings_by_file": {
                "src/main/java/Sample.java": [
                    {
                        "design_element_id": "method:Sample.run",
                        "design_element_type": "method",
                        "design_element_name": "run",
                        "planned_code_target": "src/main/java/Sample.java#run",
                    }
                ]
            },
            "proposed_files": [{"path": "src/test/java/SampleTest.java"}],
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_two_matching_clean_trials_pass_without_third_call(self) -> None:
        llm = SequenceCodeTraceLlm([_review(), _review(), _review(_finding())])

        result = CodeTraceValidator(llm, validation_trials=3, llm_call_budget=6).validate(
            target_path=self.target,
            work_packet=self.packet,
        )

        self.assertTrue(result.ok)
        self.assertEqual(llm.calls, 2)
        self.assertEqual(result.semantic_consensus["valid_trials"], 2)
        self.assertEqual(result.semantic_consensus["stable_blockers"], [])

    def test_disagreement_runs_third_trial_and_keeps_only_quorum_finding(self) -> None:
        llm = SequenceCodeTraceLlm([_review(_finding()), _review(), _review(_finding())])

        result = CodeTraceValidator(llm, validation_trials=3, llm_call_budget=6).validate(
            target_path=self.target,
            work_packet=self.packet,
        )

        self.assertFalse(result.ok)
        self.assertEqual(llm.calls, 3)
        self.assertEqual(len(result.semantic_consensus["stable_blockers"]), 1)
        self.assertEqual(result.semantic_consensus["stable_blockers"][0]["votes"], 2)

    def test_call_budget_exhaustion_is_inconclusive_without_repair_blocker(self) -> None:
        llm = InvalidCodeTraceLlm()

        result = CodeTraceValidator(llm, validation_trials=3, llm_call_budget=6).validate(
            target_path=self.target,
            work_packet=self.packet,
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.inconclusive)
        self.assertEqual(llm.calls, 6)
        self.assertEqual(result.semantic_consensus["llm_calls_used"], 6)
        self.assertEqual(result.semantic_consensus["stable_blockers"], [])

    def test_interface_parameter_mismatch_is_a_deterministic_blocker(self) -> None:
        interface = self.target / "src/main/java/SampleView.java"
        interface.write_text(
            "public interface SampleView {\n    void showError(String message);\n}\n",
            encoding="utf-8",
        )
        packet = {
            **self.packet,
            "proposed_files": [
                *self.packet["proposed_files"],
                {"path": "src/main/java/SampleView.java"},
            ],
        }
        context = {
            "class_diagram_graph_data": {
                "nodes": [
                    {
                        "className": "SampleView",
                        "methods": [{"name": "showError", "returnType": "void", "parameters": []}],
                    }
                ]
            }
        }

        findings = deterministic_contract_findings(
            target_path=self.target,
            work_packet=packet,
            approved_context=context,
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "contract_signature_mismatch")

    def test_executed_test_claim_is_rejected_as_validator_noise(self) -> None:
        claim = {
            "claim_id": "CODE-1",
            "requirement_ref": "REQ-1",
            "design_element_ids": ["method:Sample.run"],
            "code_targets": ["src/main/java/Sample.java"],
        }
        finding = {
            **_finding("SampleTest was not executed by the test runner."),
            "code_ref": "src/test/java/SampleTest.java#main",
            "claim_id": "CODE-1",
        }

        accepted, observations, rejected = prepare_semantic_trial(
            findings=[finding],
            observations=[],
            claims=[claim],
            verification_evidence={"executed_test_entrypoints": ["SampleTest"]},
            trial_id="trial-1",
        )

        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(len(observations), 1)
        self.assertEqual(rejected[0]["normalization_reason"], "contradicted_by_executed_verification_evidence")

    def test_ledger_records_resolved_and_regressed_findings(self) -> None:
        finding = {**_finding(), "canonical_key": "stable-key"}
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
