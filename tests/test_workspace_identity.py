"""Khóa contract Slice 4.9A1 trước khi workflow tạo Git worktree thật."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workspaces import RunWorkspaceManager, RunWorkspaceRecord, branch_name_for_run


class WorkspaceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.repo_root = self.base / "SDLC_Harness"
        self.repo_root.mkdir()
        self.target_repo = self.base / "SDLC_Target_eTour"
        self.target_repo.mkdir()
        self.worktree_root = self.base / "run-worktrees"
        self.runtime_root = self.repo_root / ".runtime"
        self.remote = "https://github.com/example/target.git"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_TARGET_REPO_PATH": str(self.target_repo),
                "SDLC_TARGET_WORKTREE_ROOT": str(self.worktree_root),
                "SDLC_TARGET_REPO_REMOTE": self.remote,
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_IMPLEMENTATION_BRANCH_PREFIX": "feature",
            },
        )
        self.store = RuntimeStore(repo_root=self.repo_root, runtime_root=self.runtime_root)
        self.manager = RunWorkspaceManager(self.settings)
        self.run_id = "run_20261015_120000_a1b2c3d4"
        self.source_ref = "jira_issue:SDLC-1"
        self.store.write_state({"run_id": self.run_id, "source_ref": self.source_ref, "status": "active"})

    def _record(self, **changes: object) -> RunWorkspaceRecord:
        record = RunWorkspaceRecord(
            run_id=self.run_id,
            source_ref=self.source_ref,
            canonical_repo=str(self.target_repo.resolve()),
            worktree_path=str(self.manager.expected_worktree_path(self.run_id)),
            branch=branch_name_for_run(
                prefix=self.settings.implementation_branch_prefix,
                source_ref=self.source_ref,
                issue_key="SDLC-1",
                run_id=self.run_id,
            ),
            base_ref="origin/main",
            base_sha="a" * 40,
            head_sha="b" * 40,
            remote=self.remote,
            lifecycle="active",
            provisioning_mode="created_for_run",
            created_at="2026-10-15T05:00:00Z",
            updated_at="2026-10-15T05:00:00Z",
        )
        return replace(record, **changes)

    def test_branch_name_is_deterministic_and_unique_per_run(self) -> None:
        first = branch_name_for_run(
            prefix="feature/",
            source_ref=self.source_ref,
            run_id=self.run_id,
        )
        repeated = branch_name_for_run(
            prefix="feature/",
            source_ref=self.source_ref,
            run_id=self.run_id,
        )
        second = branch_name_for_run(
            prefix="feature",
            source_ref=self.source_ref,
            run_id="run_20261015_120001_deadbeef",
        )

        self.assertEqual(first, "feature/sdlc-1-a1b2c3d4")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, second)

    def test_branch_name_normalizes_unicode_source(self) -> None:
        branch = branch_name_for_run(
            prefix="feature",
            source_ref="fixture:Yêu cầu đặt chuyến đi",
            run_id=self.run_id,
        )

        self.assertEqual(branch, "feature/yeu-cau-dat-chuyen-di-a1b2c3d4")

    def test_invalid_run_id_and_prefix_are_rejected(self) -> None:
        with self.assertRaises(HarnessError) as run_ctx:
            self.manager.expected_worktree_path("../other-run")
        self.assertEqual(run_ctx.exception.error_code, "run_workspace_invalid")

        with self.assertRaises(HarnessError) as prefix_ctx:
            branch_name_for_run(prefix="feature//unsafe", source_ref=self.source_ref, run_id=self.run_id)
        self.assertEqual(prefix_ctx.exception.error_code, "run_workspace_invalid")

    def test_config_default_is_sibling_and_does_not_create_directory(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_TARGET_REPO_PATH": str(self.target_repo)},
        )
        expected = self.target_repo.parent / f"{self.target_repo.name}-worktrees"

        self.assertEqual(settings.target_worktree_root, expected.resolve())
        self.assertFalse(expected.exists())

    def test_config_relative_override_resolves_from_harness_root(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_TARGET_REPO_PATH": str(self.target_repo),
                "SDLC_TARGET_WORKTREE_ROOT": "local-worktrees",
            },
        )

        self.assertEqual(settings.target_worktree_root, (self.repo_root / "local-worktrees").resolve())
        self.assertFalse(settings.target_worktree_root.exists())

    def test_workspace_artifact_round_trip(self) -> None:
        path = self.manager.write(self._record())

        loaded = self.manager.read(self.run_id)

        self.assertEqual(path.name, "run_workspace_v1.json")
        self.assertEqual(loaded, self._record())
        self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_missing_and_corrupt_artifact_fail_closed(self) -> None:
        with self.assertRaises(HarnessError) as missing_ctx:
            self.manager.read(self.run_id)
        self.assertEqual(missing_ctx.exception.error_code, "run_workspace_missing")

        path = self.manager.artifact_path(self.run_id)
        path.parent.mkdir(parents=True)
        path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(HarnessError) as invalid_ctx:
            self.manager.read(self.run_id)
        self.assertEqual(invalid_ctx.exception.error_code, "run_workspace_invalid")

    def test_artifact_run_id_mismatch_is_rejected(self) -> None:
        path = self.manager.artifact_path(self.run_id)
        path.parent.mkdir(parents=True)
        payload = self._record().to_dict()
        payload["run_id"] = "run_20261015_120001_deadbeef"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(HarnessError) as ctx:
            self.manager.read(self.run_id)
        self.assertEqual(ctx.exception.error_code, "run_workspace_mismatch")

    def test_artifact_version_and_required_fields_are_rejected(self) -> None:
        path = self.manager.artifact_path(self.run_id)
        path.parent.mkdir(parents=True)
        cases = []
        wrong_version = self._record().to_dict()
        wrong_version["version"] = "run_workspace_v0"
        cases.append(wrong_version)
        missing_remote = self._record().to_dict()
        del missing_remote["remote"]
        cases.append(missing_remote)
        missing_provisioning_mode = self._record().to_dict()
        del missing_provisioning_mode["provisioning_mode"]
        cases.append(missing_provisioning_mode)
        invalid_provisioning_mode = self._record().to_dict()
        invalid_provisioning_mode["provisioning_mode"] = "unknown"
        cases.append(invalid_provisioning_mode)

        for payload in cases:
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(HarnessError) as ctx:
                    self.manager.read(self.run_id)
                self.assertEqual(ctx.exception.error_code, "run_workspace_invalid")

    def test_path_remote_and_branch_mismatches_are_rejected(self) -> None:
        cases = (
            (self._record(canonical_repo=str(self.base / "other-repo")), "run_workspace_mismatch"),
            (self._record(worktree_path=str(self.base / "outside" / self.run_id)), "workspace_path_invalid"),
            (self._record(remote="https://github.com/example/other.git"), "workspace_remote_mismatch"),
            (self._record(branch="feature/sdlc-1-wrongrun"), "workspace_branch_mismatch"),
        )
        for record, error_code in cases:
            with self.subTest(error_code=error_code), self.assertRaises(HarnessError) as ctx:
                self.manager.write(record)
            self.assertEqual(ctx.exception.error_code, error_code)

    def test_runtime_source_mismatch_is_rejected_before_write(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            self.manager.write(self._record(source_ref="jira_issue:OTHER-2"))
        self.assertEqual(ctx.exception.error_code, "run_workspace_mismatch")
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())

    def test_same_run_lock_fails_fast_and_other_run_can_lock(self) -> None:
        other_run = "run_20261015_120001_deadbeef"
        with self.manager.run_lock(self.run_id):
            with self.assertRaises(HarnessError) as ctx:
                with self.manager.run_lock(self.run_id):
                    pass
            self.assertEqual(ctx.exception.error_code, "run_workspace_locked")
            with self.manager.run_lock(other_run):
                pass

    def test_lock_is_released_after_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with self.manager.registry_lock():
                raise RuntimeError("boom")

        with self.manager.registry_lock():
            pass


if __name__ == "__main__":
    unittest.main()
