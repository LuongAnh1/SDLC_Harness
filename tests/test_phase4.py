"""Test Phase 4 slice 4.1: implementation plan review -> DeveloperWorkPacket."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.developer import DeveloperActResult
from sdlc_harness.agents.tester import QaGeneratedTestSuite, QaTesterAgent
from sdlc_harness.agents.validator import CodeTraceValidator
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.verification import VerificationResult, VerificationRunner
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase4 import (
    PhaseFourWorkflow,
    _apply_developer_changes,
    _code_review_pr_body,
    _verification_repair_feedback,
    _verification_attempt_count,
)


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
                "summary": "Semantic graph review found missing request-deletion coverage.",
                "graph_edges": [],
                "missing_links": [
                    {
                        "type": "missing_behavior",
                        "blocking": True,
                        "owner": "developer",
                        "recommended_action": "developer_repair",
                        "message": "Approved requestDeletion behavior is not covered by code/test evidence.",
                        "requirement_ref": "MF-1",
                        "design_element_id": "sequence:delete_cultural_heritage_main",
                        "evidence_ref": "approved_design:sequence",
                        "evidence_quote": "requestDeletion(heritageId)",
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
                        "blocking": True,
                        "owner": "validator",
                        "recommended_action": "validator_mapping_fix",
                        "message": "Sequence boolean response is not a code symbol.",
                        "design_element_id": "sequence:sequence_diagram_1:16",
                        "evidence_ref": "design_artifact:Sequence Diagrams",
                        "evidence_quote": "Service --> Boundary: success",
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


class ProseWrappedCodeTraceLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return (
            "Here is the review:\n"
            + json.dumps(
                {
                    "ok": True,
                    "confidence": 0.9,
                    "summary": "Semantic review accepted code snippets despite prose wrapper.",
                    "graph_edges": [],
                    "missing_links": [],
                    "risks": [],
                }
            )
        )


class FailingQaTester:
    def generate_tests(self, *, context: dict[str, object]) -> QaGeneratedTestSuite:
        path = "src/test/java/com/sdlc/etour/heritage/FailingQaAcceptanceTest.java"
        return QaGeneratedTestSuite(
            summary="QA generated a failing acceptance test for workflow state coverage.",
            test_files=[
                {
                    "path": path,
                    "content": "package com.sdlc.etour.heritage;\npublic final class FailingQaAcceptanceTest {}\n",
                    "scenarios": ["Delete confirmation"],
                }
            ],
            scenarios=[
                {
                    "name": "Delete confirmation",
                    "expected_result": "The runner should surface the QA acceptance failure.",
                    "test_file": path,
                    "risk": "User may delete without confirmation.",
                }
            ],
            risks=["Confirmation behavior is unclear."],
            generation_context={"section_count": 1, "chars_sent": 10, "chars_total": 10, "any_truncated": False},
        )


class RecordingQaTester:
    def __init__(self):
        self.context: dict[str, object] = {}

    def generate_tests(self, *, context: dict[str, object]) -> QaGeneratedTestSuite:
        self.context = context
        path = "src/test/java/com/sdlc/etour/heritage/RecordingQaAcceptanceTest.java"
        return QaGeneratedTestSuite(
            summary="Recording QA generated compact acceptance tests.",
            test_files=[
                {
                    "path": path,
                    "content": "package com.sdlc.etour.heritage;\npublic final class RecordingQaAcceptanceTest {}\n",
                    "scenarios": ["Compact QA packet"],
                }
            ],
            scenarios=[
                {
                    "name": "Compact QA packet",
                    "expected_result": "Packet includes accepted requirement, code, verification, and trace summary.",
                    "test_file": path,
                    "risk": "",
                }
            ],
            risks=[],
            generation_context=context.get("context_budget") if isinstance(context.get("context_budget"), dict) else {},
        )


class InvalidQaLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return '{"status": "pass", "summary": "cut off before scenarios"'


class MissingRequirementQaLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "summary": "incomplete coverage",
                "test_files": [
                    {
                        "path": "src/test/java/com/sdlc/etour/heritage/V2AcceptanceTest.java",
                        "content": "package com.sdlc.etour.heritage; public final class V2AcceptanceTest {}",
                        "scenarios": ["one"],
                    }
                ],
                "scenarios": [
                    {
                        "name": "one",
                        "expected_result": "one",
                        "test_file": "src/test/java/com/sdlc/etour/heritage/V2AcceptanceTest.java",
                        "risk": "",
                        "requirement_ids": ["REQ-1"],
                    }
                ],
                "risks": [],
            }
        )


class SequenceVerificationRunner:
    def __init__(self, statuses: list[str]):
        self.statuses = list(statuses)
        self.calls: list[str] = []

    def run(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files: list[str] | None = None,
    ) -> VerificationResult:
        status = self.statuses.pop(0) if self.statuses else "passed"
        self.calls.append(status)
        reason_by_status = {
            "passed": "Test command completed successfully.",
            "failed": "QA acceptance failed in fake runner.",
            "skipped": "QA acceptance was skipped in fake runner.",
        }
        return VerificationResult(
            status=status,
            command="fake verification runner",
            cwd=str(target_path),
            exit_code=0 if status in {"passed", "skipped"} else 1,
            duration_seconds=0.01,
            stdout="All tests passed" if status == "passed" else "",
            stderr="qa acceptance failed" if status == "failed" else "",
            reason=reason_by_status.get(status, "Unknown fake runner status."),
        )


class PhaseFourTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.remote_path = Path(self.temp_dir.name) / "target.git"
        self.remote_path.mkdir()
        self._git(self.remote_path, "init", "--bare")
        self.remote_url = str(self.remote_path.resolve())
        self.target_repo = self._init_target_repo(remote_url=self.remote_url)
        self.settings = self._settings()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_verification_feedback_preserves_validator_repair_objective(self) -> None:
        objective = {
            "objective_id": "repair:approved-contract",
            "source": "validator_code_trace",
            "status": "open",
            "canonical_keys": ["contract:EditFormView.showConfirmation"],
            "contract_findings": [
                {
                    "code_ref": "src/main/java/EditFormView.java#showConfirmation",
                    "expected_signature": {"name": "showConfirmation", "parameters": [], "return_type": "void"},
                }
            ],
        }
        result = VerificationResult(
            status="failed",
            command="javac",
            cwd=str(self.target_repo),
            exit_code=1,
            duration_seconds=0.01,
            stdout="",
            stderr="src/test/java/FlowTest.java:12: error: FakeView does not override showConfirmation()",
            reason="Compilation failed.",
        )

        feedback = _verification_repair_feedback(
            run_id="run_lineage",
            state={"source_ref": "jira_issue:SDLC-3"},
            work_packet={"issue_key": "SDLC-3", "output_contract": {"allowed_paths": ["src/main/java/EditFormView.java", "src/test/java/FlowTest.java"]}},
            result=result,
            report_path="test_report_v2.md",
            target_repo=str(self.target_repo),
            branch="run/run_lineage",
            feedback_version=2,
            repair_iteration=1,
            prior_feedback={
                "version": "implementation_repair_feedback_v1",
                "feedback_source": "validator_code_trace",
                "repair_objective": objective,
                "repair_lineage": {"root_feedback_artifact": "feedback_v1.json"},
            },
            prior_feedback_ref="feedback_v1.json",
        )

        self.assertEqual(feedback["repair_objective"], objective)
        self.assertEqual(feedback["repair_lineage"]["objective_id"], "repair:approved-contract")
        self.assertEqual(feedback["repair_lineage"]["root_feedback_artifact"], "feedback_v1.json")
        self.assertIn("Do not revert", feedback["required_action"])

    def test_verification_feedback_backfills_legacy_validator_objective_and_refreshes_diagnostic(self) -> None:
        old_diagnostic = "FlowTest.java:12: old compiler diagnostic"
        current_diagnostic = "Controller.java:52: current compiler diagnostic"
        result = VerificationResult(
            status="failed",
            command="javac",
            cwd=str(self.target_repo),
            exit_code=1,
            duration_seconds=0.01,
            stdout="",
            stderr=current_diagnostic,
            reason="Compilation failed.",
        )
        legacy_root = {
            "version": "implementation_repair_feedback_v21",
            "feedback_source": "validator_code_trace",
            "missing_links": [
                {
                    "canonical_key": "contract:EditFormView.showConfirmation",
                    "blocking": True,
                    "owner": "developer",
                    "code_ref": "src/main/java/EditFormView.java#showConfirmation",
                    "expected_signature": {"name": "showConfirmation", "parameters": [], "return_type": "void"},
                }
            ],
        }
        prior = {
            "version": "implementation_repair_feedback_v24",
            "feedback_source": "verification",
            "repair_objective": {
                "objective_id": "repair:stale-verification",
                "source": "verification",
                "diagnostic_anchor": {"stderr": old_diagnostic},
            },
            "repair_lineage": {"root_feedback_artifact": "feedback_v21.json"},
        }

        feedback = _verification_repair_feedback(
            run_id="run_legacy_lineage",
            state={"source_ref": "jira_issue:SDLC-3"},
            work_packet={
                "issue_key": "SDLC-3",
                "output_contract": {
                    "allowed_paths": ["src/main/java/EditFormView.java", "src/main/java/Controller.java"]
                },
            },
            result=result,
            report_path="test_report_v25.md",
            target_repo=str(self.target_repo),
            branch="run/run_legacy_lineage",
            feedback_version=25,
            repair_iteration=20,
            prior_feedback=prior,
            prior_feedback_ref="feedback_v24.json",
            root_feedback=legacy_root,
            root_feedback_ref="feedback_v21.json",
        )

        objective = feedback["repair_objective"]
        self.assertEqual(objective["source"], "validator_code_trace")
        self.assertEqual(objective["canonical_keys"], ["contract:EditFormView.showConfirmation"])
        self.assertNotIn("diagnostic_anchor", objective)
        self.assertEqual(feedback["verification"]["stderr"], current_diagnostic)
        self.assertNotIn(old_diagnostic, json.dumps(feedback, ensure_ascii=False))
        self.assertEqual(feedback["repair_lineage"]["root_feedback_artifact"], "feedback_v21.json")


    def test_verification_attempt_count_ignores_phase4_artifact_version_gaps(self) -> None:
        workflow = PhaseFourWorkflow(self.settings)
        artifact_dir = workflow.store.run_dir("run_attempt_count") / "artifacts"
        artifact_dir.mkdir(parents=True)
        for name in (
            "test_report_v1.md",
            "test_report_v5.md",
            "test_report_v6.md",
            "code_change_report_v12.md",
        ):
            (artifact_dir / name).write_text("report\n", encoding="utf-8")

        self.assertEqual(_verification_attempt_count(workflow.store, "run_attempt_count"), 3)

    def test_continue_run_resumes_blocked_repair_selection_policy_drift(self) -> None:
        workflow = PhaseFourWorkflow(self._settings(SDLC_DEVELOPER_EXECUTION_MODE="tool_loop"))
        run_id = "run_repair_item_scope_mismatch"
        state = {
            "run_id": run_id,
            "status": "blocked",
            "current_phase": "implementation_repair",
            "last_error": {"error_code": "developer_input_drift"},
            "developer_execution": {"mode": "implementation_repair"},
            "latest_artifacts": {
                "implementation_repair_feedback": "feedback.json",
                "developer_repair_execution_manifest": "manifest.json",
                "developer_repair_execution_journal": "journal.json",
            },
            "allowed_next_actions": ["get_status", "cancel_run"],
        }
        resumed = {"run_id": run_id, "status": "implementation_executing"}

        with (
            patch.object(workflow.workspace_manager, "locked_workspace", return_value=contextlib.nullcontext()),
            patch.object(workflow.store, "read_state", return_value=state),
            patch.object(workflow, "_continue_implementation_repair", return_value=resumed) as repair,
        ):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response, resumed)
        repair.assert_called_once_with(state=state, run_id=run_id)

    def _settings(self, **extra_overrides: str):
        overrides = {
            "SDLC_LLM_MODE": "fake",
            "SDLC_SYNC_MODE": "mock",
            "SDLC_DEVELOPER_EXECUTION_MODE": "legacy",
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
        self._git(target, "push", "-u", "origin", "main")
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

    def _create_phase4_traced_run(self) -> dict[str, object]:
        tested = self._create_phase4_tested_run()
        return PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

    def _workspace_path(self, run: dict[str, object]) -> Path:
        artifact = self.repo_root / str(run["latest_artifacts"]["run_workspace"])
        return Path(json.loads(artifact.read_text(encoding="utf-8"))["worktree_path"])

    def test_approve_implementation_plan_creates_developer_work_packet(self) -> None:
        phase3 = self._create_phase3_run()

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_preparing")
        self.assertEqual(response["status"], "implementation_ready")
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("developer_work_packet", response["latest_artifacts"])
        self.assertIn("Workspace/branch", response["summary_for_user"])
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
        self.assertEqual(packet["run_workspace"]["workspace_mode"], "git_worktree_v1")
        self.assertEqual(packet["run_workspace"]["branch"], response["workspace"]["branch"])
        self.assertEqual(packet["output_contract"]["format"], "changed_files_json_v1")
        self.assertTrue(packet["output_contract"]["changed_files_only"])
        self.assertTrue(packet["output_contract"]["allowed_paths"])
        grouped = packet["design_mappings_by_file"]
        self.assertTrue(any(path.startswith("src/main/java/") for path in grouped))
        self.assertTrue(any(item["exists"] for item in packet["proposed_files"]))

    def test_v2_developer_rejects_invented_requirement_id(self) -> None:
        path = "src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java"
        packet = {
            "requirement_contract": {"version": "v2", "revision": 1},
            "canonical_requirements": [{"id": "REQ-1", "content": "Keep heritage data."}],
            "requirement_coverage_by_file": {
                path: [{"requirement_id": "REQ-1", "content": "Keep heritage data."}]
            },
            "output_contract": {"allowed_paths": [path]},
        }
        result = DeveloperActResult(
            summary="invalid stable ID",
            files=[
                {
                    "path": path,
                    "operation": "update",
                    "content": "package com.sdlc.etour.heritage; public final class CulturalHeritage {}",
                    "reason": "test",
                    "covered_design_elements": [],
                    "covered_requirement_ids": ["REQ-INVENTED"],
                }
            ],
        )

        with self.assertRaises(HarnessError) as caught:
            _apply_developer_changes(
                result=result,
                work_packet=packet,
                target_path=self.target_repo,
                run_id="run-v2",
            )

        self.assertEqual(caught.exception.error_code, "developer_act_requirement_coverage_invalid")

    def test_v2_code_trace_emits_one_mapping_per_stable_requirement(self) -> None:
        source_path = "src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java"
        test_path = "src/test/java/com/sdlc/etour/heritage/CulturalHeritageTest.java"
        packet = {
            "run_id": "run-v2",
            "issue_key": "SDLC-2",
            "requirement_contract": {"version": "v2", "revision": 3},
            "canonical_requirements": [{"id": "REQ-1", "content": "Keep heritage data."}],
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-1",
                    "content": "Keep heritage data.",
                    "design_targets": ["CulturalHeritage"],
                    "design_element_ids": ["class:CulturalHeritage"],
                    "planned_code_targets": [source_path],
                    "test_intents": ["Verify heritage data."],
                }
            ],
            "design_mappings_by_file": {
                source_path: [
                    {
                        "design_element_id": "class:CulturalHeritage",
                        "design_element_type": "class",
                        "design_element_name": "CulturalHeritage",
                        "planned_code_target": source_path,
                    }
                ]
            },
            "proposed_files": [{"path": test_path}],
        }

        result = CodeTraceValidator(CapturingCodeTraceLlm()).validate(
            target_path=self.target_repo,
            work_packet=packet,
        )

        payload = result.to_dict()
        self.assertTrue(result.ok)
        self.assertEqual(payload["mode"], "hybrid_rule_llm_code_trace_v3")
        self.assertEqual(payload["requirement_contract"]["revision"], 3)
        self.assertEqual([item["requirement_ref"] for item in payload["mappings"]], ["REQ-1"])
        self.assertEqual(payload["stats"]["requirements_covered"], 1)

    def test_v2_qa_requires_exact_stable_requirement_coverage(self) -> None:
        context = {
            "requirement_contract": {"version": "v2", "revision": 1},
            "canonical_requirements": [
                {"id": "REQ-1", "content": "First"},
                {"id": "REQ-2", "content": "Second"},
            ],
        }

        with self.assertRaises(HarnessError) as caught:
            QaTesterAgent(MissingRequirementQaLlm()).generate_tests(context=context)

        self.assertEqual(caught.exception.error_code, "qa_requirement_coverage_invalid")

        generated = QaTesterAgent().generate_tests(context=context)
        covered = {
            requirement_id
            for scenario in generated.scenarios
            for requirement_id in scenario["requirement_ids"]
        }
        self.assertEqual(covered, {"REQ-1", "REQ-2"})

    def test_v2_pr_body_reports_contract_and_stable_id_coverage(self) -> None:
        body = _code_review_pr_body(
            state={
                "quality_reports": {
                    "implementation_traceability": {
                        "requirement_contract_version": "v2",
                        "requirement_revision": 4,
                        "stats": {"mapping_count": 2, "requirement_count": 2, "requirements_covered": 2},
                        "llm_confidence": 0.9,
                    },
                    "qa_acceptance": {
                        "scenario_count": 1,
                        "requirement_ids": ["REQ-1", "REQ-2"],
                    },
                }
            },
            run_id="run-v2",
            issue_key="SDLC-2",
            artifact_paths={"requirement_design_code_trace": "trace.json"},
            qa_generated={
                "scenarios": [
                    {"name": "canonical flow", "requirement_ids": ["REQ-1", "REQ-2"]}
                ],
                "risks": [],
            },
        )

        self.assertIn("Requirement contract: `v2` revision `4`", body)
        self.assertIn("Canonical requirement coverage: 2/2", body)
        self.assertIn("canonical flow [REQ-1, REQ-2]", body)

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
        (self._workspace_path(phase3) / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_dirty")
        self.assertNotIn("developer_work_packet", response["latest_artifacts"])

    def test_continue_run_applies_developer_act_code_changes(self) -> None:
        ready = self._create_phase4_ready_run()

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_writing")
        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("code_change_report", response["latest_artifacts"])
        workspace_path = self._workspace_path(response)
        self.assertEqual(
            self._git_output(workspace_path, "rev-parse", "--abbrev-ref", "HEAD"),
            response["workspace"]["branch"],
        )
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), "")
        status = self._git_output(workspace_path, "status", "--porcelain")
        self.assertIn("DeleteCulturalHeritageService.java", status)
        self.assertIn("DeleteCulturalHeritageFlowTest.java", status)

        service_path = (
            workspace_path
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

    def test_canonical_checkout_change_does_not_redirect_run_workspace(self) -> None:
        ready = self._create_phase4_ready_run()
        self._git(self.target_repo, "checkout", "-b", "manual-review")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "implementation_written")
        self.assertEqual(self._git_output(self.target_repo, "branch", "--show-current"), "manual-review")
        workspace_path = self._workspace_path(response)
        self.assertEqual(
            self._git_output(workspace_path, "branch", "--show-current"),
            response["workspace"]["branch"],
        )

    def test_legacy_phase4_run_without_workspace_is_not_auto_adopted(self) -> None:
        phase3 = self._create_phase3_run()
        workspace_artifact = self.repo_root / str(phase3["latest_artifacts"]["run_workspace"])
        workspace_artifact.unlink()

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).approve_implementation_plan_checkpoint(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "run_workspace_missing")

    def test_tampered_work_packet_workspace_fails_closed(self) -> None:
        ready = self._create_phase4_ready_run()
        packet_path = self.repo_root / str(ready["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["run_workspace"]["branch"] = "feature/wrong-run"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "run_workspace_mismatch")

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
                    "--qa-repair-limit",
                    "3",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "implementation_traced")
        self.assertIn("code_change_report", payload["latest_artifacts"])
        self.assertIn("test_report", payload["latest_artifacts"])
        self.assertIn("requirement_design_code_trace", payload["latest_artifacts"])

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
        self.assertEqual(trace["mode"], "hybrid_rule_llm_code_trace_v3")
        self.assertTrue(trace["llm_required"])
        self.assertTrue(trace["llm_review"]["ok"])
        self.assertTrue(trace["llm_review"]["review_context"]["code_snippets_included"])
        self.assertIn("implementation_validation_ledger", response["latest_artifacts"])
        ledger_path = self.repo_root / str(response["latest_artifacts"]["implementation_validation_ledger"])
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(ledger["version"], "implementation_validation_ledger_v1")
        self.assertEqual(ledger["snapshot_fingerprint"], trace["snapshot_fingerprint"])

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

    def test_code_trace_llm_accepts_prose_wrapped_json(self) -> None:
        tested = self._create_phase4_tested_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator = CodeTraceValidator(ProseWrappedCodeTraceLlm())

        response = workflow.continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["status"], "implementation_traced")
        self.assertTrue(response["quality_reports"]["implementation_traceability"]["ok"])

    def test_continue_run_runs_qa_after_implementation_traced(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        recorder = RecordingQaTester()
        workflow.qa_tester = recorder
        workflow.verification_runner = SequenceVerificationRunner(["passed"])

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "qa_tested")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertIn("qa_generated_tests", response["latest_artifacts"])
        self.assertIn("qa_test_execution", response["latest_artifacts"])
        self.assertIn("qa_test_report", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["qa_acceptance"]["ok"])
        report_path = self.repo_root / str(response["latest_artifacts"]["qa_test_report"])
        report = report_path.read_text(encoding="utf-8")
        self.assertIn("QA Test Report V1", report)
        self.assertIn("Status: `pass`", report)
        self.assertIn("Generated Test Execution", report)
        self.assertIn("Packet mode:", report)
        self.assertIn("Any truncated:", report)
        self.assertTrue(
            (
                self._workspace_path(response)
                / "src"
                / "test"
                / "java"
                / "com"
                / "sdlc"
                / "etour"
                / "heritage"
                / "RecordingQaAcceptanceTest.java"
            ).exists()
        )
        self.assertEqual(recorder.context["packet_mode"], "trace_dev_style_compact_qa_packet_v2")
        self.assertIn("accepted_requirement", recorder.context)
        self.assertIn("code_under_test", recorder.context)
        self.assertIn("verification", recorder.context)
        self.assertIn("traceability", recorder.context)
        self.assertIn("artifact_references", recorder.context)
        self.assertNotIn("artifacts", recorder.context)
        self.assertNotIn("content", recorder.context["artifact_references"])
        self.assertGreaterEqual(len(recorder.context["code_under_test"]["files"]), 1)
        self.assertEqual(
            recorder.context["context_budget"]["omitted_full_artifacts"],
            ["design_artifact", "implementation_plan", "requirement_design_code_trace"],
        )

    def test_continue_run_moves_to_qa_repair_needed_when_qa_fails(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertEqual(response["current_phase"], "qa_repair")
        self.assertEqual(response["status"], "qa_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertEqual(response["qa_repair_iterations"], 0)
        self.assertIn("qa_generated_tests", response["latest_artifacts"])
        self.assertIn("qa_test_execution", response["latest_artifacts"])
        self.assertIn("qa_test_report", response["latest_artifacts"])
        self.assertIn("qa_repair_feedback", response["latest_artifacts"])
        self.assertFalse(response["quality_reports"]["qa_acceptance"]["ok"])
        self.assertTrue(response["quality_reports"]["qa_repair"]["needed"])
        feedback_path = self.repo_root / str(response["latest_artifacts"]["qa_repair_feedback"])
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        self.assertEqual(feedback["version"], "qa_repair_feedback_v1")
        self.assertEqual(feedback["repair_scope"], "code_or_test")
        self.assertTrue(feedback["failing_scenarios"])

    def test_continue_run_blocks_when_qa_fails_with_zero_repair_limit(self) -> None:
        self.settings = self._settings(SDLC_QA_REPAIR_LIMIT="0")
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "qa_repair_limit_exceeded")
        self.assertEqual(response["qa_repair_iterations"], 0)
        self.assertNotIn("qa_repair_feedback", response["latest_artifacts"])

    def test_continue_run_from_qa_repair_needed_repairs_and_retests_to_pass(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed", "passed"])
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "qa_tested")
        self.assertEqual(response["qa_repair_iterations"], 1)
        self.assertTrue(response["quality_reports"]["qa_acceptance"]["ok"])
        self.assertFalse(response["quality_reports"]["qa_repair"]["needed"])
        self.assertIn("code_change_report_v2.md", response["latest_artifacts"]["code_change_report"])
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn("requirement_design_code_trace_v2.json", response["latest_artifacts"]["requirement_design_code_trace"])
        self.assertIn("qa_generated_tests_v2.json", response["latest_artifacts"]["qa_generated_tests"])

    def test_continue_run_from_qa_repair_needed_blocks_when_qa_still_fails_at_limit(self) -> None:
        self.settings = self._settings(SDLC_QA_REPAIR_LIMIT="1")
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        repair_needed = workflow.continue_run(run_id=str(traced["run_id"]))

        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = FailingQaTester()
        workflow.verification_runner = SequenceVerificationRunner(["passed", "failed"])
        response = workflow.continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "qa_repair_limit_exceeded")
        self.assertEqual(response["qa_repair_iterations"], 1)
        self.assertTrue(response["quality_reports"]["qa_repair"]["limit_exceeded"])
    def test_continue_run_blocks_and_records_raw_response_when_qa_json_is_invalid(self) -> None:
        traced = self._create_phase4_traced_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.qa_tester = QaTesterAgent(InvalidQaLlm())

        response = workflow.continue_run(run_id=str(traced["run_id"]))

        self.assertEqual(response["current_phase"], "qa_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "qa_tester_llm_failed")
        self.assertIn("qa_tester_raw_response", response["latest_artifacts"])
        raw_path = self.repo_root / str(response["latest_artifacts"]["qa_tester_raw_response"])
        self.assertIn("cut off before scenarios", raw_path.read_text(encoding="utf-8"))

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
        self.assertEqual(payload["status"], "implementation_traced")
        self.assertIn("test_report", payload["latest_artifacts"])
        self.assertIn("requirement_design_code_trace", payload["latest_artifacts"])

    def test_continue_run_marks_repair_needed_when_code_trace_missing_test_file(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
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
        self.assertEqual(
            feedback["llm_review"]["summary"],
            "Validator reported 1 actionable blocking link(s). "
            "Approved requestDeletion behavior is not covered by code/test evidence.",
        )
        self.assertTrue(feedback["developer_constraints"]["allowed_paths"])

    def test_continue_run_from_repair_needed_repairs_code_then_retraces(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
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
            self._workspace_path(tested)
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
            self._workspace_path(tested)
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
            self._workspace_path(tested)
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
                self._workspace_path(response)
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

    def test_continue_run_from_repair_needed_requeues_when_repair_verification_fails(self) -> None:
        tested = self._create_phase4_tested_run()
        test_file = (
            self._workspace_path(tested)
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
        (self._workspace_path(repair_needed) / "scripts" / "test.ps1").write_text(
            "Write-Error 'repair unit failed'\nexit 1\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn("implementation_repair_feedback_v2.json", response["latest_artifacts"]["implementation_repair_feedback"])
        self.assertEqual(response["quality_reports"]["implementation_repair"]["feedback_source"], "verification")
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
        self.assertEqual(
            response["allowed_next_actions"],
            ["continue_run", "revalidate_implementation", "get_status", "cancel_run"],
        )
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

    def test_revalidate_implementation_rejects_non_revalidatable_blocked_state(self) -> None:
        written = self._create_phase4_written_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(written["run_id"]))
        state.update(
            {
                "status": "blocked",
                "current_phase": "implementation_testing",
                "last_error": HarnessError("workspace_dirty", "workspace changed", run_id=str(written["run_id"])).to_response(),
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

    def test_canonical_trace_survives_redundant_file_mapping_removal(self) -> None:
        tested = self._create_phase4_tested_run()
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(tested["run_id"]))
        packet_path = self.repo_root / str(state["latest_artifacts"]["developer_work_packet"])
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["design_mappings_by_file"] = {}
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(tested["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "implementation_traced")
        self.assertIsNone(response["last_error"])
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

    def test_continue_run_routes_actionable_verification_failure_to_repair(self) -> None:
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "test.ps1").write_text(
            "Write-Error 'unit failed'\nexit 1\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertIsNone(response["last_error"])
        self.assertEqual(
            response["allowed_next_actions"],
            ["continue_run", "revalidate_implementation", "get_status", "cancel_run"],
        )
        self.assertEqual(response["implementation_repair_iterations"], 0)
        self.assertIn("test_report", response["latest_artifacts"])
        self.assertIn("implementation_repair_feedback", response["latest_artifacts"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `failed`", report)
        self.assertIn("unit failed", report)
        feedback = json.loads(
            (self.repo_root / str(response["latest_artifacts"]["implementation_repair_feedback"])).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(feedback["feedback_source"], "verification")
        self.assertEqual(feedback["verification_artifact"], response["latest_artifacts"]["test_report"])
        self.assertIn("unit failed", feedback["verification"]["stderr"])

    def test_legacy_blocked_test_failure_is_exposed_and_resumed_through_repair(self) -> None:
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "test.ps1").write_text(
            "Write-Error 'legacy unit failed'\nexit 1\n",
            encoding="utf-8",
        )
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(str(written["run_id"]))
        latest = dict(state["latest_artifacts"])
        feedback_path = self.repo_root / str(latest.pop("implementation_repair_feedback"))
        feedback_path.unlink()
        quality_reports = dict(state.get("quality_reports") or {})
        quality_reports.pop("verification", None)
        quality_reports.pop("implementation_repair", None)
        state.update(
            {
                "current_phase": "implementation_testing",
                "status": "blocked",
                "latest_artifacts": latest,
                "quality_reports": quality_reports,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": HarnessError(
                    "test_failed",
                    "Test command exited with code 1.",
                    run_id=str(written["run_id"]),
                ).to_response(),
            }
        )
        store.write_state(state)

        status = PhaseOneWorkflow(self.settings).get_status(run_id=str(written["run_id"]))
        self.assertIn("continue_run", status["allowed_next_actions"])

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_repair")
        self.assertEqual(response["status"], "implementation_repair_needed")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertIn("test_report_v2.md", response["latest_artifacts"]["test_report"])
        self.assertIn("implementation_repair_feedback_v2.json", response["latest_artifacts"]["implementation_repair_feedback"])
        self.assertTrue(repair_needed["quality_reports"]["verification"]["repairable"])

    def test_continue_run_blocks_when_verification_is_skipped(self) -> None:
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "test.ps1").write_text(
            "Write-Host \"JDK compiler 'javac' was not found, so compile/runtime tests were skipped.\"\n",
            encoding="utf-8",
        )

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["current_phase"], "implementation_testing")
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_skipped")
        self.assertEqual(response["allowed_next_actions"], ["revalidate_implementation", "get_status", "cancel_run"])
        self.assertIn("test_report", response["latest_artifacts"])
        report = (self.repo_root / str(response["latest_artifacts"]["test_report"])).read_text(encoding="utf-8")
        self.assertIn("Status: `skipped`", report)

    def test_verification_timeout_stays_blocked_and_can_be_revalidated(self) -> None:
        written = self._create_phase4_written_run()
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        original_run = workflow.verification_runner.run

        def timed_out_run(**kwargs: object) -> VerificationResult:
            result = original_run(**kwargs)
            return VerificationResult(
                **{
                    **result.to_dict(),
                    "reason": "Test command timed out after 120s.",
                    "timed_out": True,
                }
            )

        workflow.verification_runner.run = timed_out_run  # type: ignore[method-assign]

        response = workflow.continue_run(run_id=str(written["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "test_failed")
        self.assertFalse(response["quality_reports"]["verification"]["repairable"])
        self.assertEqual(response["allowed_next_actions"], ["revalidate_implementation", "get_status", "cancel_run"])

    def test_verification_failure_blocks_after_shared_repair_limit(self) -> None:
        self.settings = self._settings(SDLC_IMPLEMENTATION_REPAIR_LIMIT="1")
        written = self._create_phase4_written_run()
        (self._workspace_path(written) / "scripts" / "test.ps1").write_text(
            "Write-Error 'still failing'\nexit 1\n",
            encoding="utf-8",
        )
        repair_needed = PhaseFourWorkflow(self.settings).continue_run(run_id=str(written["run_id"]))

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(repair_needed["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_repair_limit_exceeded")
        self.assertEqual(response["implementation_repair_iterations"], 1)
        self.assertEqual(response["allowed_next_actions"], ["revalidate_implementation", "get_status", "cancel_run"])

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

    def test_verification_runner_executes_required_qa_test_class(self) -> None:
        if not shutil.which("javac") or not shutil.which("java"):
            self.skipTest("JDK is required for the QA generated-test execution regression test.")
        qa_path = "src/test/java/com/sdlc/etour/heritage/RequiredQaAcceptanceTest.java"
        qa_file = self.target_repo / qa_path
        qa_file.write_text(
            (
                "package com.sdlc.etour.heritage;\n"
                "public final class RequiredQaAcceptanceTest {\n"
                "  public static void main(String[] args) {\n"
                "    throw new IllegalStateException(\"qa generated failure\");\n"
                "  }\n"
                "}\n"
            ),
            encoding="utf-8",
        )
        (self.target_repo / "scripts" / "test.ps1").write_text(
            (
                "$ErrorActionPreference = \"Stop\"\n"
                "$sources = Get-ChildItem -LiteralPath \"src\" -Filter *.java -Recurse | "
                "ForEach-Object { Resolve-Path -Relative -LiteralPath $_.FullName }\n"
                "New-Item -ItemType Directory -Force -Path build | Out-Null\n"
                "javac -encoding UTF-8 -d build @sources\n"
                "Write-Host 'baseline compile passed'\n"
            ),
            encoding="utf-8",
        )
        work_packet = {"test_intents": [{"command": "powershell -ExecutionPolicy Bypass -File scripts/test.ps1"}]}

        result = VerificationRunner(timeout_seconds=30).run(
            target_path=self.target_repo,
            work_packet=work_packet,
            required_test_files=[qa_path],
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("RequiredQaAcceptanceTest", result.reason)
        self.assertIn("qa generated failure", result.stderr)

    def test_verification_runner_rejects_required_qa_test_without_bytecode(self) -> None:
        qa_path = "src/test/java/com/sdlc/etour/heritage/RequiredQaAcceptanceTest.java"
        work_packet = {
            "test_intents": [
                {"command": "powershell -NoProfile -Command \"Write-Host 'placeholder passed'\""},
            ],
        }

        result = VerificationRunner(timeout_seconds=10).run(
            target_path=self.target_repo,
            work_packet=work_packet,
            required_test_files=[qa_path],
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("did not produce Java bytecode", result.reason)
        self.assertEqual(
            result.discovered_test_entrypoints,
            ("com.sdlc.etour.heritage.RequiredQaAcceptanceTest",),
        )

    def test_continue_run_rejects_non_ready_state(self) -> None:
        phase3 = self._create_phase3_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseFourWorkflow(self.settings).continue_run(run_id=str(phase3["run_id"]))

        self.assertEqual(ctx.exception.error_code, "run_not_continuable")

    def test_continue_run_blocks_dirty_target_repo(self) -> None:
        ready = self._create_phase4_ready_run()
        (self._workspace_path(ready) / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = PhaseFourWorkflow(self.settings).continue_run(run_id=str(ready["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_dirty")
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
                self._workspace_path(response)
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

