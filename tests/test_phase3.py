"""Kiểm thử Phase 3 cho thiết kế đã duyệt -> review implementation plan."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytest

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.developer import DeveloperPlanAgent, ImplementationPlanDraft
from sdlc_harness.agents.developer.planner import _parse_developer_plan_payload
from sdlc_harness.agents.validator import (
    ImplementationPlanReview,
    ImplementationPlanValidator,
)
from sdlc_harness.agents.validator.typescript_ast import TypeScriptAstEvidence
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import (
    build_planning_path_policy,
    build_verification_stage_policy,
    get_language_profile,
    get_project_profile,
)
from sdlc_harness.verification import VerificationResult
from sdlc_harness.sync.atlassian import real_sync_implementation_plan_checkpoint
from sdlc_harness.sync.atlassian_rendering import _confluence_implementation_plan_storage_body
from sdlc_harness.sync.github import GitHubCodeReviewResult
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow
from sdlc_harness.workspaces import RunWorkspaceManager
from tests._support import (
    GitRepositoryTemplate,
    RuntimeDirectoryTemplate,
    create_fast_run_workspace,
    populate_typescript_target,
)


pytestmark = pytest.mark.integration


def _populate_phase3_target(target: Path) -> None:
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
    (target / ".codegraph" / "README.md").write_text(
        "test codegraph index placeholder\n",
        encoding="utf-8",
    )


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


class WorkPacketTypeScriptAstExtractor:
    """Test double giữ contract AST nhưng không cần cài TypeScript trong temp repo."""

    def __init__(self, work_packet: dict[str, object]) -> None:
        self.work_packet = work_packet

    def extract(self, *, target_path, source_paths, policy, run_id=None):
        del target_path, run_id
        symbols_by_file: dict[str, list[dict[str, object]]] = {
            str(path): [] for path in source_paths
        }
        contract = self.work_packet.get("implementation_signature_contract")
        entries = contract.get("entries", []) if isinstance(contract, dict) else []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("source_path") or "")
            if path not in symbols_by_file:
                continue
            symbol = dict(raw)
            symbol.setdefault("name", str(raw.get("qualified_name") or "").split(".")[-1])
            symbol.setdefault("kind", str(raw.get("symbol_kind") or ""))
            symbols_by_file[path].append(symbol)
        mappings = self.work_packet.get("design_mappings_by_file")
        if isinstance(mappings, dict):
            for path, items in mappings.items():
                if path not in symbols_by_file or not isinstance(items, list):
                    continue
                known = {
                    str(item.get("qualified_name") or "")
                    for item in symbols_by_file[path]
                }
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    target = str(item.get("planned_code_target") or "")
                    _, separator, name = target.partition("#")
                    if not separator or not name or name in known:
                        continue
                    symbols_by_file[path].append(
                        {"name": name.split(".")[-1], "qualified_name": name, "kind": "class"}
                    )
        files = tuple(
            {"path": path, "symbols": symbols}
            for path, symbols in symbols_by_file.items()
        )
        return TypeScriptAstEvidence(
            extractor_id=policy.symbol_extractor_id,
            profile=policy.metadata(),
            files=files,
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

    def run(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        required_test_files=None,
        verification_policy=None,
        qa_policy=None,
    ):
        del work_packet, verification_policy, qa_policy
        status = self.statuses.pop(0) if self.statuses else "passed"
        selected = tuple(str(path) for path in (required_test_files or []))
        return VerificationResult(
            status=status,
            command="fixture verification runner",
            cwd=str(target_path),
            exit_code=0 if status == "passed" else 1,
            duration_seconds=0.01,
            stdout="fixture verification passed" if status == "passed" else "",
            stderr="fixture verification failed" if status == "failed" else "",
            reason=f"Fixture verification {status}.",
            selected_test_files=selected,
            verified_test_files=selected if status == "passed" else (),
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
    ISOLATED_REPOSITORY_TESTS = {
        "test_phase3_initializes_codegraph_inside_run_workspace",
        "test_profile_manifest_mismatch_blocks_before_codegraph_and_developer",
        "test_profiled_phase3_discovers_typescript_for_plan_and_revalidation",
        "test_target_repo_guard_blocks_dirty_worktree",
    }
    JOURNEY_TESTS = {
        "test_v2_phase4_runs_canonical_ids_through_qa",
        "test_v2_phase4_runs_to_code_review_approved_with_stable_ids",
        "test_v2_phase4_repair_loops_preserve_contract_and_stable_ids",
    }
    REAL_WORKSPACE_PROVISIONING_TESTS = JOURNEY_TESTS | {
        "test_profile_manifest_mismatch_blocks_before_codegraph_and_developer",
        "test_profiled_phase3_discovers_typescript_for_plan_and_revalidation",
        "test_target_repo_guard_blocks_dirty_worktree",
        "test_target_repo_guard_blocks_remote_mismatch",
        "test_target_repo_guard_blocks_when_path_points_to_harness_repo",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.git_template = GitRepositoryTemplate(populate_typescript_target)
        cls.runtime_template_temp = tempfile.TemporaryDirectory()
        template_root = Path(cls.runtime_template_temp.name)
        topology = cls.git_template.materialize(template_root)
        cls.shared_topology = topology
        runtime_root = template_root / ".runtime"
        settings = load_settings(
            root=Path(__file__).resolve().parents[1],
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_REPAIR_LIMIT": "2",
                "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT": "2",
                "SDLC_PROJECT_PROFILE": "",
                "SDLC_TARGET_REPO_PATH": str(topology.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(topology.remote.resolve()),
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))
        cls.phase2_run_id = str(approved["run_id"])
        phase2_workspace_ref = Path(str(approved["latest_artifacts"]["run_workspace"]))
        if not phase2_workspace_ref.is_absolute():
            phase2_workspace_ref = Path(__file__).resolve().parents[1] / phase2_workspace_ref
        phase2_workspace_payload = json.loads(phase2_workspace_ref.read_text(encoding="utf-8"))
        cls.phase2_template_canonical = str(phase2_workspace_payload["canonical_repo"])
        cls.phase2_template_remote = str(phase2_workspace_payload["remote"])
        cls.phase2_template_worktree = str(phase2_workspace_payload["worktree_path"])
        cls.phase2_runtime_template = RuntimeDirectoryTemplate(runtime_root)
        phase3 = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=cls.phase2_run_id)
        cls.phase3_run_id = str(phase3["run_id"])
        workspace_ref = Path(str(phase3["latest_artifacts"]["run_workspace"]))
        if not workspace_ref.is_absolute():
            workspace_ref = Path(__file__).resolve().parents[1] / workspace_ref
        workspace_payload = json.loads(workspace_ref.read_text(encoding="utf-8"))
        cls.phase3_template_canonical = str(workspace_payload["canonical_repo"])
        cls.phase3_template_remote = str(workspace_payload["remote"])
        cls.phase3_template_worktree = str(workspace_payload["worktree_path"])
        cls.phase3_runtime_template = RuntimeDirectoryTemplate(runtime_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.phase3_runtime_template.close()
        cls.phase2_runtime_template.close()
        cls.runtime_template_temp.cleanup()
        cls.git_template.close()

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        if self._testMethodName in self.ISOLATED_REPOSITORY_TESTS:
            topology = self.git_template.materialize(Path(self.temp_dir.name))
        else:
            topology = self.shared_topology
        self.remote_path = topology.remote
        self.remote_url = str(self.remote_path.resolve())
        self.target_repo = topology.canonical
        suffix = hashlib.sha256(self._testMethodName.encode("utf-8")).hexdigest()[:8]
        self.run_id = f"run_20261020_000000_{suffix}"
        self.settings = self._settings()
        if self._testMethodName not in self.JOURNEY_TESTS:
            # Workspace suite và ba journey giữ probe Git thật; các test còn lại
            # tập trung vào contract planning/state nên không lặp identity probe.
            validation_patch = patch.object(RunWorkspaceManager, "_validate_git", autospec=True, return_value=None)
            validation_patch.start()
            self.addCleanup(validation_patch.stop)
        if self._testMethodName not in self.REAL_WORKSPACE_PROVISIONING_TESTS:
            creation_patch = patch.object(
                RunWorkspaceManager,
                "_create_locked",
                autospec=True,
                side_effect=lambda manager, *, run_id, state: create_fast_run_workspace(
                    manager,
                    run_id=run_id,
                    state=state,
                ),
            )
            creation_patch.start()
            self.addCleanup(creation_patch.stop)

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

    def _install_profile_manifest(self, *, project_profile_id: str = "typescript_fullstack_v1") -> None:
        manifest = self.target_repo / ".sdlc" / "target-profile.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["project_profile_id"] = project_profile_id
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if not self._git_output(self.target_repo, "status", "--porcelain"):
            return
        self._git(self.target_repo, "add", ".sdlc/target-profile.json")
        self._git(
            self.target_repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "add target profile manifest",
        )
        self._git(self.target_repo, "push", "origin", "main")

    def _create_phase2_run(self) -> dict[str, object]:
        manager = RunWorkspaceManager(self.settings)
        worktree_path = manager.expected_worktree_path(self.run_id)
        self.phase2_runtime_template.materialize(
            self.runtime_root,
            replacements={
                self.phase2_run_id: self.run_id,
                self.phase2_run_id[-8:]: self.run_id[-8:],
                self.phase2_template_canonical: str(self.target_repo.resolve()),
                self.phase2_template_remote: self.remote_url,
                self.phase2_template_worktree: str(worktree_path),
            },
        )
        state = manager.store.read_state(self.run_id)
        create_fast_run_workspace(manager, run_id=self.run_id, state=state)
        approved = PhaseOneWorkflow(self.settings).get_status(run_id=self.run_id)
        self.assertEqual(approved["status"], "waiting_design_approval")
        return approved

    def _create_phase2_run_fresh(self) -> dict[str, object]:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        self.assertEqual(approved["status"], "waiting_design_approval")
        return approved

    def _create_phase3_run_from_template(self) -> dict[str, object]:
        manager = RunWorkspaceManager(self.settings)
        worktree_path = manager.expected_worktree_path(self.run_id)
        self.phase3_runtime_template.materialize(
            self.runtime_root,
            replacements={
                self.phase3_run_id: self.run_id,
                self.phase3_run_id[-8:]: self.run_id[-8:],
                self.phase3_template_canonical: str(self.target_repo.resolve()),
                self.phase3_template_remote: self.remote_url,
                self.phase3_template_worktree: str(worktree_path),
            },
        )
        state = manager.store.read_state(self.run_id)
        create_fast_run_workspace(manager, run_id=self.run_id, state=state)
        return PhaseOneWorkflow(self.settings).get_status(run_id=self.run_id)

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
        self.assertIn("Implementation Plan V4", content)
        self.assertIn("Component Work Units", content)
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
        self.assertIn("CulturalHeritage.execute", content)
        self.assertIn("Test intent", content)
        self.assertIn("Per-unit Developer execution remains L5.2", content)

        structured_path = self.repo_root / str(response["latest_artifacts"]["implementation_plan_structured"])
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
        self.assertEqual(structured["version"], "implementation_plan_structured_v4")
        self.assertEqual(structured["planning_mode"], "llm_component_aware_plan_v1")
        self.assertTrue(structured["work_units"])
        self.assertEqual(structured["repo_context"]["mode"], "fake_codegraph_context_v1")
        self.assertTrue(structured["implementation_steps"])
        self.assertTrue(structured["proposed_files"])
        self.assertTrue(structured["design_mappings"])
        self.assertIn(
            "method:CulturalHeritage.execute",
            {item["design_element_id"] for item in structured["design_mappings"]},
        )

        review_path = self.repo_root / str(response["latest_artifacts"]["implementation_plan_review"])
        review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertTrue(review["ok"])
        self.assertEqual(review["mode"], "implementation_plan_review_hybrid_v2")
        self.assertTrue(review["rule_review"]["ok"])
        self.assertTrue(review["llm_review"]["ok"])
        self.assertEqual(review["repair_iterations"], 0)
        self.assertEqual(
            review["implementation_plan_contract_version"],
            "implementation_plan_structured_v4",
        )
        self.assertRegex(review["implementation_plan_sha256"], r"^sha256:[0-9a-f]{64}$")

        sync_report_path = self.repo_root / str(response["last_sync"]["report"])
        sync_report = json.loads(sync_report_path.read_text(encoding="utf-8"))
        self.assertEqual(sync_report["version"], "tool_sync_report_v3")
        self.assertEqual(sync_report["phase"], "implementation_plan_review")
        self.assertEqual(sync_report["checkpoint"]["type"], "implementation_plan_review")
        self.assertEqual({target["tool"] for target in sync_report["targets"]}, {"jira", "confluence"})
        self.assertNotIn("github", {target["tool"] for target in sync_report["targets"]})

    def test_profile_manifest_mismatch_blocks_before_codegraph_and_developer(self) -> None:
        self._install_profile_manifest(project_profile_id="other_profile_v1")
        settings = self._settings(SDLC_PROJECT_PROFILE="typescript_fullstack_v1")
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        with (
            patch("sdlc_harness.repo_context.codegraph.CodeGraphContextProvider.query_readonly") as query_readonly,
            patch.object(DeveloperPlanAgent, "plan") as developer_plan,
        ):
            response = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "repo_discovery_blocked")
        self.assertEqual(response["last_error"]["error_code"], "target_profile_manifest_mismatch")
        query_readonly.assert_not_called()
        developer_plan.assert_not_called()

    def test_profiled_phase3_discovers_typescript_for_plan_and_revalidation(self) -> None:
        self._install_profile_manifest()
        source_path = self.target_repo / "apps" / "api" / "src" / "ExistingService.ts"
        source_path.write_text("export class ExistingService {}\n", encoding="utf-8")
        self._git(self.target_repo, "add", ".")
        self._git(
            self.target_repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "add TypeScript planning source",
        )
        self._git(self.target_repo, "push", "origin", "main")
        settings = self._settings(SDLC_PROJECT_PROFILE="typescript_fullstack_v1")
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        planned = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(planned["status"], "waiting_implementation_plan_review")
        structured_path = self.repo_root / str(
            planned["latest_artifacts"]["implementation_plan_structured"]
        )
        structured = json.loads(structured_path.read_text(encoding="utf-8"))
        self.assertEqual(structured["version"], "implementation_plan_structured_v4")
        self.assertIn("apps/api/src/ExistingService.ts", structured["repo_context"]["source_files"])
        self.assertTrue(
            all(path.endswith((".ts", ".tsx")) for path in structured["repo_context"]["source_files"])
        )
        self.assertEqual(
            structured["planning_profile"],
            {
                "project_profile_id": "typescript_fullstack_v1",
                "language_profile_id": "typescript_v1",
                "policy_version": "planning_path_policy_v1",
            },
        )
        self.assertEqual(
            structured["verification_stage_ids"],
            list(
                build_verification_stage_policy(
                    project=get_project_profile("typescript_fullstack_v1")
                ).stage_ids
            ),
        )
        self.assertTrue(structured["test_intents"])
        self.assertTrue(all("command" not in item for item in structured["test_intents"]))
        self.assertTrue(
            all(
                item.get("stage_id") in structured["verification_stage_ids"]
                for item in structured["test_intents"]
            )
        )
        plan_markdown = (
            self.repo_root / str(planned["latest_artifacts"]["implementation_plan"])
        ).read_text(encoding="utf-8")
        signature_contract = structured["implementation_signature_contract"]
        self.assertEqual(signature_contract["version"], "implementation_signature_contract_v1")
        self.assertTrue(signature_contract["digest"].startswith("sha256:"))
        self.assertTrue(signature_contract["entries"])
        self.assertIn("# Implementation Plan V4", plan_markdown)
        self.assertIn("## Approved Implementation Signatures", plan_markdown)
        self.assertIn("## Verification Plan", plan_markdown)
        self.assertIn("Stage `unit`", plan_markdown)
        self.assertNotIn("scripts/test.ps1", plan_markdown)
        confluence_body = _confluence_implementation_plan_storage_body(
            PhaseOneWorkflow(settings).store,
            str(planned["run_id"]),
            str(planned["source_ref"]),
            {str(key): str(value) for key, value in planned["latest_artifacts"].items()},
        )
        self.assertIn("<th>Verification stages</th>", confluence_body)
        self.assertIn("<th>Stage ID</th>", confluence_body)
        self.assertNotIn("<th>Command</th>", confluence_body)
        self.assertNotIn("scripts/test.ps1", confluence_body)
        self.assertTrue(structured["design_mappings"])
        self.assertTrue(
            all(
                item.get("component_id") in {"web", "api", "contracts", "database"}
                and ".java" not in str(item.get("planned_code_target") or "")
                for item in structured["design_mappings"]
            )
        )
        self.assertTrue(
            all(
                item.get("component_id") in {"web", "api", "contracts", "database", "e2e"}
                and ".java" not in str(item.get("path") or "")
                for item in structured["proposed_files"]
            )
        )

        revalidated = PhaseThreeWorkflow(settings).revalidate_implementation_plan(
            run_id=str(approved["run_id"])
        )

        self.assertEqual(revalidated["status"], "waiting_implementation_plan_review")
        revalidated_path = self.repo_root / str(
            revalidated["latest_artifacts"]["implementation_plan_structured"]
        )
        revalidated_plan = json.loads(revalidated_path.read_text(encoding="utf-8"))
        self.assertIn(
            "apps/api/src/ExistingService.ts",
            revalidated_plan["repo_context"]["source_files"],
        )
        self.assertTrue(
            all(
                ".java" not in str(item.get("planned_code_target") or "")
                for item in revalidated_plan["design_mappings"]
            )
        )
        self.assertEqual(
            revalidated_plan["verification_stage_ids"],
            structured["verification_stage_ids"],
        )
        self.assertEqual(
            revalidated_plan["implementation_signature_contract"]["version"],
            "implementation_signature_contract_v1",
        )
        self.assertTrue(all("command" not in item for item in revalidated_plan["test_intents"]))

    def test_v2_phase3_creates_canonical_requirement_coverage(self) -> None:
        approved = self._create_v2_phase2_run()
        response = PhaseThreeWorkflow(self.settings).approve_design_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "waiting_implementation_plan_review", response)
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
        state_after_phase3 = workflow.store.read_state(run_id)
        self.assertIn("profile_snapshot", state_after_phase3, state_after_phase3)
        response = workflow.approve_implementation_plan_checkpoint(run_id=run_id)

        self.assertEqual(response["status"], "implementation_ready", response)
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
        self.assertEqual(packet["output_contract"]["format"], "changed_file_blocks_v1")
        self.assertEqual(packet["output_contract"]["required_fields"], ["path", "content"])

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

        self.assertEqual(caught.exception.error_code, "implementation_plan_review_binding_invalid")
        self.assertEqual(workflow.store.read_state(run_id), state_before)

    @pytest.mark.journey
    def test_v2_phase4_runs_canonical_ids_through_qa(self) -> None:
        approved = self._create_phase2_run_fresh()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        packet = json.loads(
            (self.repo_root / response["latest_artifacts"]["developer_work_packet"]).read_text(
                encoding="utf-8"
            )
        )
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator.symbol_extractor = WorkPacketTypeScriptAstExtractor(packet)

        for _ in range(3):
            response = workflow.continue_run(run_id=run_id)
            self.assertNotEqual(response["status"], "blocked", response)
        workflow.verification_runner.run = lambda **kwargs: VerificationResult(
            status="passed",
            command="fixture QA runner",
            cwd=str(kwargs["target_path"]),
            exit_code=0,
            duration_seconds=0.01,
            stdout="fixture QA passed",
            stderr="",
            reason="Fixture QA execution passed.",
            selected_test_files=tuple(kwargs.get("required_test_files") or []),
            verified_test_files=tuple(kwargs.get("required_test_files") or []),
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
        self.assertEqual(trace["mode"], "hybrid_rule_llm_code_trace_v3")
        self.assertEqual(trace_ids, qa_ids)
        self.assertEqual(trace["stats"]["requirements_covered"], trace["stats"]["requirement_count"])

    @pytest.mark.journey
    def test_v2_phase4_runs_to_code_review_approved_with_stable_ids(self) -> None:
        approved = self._create_phase2_run_fresh()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        packet = json.loads(
            (self.repo_root / response["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        canonical_ids = {item["id"] for item in packet["canonical_requirements"]}
        revision = packet["requirement_contract"]["revision"]
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator.symbol_extractor = WorkPacketTypeScriptAstExtractor(packet)
        workflow.verification_runner = SequenceVerificationRunner(["passed", "passed"])

        statuses = []
        for _ in range(4):
            response = workflow.continue_run(run_id=run_id)
            self.assertNotEqual(response["status"], "blocked", response)
            statuses.append(response["status"])
        self.assertEqual(
            statuses,
            ["implementation_written", "implementation_tested", "implementation_traced", "qa_tested"],
        )

        workflow.github_publisher = RecordingPublisher()
        with patch(
            "sdlc_harness.workflows.phase4_publication.github_repository_from_remote",
            return_value="example/target",
        ):
            published = workflow.continue_run(run_id=run_id)
        ready = workflow.continue_run(run_id=run_id)
        workflow.github_approver = RecordingApprover()
        with patch(
            "sdlc_harness.workflows.phase4_publication.github_repository_from_remote",
            return_value="example/target",
        ):
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

    @pytest.mark.journey
    def test_v2_phase4_repair_loops_preserve_contract_and_stable_ids(self) -> None:
        approved = self._create_phase2_run_fresh()
        phase3 = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))
        run_id = str(phase3["run_id"])
        ready = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=run_id)
        initial_packet = json.loads(
            (self.repo_root / ready["latest_artifacts"]["developer_work_packet"]).read_text(encoding="utf-8")
        )
        canonical_ids = {item["id"] for item in initial_packet["canonical_requirements"]}
        revision = initial_packet["requirement_contract"]["revision"]
        workflow = PhaseFourWorkflow(self.settings)
        workflow.code_trace_validator.symbol_extractor = WorkPacketTypeScriptAstExtractor(initial_packet)
        workflow.verification_runner = SequenceVerificationRunner(["passed"])

        written = workflow.continue_run(run_id=run_id)
        tested = workflow.continue_run(run_id=run_id)
        workspace_path = Path(RunWorkspaceManager(self.settings).resolve(run_id).worktree_path)
        missing_test = workspace_path / "apps" / "api" / "src" / "DeleteCulturalHeritageFlow.test.ts"
        missing_test.unlink()

        repair_needed = workflow.continue_run(run_id=run_id)
        self.assertEqual(repair_needed["status"], "implementation_repair_needed", repair_needed)
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

    def test_cli_approve_checkpoint_rejects_legacy_design_run(self) -> None:
        approved = self._create_phase2_run()
        state = PhaseOneWorkflow(self.settings).store.read_state(str(approved["run_id"]))
        state.pop("profile_snapshot", None)
        PhaseOneWorkflow(self.settings).store.write_state(state)
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
        self.assertEqual(code, 1)
        self.assertEqual(payload["error_code"], "legacy_execution_profile_retired")
        self.assertEqual(payload["run_id"], approved["run_id"])

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
        project = get_project_profile("typescript_fullstack_v1")
        planning_policy = build_planning_path_policy(
            project=project,
            language=get_language_profile(project.language_profile_id),
        )
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
                "apps/api/src/DeleteCulturalHeritageService.ts"
            ],
            repo_context={"mode": "fake_codegraph_context_v1", "evidence": "service exists"},
            planning_policy=planning_policy,
            verification_policy=build_verification_stage_policy(project=project),
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
        jira_payload = calls[1][2]
        self.assertIsNotNone(jira_payload)
        self.assertNotIn(
            str(self.runtime_root).replace("\\", "\\\\"),
            json.dumps(jira_payload, ensure_ascii=False),
        )
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
        self.assertIn("Implement or update methods from the class diagram.", confluence_body)
        self.assertIn("Raw Markdown Artifact", confluence_body)
        self.assertIn('ac:parameter ac:name="language">markdown', confluence_body)
        self.assertIn("Design-to-code mappings", confluence_body)
        self.assertIn("Implementation Plan V4", confluence_body)
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
        settings = self._settings(SDLC_TARGET_REPO_PATH=str(self.repo_root))
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

        response = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "repo_discovery_blocked")
        self.assertEqual(response["last_error"]["error_code"], "target_repo_is_harness")

    def test_target_repo_guard_blocks_dirty_worktree(self) -> None:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        (self.target_repo / "dirty.txt").write_text("dirty", encoding="utf-8")

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "repo_discovery_blocked")
        self.assertEqual(response["last_error"]["error_code"], "workspace_dirty")

    def test_phase3_initializes_codegraph_inside_run_workspace(self) -> None:
        approved = self._create_phase2_run()
        index_marker = self.target_repo / ".codegraph" / "README.md"
        if index_marker.exists():
            index_marker.unlink()
            (self.target_repo / ".codegraph").rmdir()
            self._git(self.target_repo, "add", "-A")
            self._git(
                self.target_repo,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "remove codegraph",
            )

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
        self.assertTrue(report["coverage_issues"] or report["executability_issues"])
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

    def test_component_routing_failure_returns_run_to_design_revalidation(self) -> None:
        approved = self._create_phase2_run()
        workflow = PhaseThreeWorkflow(self.settings)
        state = workflow.store.read_state(str(approved["run_id"]))

        workflow._mark_phase3_failed(
            state,
            run_id=str(approved["run_id"]),
            error=HarnessError(
                "implementation_target_unclassified",
                "CulturalHeritage has no component.",
            ),
        )

        recovered = workflow.store.read_state(str(approved["run_id"]))
        self.assertEqual(recovered["current_phase"], "design_review")
        self.assertEqual(recovered["status"], "blocked")
        self.assertIsNone(recovered["pending_checkpoint"])
        self.assertEqual(
            recovered["allowed_next_actions"],
            ["revalidate_design", "get_status", "request_change", "cancel_run"],
        )
        self.assertEqual(recovered["last_error"]["error_code"], "implementation_target_unclassified")

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
