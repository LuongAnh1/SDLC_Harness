"""Test Phase 3 cho approved design -> implementation plan review."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.developer import DeveloperPlanAgent, ImplementationPlanDraft
from sdlc_harness.agents.developer.planner import _parse_developer_plan_payload
from sdlc_harness.agents.validator import (
    ImplementationPlanReview,
    ImplementationPlanValidator,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.verification import VerificationResult
from sdlc_harness.sync.atlassian import (
    _confluence_implementation_plan_storage_body,
    real_sync_implementation_plan_checkpoint,
)
from sdlc_harness.sync.github import GitHubCodeReviewResult
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow
from sdlc_harness.workspaces import RunWorkspaceManager


class RepairCountingDeveloper(DeveloperPlanAgent):
    def __init__(self) -> None:
        super().__init__()
        self.repair_calls = 0

    def repair_plan(self, **kwargs):
        self.repair_calls += 1
        return super().repair_plan(**kwargs)


class FailingDeveloper(DeveloperPlanAgent):
    def plan(self, **kwargs):
        raise HarnessError(
            "implementation_plan_developer_failed",
            "Developer.plan LLM did not return valid JSON.",
        )


class BadJsonLlm:
    def complete(self, *, system: str, prompt: str) -> str:
        return "I can describe the implementation plan, but I will not return JSON in this response."


class CaptureDeveloperLlm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "summary": "Use deterministic mappings.",
                "repo_evidence": [],
                "implementation_steps": [],
                "proposed_files": [],
                "design_mappings": [],
                "test_intents": [],
                "assumptions": [],
                "open_questions": [],
            }
        )


class SequencedImplementationPlanValidator:
    def __init__(self, ok_by_call: list[bool]):
        self.ok_by_call = ok_by_call
        self.calls = 0

    def review(self, **kwargs):
        ok = self.ok_by_call[min(self.calls, len(self.ok_by_call) - 1)]
        repair_iterations = int(kwargs.get("repair_iterations") or 0)
        self.calls += 1
        if ok:
            return ImplementationPlanReview(
                ok=True,
                mode="implementation_plan_review_hybrid_v2",
                summary="LLM review accepts the repaired implementation plan.",
                rule_review={"ok": True, "mode": "implementation_plan_rule_review_v1"},
                llm_review={"ok": True, "summary": "accepted"},
                repair_iterations=repair_iterations,
            )
        return ImplementationPlanReview(
            ok=False,
            mode="implementation_plan_review_hybrid_v2",
            implementation_quality_issues=["Plan does not describe the implementation order clearly enough."],
            developer_feedback="Add explicit ordering from design elements to code targets and tests.",
            rule_review={"ok": True, "mode": "implementation_plan_rule_review_v1"},
            llm_review={
                "ok": False,
                "implementation_quality_issues": [
                    "Plan does not describe the implementation order clearly enough."
                ],
            },
            repair_iterations=repair_iterations,
        )


class SequenceVerificationRunner:
    def __init__(self, statuses: list[str]):
        self.statuses = list(statuses)

    def run(self, *, target_path: Path, work_packet: dict[str, object], required_test_files=None):
        del work_packet, required_test_files
        status = self.statuses.pop(0) if self.statuses else "passed"
        return VerificationResult(
            status=status,
            command="fixture verification runner",
            cwd=str(target_path),
            exit_code=0 if status == "passed" else 1,
            duration_seconds=0.01,
            stdout="fixture verification passed" if status == "passed" else "",
            stderr="fixture verification failed" if status == "failed" else "",
            reason=f"Fixture verification {status}.",
        )


class RecordingPublisher:
    def publish(self, **kwargs: object) -> GitHubCodeReviewResult:
        return GitHubCodeReviewResult(
            {
                "version": "github_code_review_v1",
                "phase": "code_review_publish",
                "status": "published",
                "repository": kwargs["repository"],
                "remote": kwargs["expected_remote"],
                "base_branch": kwargs["base_branch"],
                "branch": kwargs["branch"],
                "issue_key": kwargs["issue_key"],
                "run_id": kwargs["run_id"],
                "publication_paths": kwargs["publication_paths"],
                "commit": {
                    "operation": "created",
                    "sha": "abc123",
                    "url": "https://github.com/example/target/commit/abc123",
                },
                "push": {"status": "pushed", "branch": kwargs["branch"]},
                "pull_request": {
                    "operation": "created",
                    "number": 7,
                    "url": "https://github.com/example/target/pull/7",
                    "title": kwargs["pr_title"],
                    "isDraft": True,
                    "headRefName": kwargs["branch"],
                    "baseRefName": kwargs["base_branch"],
                },
            }
        )


class RecordingApprover:
    def approve(self, **kwargs: object) -> dict[str, object]:
        return {
            "version": "github_code_review_approval_v1",
            "phase": "code_review_approval",
            "status": "ready_for_review",
            "operation": "marked_ready",
            "repository": kwargs["repository"],
            "run_id": kwargs["run_id"],
            "pull_request_before": {
                "number": kwargs["pr_number"],
                "url": "https://github.com/example/target/pull/7",
                "isDraft": True,
            },
            "pull_request": {
                "number": kwargs["pr_number"],
                "url": "https://github.com/example/target/pull/7",
                "isDraft": False,
            },
            "approved_at": "2026-10-16T12:00:00Z",
        }


class PhaseThreeTest(unittest.TestCase):
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

    def _settings(self, **extra_overrides: str):
        overrides = {
            "SDLC_LLM_MODE": "fake",
            "SDLC_SYNC_MODE": "mock",
            "SDLC_DESIGN_REPAIR_LIMIT": "2",
            "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT": "2",
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

    def _create_phase2_run(self) -> dict[str, object]:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        self.assertEqual(approved["status"], "waiting_design_approval")
        return approved

    def _create_v2_phase2_run(self) -> dict[str, object]:
        """All new Phase 2 runs already use the canonical V2 contract."""

        return self._create_phase2_run()

    def test_dispatcher_still_approves_requirement_checkpoint_to_design(self) -> None:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

        approved = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(approved["status"], "waiting_design_approval")
        self.assertEqual(approved["pending_checkpoint"]["type"], "design_approval")

    def test_approve_design_checkpoint_creates_implementation_plan(self) -> None:
        approved = self._create_phase2_run()

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_planning")
        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        self.assertEqual(response["pending_checkpoint"]["type"], "implementation_plan_review")
        self.assertIn("implementation_plan", response["latest_artifacts"])
        self.assertIn("approve_checkpoint", response["allowed_next_actions"])
        self.assertIn("run workspace", response["summary_for_user"])
        self.assertEqual(response["workspace"]["workspace_mode"], "git_worktree_v1")
        self.assertIn("implementation_plan_structured", response["latest_artifacts"])
        self.assertIn("implementation_plan_review", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["implementation_plan_review"]["ok"])
        self.assertEqual(response["last_sync"]["phase"], "implementation_plan_review")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertEqual(self._git_output(self.target_repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._git_output(self.target_repo, "status", "--porcelain"), "")

        plan_path = self.repo_root / str(response["latest_artifacts"]["implementation_plan"])
        self.assertTrue(plan_path.exists())
        content = plan_path.read_text(encoding="utf-8")
        self.assertIn("Implementation Plan V1", content)
        self.assertIn("Repo Evidence", content)
        self.assertIn("Implementation Steps", content)
        self.assertIn("Proposed Files", content)
        self.assertIn("Design To Code Mapping", content)
        self.assertIn(str(self.target_repo), content)
        self.assertIn(self.remote_url, content)
        workspace_artifact = json.loads(
            (self.repo_root / response["latest_artifacts"]["run_workspace"]).read_text(encoding="utf-8")
        )
        self.assertIn(workspace_artifact["branch"], content)
        self.assertIn(workspace_artifact["worktree_path"], content)
        self.assertIn("DeleteCulturalHeritageService.deleteHeritage", content)
        self.assertIn("Test Intent", content)
        self.assertIn("Phase 3 tạo branch/worktree riêng cho run", content)

        structured_path = self.repo_root / str(response["latest_artifacts"]["implementation_plan_structured"])
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
        self.assertEqual(structured["version"], "implementation_plan_structured_v1")
        self.assertEqual(structured["planning_mode"], "llm_developer_with_codegraph_context_v1")
        self.assertEqual(structured["repo_context"]["mode"], "fake_codegraph_context_v1")
        self.assertTrue(structured["implementation_steps"])
        self.assertTrue(structured["proposed_files"])
        self.assertTrue(structured["design_mappings"])
        self.assertIn(
            "method:DeleteCulturalHeritageService.deleteHeritage",
            {item["design_element_id"] for item in structured["design_mappings"]},
        )

        review_path = self.repo_root / str(response["latest_artifacts"]["implementation_plan_review"])
        review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertTrue(review["ok"])
        self.assertEqual(review["mode"], "implementation_plan_review_hybrid_v2")
        self.assertTrue(review["rule_review"]["ok"])
        self.assertTrue(review["llm_review"]["ok"])
        self.assertEqual(review["repair_iterations"], 0)

        sync_report_path = self.repo_root / str(response["last_sync"]["report"])
        sync_report = json.loads(sync_report_path.read_text(encoding="utf-8"))
        self.assertEqual(sync_report["version"], "tool_sync_report_v3")
        self.assertEqual(sync_report["phase"], "implementation_plan_review")
        self.assertEqual(sync_report["checkpoint"]["type"], "implementation_plan_review")
        self.assertEqual({target["tool"] for target in sync_report["targets"]}, {"jira", "confluence"})
        self.assertNotIn("github", {target["tool"] for target in sync_report["targets"]})

    def test_v2_phase3_creates_canonical_requirement_coverage(self) -> None:
        approved = self._create_v2_phase2_run()
        workflow = PhaseThreeWorkflow(self.settings)
        developer_llm = CaptureDeveloperLlm()
        workflow.developer = DeveloperPlanAgent(developer_llm)

        response = workflow.approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        self.assertEqual(response["requirement_contract_version"], "v2")
        structured = json.loads(
            (self.repo_root / response["latest_artifacts"]["implementation_plan_structured"]).read_text(
                encoding="utf-8"
            )
        )
        graph = json.loads(
            (self.repo_root / response["latest_artifacts"]["requirement_graph"]).read_text(encoding="utf-8")
        )
        expected_ids = {item["id"] for item in graph["requirements"]}
        coverage = structured["requirement_coverage"]
        self.assertEqual({item["requirement_id"] for item in coverage}, expected_ids)
        self.assertTrue(all(item["planned_code_targets"] for item in coverage))
        self.assertTrue(all(item["test_intents"] for item in coverage))
        self.assertEqual(structured["requirement_contract"]["version"], "v2")
        self.assertLess(
            developer_llm.prompts[0].index("Canonical Requirement V2 (source of truth"),
            developer_llm.prompts[0].index("Approved requirement packet"),
        )
        markdown = (self.repo_root / response["latest_artifacts"]["implementation_plan"]).read_text(
            encoding="utf-8"
        )
        self.assertIn("## Requirement Coverage", markdown)
        confluence = _confluence_implementation_plan_storage_body(
            PhaseThreeWorkflow(self.settings).store,
            str(response["run_id"]),
            str(response["source_ref"]),
            {str(key): str(value) for key, value in response["latest_artifacts"].items()},
        )
        self.assertIn("Requirement Coverage", confluence)
        self.assertIn("PRE-1", confluence)

    def test_v2_phase3_rejects_tampered_graph_before_state_mutation(self) -> None:
        approved = self._create_v2_phase2_run()
        run_id = str(approved["run_id"])
        workflow = PhaseThreeWorkflow(self.settings)
        state_before = workflow.store.read_state(run_id)
        graph_path = self.repo_root / state_before["latest_artifacts"]["requirement_graph"]
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["requirements"][0]["content"] = "tampered"
        graph_path.write_text(json.dumps(graph), encoding="utf-8")

        with self.assertRaises(HarnessError) as caught:
            workflow.approve_design_checkpoint(run_id=run_id)

        self.assertEqual(caught.exception.error_code, "requirement_v2_phase3_input_invalid")
        self.assertEqual(workflow.store.read_state(run_id), state_before)

    def test_v2_phase3_repair_rebuilds_requirement_coverage(self) -> None:
        approved = self._create_v2_phase2_run()
        workflow = PhaseThreeWorkflow(self.settings)
        developer = RepairCountingDeveloper()
        validator = SequencedImplementationPlanValidator([False, True])
        workflow.developer = developer
        workflow.validator = validator

        response = workflow.approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        self.assertEqual(developer.repair_calls, 1)
        self.assertEqual(validator.calls, 2)
        structured = json.loads(
            (self.repo_root / response["latest_artifacts"]["implementation_plan_structured"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(structured["repair_iteration"], 1)
        self.assertEqual(structured["requirement_contract"]["version"], "v2")
        self.assertTrue(structured["requirement_coverage"])
        self.assertTrue(all(item["planned_code_targets"] for item in structured["requirement_coverage"]))

    def test_v2_phase4_creates_canonical_developer_work_packet(self) -> None:
        approved = self._create_v2_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        workflow = PhaseFourWorkflow(self.settings)
        response = workflow.approve_implementation_plan_checkpoint(run_id=run_id)

        self.assertEqual(response["status"], "implementation_ready")
        self.assertEqual(response["requirement_contract_version"], "v2")
        packet = json.loads(
            (self.repo_root / response["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        canonical_ids = {item["id"] for item in packet["canonical_requirements"]}
        coverage_ids = {item["requirement_id"] for item in packet["requirement_coverage"]}
        projected_ids = {
            item["requirement_id"]
            for items in packet["requirement_coverage_by_file"].values()
            for item in items
        }
        self.assertEqual(packet["requirement_contract"]["version"], "v2")
        self.assertEqual(packet["requirement_contract"]["revision"], 1)
        self.assertEqual(canonical_ids, coverage_ids)
        self.assertEqual(canonical_ids, projected_ids)
        self.assertIn("covered_requirement_ids", packet["output_contract"]["required_fields"])

    def test_v2_phase4_rejects_tampered_coverage_before_state_mutation(self) -> None:
        approved = self._create_v2_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        workflow = PhaseFourWorkflow(self.settings)
        state_before = workflow.store.read_state(run_id)
        structured_path = self.repo_root / state_before["latest_artifacts"]["implementation_plan_structured"]
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
        structured["requirement_coverage"][0]["content"] = "tampered"
        structured_path.write_text(json.dumps(structured), encoding="utf-8")

        with self.assertRaises(HarnessError) as caught:
            workflow.approve_implementation_plan_checkpoint(run_id=run_id)

        self.assertEqual(caught.exception.error_code, "requirement_v2_phase4_input_invalid")
        self.assertEqual(workflow.store.read_state(run_id), state_before)

    def test_v2_phase4_runs_canonical_ids_through_qa(self) -> None:
        approved = self._create_v2_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id)

        for _ in range(3):
            response = PhaseFourWorkflow(self.settings).continue_run(run_id=run_id)
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner.run = lambda **kwargs: VerificationResult(
            status="passed",
            command="fixture QA runner",
            cwd=str(kwargs["target_path"]),
            exit_code=0,
            duration_seconds=0.01,
            stdout="fixture QA passed",
            stderr="",
            reason="Fixture QA execution passed.",
        )
        response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "qa_tested", response)
        self.assertEqual(response["requirement_contract_version"], "v2")
        trace = json.loads(
            (self.repo_root / response["latest_artifacts"]["requirement_design_code_trace"]).read_text(
                encoding="utf-8"
            )
        )
        qa_generated = json.loads(
            (self.repo_root / response["latest_artifacts"]["qa_generated_tests"]).read_text(encoding="utf-8")
        )
        trace_ids = {item["requirement_ref"] for item in trace["mappings"]}
        qa_ids = {
            requirement_id
            for scenario in qa_generated["scenarios"]
            for requirement_id in scenario["requirement_ids"]
        }
        self.assertEqual(trace["mode"], "hybrid_rule_llm_code_trace_v2")
        self.assertEqual(trace_ids, qa_ids)
        self.assertEqual(trace["stats"]["requirements_covered"], trace["stats"]["requirement_count"])

    def test_v2_phase4_runs_to_code_review_approved_with_stable_ids(self) -> None:
        approved = self._create_v2_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        packet = json.loads(
            (self.repo_root / response["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        canonical_ids = {item["id"] for item in packet["canonical_requirements"]}
        revision = packet["requirement_contract"]["revision"]
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed", "passed"])

        statuses = []
        for _ in range(4):
            response = workflow.continue_run(run_id=run_id)
            statuses.append(response["status"])
        self.assertEqual(
            statuses,
            ["implementation_written", "implementation_tested", "implementation_traced", "qa_tested"],
        )

        workflow.github_publisher = RecordingPublisher()
        with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
            published = workflow.continue_run(run_id=run_id)
        ready = workflow.continue_run(run_id=run_id)
        workflow.github_approver = RecordingApprover()
        with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
            final = workflow.approve_code_review_checkpoint(
                run_id=run_id,
                checkpoint_id=str(ready["pending_checkpoint"]["checkpoint_id"]),
            )

        self.assertEqual(published["status"], "code_review_published")
        self.assertEqual(ready["status"], "code_review_ready")
        self.assertEqual(final["status"], "code_review_approved")
        self.assertEqual(final["requirement_contract_version"], "v2")
        pr_body = (self.repo_root / final["latest_artifacts"]["github_pr_body"]).read_text(encoding="utf-8")
        jira_sync = (self.runtime_root / "runs" / run_id / "sync" / "code-review-jira-comment.md").read_text(
            encoding="utf-8"
        )
        confluence_sync = (
            self.runtime_root / "runs" / run_id / "sync" / "code-review-confluence-page.md"
        ).read_text(encoding="utf-8")
        self.assertIn(f"Requirement contract: `v2` revision `{revision}`", pr_body)
        for requirement_id in canonical_ids:
            self.assertIn(requirement_id, pr_body)
            self.assertIn(requirement_id, jira_sync)
            self.assertIn(requirement_id, confluence_sync)

    def test_v2_phase4_repair_loops_preserve_contract_and_stable_ids(self) -> None:
        approved = self._create_v2_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        ready = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        initial_packet = json.loads(
            (self.repo_root / ready["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        canonical_ids = {item["id"] for item in initial_packet["canonical_requirements"]}
        revision = initial_packet["requirement_contract"]["revision"]
        workflow = PhaseFourWorkflow(self.settings)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])

        written = workflow.continue_run(run_id=run_id)
        tested = workflow.continue_run(run_id=run_id)
        workspace_path = Path(RunWorkspaceManager(self.settings).resolve(run_id).worktree_path)
        missing_test = (
            workspace_path
            / "src"
            / "test"
            / "java"
            / "com"
            / "sdlc"
            / "etour"
            / "heritage"
            / "DeleteCulturalHeritageFlowTest.java"
        )
        missing_test.unlink()

        repair_needed = workflow.continue_run(run_id=run_id)
        self.assertEqual(repair_needed["status"], "implementation_repair_needed")
        workflow.verification_runner = SequenceVerificationRunner(["passed"])
        traced = workflow.continue_run(run_id=run_id)
        self.assertEqual(traced["status"], "implementation_traced")

        repaired_packet = json.loads(
            (self.repo_root / traced["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        repaired_trace = json.loads(
            (self.repo_root / traced["latest_artifacts"]["requirement_design_code_trace"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(repaired_packet["requirement_contract"]["revision"], revision)
        self.assertEqual({item["id"] for item in repaired_packet["canonical_requirements"]}, canonical_ids)
        self.assertEqual({item["requirement_ref"] for item in repaired_trace["mappings"]}, canonical_ids)

        workflow.verification_runner = SequenceVerificationRunner(["failed"])
        qa_repair_needed = workflow.continue_run(run_id=run_id)
        self.assertEqual(qa_repair_needed["status"], "qa_repair_needed")
        workflow.verification_runner = SequenceVerificationRunner(["passed", "passed"])
        qa_tested = workflow.continue_run(run_id=run_id)

        self.assertEqual(qa_tested["status"], "qa_tested")
        final_packet = json.loads(
            (self.repo_root / qa_tested["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        final_trace = json.loads(
            (self.repo_root / qa_tested["latest_artifacts"]["requirement_design_code_trace"]).read_text(
                encoding="utf-8"
            )
        )
        qa_generated = json.loads(
            (self.repo_root / qa_tested["latest_artifacts"]["qa_generated_tests"]).read_text(encoding="utf-8")
        )
        qa_ids = {
            requirement_id
            for scenario in qa_generated["scenarios"]
            for requirement_id in scenario["requirement_ids"]
        }
        self.assertEqual(final_packet["requirement_contract"]["revision"], revision)
        self.assertEqual({item["id"] for item in final_packet["canonical_requirements"]}, canonical_ids)
        self.assertEqual({item["requirement_ref"] for item in final_trace["mappings"]}, canonical_ids)
        self.assertEqual(qa_ids, canonical_ids)
        self.assertEqual(qa_tested["implementation_repair_iterations"], 1)
        self.assertEqual(qa_tested["qa_repair_iterations"], 1)

    def test_cli_approve_checkpoint_dispatches_design_to_phase3(self) -> None:
        approved = self._create_phase2_run()
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
                    str(approved["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--implementation-plan-repair-limit",
                    "1",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "waiting_implementation_plan_review")
        self.assertEqual(payload["pending_checkpoint"]["type"], "implementation_plan_review")
        self.assertEqual(payload["last_sync"]["phase"], "implementation_plan_review")

    def test_settings_reads_implementation_plan_repair_limit(self) -> None:
        self.assertEqual(self.settings.implementation_plan_repair_limit, 2)
        settings = self._settings(SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT="0")

        self.assertEqual(settings.implementation_plan_repair_limit, 0)

    def test_developer_plan_parser_accepts_fenced_json_with_prose(self) -> None:
        payload = _parse_developer_plan_payload(
            """Here is the plan:
