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
    def __init__(self, completions: list[LlmCompletion], *, max_tokens: int = 4096):
        self.completions = list(completions)
        self.settings = SimpleNamespace(llm_structured_max_tokens=max_tokens)
        self.calls: list[dict[str, object]] = []

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        return self.completions.pop(0)


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
        client = ScriptedLlmClient(list(completions))
        result = RequirementModelV2Refiner(client).refine(
            self.source,
            run_id="run_v2_test",
            source_artifact_ref=".runtime/runs/run_v2_test/artifacts/use_case_packet_v1.md",
        )
        return result, client

    def test_valid_json_is_stamped_by_python_and_keeps_clarification_state(self) -> None:
        payload = deepcopy(self.payload)
        payload["schema_version"] = "hostile"
        payload["revision"] = 999
        payload["run_id"] = "wrong"
        payload["decisions"] = [{"id": "D-pretend"}]
        result, client = self.refine(self.completion(payload, usage={"completion_tokens": 1200}))

        self.assertEqual(result.model.schema_version, "requirement_model_v2")
        self.assertEqual(result.model.revision, 1)
        self.assertEqual(result.model.run_id, "run_v2_test")
        self.assertEqual(result.model.decisions, [])
        self.assertEqual(result.final_report.status, "clarification_required")
        self.assertFalse(result.repaired)
        self.assertEqual(
            result.model.source["fingerprint"],
            "sha256:" + hashlib.sha256(self.source.text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(result.attempts[0].usage, {"completion_tokens": 1200})
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0]["json_mode"])
        self.assertEqual(client.calls[0]["max_tokens"], 4096)

    def test_truncated_output_uses_one_format_recovery(self) -> None:
        result, client = self.refine(
            self.completion('{"use_case":', finish_reason="length"),
            self.completion(self.payload),
        )
        self.assertTrue(result.repaired)
        self.assertEqual([attempt.purpose for attempt in result.attempts], ["initial", "format_recovery"])
        self.assertEqual(len(client.calls), 2)

    def test_unparseable_output_fails_after_one_recovery(self) -> None:
        client = ScriptedLlmClient([self.completion("not json"), self.completion("still not json")])
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_model_v2_parse_failed")
        self.assertEqual(len(client.calls), 2)

    def test_second_truncated_output_has_stable_error(self) -> None:
        client = ScriptedLlmClient(
            [
                self.completion("not json"),
                self.completion('{"use_case":', finish_reason="length"),
            ]
        )
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_model_v2_output_truncated")
        self.assertEqual(len(client.calls), 2)

    def test_grounding_violation_uses_one_validator_repair(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["evidence"][0]["quote"] = "invented evidence"
        result, client = self.refine(self.completion(invalid), self.completion(self.payload))
        self.assertTrue(result.repaired)
        self.assertEqual(result.attempts[1].purpose, "validator_repair")
        self.assertIn("evidence_not_in_source", str(client.calls[1]["prompt"]))
        self.assertTrue(result.final_report.ok)

    def test_invalid_root_schema_uses_bounded_validator_repair(self) -> None:
        result, _ = self.refine(self.completion({"use_case": {}}), self.completion(self.payload))
        self.assertTrue(result.repaired)
        self.assertEqual(result.attempts[0].parse_status, "schema_failed")
        self.assertEqual(result.attempts[1].purpose, "validator_repair")

    def test_invalid_after_validator_repair_fails_closed(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["evidence"][0]["quote"] = "invented evidence"
        client = ScriptedLlmClient([self.completion(invalid), self.completion(invalid)])
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_model_v2_validation_failed")
        self.assertEqual(len(client.calls), 2)

    def test_exception_placement_violations_use_one_bounded_repair(self) -> None:
        invalid = deepcopy(self.payload)
        invalid["flows"]["main"][0]["action"] = "Cancel the operation."
        invalid["postconditions"][0]["text"] = "Connection failure is reported."

        result, client = self.refine(self.completion(invalid), self.completion(self.payload))

        self.assertTrue(result.repaired)
        self.assertEqual([attempt.purpose for attempt in result.attempts], ["initial", "validator_repair"])
        repair_prompt = str(client.calls[1]["prompt"])
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
        client = ScriptedLlmClient([self.completion(invalid)])
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2Refiner(client).refine(
                self.source,
                run_id="run_v2_test",
                source_artifact_ref="use_case_packet_v1.md",
            )
        self.assertEqual(context.exception.error_code, "requirement_model_v2_validation_failed")
        self.assertEqual(len(client.calls), 1)

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
