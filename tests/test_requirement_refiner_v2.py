"""Kiểm thử tinh chỉnh Requirement Model V2 có cấu trúc và ghi artifact."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest

from sdlc_harness.agents.requirements import (
    RequirementModelV2Refiner,
    render_formal_use_case_v2,
    render_requirement_packet_v2,
    validate_requirement_v2_projections,
)
from sdlc_harness.artifacts import write_requirement_v2_artifacts
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.sources.fixtures import UseCaseSource
from sdlc_harness.storage.runtime import RuntimeStore


class ScriptedLlmClient:
    def __init__(self, completions: list[object], *, max_tokens: int = 4096):
        self.completions = list(completions)
        self.settings = SimpleNamespace(llm_structured_max_tokens=max_tokens)
        self.calls: list[dict[str, object]] = []

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        completion = self.completions.pop(0)
        if callable(completion):
            return completion(kwargs)
        assert isinstance(completion, LlmCompletion)
        return completion


class RequirementModelV2RefinerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        source_path = root / "fixtures" / "etour" / "UC2.txt"
        cls.source = UseCaseSource(
            dataset="eTour",
            use_case="UC2",
            source_ref="dataset_use_case:eTour:UC2",
            source_path=source_path,
            text=source_path.read_text(encoding="utf-8").strip(),
        )
        cls.payload = json.loads(
            (root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8")
        )

    def completion(
        self,
        payload: dict[str, object] | str,
        *,
        finish_reason: str = "stop",
        usage: dict[str, object] | None = None,
    ) -> LlmCompletion:
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return LlmCompletion(content=content, finish_reason=finish_reason, usage=usage or {})

    def refine(self, *completions: LlmCompletion):
        scripted = [
            self.as_complete(item) if isinstance(item, LlmCompletion) else item
            for item in completions
        ]
        client = ScriptedLlmClient(self.source_actions() + scripted)
        result = RequirementModelV2Refiner(client).refine(
            self.source,
            run_id="run_v2_test",
            source_artifact_ref=".runtime/runs/run_v2_test/artifacts/use_case_packet_v1.md",
        )
        return result, client

    def source_actions(self) -> list[LlmCompletion]:
        return [
            self.completion({"action": "source.list", "arguments": {}, "summary": "list"}),
            self.completion(
                {
                    "action": "source.read",
                    "arguments": {"source_id": "SRC-1", "start_line": 1, "line_count": 200},
                    "summary": "read",
                }
            ),
        ]

    def as_complete(self, completion: LlmCompletion) -> LlmCompletion:
        if completion.finish_reason == "length":
            return completion
        try:
            payload = json.loads(completion.content)
        except json.JSONDecodeError:
            return completion
        if isinstance(payload, dict) and "action" not in payload:
            payload = deepcopy(payload)
            for evidence in payload.get("evidence", []) if isinstance(payload.get("evidence"), list) else []:
                if isinstance(evidence, dict):
                    evidence["observation_ref"] = "OBS-1"
            payload = {
                "action": "complete",
                "arguments": {"requirement_model": payload},
                "summary": "complete",
            }
        return LlmCompletion(
            content=json.dumps(payload, ensure_ascii=False),
            finish_reason=completion.finish_reason,
            usage=completion.usage,
        )

    def delta_action(self, operations: list[dict[str, object]]):
        def completion(call: dict[str, object]) -> LlmCompletion:
            prompt = str(call["prompt"])
            raw_state = prompt.split("TOOL_STATE_JSON_BEGIN\n", 1)[1].split(
                "\nTOOL_STATE_JSON_END", 1
            )[0]
            state = json.loads(raw_state)
            repair = state["repair_context"]
            return self.completion(
                {
                    "action": "requirement.propose_delta",
                    "arguments": {
                        "requirement_delta": {
                            "schema_version": "requirement_delta_v1",
                            "base_candidate_sha256": repair["base_candidate_sha256"],
                            "operations": operations,
                        }
                    },
                    "summary": "Apply the bounded validator repair.",
                }
            )

        return completion

    def test_valid_json_is_stamped_by_python_and_keeps_clarification_state(self) -> None:
        payload = deepcopy(self.payload)
        payload["schema_version"] = "hostile"
        payload["revision"] = 999
        payload["run_id"] = "wrong"
        payload["decisions"] = [{"id": "D-pretend"}]
        payload["source"] = {"source_id": "SRC-999", "source_ref": "hostile"}
        payload["provenance_ledger"] = {
            "schema_version": "hostile",
            "sources": [{"id": "SRC-999", "source_ref": "hostile"}],
        }
        for evidence in payload["evidence"]:
            evidence["source_id"] = "SRC-999"
        result, client = self.refine(self.completion(payload, usage={"completion_tokens": 1200}))

        self.assertEqual(result.model.schema_version, "requirement_model_v2")
        self.assertEqual(result.model.revision, 1)
        self.assertEqual(result.model.run_id, "run_v2_test")
        self.assertEqual(result.model.decisions, [])
        self.assertEqual(result.model.source["source_id"], "SRC-1")
        self.assertEqual(result.model.provenance_ledger["schema_version"], "requirement_provenance_v1")
        self.assertEqual(result.model.provenance_ledger["sources"][0]["id"], "SRC-1")
        self.assertTrue(all(item["source_id"] == "SRC-1" for item in result.model.evidence))
        self.assertEqual(result.final_report.status, "clarification_required")
        self.assertFalse(result.repaired)
        self.assertEqual(
            result.model.source["fingerprint"],
            "sha256:" + hashlib.sha256(self.source.text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(result.attempts[0].usage, {"completion_tokens": 1200})
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(all(call["json_mode"] for call in client.calls))
        self.assertTrue(all(call["max_tokens"] == 4096 for call in client.calls))

    def test_llm_output_may_omit_python_owned_provenance(self) -> None:
        payload = deepcopy(self.payload)
        payload.pop("provenance_ledger")
        payload["source"] = {}
        for evidence in payload["evidence"]:
            evidence.pop("source_id", None)

        result, client = self.refine(self.completion(payload))

        self.assertTrue(result.final_report.ok)
        self.assertEqual(result.model.source["source_id"], "SRC-1")
        self.assertEqual(result.model.provenance_ledger["sources"][0]["id"], "SRC-1")
        self.assertEqual(len(client.calls), 3)

    def test_tool_prompt_explains_scenario_and_surface_question_relationships(self) -> None:
        _, client = self.refine(self.completion(self.payload))
        prompt = str(client.calls[2]["prompt"])
        self.assertIn("scenario-local allowlist", prompt)
        self.assertIn("question's `affects`", prompt)
        self.assertIn("surface's `requirement_refs`", prompt)
        self.assertIn("Never put", prompt)
        self.assertIn("AC-*", prompt)
        self.assertIn("Gap routing before clarification", prompt)
        self.assertIn('category="source_conflict"', prompt)
        self.assertIn("Never ask a human to repair", prompt)

    def test_truncated_action_uses_bounded_action_retry(self) -> None:
        result, client = self.refine(
            self.completion('{"use_case":', finish_reason="length"),
            self.completion(self.payload),
        )
        self.assertFalse(result.repaired)
        self.assertEqual([attempt.purpose for attempt in result.attempts], ["initial"])
        self.assertEqual(len(client.calls), 4)

    def test_unparseable_action_fails_after_bounded_retries(self) -> None:
        client = ScriptedLlmClient(
            self.source_actions()
            + [self.completion("not json"), self.completion("still not json")]
        )
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client, max_action_retries=1).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_tool_action_invalid")
        self.assertEqual(len(client.calls), 4)

    def test_second_truncated_output_has_stable_error(self) -> None:
        client = ScriptedLlmClient(
            self.source_actions()
            + [
                self.completion('{"action":', finish_reason="length"),
                self.completion('{"action":', finish_reason="length"),
            ]
        )
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client, max_action_retries=1).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_tool_action_truncated")
        self.assertEqual(len(client.calls), 4)

    def test_grounding_violation_uses_one_validator_repair(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["evidence"][0]["quote"] = "invented evidence"
        result, client = self.refine(self.completion(invalid), self.completion(self.payload))
        self.assertTrue(result.repaired)
        self.assertEqual(result.attempts[1].purpose, "validator_repair")
        self.assertIn("requirement_evidence_observation_invalid", str(client.calls[3]["prompt"]))
        self.assertTrue(result.final_report.ok)
        missing = [
            gap for gap in result.gap_report["gaps"] if gap["type"] == "missing_evidence"
        ]
        self.assertTrue(missing)
        self.assertTrue(all(gap["status"] == "resolved" for gap in missing))

    def test_repair_prompt_explains_acceptance_and_scenario_question_fixes(self) -> None:
        invalid = deepcopy(self.payload)
        scenario = invalid["scenario_contract"]["scenarios"][0]
        scenario["requirement_refs"].append("AC-1")
        scenario["open_question_refs"] = []
        result, client = self.refine(
            self.completion(invalid),
            self.delta_action(
                [
                    {
                        "op": "entity.update",
                        "target_id": "SC-1",
                        "fields": {
                            "requirement_refs": [
                                ref for ref in scenario["requirement_refs"] if ref != "AC-1"
                            ]
                        },
                    }
                ]
            ),
        )
        self.assertTrue(result.repaired)
        repair_prompt = str(client.calls[3]["prompt"])
        self.assertIn("Move every AC-* ID out of scenario.requirement_refs", repair_prompt)
        self.assertIn("scenario.open_question_refs", repair_prompt)
        self.assertIn("requirement.propose_delta", repair_prompt)
        self.assertIn("Requirement Repair Delta V1", repair_prompt)
        self.assertNotIn("## Required JSON shape", repair_prompt)

    def test_full_model_repair_is_rejected_when_delta_base_exists(self) -> None:
        invalid = deepcopy(self.payload)
        scenario = invalid["scenario_contract"]["scenarios"][0]
        scenario["requirement_refs"].append("AC-1")
        result, client = self.refine(
            self.completion(invalid),
            self.completion(self.payload),
            self.delta_action(
                [
                    {
                        "op": "entity.update",
                        "target_id": "SC-1",
                        "fields": {
                            "requirement_refs": [
                                ref for ref in scenario["requirement_refs"] if ref != "AC-1"
                            ]
                        },
                    }
                ]
            ),
        )

        self.assertTrue(result.final_report.ok)
        self.assertEqual(len(client.calls), 5)
        self.assertIn("requirement_delta_required", str(client.calls[4]["prompt"]))

    def test_applied_delta_is_persisted_and_journal_arguments_are_sanitized(self) -> None:
        invalid = deepcopy(self.payload)
        scenario = invalid["scenario_contract"]["scenarios"][0]
        scenario["requirement_refs"].append("AC-1")
        delta = self.delta_action(
            [
                {
                    "op": "entity.update",
                    "target_id": "SC-1",
                    "fields": {
                        "requirement_refs": [
                            ref for ref in scenario["requirement_refs"] if ref != "AC-1"
                        ]
                    },
                }
            ]
        )
        client = ScriptedLlmClient(
            self.source_actions() + [self.as_complete(self.completion(invalid)), delta]
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            result = RequirementModelV2Refiner(client, store=store).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref=".runtime/runs/run_v2_test/artifacts/use_case_packet_v1.md",
            )
            delta_artifact = json.loads(
                (root / result.delta_artifact_paths[0]).read_text(encoding="utf-8")
            )
            journal = json.loads((root / result.source_journal_path).read_text(encoding="utf-8"))

        self.assertEqual(delta_artifact["status"], "applied")
        self.assertEqual(delta_artifact["delta"]["schema_version"], "requirement_delta_v1")
        delta_call = journal["calls"][-1]
        self.assertEqual(delta_call["action"], "requirement.propose_delta")
        self.assertIn("delta_sha256", delta_call["arguments"])
        self.assertNotIn("requirement_delta", delta_call["arguments"])

    def test_rejected_delta_is_persisted_and_journal_arguments_are_sanitized(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["scenario_contract"]["scenarios"][0]["requirement_refs"].append("AC-1")
        rejected_delta = self.delta_action(
            [
                {
                    "op": "entity.update",
                    "target_id": "SC-1",
                    "fields": {"id": "SC-999"},
                }
            ]
        )
        client = ScriptedLlmClient(
            self.source_actions() + [self.as_complete(self.completion(invalid)), rejected_delta]
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            with self.assertRaises(HarnessError) as context:
                RequirementModelV2Refiner(client, store=store).refine(
                    self.source,
                    run_id="run_v2_test",
                    source_artifact_ref=(
                        ".runtime/runs/run_v2_test/artifacts/use_case_packet_v1.md"
                    ),
                )
            delta_artifact = json.loads(
                (
                    root
                    / ".runtime/runs/run_v2_test/artifacts/requirement_delta_v1_a1.json"
                ).read_text(encoding="utf-8")
            )
            journal = json.loads(
                (
                    root
                    / ".runtime/runs/run_v2_test/artifacts/requirement_source_journal_v1.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(context.exception.error_code, "requirement_delta_policy_violation")
        self.assertEqual(delta_artifact["status"], "rejected")
        self.assertEqual(
            delta_artifact["error"]["error_code"],
            "requirement_delta_policy_violation",
        )
        self.assertEqual(journal["status"], "failed")
        delta_call = journal["calls"][-1]
        self.assertEqual(delta_call["action"], "requirement.propose_delta")
        self.assertIn("delta_sha256", delta_call["arguments"])
        self.assertNotIn("requirement_delta", delta_call["arguments"])

    def test_invalid_root_schema_uses_bounded_validator_repair(self) -> None:
        result, _ = self.refine(self.completion({"use_case": {}}), self.completion(self.payload))
        self.assertTrue(result.repaired)
        self.assertEqual(result.attempts[0].parse_status, "schema_failed")
        self.assertEqual(result.attempts[1].purpose, "validator_repair")

    def test_missing_scenario_contract_uses_bounded_validator_repair(self) -> None:
        legacy = deepcopy(self.payload)
        legacy.pop("scenario_contract")
        result, client = self.refine(self.completion(legacy), self.completion(self.payload))
        self.assertTrue(result.repaired)
        self.assertEqual(result.attempts[0].parse_status, "schema_failed")
        self.assertEqual(result.attempts[1].purpose, "validator_repair")
        self.assertIn("scenario_contract", str(client.calls[3]["prompt"]))

    def test_invalid_after_validator_repair_fails_closed(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["evidence"][0]["quote"] = "invented evidence"
        client = ScriptedLlmClient(
            self.source_actions()
            + [self.as_complete(self.completion(invalid)), self.as_complete(self.completion(invalid))]
        )
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_model_v2_validation_failed")
        self.assertEqual(len(client.calls), 4)

    def test_exception_placement_violations_use_one_bounded_repair(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["flows"]["main"][0]["action"] = "Cancel the operation."
        invalid["postconditions"][0]["text"] = "Connection failure is reported."

        result, client = self.refine(
            self.completion(invalid),
            self.delta_action(
                [
                    {
                        "op": "entity.update",
                        "target_id": "MF-1",
                        "fields": {"action": self.payload["flows"]["main"][0]["action"]},
                    },
                    {
                        "op": "entity.update",
                        "target_id": "POST-1",
                        "fields": {"text": self.payload["postconditions"][0]["text"]},
                    },
                ]
            ),
        )

        self.assertTrue(result.repaired)
        self.assertEqual([attempt.purpose for attempt in result.attempts], ["initial", "validator_repair"])
        repair_prompt = str(client.calls[3]["prompt"])
        self.assertIn("exception_in_main_flow", repair_prompt)
        self.assertIn("exception_in_postcondition", repair_prompt)
        self.assertIn("flows.exceptions", repair_prompt)
        self.assertTrue(result.final_report.ok)

    def test_unapproved_semantic_violation_still_fails_without_repair(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["assumptions"] = [
            {
                "id": "ASM-1",
                "text": "The operator uses a desktop browser.",
                "status": "explicit",
                "evidence_refs": [],
                "open_question_refs": [],
                "reason": "Not confirmed by the source.",
                "affects": [],
            }
        ]
        client = ScriptedLlmClient(self.source_actions() + [self.as_complete(self.completion(invalid))])
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_model_v2_validation_failed")
        self.assertEqual(len(client.calls), 3)

    def test_projection_validator_detects_modified_output(self) -> None:
        result, _ = self.refine(self.completion(self.payload))
        packet = render_requirement_packet_v2(result.model)
        formal = render_formal_use_case_v2(result.model)
        report = validate_requirement_v2_projections(
            result.model,
            requirement_packet=packet + "changed\n",
            formal_use_case=formal,
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.issues), 1)

    def test_artifact_writer_writes_revisioned_bundle(self) -> None:
        result, _ = self.refine(self.completion(self.payload))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            artifacts = write_requirement_v2_artifacts(
                store=store,
                run_id="run_v2_test",
                refinement_result=result,
            )
            self.assertEqual(
                set(artifacts),
                {
                    "requirement_model",
                    "requirement_packet",
                    "formal_use_case",
                    "requirement_validation",
                    "requirement_decisions",
                },
            )
            for relative_path in artifacts.values():
                self.assertTrue((root / relative_path).is_file())
            model = json.loads((root / artifacts["requirement_model"]).read_text(encoding="utf-8"))
            audit = json.loads((root / artifacts["requirement_validation"]).read_text(encoding="utf-8"))
            self.assertEqual(model["validation"]["status"], "clarification_required")
            self.assertTrue(audit["projection_report"]["ok"])
            self.assertEqual(audit["attempts"][0]["max_tokens"], 4096)

    def test_artifact_writer_rejects_run_mismatch_and_invalid_report(self) -> None:
        result, _ = self.refine(self.completion(self.payload))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            with self.assertRaises(HarnessError) as mismatch:
                write_requirement_v2_artifacts(
                    store=store,
                    run_id="another_run",
                    refinement_result=result,
                )
            self.assertEqual(mismatch.exception.error_code, "requirement_model_v2_validation_failed")

            invalid_report = replace(result.final_report, ok=False, status="invalid")
            with self.assertRaises(HarnessError) as invalid:
                write_requirement_v2_artifacts(
                    store=store,
                    run_id="run_v2_test",
                    refinement_result=replace(result, final_report=invalid_report),
                )
            self.assertEqual(invalid.exception.error_code, "requirement_model_v2_validation_failed")


if __name__ == "__main__":
    unittest.main()
