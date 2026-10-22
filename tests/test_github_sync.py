"""Kiểm GitHub PR reader, publication và điểm nối sang Code Review."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pytest

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.sync.github import (
    GitHubCodeReviewApprover,
    GitHubCodeReviewPublisher,
    GitHubCodeReviewResult,
    GitHubPullRequestReader,
    GitHubPublishError,
)
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow
from sdlc_harness.workspaces import RunWorkspaceManager
from tests._support import GitRepositoryTemplate, create_fast_run_workspace, populate_typescript_target


pytestmark = pytest.mark.integration


def _populate_publisher_target(target: Path) -> None:
    source = target / "src" / "main" / "java" / "example"
    source.mkdir(parents=True)
    (source / "Feature.java").write_text("final class InitialFeature {}\n", encoding="utf-8")


def _populate_workflow_target(target: Path) -> None:
    populate_typescript_target(target)
    (target / ".codegraph").mkdir()


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


class FakeReadyGh:
    """Giả lập read/ready PR nhưng chặn mọi thao tác merge ngoài contract."""

    def __init__(self) -> None:
        self.pull_request = {
            "number": 7,
            "url": "https://github.com/example/target/pull/7",
            "state": "OPEN",
            "isDraft": True,
            "headRefName": "feature/sdlc-1-implementation",
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "baseRefOid": "b" * 40,
            "mergedAt": None,
            "mergeCommit": None,
        }
        self.ready_calls = 0
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path) -> str:
        del cwd
        self.commands.append(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps(self.pull_request)
        if args[:2] == ["pr", "ready"]:
            self.ready_calls += 1
            self.pull_request["isDraft"] = False
            return ""
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


class RecordingApprover:
    """Double cho workflow test, trả PR Ready đúng identity mà không gọi GitHub."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def approve(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
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
            "approved_at": "2026-10-15T12:00:00Z",
        }


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
    @classmethod
    def setUpClass(cls) -> None:
        cls.publisher_template = GitRepositoryTemplate(_populate_publisher_target)
        cls.workflow_template = GitRepositoryTemplate(_populate_workflow_target)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workflow_template.close()
        cls.publisher_template.close()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        validation_patch = patch.object(RunWorkspaceManager, "_validate_git", autospec=True, return_value=None)
        validation_patch.start()
        self.addCleanup(validation_patch.stop)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pull_request_reader_requests_full_workspace_identity(self) -> None:
        calls: list[tuple[list[str], Path]] = []
        expected = {
            "number": 7,
            "url": "https://github.com/example/target/pull/7",
            "state": "OPEN",
            "isDraft": True,
            "headRefName": "feature/example",
            "headRefOid": "a" * 40,
            "baseRefName": "main",
            "baseRefOid": "b" * 40,
            "mergedAt": None,
            "mergeCommit": None,
        }

        def fake_gh(args: list[str], cwd: Path) -> str:
            calls.append((args, cwd))
            return json.dumps(expected)

        result = GitHubPullRequestReader(gh_runner=fake_gh).read(
            target_path=self.root,
            repository="example/target",
            pr_number=7,
            run_id="run_20261015_120000_a1b2c3d4",
        )

        self.assertEqual(result, expected)
        self.assertEqual(calls[0][0][:3], ["pr", "view", "7"])
        self.assertIn("headRefOid", calls[0][0][-1])
        self.assertIn("mergedAt", calls[0][0][-1])
        self.assertIn("mergeCommit", calls[0][0][-1])
        self.assertEqual(calls[0][1], self.root)

    def test_approver_marks_draft_ready_idempotently_without_merge(self) -> None:
        fake_gh = FakeReadyGh()
        approver = GitHubCodeReviewApprover(gh_runner=fake_gh)
        kwargs = {
            "target_path": self.root,
            "repository": "example/target",
            "pr_number": 7,
            "expected_branch": "feature/sdlc-1-implementation",
            "expected_head_sha": "a" * 40,
            "expected_base_branch": "main",
            "run_id": "run_20261015_120000_a1b2c3d4",
        }

        first = approver.approve(**kwargs)
        second = approver.approve(**kwargs)

        self.assertEqual(first["operation"], "marked_ready")
        self.assertEqual(second["operation"], "already_ready")
        self.assertFalse(first["pull_request"]["isDraft"])
        self.assertEqual(fake_gh.ready_calls, 1)
        self.assertTrue(all(command[:2] in (["pr", "view"], ["pr", "ready"]) for command in fake_gh.commands))
        self.assertFalse(any("merge" in command or "auto-merge" in command for command in fake_gh.commands))

    def test_approver_rejects_pr_identity_or_lifecycle_mismatch_before_ready(self) -> None:
        cases = (
            ("number", 8, "github_pr_identity_mismatch"),
            ("headRefName", "feature/other", "github_pr_identity_mismatch"),
            ("headRefOid", "c" * 40, "github_pr_identity_mismatch"),
            ("baseRefName", "develop", "github_pr_identity_mismatch"),
            ("state", "CLOSED", "github_pr_not_open"),
            ("mergedAt", "2026-10-15T12:00:00Z", "github_pr_not_open"),
        )

        for field, value, error_code in cases:
            with self.subTest(field=field):
                fake_gh = FakeReadyGh()
                fake_gh.pull_request[field] = value
                approver = GitHubCodeReviewApprover(gh_runner=fake_gh)

                with self.assertRaises(HarnessError) as context:
                    approver.approve(
                        target_path=self.root,
                        repository="example/target",
                        pr_number=7,
                        expected_branch="feature/sdlc-1-implementation",
                        expected_head_sha="a" * 40,
                        expected_base_branch="main",
                        run_id="run_20261015_120000_a1b2c3d4",
                    )

                self.assertEqual(context.exception.error_code, error_code)
                self.assertEqual(fake_gh.ready_calls, 0)

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
        self.assertIn("apps/api/src/Feature.ts", publication_paths)
        self.assertIn("apps/api/src/FeatureAcceptance.test.ts", publication_paths)

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

    def test_approval_dispatch_writes_artifact_syncs_status_and_keeps_workspace(self) -> None:
        settings, store, run_id, workflow, ready = self._ready_code_review_workflow()
        approver = RecordingApprover()
        workflow.github_approver = approver
        workspace_before = RunWorkspaceManager(settings).resolve(run_id)

        with patch("sdlc_harness.workflows.approval.PhaseFourWorkflow", return_value=workflow):
            with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
                response = CheckpointApprovalWorkflow(settings).approve_checkpoint(
                    run_id=run_id,
                    checkpoint_id=ready["pending_checkpoint"]["checkpoint_id"],
                )

        self.assertEqual(response["status"], "code_review_approved")
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(response["allowed_next_actions"], ["get_status", "sync_run", "cancel_run"])
        self.assertEqual(len(approver.calls), 1)
        approval_path = Path(str(response["latest_artifacts"]["github_code_review_approval"]))
        if not approval_path.is_absolute():
            approval_path = settings.repo_root / approval_path
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        self.assertEqual(approval["version"], "github_code_review_approval_v1")
        self.assertEqual(approval["operation"], "marked_ready")
        self.assertFalse(approval["pull_request"]["isDraft"])
        sync_report = json.loads((store.run_dir(run_id) / "sync" / "tool_sync_report_v4.json").read_text(encoding="utf-8"))
        self.assertEqual(sync_report["checkpoint"]["status"], "code_review_approved")
        self.assertEqual(sync_report["checkpoint"]["artifact_path"], store.relative(approval_path))
        workspace_after = RunWorkspaceManager(settings).resolve(run_id)
        self.assertEqual(workspace_after.lifecycle, "active")
        self.assertEqual(workspace_after.branch, workspace_before.branch)
        self.assertEqual(workspace_after.head_sha, workspace_before.head_sha)

    def test_approval_sync_retry_does_not_repeat_github_ready(self) -> None:
        settings, _, run_id, workflow, ready = self._ready_code_review_workflow()
        approver = RecordingApprover()
        workflow.github_approver = approver
        failed_sync = {
            "phase": "code_review",
            "status": "partial_failed",
            "mode": "real",
            "review_status": "code_review_approved",
            "report": "sync/tool_sync_report_v4.json",
            "targets": [],
        }
        passed_sync = {
            "phase": "code_review",
            "status": "synced",
            "mode": "real",
            "review_status": "code_review_approved",
            "report": "sync/tool_sync_report_v4.json",
            "targets": [],
        }

        with patch("sdlc_harness.workflows.phase4.sync_code_review_checkpoint", return_value=failed_sync):
            with patch("sdlc_harness.workflows.approval.PhaseFourWorkflow", return_value=workflow):
                with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
                    failed = CheckpointApprovalWorkflow(settings).approve_checkpoint(
                        run_id=run_id,
                        checkpoint_id=ready["pending_checkpoint"]["checkpoint_id"],
                    )
        with patch("sdlc_harness.workflows.phase4.sync_code_review_checkpoint", return_value=passed_sync):
            approved = workflow.sync_code_review(run_id=run_id)

        self.assertEqual(failed["status"], "code_review_approval_sync_failed")
        self.assertIn("github_code_review_approval", failed["latest_artifacts"])
        self.assertEqual(approved["status"], "code_review_approved")
        self.assertEqual(len(approver.calls), 1)

    def test_approval_rejects_stale_checkpoint_before_github_call(self) -> None:
        _, _, run_id, workflow, _ = self._ready_code_review_workflow()
        approver = RecordingApprover()
        workflow.github_approver = approver

        with self.assertRaises(HarnessError) as context:
            workflow.approve_code_review_checkpoint(
                run_id=run_id,
                checkpoint_id="checkpoint_code_review_stale",
            )

        self.assertEqual(context.exception.error_code, "checkpoint_not_current")
        self.assertEqual(approver.calls, [])

    def _ready_code_review_workflow(self):
        settings, store, run_id = self._workflow_fixture()
        workflow = PhaseFourWorkflow(settings)
        workflow.github_publisher = RecordingPublisher()
        with patch("sdlc_harness.workflows.phase4.github_repository_from_remote", return_value="example/target"):
            workflow.continue_run(run_id=run_id)
        ready = workflow.continue_run(run_id=run_id)
        self.assertEqual(ready["status"], "code_review_ready")
        return settings, store, run_id, workflow, ready

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
        profile_snapshot = {
            "version": "profile_snapshot_v1",
            "project_profile_id": "typescript_fullstack_v1",
            "project_profile_schema": "project_profile_v1",
            "language_profile_id": "typescript_v1",
            "language_profile_schema": "language_profile_v1",
            "target_remote": str(remote.resolve()),
            "target_base_branch": "main",
        }
        store.write_state(
            {
                "ok": True,
                "run_id": run_id,
                "source_ref": "jira_issue:SDLC-1",
                "current_phase": "qa_testing",
                "status": "qa_tested",
                "latest_artifacts": {},
                "profile_snapshot": profile_snapshot,
            }
        )
        manager = RunWorkspaceManager(settings)
        workspace = create_fast_run_workspace(
            manager,
            run_id=run_id,
            state=store.read_state(run_id),
        )
        worktree = Path(workspace.worktree_path)
        (worktree / ".codegraph").mkdir(exist_ok=True)
        source_path = worktree / "apps" / "api" / "src" / "Feature.ts"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(
            "export class Feature { value = 1; }\n",
            encoding="utf-8",
        )
        artifacts = store.run_dir(run_id) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        work_packet = {
            "schema_version": "developer_work_packet_profiled_v1",
            "planning_profile": {
                "project_profile_id": "typescript_fullstack_v1",
                "language_profile_id": "typescript_v1",
                "policy_version": "planning_path_policy_v1",
            },
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
            "verification_stage_ids": ["typecheck", "unit"],
            "test_intents": [{"stage_id": "unit"}],
            "output_contract": {
                "allowed_paths": ["apps/api/src/Feature.ts"],
                "path_policy": {
                    "version": "editable_path_policy_v1",
                    "project_profile_id": "typescript_fullstack_v1",
                    "language_profile_id": "typescript_v1",
                    "planning_policy_version": "planning_path_policy_v1",
                    "component_roots": {"api": ["apps/api/src"]},
                    "protected_paths": [],
                },
            },
        }
        qa_generated = {
            "test_files": [{"path": "apps/api/src/FeatureAcceptance.test.ts"}],
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
                "profile_snapshot": profile_snapshot,
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
        topology = self.publisher_template.materialize(self.root, canonical_name="target")
        self._git(topology.canonical, "checkout", "-b", "feature/sdlc-1-implementation")
        return topology.canonical, topology.remote

    def _init_workflow_target(self) -> tuple[Path, Path]:
        topology = self.workflow_template.materialize(self.root, canonical_name="workflow-target")
        return topology.canonical, topology.remote

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8")

    def _git_output(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8"
        )
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
