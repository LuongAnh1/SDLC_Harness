"""Kiểm GitHub publication và điểm nối `qa_tested -> code_review_published`."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.core.config import load_settings
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.sync.github import (
    GitHubCodeReviewPublisher,
    GitHubCodeReviewResult,
    GitHubPublishError,
)
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow
from sdlc_harness.workspaces import RunWorkspaceManager


class FakeGh:
    """Giả lập `gh pr` nhưng giữ state để kiểm create/update idempotent."""

    def __init__(self) -> None:
        self.pull_request: dict[str, object] | None = None
        self.created = 0
        self.updated = 0

    def __call__(self, args: list[str], cwd: Path) -> str:
        del cwd
        if args[:2] == ["pr", "list"]:
            state = args[args.index("--state") + 1]
            if state == "open" and self.pull_request:
                return json.dumps([self.pull_request])
            return "[]"
        if args[:2] == ["pr", "create"]:
            self.created += 1
            self.pull_request = {
                "number": 1,
                "url": "https://github.com/example/target/pull/1",
                "title": args[args.index("--title") + 1],
                "isDraft": True,
                "headRefName": args[args.index("--head") + 1],
                "baseRefName": args[args.index("--base") + 1],
            }
            return str(self.pull_request["url"])
        if args[:2] == ["pr", "edit"]:
            self.updated += 1
            assert self.pull_request is not None
            self.pull_request["title"] = args[args.index("--title") + 1]
            return ""
        if args[:2] == ["pr", "view"]:
            assert self.pull_request is not None
            return json.dumps(self.pull_request)
        raise AssertionError(f"Unexpected gh command: {args}")


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def publish(self, **kwargs: object) -> GitHubCodeReviewResult:
        self.calls.append(dict(kwargs))
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
                "commit": {"operation": "created", "sha": "abc123", "url": "https://example/commit/abc123"},
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


class FailingPublisher:
    def publish(self, **kwargs: object) -> GitHubCodeReviewResult:
        raise GitHubPublishError(
            "github_cli_failed",
            "simulated PR failure",
            run_id=str(kwargs["run_id"]),
            partial_result={
                "status": "publishing",
                "run_id": kwargs["run_id"],
                "commit": {"sha": "abc123"},
                "push": {"status": "pushed"},
                "pull_request": {},
            },
        )


class GitHubSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_publisher_commits_pushes_and_updates_same_draft_pr(self) -> None:
        target, remote = self._target_with_bare_remote()
        source_path = "src/main/java/example/Feature.java"
        qa_path = "src/test/java/example/FeatureAcceptanceTest.java"
        (target / source_path).write_text("final class Feature {}\n", encoding="utf-8")
        qa_file = target / qa_path
        qa_file.parent.mkdir(parents=True, exist_ok=True)
        qa_file.write_text("final class FeatureAcceptanceTest {}\n", encoding="utf-8")
        body_path = self.root / "pr-body.md"
        body_path.write_text("# Review\n", encoding="utf-8")
        fake_gh = FakeGh()
        publisher = GitHubCodeReviewPublisher(gh_runner=fake_gh)

        first = publisher.publish(
            target_path=target,
            expected_remote=str(remote),
            repository="example/target",
            branch="feature/sdlc-1-implementation",
            base_branch="main",
            issue_key="SDLC-1",
            run_id="run_test_github",
            publication_paths=[source_path, qa_path],
            pr_title="[SDLC-1] Implement approved use case",
            pr_body_path=body_path,
        ).to_dict()
        second = publisher.publish(
            target_path=target,
            expected_remote=str(remote),
            repository="example/target",
            branch="feature/sdlc-1-implementation",
            base_branch="main",
            issue_key="SDLC-1",
            run_id="run_test_github",
            publication_paths=[source_path, qa_path],
            pr_title="[SDLC-1] Implement approved use case",
            pr_body_path=body_path,
        ).to_dict()

        self.assertEqual(first["status"], "published")
        self.assertEqual(first["commit"]["operation"], "created")
        self.assertEqual(second["commit"]["operation"], "reused")
        self.assertEqual(first["commit"]["sha"], second["commit"]["sha"])
        self.assertEqual(fake_gh.created, 1)
        self.assertEqual(fake_gh.updated, 1)
        self.assertEqual(self._git_output(target, "rev-list", "--count", "HEAD"), "2")
        self.assertTrue(self._git_output(remote, "show-ref", "--verify", "refs/heads/feature/sdlc-1-implementation"))

    def test_publisher_blocks_changed_file_outside_manifest(self) -> None:
        target, remote = self._target_with_bare_remote()
        unexpected = target / "notes.txt"
        unexpected.write_text("not owned by the run\n", encoding="utf-8")
        body_path = self.root / "pr-body.md"
        body_path.write_text("# Review\n", encoding="utf-8")

        with self.assertRaises(GitHubPublishError) as context:
            GitHubCodeReviewPublisher(gh_runner=FakeGh()).publish(
                target_path=target,
                expected_remote=str(remote),
                repository="example/target",
                branch="feature/sdlc-1-implementation",
                base_branch="main",
                issue_key="SDLC-1",
                run_id="run_unexpected",
                publication_paths=["src/main/java/example/Feature.java"],
                pr_title="[SDLC-1] Implement approved use case",
                pr_body_path=body_path,
            )

        self.assertEqual(context.exception.error_code, "github_publish_unexpected_changes")
        self.assertEqual(self._git_output(target, "rev-list", "--count", "HEAD"), "1")

    def test_workflow_publishes_qa_tested_run_and_writes_artifact(self) -> None:
        settings, store, run_id = self._workflow_fixture()
        publisher = RecordingPublisher()
        workflow = PhaseFourWorkflow(settings)
        workflow.github_publisher = publisher

        with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "code_review_published")
        self.assertIsNone(response["pending_checkpoint"])
        self.assertIn("github_code_review", response["latest_artifacts"])
        self.assertEqual(response["workspace"]["pr_number"], 7)
        self.assertEqual(response["workspace"]["pr_url"], "https://github.com/example/target/pull/7")
        self.assertEqual(response["quality_reports"]["code_review_publication"]["pr_url"], "https://github.com/example/target/pull/7")
        self.assertEqual(len(publisher.calls), 1)
        publication_paths = publisher.calls[0]["publication_paths"]
        self.assertIn("src/main/java/example/Feature.java", publication_paths)
        self.assertIn("src/test/java/example/FeatureAcceptanceTest.java", publication_paths)
        self.assertIn("scripts/test.ps1", publication_paths)

        repeated = workflow.continue_run(run_id=run_id)

        self.assertEqual(repeated["status"], "code_review_ready")
        self.assertEqual(repeated["pending_checkpoint"]["type"], "code_review")
        self.assertEqual(len(publisher.calls), 1)
        self.assertTrue((store.run_dir(run_id) / "sync" / "tool_sync_report_v4.json").exists())
        workspace = RunWorkspaceManager(settings).resolve(run_id)
        self.assertEqual(workspace.pr_number, 7)
        self.assertEqual(workspace.pr_url, "https://github.com/example/target/pull/7")

    def test_workflow_keeps_partial_publication_evidence_for_retry(self) -> None:
        settings, store, run_id = self._workflow_fixture()
        workflow = PhaseFourWorkflow(settings)
        workflow.github_publisher = FailingPublisher()

        with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
            response = workflow.continue_run(run_id=run_id)

        self.assertEqual(response["status"], "code_review_publish_failed")
        self.assertEqual(response["allowed_next_actions"], ["continue_run", "get_status", "cancel_run"])
        self.assertEqual(response["last_error"]["error_code"], "github_cli_failed")
        report_path = Path(str(response["latest_artifacts"]["github_code_review"]))
        if not report_path.is_absolute():
            report_path = settings.repo_root / report_path
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "partial_failed")
        self.assertEqual(report["commit"]["sha"], "abc123")
        self.assertEqual(report["push"]["status"], "pushed")

    def _workflow_fixture(self):
        target, remote = self._init_workflow_target()
        runtime_root = self.root / "runtime"
        settings = load_settings(
            root=Path(__file__).resolve().parents[1],
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_TARGET_REPO_PATH": str(target),
                "SDLC_TARGET_REPO_REMOTE": str(remote.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
            },
        )
        run_id = "run_20261015_150000_c0decafe"
        store = RuntimeStore(repo_root=settings.repo_root, runtime_root=runtime_root)
        store.write_state(
            {
                "ok": True,
                "run_id": run_id,
                "source_ref": "jira_issue:SDLC-1",
                "current_phase": "qa_testing",
                "status": "qa_tested",
                "latest_artifacts": {},
            }
        )
        workspace = RunWorkspaceManager(settings).create_for_run(run_id)
        worktree = Path(workspace.worktree_path)
        (worktree / ".codegraph").mkdir(exist_ok=True)
        (worktree / "src" / "main" / "java" / "example" / "Feature.java").write_text(
            "final class Feature { int value; }\n",
            encoding="utf-8",
        )
        artifacts = store.run_dir(run_id) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        work_packet = {
            "issue_key": "SDLC-1",
            "target_repository": {
                "local_path": str(worktree),
                "remote": workspace.remote,
                "planned_branch": workspace.branch,
                "base_branch": "main",
                "workspace_mode": workspace.workspace_mode,
                "workspace_ref": store.relative(RunWorkspaceManager(settings).artifact_path(run_id)),
                "base_sha": workspace.base_sha,
            },
            "run_workspace": {
                "version": workspace.version,
                "workspace_mode": workspace.workspace_mode,
                "workspace_ref": store.relative(RunWorkspaceManager(settings).artifact_path(run_id)),
                "branch": workspace.branch,
                "base_ref": workspace.base_ref,
                "base_sha": workspace.base_sha,
                "head_sha": workspace.head_sha,
                "remote": workspace.remote,
            },
            "verification_plan": [{"command": "powershell -File scripts/test.ps1"}],
            "output_contract": {"allowed_paths": ["src/main/java/example/Feature.java"]},
        }
        qa_generated = {
            "test_files": [{"path": "src/test/java/example/FeatureAcceptanceTest.java"}],
            "scenarios": [{"name": "Approved behavior"}],
            "risks": [],
        }
        latest: dict[str, str] = {
            "run_workspace": store.relative(RunWorkspaceManager(settings).artifact_path(run_id)),
        }
        for name, value in (
            ("developer_work_packet", json.dumps(work_packet)),
            ("qa_generated_tests", json.dumps(qa_generated)),
            ("code_change_report", "code"),
            ("test_report", "tests"),
            ("requirement_design_code_trace", "{}"),
            ("qa_test_execution", "qa execution"),
            ("qa_test_report", "qa report"),
        ):
            suffix = ".json" if name in {"developer_work_packet", "qa_generated_tests", "requirement_design_code_trace"} else ".md"
            path = artifacts / f"{name}_v1{suffix}"
            path.write_text(value + "\n", encoding="utf-8")
            latest[name] = store.relative(path)
        store.write_state(
            {
                "ok": True,
                "run_id": run_id,
                "source_ref": "jira_issue:SDLC-1",
                "current_phase": "qa_testing",
                "status": "qa_tested",
                "latest_artifacts": latest,
                "quality_reports": {
                    "implementation_traceability": {
                        "ok": True,
                        "stats": {"mapping_count": 4},
                        "llm_confidence": 0.9,
                    },
                    "qa_acceptance": {"ok": True, "status": "pass", "scenario_count": 1},
                },
                "last_sync": {
                    "targets": [
                        {"tool": "jira", "status": "synced", "url": "https://example.atlassian.net/browse/SDLC-1"},
                        {"tool": "confluence", "status": "synced", "url": "https://example.atlassian.net/wiki/page"},
                    ]
                },
                "pending_checkpoint": None,
                "allowed_next_actions": ["continue_run", "get_status", "cancel_run"],
            }
        )
        return settings, store, run_id

    def _target_with_bare_remote(self) -> tuple[Path, Path]:
        remote = self.root / "remote.git"
        self._git(self.root, "init", "--bare", str(remote))
        target = self.root / "target"
        target.mkdir()
        source = target / "src" / "main" / "java" / "example"
        source.mkdir(parents=True)
        (source / "Feature.java").write_text("final class InitialFeature {}\n", encoding="utf-8")
        self._git(target, "init")
        self._git(target, "config", "user.name", "Test")
        self._git(target, "config", "user.email", "test@example.com")
        self._git(target, "add", ".")
        self._git(target, "commit", "-m", "Initial")
        self._git(target, "branch", "-M", "main")
        self._git(target, "remote", "add", "origin", str(remote))
        self._git(target, "push", "-u", "origin", "main")
        self._git(target, "checkout", "-b", "feature/sdlc-1-implementation")
        return target, remote

    def _init_workflow_target(self) -> tuple[Path, Path]:
        remote = self.root / "workflow-remote.git"
        self._git(self.root, "init", "--bare", str(remote))
        target = self.root / "workflow-target"
        source = target / "src" / "main" / "java" / "example"
        tests = target / "src" / "test" / "java" / "example"
        scripts = target / "scripts"
        source.mkdir(parents=True)
        tests.mkdir(parents=True)
        scripts.mkdir(parents=True)
        (source / "Feature.java").write_text("final class Feature {}\n", encoding="utf-8")
        (tests / "FeatureAcceptanceTest.java").write_text("final class FeatureAcceptanceTest {}\n", encoding="utf-8")
        (scripts / "test.ps1").write_text("Write-Host pass\n", encoding="utf-8")
        (target / ".codegraph").mkdir()
        self._git(target, "init")
        self._git(target, "config", "user.name", "Test")
        self._git(target, "config", "user.email", "test@example.com")
        self._git(target, "add", ".")
        self._git(target, "commit", "-m", "Initial")
        self._git(target, "branch", "-M", "main")
        self._git(target, "remote", "add", "origin", str(remote))
        self._git(target, "push", "-u", "origin", "main")
        return target, remote

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8")

    def _git_output(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8"
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
