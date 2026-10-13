"""Test Phase 3 Slice 1 cho approved design -> implementation plan."""

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
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow


class PhaseThreeTest(unittest.TestCase):
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
        self._git(target, "init")
        self._git(target, "add", ".")
        self._git(target, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init")
        self._git(target, "branch", "-M", "main")
        self._git(target, "remote", "add", "origin", remote_url)
        return target

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8")

    def _create_phase2_run(self) -> dict[str, object]:
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        approved = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        self.assertEqual(approved["status"], "waiting_design_approval")
        return approved

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
        self.assertIn("planned branch", response["summary_for_user"])

        plan_path = self.repo_root / str(response["latest_artifacts"]["implementation_plan"])
        self.assertTrue(plan_path.exists())
        content = plan_path.read_text(encoding="utf-8")
        self.assertIn("Implementation Plan V1", content)
        self.assertIn(str(self.target_repo), content)
        self.assertIn(self.remote_url, content)
        self.assertIn("feature/etour-uc1-implementation", content)
        self.assertIn("src/main/java/com/sdlc/etour/heritage/CulturalHeritage.java", content)
        self.assertIn("No source code, branch, commit, test report, or pull request", content)

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
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "waiting_implementation_plan_review")
        self.assertEqual(payload["pending_checkpoint"]["type"], "implementation_plan_review")

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
        self.assertEqual(response["last_error"]["error_code"], "target_repo_dirty")

    def test_target_repo_guard_blocks_remote_mismatch(self) -> None:
        approved = self._create_phase2_run()
        settings = self._settings(SDLC_TARGET_REPO_REMOTE="https://github.com/example/other.git")

        response = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["last_error"]["error_code"], "target_repo_remote_mismatch")

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

    def test_direct_phase2_still_rejects_design_checkpoint(self) -> None:
        approved = self._create_phase2_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(approved["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")


if __name__ == "__main__":
    unittest.main()
