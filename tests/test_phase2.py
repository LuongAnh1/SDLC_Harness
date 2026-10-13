"""Test Phase 2 V0 cho contract approve checkpoint -> design checkpoint."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.designer import DesignerAgent, parse_design_model
from sdlc_harness.agents.validator import ValidatorAgent, build_requirement_graph
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmClient, fake_design_model, fake_refined_requirement
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow


class StaticLlmClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.responses:
            return "[]"
        return self.responses.pop(0)


class PhaseTwoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock"},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_phase1_run(self) -> dict[str, object]:
        return PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

    def test_designer_fake_mode_creates_analysis_and_design_model(self) -> None:
        draft = DesignerAgent(LlmClient(self.settings)).draft(
            requirement_packet="# Requirement Packet V1\n\n## Goal\nDelete cultural heritage.",
            formal_use_case="UsecaseName: Delete Cultural Heritage",
        )

        self.assertIn("layered architecture", draft.analysis_report.lower())
        self.assertIn("## Class Diagram", draft.design_model)
        self.assertIn("## Sequence Diagrams", draft.design_model)
        self.assertIn("@startuml", draft.design_model)

    def test_parse_design_model_reads_class_and_sequence_graph(self) -> None:
        parsed = parse_design_model(fake_design_model())

        class_names = {node["className"] for node in parsed.class_graph["nodes"]}
        self.assertIn("DeleteCulturalHeritageBoundary", class_names)
        self.assertIn("DeleteCulturalHeritageService", class_names)
        self.assertGreaterEqual(len(parsed.class_graph["relationships"]), 2)

        sequence_diagram = parsed.sequence_graph["sequenceDiagrams"][0]
        messages = [message["message"] for message in sequence_diagram["messages"]]
        self.assertIn("requestDeletion(heritageId)", messages)
        self.assertIn("showResult(success message)", messages)

    def test_parse_design_model_rejects_missing_sequence_diagram(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            parse_design_model(
                """## Class Diagram
