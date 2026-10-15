"""Kiểm thử engine và isolation Git worktree bằng remote cục bộ.

Suite này khóa các bất biến của Slice 4.9A2/A3: mỗi run có branch/worktree riêng,
base SHA lấy sau khi fetch remote, identity được xác minh fail-closed, hai run
không ghi chéo file và rollback không xóa workspace nếu không chứng minh được nó
còn nguyên trạng thái mới tạo. Suite không chạm GitHub thật.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workspaces import RunWorkspaceManager, branch_name_for_run


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


class WorkspaceGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.harness_root = self.base / "SDLC_Harness"
        self.harness_root.mkdir()
        self.remote = self.base / "target.git"
        self.seed = self.base / "seed"
        self.canonical = self.base / "target"
        self.worktree_root = self.base / "target-worktrees"
        self.runtime_root = self.harness_root / ".runtime"

        self.remote.mkdir()
        _git(self.remote, "init", "--bare")
        self.seed.mkdir()
        _git(self.seed, "init")
        _git(self.seed, "config", "user.name", "SDLC Test")
        _git(self.seed, "config", "user.email", "sdlc-test@example.com")
        _git(self.seed, "checkout", "-b", "main")
        (self.seed / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.seed, "add", "README.md")
        _git(self.seed, "commit", "-m", "initial")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _git(self.seed, "push", "-u", "origin", "main")
        _git(self.base, "clone", "-b", "main", str(self.remote), str(self.canonical))
        _git(self.canonical, "config", "user.name", "SDLC Test")
        _git(self.canonical, "config", "user.email", "sdlc-test@example.com")

        self.settings = self._settings(remote=str(self.remote.resolve()))
        self.store = RuntimeStore(repo_root=self.harness_root, runtime_root=self.runtime_root)
        self.manager = RunWorkspaceManager(self.settings)
        self.run_id = "run_20261015_120000_a1b2c3d4"
        self._write_state(self.run_id)

    def _settings(self, *, remote: str):
        return load_settings(
            root=self.harness_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_TARGET_REPO_PATH": str(self.canonical),
                "SDLC_TARGET_WORKTREE_ROOT": str(self.worktree_root),
                "SDLC_TARGET_REPO_REMOTE": remote,
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_IMPLEMENTATION_BRANCH_PREFIX": "feature",
            },
        )

    def _write_state(self, run_id: str, *, source_ref: str = "dataset_use_case:etour:UC1") -> None:
        self.store.write_state(
            {
                "run_id": run_id,
                "source_ref": source_ref,
                "dataset": "etour",
                "use_case": "UC1",
                "status": "active",
                "latest_artifacts": {},
            }
        )

    def _push_remote_change(self) -> str:
        feature = self.seed / "feature.txt"
        feature.write_text("new remote base\n", encoding="utf-8")
        _git(self.seed, "add", feature.name)
        _git(self.seed, "commit", "-m", "advance remote base")
        _git(self.seed, "push", "origin", "main")
        return _git(self.seed, "rev-parse", "HEAD")

    def test_create_fetches_remote_and_preserves_canonical_checkout(self) -> None:
        canonical_branch = _git(self.canonical, "branch", "--show-current")
        canonical_head = _git(self.canonical, "rev-parse", "HEAD")
        newest_remote_sha = self._push_remote_change()

        record = self.manager.create_for_run(self.run_id)

        self.assertEqual(record.base_sha, newest_remote_sha)
        self.assertEqual(record.head_sha, newest_remote_sha)
        self.assertEqual(record.provisioning_mode, "created_for_run")
        self.assertEqual(record.branch, "feature/etour-uc1-a1b2c3d4")
        self.assertEqual(_git(self.canonical, "branch", "--show-current"), canonical_branch)
        self.assertEqual(_git(self.canonical, "rev-parse", "HEAD"), canonical_head)
        self.assertEqual(_git(Path(record.worktree_path), "branch", "--show-current"), record.branch)

        state = self.store.read_state(self.run_id)
        self.assertEqual(state["workspace"]["base_sha"], newest_remote_sha)
        self.assertEqual(
            state["latest_artifacts"]["run_workspace"],
            f".runtime/runs/{self.run_id}/artifacts/run_workspace_v1.json",
        )

    def test_create_is_idempotent_and_locked_workspace_resolves_identity(self) -> None:
        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(self.run_id)

        self.assertEqual(first, second)
        with self.manager.locked_workspace(self.run_id) as locked:
            self.assertEqual(locked, first)
            self.assertEqual(_git(Path(locked.worktree_path), "rev-parse", "HEAD"), locked.head_sha)

    def test_refresh_locked_records_new_head_and_pull_request_identity(self) -> None:
        record = self.manager.create_for_run(self.run_id)

        with self.manager.locked_workspace(self.run_id) as locked:
            worktree = Path(locked.worktree_path)
            (worktree / "generated.txt").write_text("generated\n", encoding="utf-8")
            _git(worktree, "add", "generated.txt")
            _git(worktree, "commit", "-m", "generated by run")
            refreshed = self.manager.refresh_locked(
                locked,
                pr_number=17,
                pr_url="https://github.com/example/target/pull/17",
            )

        self.assertNotEqual(refreshed.head_sha, record.head_sha)
        self.assertEqual(refreshed.head_sha, _git(Path(refreshed.worktree_path), "rev-parse", "HEAD"))
        self.assertEqual(refreshed.pr_number, 17)
        self.assertEqual(refreshed.pr_url, "https://github.com/example/target/pull/17")
        self.assertEqual(refreshed.status_summary()["head_sha"], refreshed.head_sha)
        self.assertEqual(refreshed.status_summary()["pr_number"], 17)
        self.assertNotIn("worktree_path", refreshed.status_summary())
        self.assertEqual(self.manager.resolve(self.run_id), refreshed)
        state = self.store.read_state(self.run_id)
        self.assertEqual(state["workspace"]["head_sha"], refreshed.head_sha)
        self.assertEqual(state["workspace"]["pr_number"], 17)

    def test_same_source_uses_distinct_branch_and_worktree_per_run(self) -> None:
        second_run = "run_20261015_120001_deadbeef"
        self._write_state(second_run)

        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(second_run)

        self.assertNotEqual(first.branch, second.branch)
        self.assertNotEqual(first.worktree_path, second.worktree_path)
        self.assertTrue(Path(first.worktree_path).is_dir())
        self.assertTrue(Path(second.worktree_path).is_dir())

    def test_interleaved_runs_keep_files_and_branches_isolated(self) -> None:
        second_run = "run_20261015_120001_deadbeef"
        self._write_state(second_run, source_ref="dataset_use_case:etour:UC2")
        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(second_run)

        with self.manager.locked_workspace(self.run_id) as locked:
            (Path(locked.worktree_path) / "first-only.txt").write_text("first\n", encoding="utf-8")
        with self.manager.locked_workspace(second_run) as locked:
            (Path(locked.worktree_path) / "second-only.txt").write_text("second\n", encoding="utf-8")

        first_path = Path(first.worktree_path)
        second_path = Path(second.worktree_path)
        self.assertTrue((first_path / "first-only.txt").exists())
        self.assertFalse((first_path / "second-only.txt").exists())
        self.assertTrue((second_path / "second-only.txt").exists())
        self.assertFalse((second_path / "first-only.txt").exists())
        self.assertEqual(_git(first_path, "branch", "--show-current"), first.branch)
        self.assertEqual(_git(second_path, "branch", "--show-current"), second.branch)
        self.assertFalse((self.canonical / "first-only.txt").exists())
        self.assertFalse((self.canonical / "second-only.txt").exists())

    def test_dirty_workspace_does_not_block_resolving_another_run(self) -> None:
        second_run = "run_20261015_120001_deadbeef"
        self._write_state(second_run, source_ref="dataset_use_case:etour:UC2")
        first = self.manager.create_for_run(self.run_id)
        second = self.manager.create_for_run(second_run)
        (Path(first.worktree_path) / "dirty-only-in-first.txt").write_text("dirty\n", encoding="utf-8")

        resolved_second = self.manager.resolve(second_run)

        self.assertEqual(resolved_second, second)
        self.assertEqual(_git(Path(second.worktree_path), "status", "--porcelain"), "")

    def test_second_action_for_same_run_is_rejected_while_lock_is_held(self) -> None:
        self.manager.create_for_run(self.run_id)

        with self.manager.run_lock(self.run_id):
            with self.assertRaises(HarnessError) as ctx:
                with self.manager.locked_workspace(self.run_id):
                    pass

        self.assertEqual(ctx.exception.error_code, "run_workspace_locked")

    def test_dirty_canonical_repo_is_rejected_without_side_effects(self) -> None:
        (self.canonical / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(HarnessError) as ctx:
            self.manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_dirty")
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())

    def test_codegraph_changes_do_not_block_workspace_creation(self) -> None:
        codegraph = self.canonical / ".codegraph"
        codegraph.mkdir()
        (codegraph / "index.json").write_text("{}\n", encoding="utf-8")

        record = self.manager.create_for_run(self.run_id)

        self.assertTrue(Path(record.worktree_path).is_dir())

    def test_remote_mismatch_is_rejected(self) -> None:
        manager = RunWorkspaceManager(self._settings(remote=str(self.base / "other.git")))

        with self.assertRaises(HarnessError) as ctx:
            manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_remote_mismatch")
        self.assertFalse(manager.expected_worktree_path(self.run_id).exists())

    def test_branch_and_path_conflicts_fail_closed(self) -> None:
        branch = branch_name_for_run(
            prefix="feature",
            source_ref="dataset_use_case:etour:UC1",
            issue_key="etour-UC1",
            run_id=self.run_id,
        )
        _git(self.canonical, "branch", branch)
        with self.assertRaises(HarnessError) as branch_ctx:
            self.manager.create_for_run(self.run_id)
        self.assertEqual(branch_ctx.exception.error_code, "workspace_branch_conflict")

        _git(self.canonical, "branch", "-d", branch)
        self.manager.expected_worktree_path(self.run_id).mkdir(parents=True)
        with self.assertRaises(HarnessError) as path_ctx:
            self.manager.create_for_run(self.run_id)
        self.assertEqual(path_ctx.exception.error_code, "workspace_path_invalid")

    def test_resolve_detects_git_identity_drift(self) -> None:
        record = self.manager.create_for_run(self.run_id)
        _git(self.canonical, "worktree", "remove", record.worktree_path)

        with self.assertRaises(HarnessError) as ctx:
            self.manager.resolve(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_path_invalid")

    def test_artifact_persistence_failure_rolls_back_clean_creation(self) -> None:
        branch = "feature/etour-uc1-a1b2c3d4"
        failure = HarnessError("runtime_write_failed", "simulated artifact failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_json_artifact", side_effect=failure):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "runtime_write_failed")
        self.assertFalse(self.manager.expected_worktree_path(self.run_id).exists())
        self.assertFalse(self.manager.artifact_path(self.run_id).exists())
        refs = _git(self.canonical, "branch", "--list", branch)
        self.assertEqual(refs, "")

    def test_state_persistence_failure_keeps_owned_workspace_for_retry(self) -> None:
        failure = HarnessError("runtime_write_failed", "simulated state failure", run_id=self.run_id)

        with patch.object(self.manager.store, "write_state", side_effect=failure):
            with self.assertRaises(HarnessError) as ctx:
                self.manager.create_for_run(self.run_id)

        self.assertEqual(ctx.exception.error_code, "runtime_write_failed")
        self.assertTrue(self.manager.artifact_path(self.run_id).is_file())
        record = self.manager.read(self.run_id)
        self.assertTrue(Path(record.worktree_path).is_dir())

        recovered = self.manager.create_for_run(self.run_id)
        state = self.store.read_state(self.run_id)
        self.assertEqual(recovered, record)
        self.assertEqual(state["workspace"]["head_sha"], record.head_sha)

    def test_tampered_artifact_does_not_resolve(self) -> None:
        self.manager.create_for_run(self.run_id)
        artifact = self.manager.artifact_path(self.run_id)
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        payload["head_sha"] = "f" * 40
        artifact.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(HarnessError) as ctx:
            self.manager.resolve(self.run_id)

        self.assertEqual(ctx.exception.error_code, "workspace_branch_mismatch")


if __name__ == "__main__":
    unittest.main()
