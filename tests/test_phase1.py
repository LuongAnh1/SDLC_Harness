"""Test Phase 1 cho vertical slice use case -> requirement checkpoint."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.cli import main as cli_main
from sdlc_harness.core.config import load_settings, repo_root
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.sources.fixtures import load_use_case
from sdlc_harness.llm.client import LlmClient, fake_formal_use_case
from sdlc_harness.agents.requirements import FormalUseCaseValidator, RequirementRefiner, RequirementValidator
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


class PhaseOneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_LLM_MODE": "fake"},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repo_root_points_to_project_root(self) -> None:
        self.assertEqual(repo_root(), self.repo_root)
        self.assertTrue((repo_root() / "fixtures" / "etour" / "UC1.txt").exists())

    def test_fixture_loader_reads_etour_uc1(self) -> None:
        source = load_use_case(dataset="eTour", use_case="UC1", repo_root=self.repo_root)
        self.assertEqual(source.source_ref, "dataset_use_case:eTour:UC1")
        self.assertIn("DeleteCulturalHeritage", source.text)

    def test_fixture_loader_rejects_unsupported_dataset(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            load_use_case(dataset="unknown", use_case="UC1", repo_root=self.repo_root)
        self.assertEqual(ctx.exception.error_code, "source_not_supported")

    def test_fixture_loader_returns_source_not_found(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            load_use_case(dataset="eTour", use_case="missing", repo_root=self.repo_root)
        self.assertEqual(ctx.exception.error_code, "source_not_found")

    def test_requirement_prompt_templates_exist(self) -> None:
        prompt_root = self.repo_root / "sdlc_harness" / "agents" / "requirements" / "prompts"
        packet_prompt = (prompt_root / "requirement_packet_v1.md").read_text(encoding="utf-8")
        formal_prompt = (prompt_root / "formal_use_case_v1.txt").read_text(encoding="utf-8")
        repair_prompt = (prompt_root / "formal_use_case_repair_v1.txt").read_text(encoding="utf-8")

        self.assertIn("## Source Evidence", packet_prompt)
        self.assertIn("{formal_use_case}", packet_prompt)
        self.assertIn("Formal Syntactic Constraint", formal_prompt)
        self.assertIn("{OriginalUseCase}", formal_prompt)
        self.assertIn("{FormalCheckResult}", repair_prompt)
        self.assertIn("{ReconstructedUseCase}", repair_prompt)

    def test_trace_dev_role_prompts_are_available(self) -> None:
        prompt_paths = [
            self.repo_root / "sdlc_harness" / "agents" / "designer" / "prompts" / "designer.txt",
            self.repo_root / "sdlc_harness" / "agents" / "validator" / "prompts" / "validator.txt",
            self.repo_root / "sdlc_harness" / "agents" / "developer" / "prompts" / "developer.txt",
            self.repo_root / "sdlc_harness" / "agents" / "tester" / "prompts" / "tester.txt",
        ]
        for path in prompt_paths:
            self.assertTrue(path.exists(), str(path))
            self.assertGreater(len(path.read_text(encoding="utf-8")), 100)

    def test_formal_use_case_validator_passes_fake_formal_output(self) -> None:
        report = FormalUseCaseValidator().require_valid(fake_formal_use_case())
        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.flow_step_count, 1)

    def test_formal_use_case_validator_rejects_missing_grammar(self) -> None:
        report = FormalUseCaseValidator().validate("UsecaseName: Broken\nFlow of Events:\n1. Do something")
        self.assertFalse(report.ok)
        self.assertIn("Description:", " ".join(report.issues))

    def test_fake_refiner_output_passes_validators(self) -> None:
        source = load_use_case(dataset="eTour", use_case="UC1", repo_root=self.repo_root)
        refiner = RequirementRefiner(LlmClient(self.settings))
        formal_result = refiner.refine_formal_use_case(source)
        packet = refiner.refine(source, formal_use_case=formal_result.final_use_case)
        report = RequirementValidator().require_valid(packet)

        self.assertTrue(formal_result.final_report.ok)
        self.assertFalse(formal_result.repaired)
        self.assertTrue(report.ok)
        self.assertIn("## Main Flow", packet)

    def test_start_or_get_run_creates_runtime_files(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        response = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertTrue(response["ok"])
        self.assertTrue(response["created_new_run"])
        self.assertEqual(response["current_phase"], "waiting_requirement_approval")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertIn("formal_use_case", response["latest_artifacts"])
        self.assertIn("formal_check_report", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["formal_use_case"]["ok"])

        run_id = str(response["run_id"])
        run_dir = self.runtime_root / "runs" / run_id
        self.assertTrue((run_dir / "state.json").exists())
        self.assertTrue((run_dir / "artifacts" / "use_case_packet_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "formal_use_case_v1.txt").exists())
        self.assertTrue((run_dir / "artifacts" / "formal_check_report_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_packet_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "trace_graph_v1.json").exists())
        self.assertTrue((run_dir / "sync" / "tool_sync_report_v1.json").exists())

    def test_start_or_get_run_is_idempotent_for_active_source_ref(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        first = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        second = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertTrue(first["created_new_run"])
        self.assertFalse(second["created_new_run"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_get_status_reads_existing_run(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        status = workflow.get_status(run_id=str(created["run_id"]))

        self.assertEqual(status["run_id"], created["run_id"])
        self.assertEqual(status["pending_checkpoint"]["type"], "requirement_approval")
        self.assertIn("summary_for_user", status)
        self.assertIn("quality_reports", status)

    def test_get_status_returns_run_not_found(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        with self.assertRaises(HarnessError) as ctx:
            workflow.get_status(run_id="run_missing")
        self.assertEqual(ctx.exception.error_code, "run_not_found")

    def test_cli_error_is_json(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main([
                "get-status",
                "--run-id",
                "run_missing",
                "--runtime-root",
                str(self.runtime_root),
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "run_not_found")


if __name__ == "__main__":
    unittest.main()
