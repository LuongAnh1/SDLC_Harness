"""Focused tests for the standalone Requirement Model V2 slice."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from sdlc_harness.agents.requirements import (
    RequirementModelV2,
    normalize_and_validate_requirement_model_v2,
    render_formal_use_case_v2,
    render_requirement_packet_v2,
)
from sdlc_harness.core.errors import HarnessError


class RequirementModelV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source_text = (root / "fixtures" / "etour" / "UC2.txt").read_text(encoding="utf-8")
        cls.payload = json.loads((root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8"))

    def validate(self, payload: dict[str, object] | None = None):
        return normalize_and_validate_requirement_model_v2(deepcopy(payload or self.payload), self.source_text)

    def issue_codes(self, payload: dict[str, object]) -> set[str]:
        return {issue.code for issue in self.validate(payload).report.issues}

    def test_uc2_parses_normalizes_and_renders_deterministically(self) -> None:
        result = self.validate()
        self.assertTrue(result.report.ok)
        self.assertEqual(result.report.status, "clarification_required")
        self.assertEqual(result.report.blocking_question_ids, ["Q-1", "Q-2", "Q-3", "Q-4"])
        self.assertTrue(all(question["effective_blocking"] for question in result.model.open_questions))

        packet = render_requirement_packet_v2(result.model)
        formal = render_formal_use_case_v2(result.model)
        self.assertEqual(packet, render_requirement_packet_v2(result.model))
        self.assertEqual(formal, render_formal_use_case_v2(result.model))
        self.assertIn("[AF-1.1] UNKNOWN_FROM_SOURCE [Q-1]", formal)
        self.assertIn("[EF-2.OUTCOME] UNKNOWN_FROM_SOURCE [Q-3]", formal)
        self.assertIn("[Q-4] BLOCKING | data_identity", formal)
        main_flow = self._formal_section(formal, "Main Flow:", "Alternative Flows:")
        self.assertNotIn("cancels the operation", main_flow)
        self.assertNotIn("invalid or insufficient", main_flow)
        self.assertNotIn("Interruption of the connection", main_flow)
        self.assertIn("[BR-1]", packet)
        self.assertIn("[AC-1]", packet)

    def test_round_trip_returns_a_detached_copy(self) -> None:
        model = RequirementModelV2.from_dict(self.payload)
        serialized = model.to_dict()
        serialized["open_questions"][0]["question"] = "changed"
        self.assertNotEqual(serialized, model.to_dict())

    def test_invalid_root_payload_raises_stable_harness_error(self) -> None:
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2.from_dict({"schema_version": "requirement_model_v2"})
        self.assertEqual(context.exception.error_code, "requirement_model_v2_invalid")

    def test_revision_type_is_not_coerced(self) -> None:
        payload = deepcopy(self.payload)
        payload["revision"] = "1"
        with self.assertRaises(HarnessError) as context:
            RequirementModelV2.from_dict(payload)
        self.assertEqual(context.exception.error_code, "requirement_model_v2_invalid")

    def test_duplicate_id_and_wrong_prefix_are_rejected(self) -> None:
        payload = deepcopy(self.payload)
        payload["preconditions"][0]["id"] = "GOAL-1"
        codes = self.issue_codes(payload)
        self.assertIn("invalid_id_prefix", codes)
        self.assertIn("duplicate_id", codes)

    def test_invalid_requirement_status_is_rejected_for_goal_and_actor(self) -> None:
        payload = deepcopy(self.payload)
        payload["use_case"]["goal"]["status"] = "confirmed"
        payload["use_case"]["actors"][0]["status"] = "confirmed"
        issues = self.validate(payload).report.issues
        paths = {issue.path for issue in issues if issue.code == "invalid_requirement_status"}
        self.assertEqual(paths, {"use_case.goal.status", "use_case.actors[0].status"})

    def test_dangling_references_are_rejected(self) -> None:
        payload = deepcopy(self.payload)
        payload["open_questions"][0]["affects"] = ["BR-999"]
        payload["acceptance_criteria"][0]["verifies"] = ["MF-999"]
        self.assertIn("dangling_reference", self.issue_codes(payload))

    def test_flow_references_are_validated(self) -> None:
        payload = deepcopy(self.payload)
        payload["flows"]["main"][0]["actor"] = "ACT-999"
        payload["flows"]["alternatives"][0]["evidence_refs"] = ["E-999"]
        payload["flows"]["exceptions"][0]["open_question_refs"] = ["Q-999"]
        issues = self.validate(payload).report.issues
        paths = {issue.path for issue in issues if issue.code == "dangling_reference"}
        self.assertIn("flows.main[0].actor", paths)
        self.assertIn("flows.alternatives[0].evidence_refs", paths)
        self.assertIn("flows.exceptions[0].open_question_refs", paths)

    def test_evidence_quote_must_exist_in_source(self) -> None:
        payload = deepcopy(self.payload)
        payload["evidence"][0]["quote"] = "invented evidence"
        self.assertIn("evidence_not_in_source", self.issue_codes(payload))

    def test_explicit_and_human_decision_require_provenance(self) -> None:
        payload = deepcopy(self.payload)
        payload["preconditions"][0]["evidence_refs"] = []
        payload["business_rules"][0]["status"] = "human_decision"
        payload["business_rules"][0]["decision_ref"] = "D-999"
        codes = self.issue_codes(payload)
        self.assertIn("explicit_without_evidence", codes)
        self.assertIn("human_decision_without_accepted_decision", codes)

    def test_question_dependency_cycle_is_rejected(self) -> None:
        payload = deepcopy(self.payload)
        payload["open_questions"][0]["depends_on"] = ["Q-2"]
        payload["open_questions"][1]["depends_on"] = ["Q-1"]
        self.assertIn("question_dependency_cycle", self.issue_codes(payload))

    def test_exception_behavior_is_rejected_in_main_and_postconditions(self) -> None:
        payload = deepcopy(self.payload)
        payload["flows"]["main"][0]["action"] = "Cancel the operation."
        payload["postconditions"][0]["text"] = "Connection failure is reported."
        codes = self.issue_codes(payload)
        self.assertIn("exception_in_main_flow", codes)
        self.assertIn("exception_in_postcondition", codes)

    def test_assumption_cannot_be_marked_as_confirmed(self) -> None:
        payload = deepcopy(self.payload)
        payload["assumptions"] = [
            {
                "id": "ASM-1",
                "text": "The operator uses a desktop browser.",
                "status": "explicit",
                "evidence_refs": ["E-2"],
                "open_question_refs": [],
                "reason": "Convenience",
                "affects": ["MF-1"]
            }
        ]
        self.assertIn("assumption_marked_confirmed", self.issue_codes(payload))

    def test_resolved_questions_require_an_accepted_decision(self) -> None:
        payload = deepcopy(self.payload)
        payload["open_questions"][0]["status"] = "resolved"
        self.assertIn("resolved_question_without_decision", self.issue_codes(payload))

    @staticmethod
    def _formal_section(text: str, start: str, end: str) -> str:
        return text.split(start, 1)[1].split(end, 1)[0]


if __name__ == "__main__":
    unittest.main()