```json
{
  "summary": "Implement approved delete flow",
  "repo_evidence": ["CulturalHeritage.java exists"],
  "implementation_steps": [
    {
      "order": 1,
      "title": "Add delete service",
      "description": "Implement deletion",
      "files": ["src/main/java/DeleteService.java"],
      "design_refs": ["method:DeleteService.delete"]
    }
  ],
  "proposed_files": [],
  "design_mappings": [],
  "test_intents": [],
  "assumptions": [],
  "open_questions": []
}
```
Thanks."""
        )

        self.assertEqual(payload["summary"], "Implement approved delete flow")
        self.assertEqual(payload["repo_evidence"], ["CulturalHeritage.java exists"])
        self.assertEqual(payload["implementation_steps"][0]["order"], 1)

    def test_developer_plan_falls_back_when_llm_returns_invalid_json(self) -> None:
        draft = DeveloperPlanAgent(BadJsonLlm()).plan(
            run_id="run_test",
            source_ref="jira_issue:SDLC-1",
            issue_key="SDLC-1",
            requirement_packet="# Requirement\nDelete an item.",
            design_artifact="# Design\nDeleteCulturalHeritageService.deleteHeritage",
            requirement_design_trace={
                "version": "requirement_design_trace_v1",
                "class_trace": [],
                "sequence_trace": [],
            },
            class_graph={
                "version": "class_diagram_graph_v1",
                "nodes": [
                    {
                        "id": "DeleteCulturalHeritageService",
                        "className": "DeleteCulturalHeritageService",
                        "methods": [{"name": "deleteHeritage"}],
                    }
                ],
            },
            sequence_graph={"version": "sequence_diagram_graph_v1", "sequenceDiagrams": []},
            target_repo_path=str(self.target_repo),
            target_repo_remote=self.remote_url,
            target_base_branch="main",
            planned_branch="feature/sdlc-1-implementation",
            target_files=[
                "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java"
            ],
            test_command="powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
            repo_context={"mode": "fake_codegraph_context_v1", "evidence": "service exists"},
        )

        self.assertEqual(
            draft.structured["planning_mode"],
            "deterministic_graph_mapping_after_llm_parse_error_v1",
        )
        self.assertIn("Developer.plan LLM did not return valid JSON", draft.structured["developer_llm"]["parse_error"])
        self.assertTrue(draft.structured["implementation_steps"])

    def test_sync_run_resyncs_implementation_plan_checkpoint(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        synced = PhaseOneWorkflow(self.settings).sync_run(run_id=str(phase3["run_id"]))

        self.assertEqual(synced["run_id"], phase3["run_id"])
        self.assertEqual(synced["last_sync"]["phase"], "implementation_plan_review")
        self.assertEqual(synced["last_sync"]["status"], "mock_synced")
        self.assertIn("implementation plan", synced["summary_for_user"])

    def test_get_status_reads_phase3_state(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        status = PhaseOneWorkflow(self.settings).get_status(run_id=str(phase3["run_id"]))

        self.assertEqual(status["status"], "waiting_implementation_plan_review")
        self.assertEqual(status["pending_checkpoint"]["type"], "implementation_plan_review")
        self.assertEqual(status["last_sync"]["phase"], "implementation_plan_review")

    def test_real_sync_implementation_plan_writes_jira_and_confluence_targets(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        settings = self._settings(
            SDLC_SYNC_MODE="real",
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_EMAIL="test@example.com",
            ATLASSIAN_API_TOKEN="secret",
            JIRA_SYNC_ENABLED="1",
            JIRA_SYNC_ISSUE_KEY="SDLC-1",
            CONFLUENCE_SYNC_ENABLED="1",
            CONFLUENCE_SYNC_SPACE_ID="98315",
            CONFLUENCE_SYNC_PARENT_PAGE_ID="294923",
        )
        artifact_paths = {str(key): str(value) for key, value in phase3["latest_artifacts"].items()}
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if url.endswith("/wiki/api/v2/pages"):
                return {"id": "30003", "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            if "/comment" in url:
                return {"id": "10003"}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_implementation_plan_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(phase3["run_id"]),
            source_ref=str(phase3["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["phase"], "implementation_plan_review")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        confluence_payload = calls[0][2]
        self.assertIsNotNone(confluence_payload)
        confluence_body = confluence_payload["body"]["value"]
        self.assertIn("<h1>SDLC Harness - UC1 - Implementation Plan Checkpoint</h1>", confluence_body)
        self.assertIn(
            "SDLC-HARNESS-CHECKPOINT::implementation_plan::dataset_use_case:eTour:UC1",
            confluence_body,
        )
        self.assertIn("<h2>Decision Needed</h2>", confluence_body)
        self.assertIn("<h2>Execution Steps</h2>", confluence_body)
        self.assertIn("<h2>Code Targets</h2>", confluence_body)
        self.assertIn("<th>Code target</th>", confluence_body)
        self.assertNotIn("target_repo-worktrees", confluence_body)
        self.assertNotIn("<th>Local path</th>", confluence_body)
        self.assertIn("Implement class/method behavior and wire flow interactions.", confluence_body)
        self.assertIn("Raw Markdown Artifact", confluence_body)
        self.assertIn('ac:parameter ac:name="language">markdown', confluence_body)
        self.assertIn("Design-to-code mappings", confluence_body)
        self.assertIn("Implementation Plan V1", confluence_body)
        self.assertNotIn("<p>| Design element | Type | Planned code target | Reason | Test intent |</p>", confluence_body)
        jira_payload = calls[1][2]
        self.assertIsNotNone(jira_payload)
        jira_blocks = jira_payload["body"]["content"]
        self.assertIn("Implementation plan review", jira_blocks[0]["content"][0]["text"])
        summary_list = next(block for block in jira_blocks if block["type"] == "bulletList")
        self.assertTrue(
            any(
                "Confluence review page" in item["content"][0]["content"][0]["text"]
                for item in summary_list["content"]
            )
        )

    def test_real_sync_implementation_plan_resync_uses_put(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        settings = self._settings(
            SDLC_SYNC_MODE="real",
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_EMAIL="test@example.com",
            ATLASSIAN_API_TOKEN="secret",
            JIRA_SYNC_ENABLED="1",
            JIRA_SYNC_ISSUE_KEY="SDLC-1",
            CONFLUENCE_SYNC_ENABLED="1",
            CONFLUENCE_SYNC_SPACE_ID="98315",
            CONFLUENCE_SYNC_PARENT_PAGE_ID="294923",
        )
        artifact_paths = {str(key): str(value) for key, value in phase3["latest_artifacts"].items()}
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "GET" and "/wiki/api/v2/pages/" in url:
                return {"version": {"number": 7}, "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            if method == "PUT" and "/wiki/api/v2/pages/" in url:
                return {"id": "30003", "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            if method == "PUT" and "/comment/" in url:
                return {"id": "10003"}
            self.fail(f"Unexpected request: {method} {url}")

        store = PhaseOneWorkflow(self.settings).store
        sync_dir = self.runtime_root / "runs" / str(phase3["run_id"]) / "sync"
        (sync_dir / "tool_sync_report_v3.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "confluence", "status": "synced", "external_id": "30003"},
                        {"tool": "jira", "status": "synced", "external_id": "10003"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = real_sync_implementation_plan_checkpoint(
            store=store,
            settings=settings,
            run_id=str(phase3["run_id"]),
            source_ref=str(phase3["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["GET", "PUT", "PUT"])

    def test_real_sync_implementation_plan_ignores_phase2_targets(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        settings = self._settings(
            SDLC_SYNC_MODE="real",
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_EMAIL="test@example.com",
            ATLASSIAN_API_TOKEN="secret",
            JIRA_SYNC_ENABLED="1",
            JIRA_SYNC_ISSUE_KEY="SDLC-1",
            CONFLUENCE_SYNC_ENABLED="1",
            CONFLUENCE_SYNC_SPACE_ID="98315",
            CONFLUENCE_SYNC_PARENT_PAGE_ID="294923",
        )
        artifact_paths = {str(key): str(value) for key, value in phase3["latest_artifacts"].items()}
        sync_dir = self.runtime_root / "runs" / str(phase3["run_id"]) / "sync"
        (sync_dir / "tool_sync_report_v2.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "confluence", "status": "synced", "external_id": "30002"},
                        {"tool": "jira", "status": "synced", "external_id": "10002"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "POST" and url.endswith("/wiki/api/v2/pages"):
                return {"id": "30003", "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            if method == "POST" and "/comment" in url:
                return {"id": "10003"}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_implementation_plan_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(phase3["run_id"]),
            source_ref=str(phase3["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["targets"][0]["operation"], "created")
        self.assertEqual(report["targets"][1]["operation"], "created")

    def test_real_sync_implementation_plan_recreates_deleted_jira_comment(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        settings = self._settings(
            SDLC_SYNC_MODE="real",
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_EMAIL="test@example.com",
            ATLASSIAN_API_TOKEN="secret",
            JIRA_SYNC_ENABLED="1",
            JIRA_SYNC_ISSUE_KEY="SDLC-1",
            CONFLUENCE_SYNC_ENABLED="1",
            CONFLUENCE_SYNC_SPACE_ID="98315",
            CONFLUENCE_SYNC_PARENT_PAGE_ID="294923",
        )
        artifact_paths = {str(key): str(value) for key, value in phase3["latest_artifacts"].items()}
        sync_dir = self.runtime_root / "runs" / str(phase3["run_id"]) / "sync"
        (sync_dir / "tool_sync_report_v3.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "confluence", "status": "synced", "external_id": "30003"},
                        {"tool": "jira", "status": "synced", "external_id": "10003"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "GET" and "/wiki/api/v2/pages/" in url:
                return {"version": {"number": 7}, "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            if method == "PUT" and "/wiki/api/v2/pages/" in url:
                return {"id": "30003", "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            if method == "PUT" and "/comment/10003" in url:
                raise RuntimeError('Atlassian API HTTP 404: {"errorMessages":["Can not find a comment for the id: 10003."]}')
            if method == "POST" and "/comment" in url:
                return {"id": "10004"}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_implementation_plan_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(phase3["run_id"]),
            source_ref=str(phase3["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["GET", "PUT", "PUT", "POST"])
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["targets"][1]["operation"], "created")
        self.assertEqual(report["targets"][1]["external_id"], "10004")

    def test_real_sync_implementation_plan_partial_failure_keeps_artifacts(self) -> None:
        approved = self._create_phase2_run()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        settings = self._settings(
            SDLC_SYNC_MODE="real",
            ATLASSIAN_BASE_URL="https://example.atlassian.net",
            ATLASSIAN_EMAIL="test@example.com",
            ATLASSIAN_API_TOKEN="secret",
            JIRA_SYNC_ENABLED="1",
            JIRA_SYNC_ISSUE_KEY="SDLC-1",
            CONFLUENCE_SYNC_ENABLED="1",
            CONFLUENCE_SYNC_SPACE_ID="98315",
            CONFLUENCE_SYNC_PARENT_PAGE_ID="294923",
        )
        artifact_paths = {str(key): str(value) for key, value in phase3["latest_artifacts"].items()}

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            if url.endswith("/wiki/api/v2/pages"):
                return {"id": "30003", "_links": {"webui": "/spaces/SDLC/pages/30003/Implementation"}}
            raise RuntimeError("jira unavailable")

        result = real_sync_implementation_plan_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(phase3["run_id"]),
            source_ref=str(phase3["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "partial_failed")
        self.assertTrue((self.repo_root / str(phase3["latest_artifacts"]["implementation_plan"])).exists())

    def test_target_repo_guard_blocks_when_path_points_to_harness_repo(self) -> None:
        approved = self._create_phase2_run()
        settings = self._settings(SDLC_TARGET_REPO_PATH=str(self.repo_root))

        response = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "target_repo_is_harness")

    def test_target_repo_guard_blocks_dirty_worktree(self) -> None:
        approved = self._create_phase2_run()
        (self.target_repo / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_dirty")

    def test_phase3_initializes_codegraph_inside_run_workspace(self) -> None:
        approved = self._create_phase2_run()
        index_marker = self.target_repo / ".codegraph" / "README.md"
        index_marker.unlink()
        (self.target_repo / ".codegraph").rmdir()
        self._git(self.target_repo, "add", "-A")
        self._git(self.target_repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "remove codegraph")

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        workspace_path = Path(
            json.loads(
                (self.repo_root / response["latest_artifacts"]["run_workspace"]).read_text(encoding="utf-8")
            )["worktree_path"]
        )
        self.assertTrue((workspace_path / ".codegraph").is_dir())

    def test_target_repo_guard_blocks_remote_mismatch(self) -> None:
        approved = self._create_phase2_run()
        settings = self._settings(SDLC_TARGET_REPO_REMOTE="https://github.com/example/other.git")

        response = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_remote_mismatch")

    def test_missing_approved_design_artifact_is_rejected(self) -> None:
        approved = self._create_phase2_run()
        state = PhaseOneWorkflow(self.settings).store.read_state(str(approved["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("design_artifact")
        state["latest_artifacts"] = latest
        PhaseOneWorkflow(self.settings).store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseThreeWorkflow(self.settings).approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(ctx.exception.error_code, "approved_design_not_found")

    def test_validator_review_blocks_missing_design_mapping(self) -> None:
        approved = self._create_phase2_run()
        bad_plan = ImplementationPlanDraft(
            markdown="# Bad Implementation Plan\n",
            structured={
                "version": "implementation_plan_structured_v1",
                "run_id": str(approved["run_id"]),
                "source_ref": approved["source_ref"],
                "issue_key": "eTour-UC1",
                "target_repository": {
                    "local_path": str(self.target_repo),
                    "remote": self.remote_url,
                    "base_branch": "main",
                    "planned_branch": "feature/etour-uc1-implementation",
                },
                "design_mappings": [],
                "test_intents": [],
                "assumptions": [],
                "open_questions": [],
                "out_of_scope": [],
            },
        )

        with patch("sdlc_harness.workflows.phase3.DeveloperPlanAgent.plan", return_value=bad_plan):
            response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_plan_review_failed")
        report = response["quality_reports"]["implementation_plan_review"]
        self.assertFalse(report["ok"])
        self.assertEqual(report["mode"], "implementation_plan_review_hybrid_v2")
        self.assertEqual(report["llm_review"]["reason"], "rule_review_failed")
        self.assertTrue(report["coverage_issues"])
        self.assertTrue(report["executability_issues"])

    def test_developer_plan_failure_marks_run_failed_without_stale_checkpoint(self) -> None:
        approved = self._create_phase2_run()
        workflow = PhaseThreeWorkflow(self.settings)
        workflow.developer = FailingDeveloper()

        with self.assertRaises(HarnessError) as ctx:
            workflow.approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(ctx.exception.error_code, "implementation_plan_developer_failed")
        self.assertEqual(ctx.exception.run_id, approved["run_id"])
        state = workflow.store.read_state(str(approved["run_id"]))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["current_phase"], "implementation_planning")
        self.assertIsNone(state["pending_checkpoint"])
        self.assertEqual(state["allowed_next_actions"], ["get_status", "cancel_run"])
        self.assertEqual(state["last_error"]["error_code"], "implementation_plan_developer_failed")
        self.assertEqual(state["last_error"]["run_id"], approved["run_id"])

    def test_implementation_plan_repair_loop_passes_after_one_repair(self) -> None:
        approved = self._create_phase2_run()
        workflow = PhaseThreeWorkflow(self.settings)
        developer = RepairCountingDeveloper()
        validator = SequencedImplementationPlanValidator([False, True])
        workflow.developer = developer
        workflow.validator = validator

        response = workflow.approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        self.assertEqual(developer.repair_calls, 1)
        self.assertEqual(validator.calls, 2)
        self.assertTrue(response["latest_artifacts"]["implementation_plan"].endswith("implementation_plan_v2.md"))
        self.assertEqual(response["pending_checkpoint"]["artifact_ref"], "implementation_plan_v2")
        report = response["quality_reports"]["implementation_plan_review"]
        self.assertTrue(report["ok"])
        self.assertEqual(report["versions"], ["v1", "v2"])
        self.assertEqual(report["repair_iterations"], 1)

        run_dir = self.runtime_root / "runs" / str(approved["run_id"])
        self.assertTrue((run_dir / "artifacts" / "implementation_plan_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "implementation_plan_v2.md").exists())
        repaired_plan = (run_dir / "artifacts" / "implementation_plan_v2.md").read_text(encoding="utf-8")
        self.assertIn("Repair Feedback Addressed", repaired_plan)

    def test_implementation_plan_repair_loop_blocks_after_zero_limit(self) -> None:
        settings = self._settings(SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT="0")
        approved = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(approved["run_id"]))
        workflow = PhaseThreeWorkflow(settings)
        developer = RepairCountingDeveloper()
        validator = SequencedImplementationPlanValidator([False, True])
        workflow.developer = developer
        workflow.validator = validator

        response = workflow.approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_plan_review_failed")
        self.assertEqual(developer.repair_calls, 0)
        self.assertEqual(validator.calls, 1)
        self.assertTrue(response["latest_artifacts"]["implementation_plan"].endswith("implementation_plan_v1.md"))
        self.assertEqual(response["quality_reports"]["implementation_plan_review"]["versions"], ["v1"])
        self.assertFalse((self.runtime_root / "runs" / str(approved["run_id"]) / "sync" / "tool_sync_report_v3.json").exists())

    def test_validator_llm_invalid_json_blocks_with_review_report(self) -> None:
        settings = self._settings(SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT="0")
        approved = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(approved["run_id"]))
        workflow = PhaseThreeWorkflow(settings)
        workflow.validator = ImplementationPlanValidator(BadJsonLlm())

        response = workflow.approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "implementation_plan_review_failed")
        report = response["quality_reports"]["implementation_plan_review"]
        self.assertFalse(report["ok"])
        self.assertEqual(report["llm_review"]["parse_error"], "Implementation plan LLM review did not return valid JSON.")
        self.assertTrue(response["latest_artifacts"]["implementation_plan_review"].endswith("implementation_plan_review_v1.json"))

    def test_validator_review_detects_scope_violation(self) -> None:
        review = ImplementationPlanValidator().review(
            structured_plan={
                "target_repository": {
                    "local_path": str(self.target_repo),
                    "remote": self.remote_url,
                    "base_branch": "main",
                    "planned_branch": "feature/test",
                },
                "design_mappings": [
                    {
                        "design_element_id": "class:InventedService",
                        "design_element_name": "InventedService",
                        "planned_code_target": "src/main/java/com/sdlc/etour/heritage/InventedService.java",
                        "test_intent": "test invented behavior",
                    }
                ],
                "test_intents": [{"scope": "unit", "intent": "run tests", "command": "scripts/test.ps1"}],
            },
            requirement_design_trace={"class_trace": [], "sequence_trace": []},
            class_graph={"nodes": []},
            sequence_graph={"sequenceDiagrams": []},
        )

        self.assertFalse(review.ok)
        self.assertTrue(review.scope_issues)

    def test_v2_validator_hard_gates_malformed_requirement_coverage(self) -> None:
        review = ImplementationPlanValidator().review_rules(
            structured_plan={
                "target_repository": {
                    "local_path": str(self.target_repo),
                    "remote": self.remote_url,
                    "base_branch": "main",
                    "planned_branch": "feature/test",
                },
                "design_mappings": [
                    {
                        "design_element_id": "class:ApprovedService",
                        "design_element_name": "ApprovedService",
                        "planned_code_target": "src/main/java/ApprovedService.java",
                        "test_intent": "test approved behavior",
                    }
                ],
                "test_intents": [{"scope": "unit", "intent": "run tests", "command": "scripts/test.ps1"}],
                "requirement_coverage": [
                    {
                        "requirement_id": "R-1",
                        "content": "approved",
                        "design_targets": ["InventedService"],
                        "design_element_ids": [],
                        "planned_code_targets": [],
                        "test_intents": [],
                    },
                    {
                        "requirement_id": "R-UNKNOWN",
                        "content": "invented",
                        "design_targets": ["InventedService"],
                        "design_element_ids": [],
                        "planned_code_targets": [],
                        "test_intents": [],
                    }
                ],
            },
            requirement_design_trace={
                "class_trace": [
                    {"requirement": "R-1", "links": [{"target": "ApprovedService", "reason": "covered"}]}
                ],
                "sequence_trace": [{"requirement": "R-1", "links": []}],
            },
            class_graph={"nodes": [{"id": "ApprovedService", "className": "ApprovedService", "methods": []}]},
            sequence_graph={"sequenceDiagrams": []},
            requirement_graph={
                "requirements": [
                    {"id": "R-1", "content": "approved"},
                    {"id": "R-2", "content": "also approved"},
                ]
            },
            requirement_contract_version="v2",
        )

        self.assertFalse(review.ok)
        self.assertEqual(review.mode, "implementation_plan_rule_review_v2")
        self.assertTrue(any("Missing requirement coverage" in item for item in review.requirement_coverage_issues))
        self.assertTrue(any("invented unknown" in item for item in review.requirement_coverage_issues))
        self.assertTrue(any("outside its approved trace" in item for item in review.requirement_coverage_issues))
        self.assertTrue(any("no planned code target" in item for item in review.requirement_coverage_issues))
        self.assertTrue(any("no test intent" in item for item in review.requirement_coverage_issues))

    def test_direct_phase2_still_rejects_design_checkpoint(self) -> None:
        approved = self._create_phase2_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")


if __name__ == "__main__":
    unittest.main()
