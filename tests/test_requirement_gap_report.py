"""Kiểm thử artifact phân loại GAP của Requirement workflow."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sdlc_harness.agents.requirements import (
    build_requirement_gap_report,
    normalize_and_validate_requirement_model_v2,
    validate_requirement_gap_report,
)
from sdlc_harness.artifacts import read_requirement_gap_report, write_requirement_gap_report
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.storage.runtime import RuntimeStore


class RequirementGapReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.root = root
        cls.source_text = (root / "fixtures" / "etour" / "UC2.txt").read_text(
            encoding="utf-8"
        )
        cls.payload = json.loads(
            (root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(
                encoding="utf-8"
            )
        )

    def model(self, payload: dict[str, object] | None = None):
        result = normalize_and_validate_requirement_model_v2(
            deepcopy(payload or self.payload),
            {"SRC-1": self.source_text},
        )
        self.assertTrue(result.report.ok)
        return result.model

    def test_business_decisions_have_python_owned_stable_identity_and_route(self) -> None:
        first = build_requirement_gap_report(run_id="run_gap", model=self.model())
        second = build_requirement_gap_report(run_id="run_gap", model=self.model())

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "clarification_required")
        self.assertEqual(
            [gap["id"] for gap in first["gaps"]],
            [f"GAP-{index}" for index in range(1, len(first["gaps"]) + 1)],
        )
        self.assertTrue(first["gaps"])
        self.assertTrue(all(gap["type"] == "business_decision" for gap in first["gaps"]))
        self.assertTrue(all(gap["owner"] == "human" for gap in first["gaps"]))
        self.assertTrue(all(gap["route"] == "clarification" for gap in first["gaps"]))

    def test_source_conflict_requires_human_clarification_and_keeps_both_sources(self) -> None:
        payload = deepcopy(self.payload)
        question = payload["open_questions"][0]
        question["category"] = "source_conflict"
        question["evidence_refs"] = ["E-1", "E-2"]

        report = build_requirement_gap_report(run_id="run_conflict", model=self.model(payload))
        gap = next(item for item in report["gaps"] if item["question_ref"] == question["id"])

        self.assertEqual(gap["type"], "source_conflict")
        self.assertEqual(gap["status"], "unresolved")
        self.assertEqual(gap["owner"], "human")
        self.assertEqual(gap["route"], "clarification")
        self.assertEqual(gap["evidence_refs"], ["E-1", "E-2"])

    def test_missing_evidence_is_resolved_after_the_source_loop_produces_a_valid_model(self) -> None:
        attempts = [
            {
                "validation_report": {
                    "issues": [
                        {
                            "code": "evidence_not_in_source",
                            "path": "evidence[0].quote",
                            "message": "Quote is not present in the selected source.",
                        }
                    ]
                }
            }
        ]
        before = build_requirement_gap_report(run_id="run_repair", attempts=attempts)
        after = build_requirement_gap_report(
            run_id="run_repair",
            model=self.model(),
            previous_report=before,
        )
        gap = next(item for item in after["gaps"] if item["type"] == "missing_evidence")

        self.assertEqual(before["gaps"][0]["status"], "unresolved")
        self.assertEqual(gap["status"], "resolved")
        self.assertEqual(gap["owner"], "requirement_agent")
        self.assertEqual(gap["route"], "continue_source_loop")

    def test_source_unavailable_is_a_failed_environment_gap(self) -> None:
        report = build_requirement_gap_report(
            run_id="run_source_failure",
            error=HarnessError(
                "source_snapshot_corrupted",
                "The immutable source snapshot no longer matches its digest.",
            ),
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["gaps"]), 1)
        gap = report["gaps"][0]
        self.assertEqual(gap["type"], "source_unavailable")
        self.assertEqual(gap["owner"], "environment")
        self.assertEqual(gap["route"], "fail")

    def test_tool_protocol_error_is_not_misclassified_as_a_requirement_gap(self) -> None:
        report = build_requirement_gap_report(
            run_id="run_tool_failure",
            error=HarnessError("requirement_source_query_invalid", "Invalid query."),
        )

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["gaps"], [])

    def test_digest_detects_tampering(self) -> None:
        report = build_requirement_gap_report(run_id="run_tamper", model=self.model())
        report["gaps"][0]["message"] = "tampered"

        with self.assertRaises(HarnessError) as context:
            validate_requirement_gap_report(report, run_id="run_tamper")
        self.assertEqual(context.exception.error_code, "requirement_gap_report_invalid")

    def test_artifact_round_trip_validates_the_report(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            store.ensure_layout()
            report = build_requirement_gap_report(run_id="run_round_trip", model=self.model())

            artifact_ref = write_requirement_gap_report(
                store=store,
                run_id="run_round_trip",
                report=report,
            )
            restored = read_requirement_gap_report(
                store=store,
                run_id="run_round_trip",
                artifact_ref=artifact_ref,
            )

        self.assertEqual(restored, report)


if __name__ == "__main__":
    unittest.main()
