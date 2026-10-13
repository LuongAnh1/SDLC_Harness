"""Test Phase 4 slice 4.1: implementation plan review -> DeveloperWorkPacket."""

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
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow


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

    def test_approve_implementation_plan_creates_developer_work_packet(self) -> None:
        phase3 = self._create_phase3_run()

        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=str(phase3["run_id"]))

        self.assertTrue(response["ok"])
        self.assertEqual(response["current_phase"], "implementation_preparing")
        self.assertEqual(response["status"], "implementation_ready")
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
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


if __name__ == "__main__":
    unittest.main()
