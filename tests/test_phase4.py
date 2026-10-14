"""Test Phase 4 slice 4.1: implementation plan review -> DeveloperWorkPacket."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.developer import DeveloperActResult
from sdlc_harness.agents.validator import CodeTraceValidator
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.verification import VerificationRunner
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow


class OutOfScopeDeveloper:
    def act(self, *, work_packet: dict[str, object]) -> DeveloperActResult:
        return DeveloperActResult(
            summary="bad output",
            files=[
                {
                    "path": "src/main/java/com/sdlc/etour/heritage/Unexpected.java",
                    "operation": "update",
                    "content": "not allowed\n",
                    "reason": "outside scope",
                    "covered_design_elements": [],
                }
            ],
        )


class OutOfScopeRepairDeveloper:
    def repair(
        self,
        *,
        work_packet: dict[str, object],
        repair_feedback: dict[str, object],
        repair_iteration: int,
    ) -> DeveloperActResult:
        return DeveloperActResult(
            summary="bad repair output",
            files=[
                {
                    "path": "src/main/java/com/sdlc/etour/heritage/UnexpectedRepair.java",
                    "operation": "update",
                    "content": "not allowed\n",
                    "reason": "outside repair scope",
                    "covered_design_elements": [],
                }
            ],
        )


class CapturingOutOfScopeRepairDeveloper(OutOfScopeRepairDeveloper):
    def __init__(self) -> None:
        self.work_packet: dict[str, object] | None = None

    def repair(
        self,
        *,
        work_packet: dict[str, object],
        repair_feedback: dict[str, object],
        repair_iteration: int,
    ) -> DeveloperActResult:
        self.work_packet = work_packet
        result = super().repair(
            work_packet=work_packet,
            repair_feedback=repair_feedback,
            repair_iteration=repair_iteration,
        )
        return DeveloperActResult(
            summary=result.summary,
            files=result.files,
            raw_response='{"summary":"raw repair response"}',
        )


class RejectingCodeTraceLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "ok": False,
                "confidence": 0.87,
                "summary": "Semantic graph review found missing delete confirmation behavior.",
                "graph_edges": [],
                "missing_links": [
                    {
                        "type": "missing_behavior",
                        "message": "Delete confirmation is not convincingly covered by code/test evidence.",
                        "requirement_ref": "approved_requirement_packet",
                        "design_element_id": "sequence:delete_cultural_heritage_main",
                    }
                ],
                "risks": ["Code file exists but behavior evidence is weak."],
            }
        )


class TraceMappingIssueCodeTraceLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "ok": False,
                "confidence": 0.9,
                "summary": "Trace graph mapping is weak; code repair should not run.",
                "graph_edges": [],
                "missing_links": [
                    {
                        "type": "trace_mapping_issue",
                        "message": "Sequence boolean response is not a code symbol.",
                        "design_element_id": "sequence:sequence_diagram_1:16",
                    }
                ],
                "risks": ["Validator evidence/mapping issue."],
            }
        )


class CapturingCodeTraceLlm:
    def __init__(self) -> None:
        self.prompt = ""

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompt = prompt
        return json.dumps(
            {
                "ok": True,
                "confidence": 0.93,
                "summary": "Semantic review accepted code snippets.",
                "graph_edges": [],
                "missing_links": [],
                "risks": [],
            }
        )


class PhaseFourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.remote_url = "https://github.com/example/SDLC_Target_eTour.git"
        self.target_repo = self._init_target_repo(remote_url=self.remote_url)
        self.settings = self._settings()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _settings(self, **extra_overrides: str):
        overrides = {
            "SDLC_LLM_MODE": "fake",
            "SDLC_SYNC_MODE": "mock",
            "SDLC_DESIGN_REPAIR_LIMIT": "2",
            "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT": "2",
            "SDLC_IMPLEMENTATION_REPAIR_LIMIT": "2",
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            **extra_overrides,
        }
        return load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides=overrides,
        )

    def _init_target_repo(self, *, remote_url: str) -> Path:
        target = Path(self.temp_dir.name) / "target_repo"
        source_dir = target / "src" / "main" / "java" / "com" / "sdlc" / "etour" / "heritage"
        test_dir = target / "src" / "test" / "java" / "com" / "sdlc" / "etour" / "heritage"
        source_dir.mkdir(parents=True)
        test_dir.mkdir(parents=True)
        (source_dir / "CulturalHeritage.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class CulturalHeritage {}\n",
            encoding="utf-8",
        )
        (source_dir / "DeleteCulturalHeritageService.java").write_text(
            (
                "package com.sdlc.etour.heritage;\n"
                "public final class DeleteCulturalHeritageService {\n"
                "  public void deleteHeritage(String id) {}\n"
                "}\n"
            ),
            encoding="utf-8",
        )
        (test_dir / "CulturalHeritageTest.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class CulturalHeritageTest {}\n",
            encoding="utf-8",
        )
        (target / "scripts").mkdir()
        (target / "scripts" / "test.ps1").write_text("Write-Host 'test placeholder'\n", encoding="utf-8")
        (target / ".codegraph").mkdir()
        (target / ".codegraph" / "README.md").write_text("test codegraph index placeholder\n", encoding="utf-8")
        self._git(target, "init")
        self._git(target, "add", ".")
        self._git(target, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")
        self._git(target, "branch", "-M", "main")
        self._git(target, "remote", "add", "origin", remote_url)
        return target

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8")

    def _git_output(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def _create_phase3_run(self) -> dict[str, object]:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        phase2 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        return CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase2["run_id"]))

    def _create_phase4_ready_run(self) -> dict[str, object]:
        phase3 = self._create_phase3_run()
        return CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

    def _create_phase4_written_run(self) -> dict[str, object]:
        ready = self._create_phase4_ready_run()
        return PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

    def _create_phase4_tested_run(self) -> dict[str, object]:
        written = self._create_phase4_written_run()
        return PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

    def test_approve_implementation_plan_creates_developer_work_packet(self) -> None:
        phase3 = self._create_phase3_run()

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_preparing")
        self.assertEqual(response["status"], "implementation_ready")
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("developer_work_packet", response["latest_artifacts"])
        self.assertIn("chưa tạo branch", response["summary_for_user"])
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), "")

        packet_path = self.repo_root / str(response["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["version"], "developer_work_packet_v1")
        self.assertEqual(packet["run_id"], phase3["run_id"])
        self.assertEqual(packet["approved_plan"]["implementation_plan"], phase3["latest_artifacts"]["implementation_plan"])
        self.assertTrue(packet["implementation_steps"])
        self.assertTrue(packet["proposed_files"])
        self.assertTrue(packet["current_files"])
        self.assertEqual(packet["output_contract"]["format"], "changed_files_json_v1")
        self.assertTrue(packet["output_contract"]["changed_files_only"])
        self.assertTrue(packet["output_contract"]["allowed_paths"])
        grouped = packet["design_mappings_by_file"]
        self.assertTrue(any(path.startswith("src/main/java/") for path in grouped))
        self.assertTrue(any(item["exists"] for item in packet["proposed_files"]))

    def test_cli_approve_checkpoint_dispatches_implementation_plan_to_phase4(self) -> None:
        phase3 = self._create_phase3_run()
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "approve-checkpoint",
                    "--run-id",
                    str(phase3["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "implementation_ready")
        self.assertIn("developer_work_packet", payload["latest_artifacts"])

    def test_phase4_rejects_non_implementation_plan_checkpoint(self) -> None:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_missing_structured_plan_is_rejected(self) -> None:
        phase3 = self._create_phase3_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(phase3["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("implementation_plan_structured")
        state["latest_artifacts"] = latest
        store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "approved_implementation_plan_not_found")

    def test_target_repo_dirty_blocks_without_writing_packet(self) -> None:
        phase3 = self._create_phase3_run()
        (self.target_repo / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "target_repo_dirty")
        self.assertNotIn("developer_work_packet", response["latest_artifacts"])

    def test_continue_run_applies_developer_act_code_changes(self) -> None:
        ready = self._create_phase4_ready_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_writing")
        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("code_change_report", response["latest_artifacts"])
        self.assertEqual(
            self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "feature/etour-uc1-implementation",
        )
        status = self._git_output(self.target_repo, "status", "--porcelain")
        self.assertIn("DeleteCulturalHeritageService.java", status)
        self.assertIn("DeleteCulturalHeritageFlowTest.java", status)

        service_path = (
            self.target_repo
            / "src"
            / "main"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageService.java"
        )
        self.assertIn("isDeleted", service_path.read_text(encoding="utf-8"))
        report_path = self.repo_root / str(response["latest_artifacts"]["code_change_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("Code Change Report V1", report)
        self.assertIn("Tests: not run in Slice 4.2", report)

    def test_cli_continue_run_dispatches_phase4_developer_act(self) -> None:
        ready = self._create_phase4_ready_run()
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "continue-run",
                    "--run-id",
                    str(ready["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "implementation_written")
        self.assertIn("code_change_report", payload["latest_artifacts"])

    def test_continue_run_runs_verification_after_implementation_written(self) -> None:
        written = self._create_phase4_written_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_tested")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("test_report", response["latest_artifacts"])
        report_path = self.repo_root / str(response["latest_artifacts"]["test_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("Test Report V1", report)
        self.assertIn("Status: `passed`", report)
        self.assertIn("test placeholder", report)

    def test_continue_run_runs_code_trace_after_implementation_tested(self) -> None:
        tested = self._create_phase4_tested_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["implementation_traceability"]["ok"])
        trace_path = self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(trace["version"], "requirement_design_code_trace_v1")
        self.assertTrue(trace["ok"])
        self.assertTrue(trace["mappings"])
        self.assertFalse(trace["missing_links"])
        self.assertGreater(trace["stats"]["test_file_count"], 0)
        self.assertEqual(trace["mode"], "hybrid_rule_llm_code_trace_v1")
        self.assertTrue(trace["llm_required"])
        self.assertTrue(trace["llm_review"]["ok"])
        self.assertTrue(trace["llm_review"]["review_context"]["code_snippets_included"])

    def test_code_trace_llm_receives_source_and_test_snippets(self) -> None:
        tested = self._create_phase4_tested_run()
        llm = CapturingCodeTraceLlm()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(llm)

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["status"], "implementation_traced")
        self.assertIn('"code_snippets"', llm.prompt)
        self.assertIn("DeleteCulturalHeritageService", llm.prompt)
        self.assertIn("DeleteCulturalHeritageFlowTest", llm.prompt)
        self.assertIn("deleteHeritage", llm.prompt)
        payload = json.loads(llm.prompt.split("\n\n", 1)[1])
        snippets = {item["path"]: item for item in payload["code_snippets"]}
        flow_test = next(path for path in snippets if path.endswith("DeleteCulturalHeritageFlowTest.java"))
        self.assertFalse(snippets[flow_test]["content_truncated"])
        self.assertEqual(snippets[flow_test]["chars_sent"], snippets[flow_test]["chars_total"])
        self.assertTrue(any(item["content_truncated"] is False for item in payload["code_snippets"]))

    def test_code_trace_normalizes_boolean_and_notification_sequence_mappings(self) -> None:
        source_dir = self.target_repo / "src" / "main" / "java" / "com" / "sdlc" / "etour" / "heritage"
        test_dir = self.target_repo / "src" / "test" / "java" / "com" / "sdlc" / "etour" / "heritage"
        controller_path = "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageController.java"
        connection_path = "src/main/java/com/sdlc/etour/heritage/ETOURConnectionService.java"
        notification_path = "src/main/java/com/sdlc/etour/heritage/NotificationService.java"
        test_path = "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java"
        (source_dir / "DeleteCulturalHeritageController.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class DeleteCulturalHeritageController { void confirmDeletion() {} }\n",
            encoding="utf-8",
        )
        (source_dir / "ETOURConnectionService.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class ETOURConnectionService {}\n",
            encoding="utf-8",
        )
        (source_dir / "NotificationService.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class NotificationService { void notifyError(String message) {} }\n",
            encoding="utf-8",
        )
        (test_dir / "DeleteCulturalHeritageFlowTest.java").write_text(
            "package com.sdlc.etour.heritage;\npublic final class DeleteCulturalHeritageFlowTest {}\n",
            encoding="utf-8",
        )
        work_packet = {
            "run_id": "test-run",
            "issue_key": "SDLC-1",
            "design_mappings_by_file": {
                controller_path: [
                    {
                        "design_element_id": "sequence:sequence_diagram_1:16",
                        "design_element_type": "sequence_message",
                        "design_element_name": "EC->DC: true",
                        "planned_code_target": f"{controller_path}#true",
                    }
                ],
                connection_path: [
                    {
                        "design_element_id": "sequence:sequence_diagram_1:22",
                        "design_element_type": "sequence_message",
                        "design_element_name": 'DC->NS: notifyError("Connection to ETOUR server interrupted")',
                        "planned_code_target": f"{connection_path}#notifyError",
                    }
                ],
            },
            "proposed_files": [{"path": test_path}],
        }
        llm = CapturingCodeTraceLlm()

        result = CodeTraceValidator(llm).validate(target_path=self.target_repo, work_packet=work_packet)

        self.assertTrue(result.ok)
        payload = json.loads(llm.prompt.split("\n\n", 1)[1])
        boolean_mapping = next(
            item for item in payload["static_trace_mappings"] if item["design_element_id"] == "sequence:sequence_diagram_1:16"
        )
        notification_mapping = next(
            item for item in payload["static_trace_mappings"] if item["design_element_id"] == "sequence:sequence_diagram_1:22"
        )
        self.assertEqual(boolean_mapping["trace_policy"], "boolean_response")
        self.assertFalse(boolean_mapping["requires_direct_symbol"])
        self.assertEqual(boolean_mapping["symbol"], "")
        self.assertEqual(notification_mapping["trace_policy"], "semantic_side_effect")
        self.assertEqual(notification_mapping["source_file"], notification_path)
        self.assertEqual(notification_mapping["symbol"], "notifyError")
        self.assertTrue(notification_mapping["symbol_found"])

    def test_cli_continue_run_dispatches_phase4_code_trace(self) -> None:
        tested = self._create_phase4_tested_run()
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "continue-run",
                    "--run-id",
                    str(tested["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "implementation_traced")
        self.assertIn("requirement_design_code_trace", payload["latest_artifacts"])

    def test_cli_continue_run_dispatches_phase4_verification(self) -> None:
        written = self._create_phase4_written_run()
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "continue-run",
                    "--run-id",
                    str(written["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "implementation_tested")
        self.assertIn("test_report", payload["latest_artifacts"])

    def test_continue_run_marks_repair_needed_when_code_trace_missing_test_file(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self.target_repo
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        test_file.unlink()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["implementation_repair"]["needed"])
        trace = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(trace["ok"])
        self.assertTrue(any(item["type"] == "missing_test_file" for item in trace["missing_links"]))
        feedback = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["implementation_repair_feedback"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(feedback["version"], "implementation_repair_feedback_v1")
        self.assertEqual(feedback["status"], "implementation_repair_needed")
        self.assertEqual(feedback["repair_iteration"], 0)
        self.assertEqual(feedback["repair_scope"], "code_or_test")
        self.assertEqual(feedback["trace_artifact"], response["latest_artifacts"]["requirement_design_code_trace"])
        self.assertTrue(any(item["type"] == "missing_test_file" for item in feedback["missing_links"]))

    def test_continue_run_marks_repair_needed_when_llm_code_trace_finds_semantic_gap(self) -> None:
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        trace = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(trace["ok"])
        self.assertTrue(trace["llm_required"])
        self.assertFalse(trace["llm_review"]["ok"])
        self.assertTrue(any(item["type"] == "llm_semantic_gap" for item in trace["missing_links"]))
        feedback = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["implementation_repair_feedback"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(feedback["llm_review"]["summary"], "Semantic graph review found missing delete confirmation behavior.")
        self.assertTrue(feedback["developer_constraints"]["allowed_paths"])

    def test_continue_run_from_repair_needed_repairs_code_then_retraces(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self.target_repo
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["quality_reports"]["implementation_repair"]["needed"], False)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["repair_iterations"], 1)
        self.assertTrue(test_file.exists())
        self.assertIn("developer_work_packet_v2.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("code_change_report_v2.md", response["latest_artifacts"]["code_change_report"])
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn(
            "requirement_design_code_trace_v2.json",
            response["latest_artifacts"]["requirement_design_code_trace"],
        )
        trace = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["requirement_design_code_trace"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(trace["ok"])
        self.assertEqual(trace["repair_iteration"], 1)
        self.assertEqual(trace["repaired_from"], repair_needed["latest_artifacts"]["implementation_repair_feedback"])
        report = (self.repo_root / str(response["latest_artifacts"]["code_change_report"])).read_text(encoding="utf-8")
        self.assertIn("Developer work packet:", report)
        self.assertIn("developer_work_packet_v2.json", report)

    def test_repair_refreshes_developer_work_packet_from_current_target_repo(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self.target_repo
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        old_packet_path = self.repo_root / str(repair_needed["latest_artifacts"]["developer_work_packet"])
        old_packet = json.loads(old_packet_path.read_text(encoding="utf-8"))
        old_service = next(
            item
            for item in old_packet["current_files"]
            if item["path"].endswith("DeleteCulturalHeritageService.java")
        )
        self.assertTrue(old_service["exists"])
        self.assertNotIn("isDeleted", old_service["content"])

        workflow = PhaseFourWorkflow(self.settings)
        developer = CapturingOutOfScopeRepairDeveloper()
        workflow.developer = developer
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_act_out_of_scope")
        self.assertIsNotNone(developer.work_packet)
        refreshed_packet = developer.work_packet or {}
        self.assertEqual(refreshed_packet["version"], "developer_work_packet_v2")
        refreshed_service = next(
            item
            for item in refreshed_packet["current_files"]
            if item["path"].endswith("DeleteCulturalHeritageService.java")
        )
        self.assertTrue(refreshed_service["exists"])
        self.assertIn("isDeleted", refreshed_service["content"])
        refreshed_test = next(
            item
            for item in refreshed_packet["current_files"]
            if item["path"].endswith("DeleteCulturalHeritageFlowTest.java")
        )
        self.assertFalse(refreshed_test["exists"])
        self.assertIn("developer_work_packet_v2.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("developer_repair_raw_response_v2.txt", response["latest_artifacts"]["developer_repair_raw_response"])
        raw_path = self.repo_root / str(response["latest_artifacts"]["developer_repair_raw_response"])
        self.assertIn("raw repair response", raw_path.read_text(encoding="utf-8"))
        packet_path = self.repo_root / str(response["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["artifact_ref"], "developer_work_packet_v2")
        self.assertEqual(packet["version"], "developer_work_packet_v2")

    def test_continue_run_from_repair_needed_respects_zero_repair_limit(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="0")
        tested = self._create_phase4_tested_run()
        test_file = (
            self.target_repo
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertNotIn("code_change_report_v2.md", str(response["latest_artifacts"].get("code_change_report", "")))

    def test_continue_run_from_repair_needed_blocks_out_of_scope_repair_output(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self.target_repo
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.developer = OutOfScopeRepairDeveloper()

        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_act_out_of_scope")
        self.assertFalse(
            (
                self.target_repo
                / "src"
                / "main"
                / "java"
                / "com"
                / "sdlc"
                / "etour"
                / "heritage"
                / "UnexpectedRepair.java"
            ).exists()
        )

    def test_continue_run_from_repair_needed_blocks_when_repair_verification_fails(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self.target_repo
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        test_file.unlink()
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))
        (self.target_repo / "scripts" / "test.ps1").write_text(
            "Write-Error 'repair unit failed'\nexit 1\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_failed")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `failed`", report)
        self.assertIn("repair unit failed", report)

    def test_continue_run_from_repair_needed_blocks_when_trace_still_fails_at_limit(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["repair_iterations"], 1)
        self.assertTrue(response["quality_reports"]["implementation_repair"]["limit_exceeded"])
        self.assertIn("requirement_design_code_trace_v2.json", response["latest_artifacts"]["requirement_design_code_trace"])

    def test_revalidate_blocked_limit_reopens_repair_when_new_limit_allows(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertEqual(blocked["implementation_repair_iterations"], 1)

        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="2")
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        response = workflow.revalidate_implementation(run_id=str(blocked["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("developer_work_packet_v3.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("test_report_v3.md", response["latest_artifacts"]["test_report"])
        self.assertIn(
            "requirement_design_code_trace_v3.json",
            response["latest_artifacts"]["requirement_design_code_trace"],
        )
        self.assertIn(
            "implementation_repair_feedback_v3.json",
            response["latest_artifacts"]["implementation_repair_feedback"],
        )
        self.assertTrue(response["quality_reports"]["implementation_repair"]["revalidated"])
        self.assertFalse(response["quality_reports"]["implementation_repair"].get("limit_exceeded", False))

    def test_revalidate_blocked_limit_can_pass_without_more_repair(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="2")
        response = PhaseFourWorkflow(self.settings).revalidate_implementation(run_id=str(blocked["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["quality_reports"]["implementation_repair"]["needed"], False)
        self.assertTrue(response["quality_reports"]["implementation_repair"]["revalidated"])
        self.assertIn("developer_work_packet_v3.json", response["latest_artifacts"]["developer_work_packet"])
        self.assertIn("requirement_design_code_trace_v3.json", response["latest_artifacts"]["requirement_design_code_trace"])

    def test_revalidate_implementation_rejects_non_limit_blocked_state(self) -> None:
        written = self._create_phase4_written_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(written["run_id"]))
        state.update(
            {
                "status": "blocked",
                "current_phase": "implementation_testing",
                "last_error": HarnessError("test_failed", "unit failed", run_id=str(written["run_id"])).to_response(),
            }
        )
        store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).revalidate_implementation(run_id=str(written["run_id"]))

        self.assertEqual(ctx.exception.error_code, "implementation_revalidate_not_allowed")

    def test_cli_revalidate_implementation_dispatches_phase4(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        repair_needed = workflow.continue_run(run_id=str(tested["run_id"]))
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(RejectingCodeTraceLlm())
        blocked = workflow.continue_run(run_id=str(repair_needed["run_id"]))
        stdout = io.StringIO()
        env = {
            "SDLC_TARGET_REPO_PATH": str(self.target_repo),
            "SDLC_TARGET_REPO_REMOTE": self.remote_url,
            "SDLC_SYNC_MODE": "mock",
        }

        with patch.dict("os.environ", env), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "revalidate-implementation",
                    "--run-id",
                    str(blocked["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--implementation-repair-limit",
                    "2",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "implementation_traced")
        self.assertEqual(payload["implementation_repair_iterations"], 1)
        self.assertIn("requirement_design_code_trace_v3.json", payload["latest_artifacts"]["requirement_design_code_trace"])

    def test_continue_run_blocks_when_code_trace_failure_is_not_repairable(self) -> None:
        tested = self._create_phase4_tested_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(tested["run_id"]))
        packet_path = self.repo_root / str(state["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["design_mappings_by_file"] = {}
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_traceability_failed")
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertNotIn("implementation_repair_feedback", response["latest_artifacts"])

    def test_continue_run_blocks_mapping_issue_without_developer_repair(self) -> None:
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(TraceMappingIssueCodeTraceLlm())

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "validator_evidence_or_mapping_issue")
        self.assertIn("requirement_design_code_trace", response["latest_artifacts"])
        self.assertNotIn("implementation_repair_feedback", response["latest_artifacts"])

    def test_continue_run_blocks_when_code_trace_missing_test_report(self) -> None:
        tested = self._create_phase4_tested_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(tested["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("test_report")
        state["latest_artifacts"] = latest
        store.write_state(state)

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "approved_implementation_plan_not_found")

    def test_continue_run_blocks_when_verification_fails(self) -> None:
        written = self._create_phase4_written_run()
        (self.target_repo / "scripts" / "test.ps1").write_text(
            "Write-Error 'unit failed'\nexit 1\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_failed")
        self.assertIn("test_report", response["latest_artifacts"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `failed`", report)
        self.assertIn("unit failed", report)

    def test_continue_run_blocks_when_verification_is_skipped(self) -> None:
        written = self._create_phase4_written_run()
        (self.target_repo / "scripts" / "test.ps1").write_text(
            "Write-Host \"JDK compiler 'javac' was not found, so compile/runtime tests were skipped.\"\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_skipped")
        self.assertIn("test_report", response["latest_artifacts"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `skipped`", report)

    def test_verification_runner_adds_sdlc_java_home_to_path(self) -> None:
        fake_jdk = Path(self.temp_dir.name) / "fake-jdk"
        java_bin = fake_jdk / "bin"
        java_bin.mkdir(parents=True)
        javac = java_bin / ("javac.exe" if os.name == "nt" else "javac")
        javac.write_text("", encoding="utf-8")
        javac.chmod(0o755)
        (self.target_repo / "scripts" / "test.ps1").write_text(
            (
                "if (-not (Get-Command javac -ErrorAction SilentlyContinue)) {\n"
                "  Write-Host \"JDK compiler 'javac' was not found, so compile/runtime tests were skipped.\"\n"
                "  exit 0\n"
                "}\n"
                "Write-Host 'javac visible'\n"
            ),
            encoding="utf-8",
        )
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        work_packet = {
            "test_intents": [
                {"command": f'"{powershell}" -ExecutionPolicy Bypass -File scripts/test.ps1'},
            ],
        }

        with patch.dict(os.environ, {"SDLC_JAVA_HOME": str(fake_jdk), "PATH": ""}, clear=False):
            result = VerificationRunner(timeout_seconds=10).run(target_path=self.target_repo, work_packet=work_packet)

        self.assertEqual(result.status, "passed")
        self.assertIn("javac visible", result.stdout)

    def test_continue_run_rejects_non_ready_state(self) -> None:
        phase3 = self._create_phase3_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).continue_run(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "run_not_continuable")

    def test_continue_run_blocks_dirty_target_repo(self) -> None:
        ready = self._create_phase4_ready_run()
        (self.target_repo / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "target_repo_dirty")
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_continue_run_blocks_missing_developer_work_packet(self) -> None:
        ready = self._create_phase4_ready_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(ready["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("developer_work_packet")
        state["latest_artifacts"] = latest
        store.write_state(state)

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "approved_implementation_plan_not_found")
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_continue_run_blocks_out_of_scope_developer_output(self) -> None:
        ready = self._create_phase4_ready_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.developer = OutOfScopeDeveloper()

        response = workflow.continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "developer_act_out_of_scope")
        self.assertFalse(
            (
                self.target_repo
                / "src"
                / "main"
                / "java"
                / "com"
                / "sdlc"
                / "etour"
                / "heritage"
                / "Unexpected.java"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