@startuml
class OnlyClass {
  +run() : void
}
@enduml"""
            )

        self.assertEqual(ctx.exception.error_code, "design_parser_failed")

    def test_build_requirement_graph_creates_stable_requirement_ids(self) -> None:
        graph = build_requirement_graph(fake_refined_requirement(""))

        ids = [item["id"] for item in graph["requirements"]]
        self.assertIn("REQ-PRE-001", ids)
        self.assertIn("REQ-MAIN-001", ids)
        self.assertIn("REQ-AC-001", ids)
        self.assertIn("REQ-NFR-001", ids)
        self.assertEqual(graph["count"], len(graph["requirements"]))

    def test_validator_fake_mode_creates_traceability_without_missing_links(self) -> None:
        parsed = parse_design_model(fake_design_model())
        result = ValidatorAgent(LlmClient(self.settings)).construct_traceability(
            requirement_packet=fake_refined_requirement(""),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.missing_links["class_diagram"], [])
        self.assertEqual(result.missing_links["sequence_diagram"], [])
        self.assertEqual(len(result.class_trace), result.requirement_graph["count"])
        self.assertEqual(len(result.sequence_trace), result.requirement_graph["count"])

    def test_validator_rejects_invalid_traceability_json(self) -> None:
        parsed = parse_design_model(fake_design_model())

        with self.assertRaises(HarnessError) as ctx:
            ValidatorAgent(StaticLlmClient(["not json"])).construct_traceability(
                requirement_packet=fake_refined_requirement(""),
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )

        self.assertEqual(ctx.exception.error_code, "validator_traceability_failed")

    def test_validator_rejects_traceability_targets_not_in_graph(self) -> None:
        parsed = parse_design_model(fake_design_model())
        first_requirement = build_requirement_graph(fake_refined_requirement(""))["requirements"][0]
        bad_trace = json.dumps(
            [
                {
                    "requirement": first_requirement["id"],
                    "content": first_requirement["content"],
                    "links": [{"target": "GhostClass.missingMethod", "reason": "bad"}],
                }
            ]
        )

        with self.assertRaises(HarnessError) as ctx:
            ValidatorAgent(StaticLlmClient([bad_trace])).construct_traceability(
                requirement_packet=fake_refined_requirement(""),
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )

        self.assertEqual(ctx.exception.error_code, "validator_traceability_failed")

    def test_validator_reports_missing_links_when_trace_links_are_empty(self) -> None:
        parsed = parse_design_model(fake_design_model())
        graph = build_requirement_graph(fake_refined_requirement(""))
        empty_trace = json.dumps(
            [
                {"requirement": item["id"], "content": item["content"], "links": []}
                for item in graph["requirements"]
            ]
        )

        result = ValidatorAgent(StaticLlmClient([empty_trace, empty_trace])).construct_traceability(
            requirement_packet=fake_refined_requirement(""),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
        )

        self.assertFalse(result.ok)
        self.assertEqual(len(result.missing_links["class_diagram"]), graph["count"])
        self.assertEqual(len(result.missing_links["sequence_diagram"]), graph["count"])

    def test_approve_checkpoint_creates_design_checkpoint(self) -> None:
        created = self._create_phase1_run()
        response = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertTrue(response["ok"])
        self.assertFalse(response["created_new_run"])
        self.assertEqual(response["current_phase"], "design_review")
        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertEqual(response["pending_checkpoint"]["type"], "design_approval")
        self.assertEqual(response["last_sync"]["phase"], "design_checkpoint")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertIn("design_artifact", response["latest_artifacts"])
        self.assertIn("requirement_graph", response["latest_artifacts"])
        self.assertIn("requirement_design_trace", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["design_traceability"]["ok"])
        self.assertEqual(response["quality_reports"]["design_traceability"]["mode"], "traceability_v1")
        self.assertIn("không phát hiện missing link", response["quality_reports"]["design_traceability"]["summary"])

        run_dir = self.runtime_root / "runs" / str(created["run_id"])
        self.assertTrue((run_dir / "artifacts" / "designer_analysis_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "design_artifact_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "class_diagram_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_design_trace_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "missing_design_links_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "design_quality_report_v1.md").exists())
        self.assertTrue((run_dir / "sync" / "design-jira-comment.md").exists())
        self.assertTrue((run_dir / "sync" / "design-confluence-page.md").exists())
        class_graph = json.loads((run_dir / "artifacts" / "class_diagram_graph_v1.json").read_text(encoding="utf-8"))
        class_names = {node["className"] for node in class_graph["nodes"]}
        self.assertIn("DeleteCulturalHeritageBoundary", class_names)
        sequence_graph = json.loads((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").read_text(encoding="utf-8"))
        first_sequence = sequence_graph["sequenceDiagrams"][0]
        self.assertTrue(
            any(message["message"] == "requestDeletion(heritageId)" for message in first_sequence["messages"])
        )
        trace = json.loads((run_dir / "artifacts" / "requirement_design_trace_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["mode"], "traceability_v1")
        self.assertIn("class_trace", trace)
        self.assertIn("sequence_trace", trace)
        missing = json.loads((run_dir / "artifacts" / "missing_design_links_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(missing["mode"], "traceability_v1")
        self.assertEqual(missing["missing_count"], 0)
        self.assertIn("class_diagram", missing["missing_links"])
        report = json.loads((run_dir / "sync" / "tool_sync_report_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "tool_sync_report_v2")
        self.assertEqual(report["phase"], "design_checkpoint")
        self.assertEqual(report["checkpoint"]["type"], "design_approval")

    def test_approve_checkpoint_rejects_wrong_checkpoint_id(self) -> None:
        created = self._create_phase1_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(
                run_id=str(created["run_id"]),
                checkpoint_id="checkpoint_requirement_wrong",
            )

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_approve_checkpoint_rejects_missing_pending_checkpoint(self) -> None:
        created = self._create_phase1_run()
        phase1 = PhaseOneWorkflow(self.settings)
        state = phase1.store.read_state(str(created["run_id"]))
        state["pending_checkpoint"] = None
        phase1.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "no_pending_checkpoint")

    def test_approve_checkpoint_rejects_non_requirement_status(self) -> None:
        created = self._create_phase1_run()
        phase2 = PhaseTwoWorkflow(self.settings)
        phase2.approve_checkpoint(run_id=str(created["run_id"]))

        with self.assertRaises(HarnessError) as ctx:
            phase2.approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_approve_checkpoint_rejects_missing_requirement_artifact(self) -> None:
        created = self._create_phase1_run()
        phase1 = PhaseOneWorkflow(self.settings)
        state = phase1.store.read_state(str(created["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("requirement_packet")
        state["latest_artifacts"] = latest
        phase1.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "approved_requirement_not_found")

    def test_cli_approve_checkpoint_returns_json(self) -> None:
        created = self._create_phase1_run()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "approve-checkpoint",
                    "--run-id",
                    str(created["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "waiting_design_approval")
        self.assertEqual(payload["last_sync"]["phase"], "design_checkpoint")


if __name__ == "__main__":
    unittest.main()
