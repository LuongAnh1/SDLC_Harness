"""Kiểm checkpoint Code Review và identity remote ổn định của Slice 4.8B."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.interfaces.cli import _settings_from_args, build_parser
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.sync.atlassian import _find_remote_confluence_checkpoint, real_sync_code_review_checkpoint
from sdlc_harness.workflows.phase4 import PhaseFourWorkflow
from sdlc_harness.workspaces import RunWorkspaceManager


class CodeReviewSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.remote_path = Path(self.temp_dir.name) / "target.git"
        self.remote_path.mkdir()
        self._git(self.remote_path, "init", "--bare")
        self.target_repo = Path(self.temp_dir.name) / "target_repo"
        self.target_repo.mkdir()
        (self.target_repo / "README.md").write_text("# Test target\n", encoding="utf-8")
        self._git(self.target_repo, "init")
        self._git(self.target_repo, "add", ".")
        self._git(
            self.target_repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        )
        self._git(self.target_repo, "branch", "-M", "main")
        self._git(self.target_repo, "remote", "add", "origin", str(self.remote_path.resolve()))
        self._git(self.target_repo, "push", "-u", "origin", "main")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_side_effect_commands_default_to_mock_even_when_env_is_real(self) -> None:
        parser = build_parser()
        with patch.dict(os.environ, {"SDLC_SYNC_MODE": "real"}):
            default_args = parser.parse_args(
                ["start-from-jira", "--issue-key", "SDLC-1", "--requested-by", "test"]
            )
            explicit_args = parser.parse_args(
                [
                    "start-from-jira",
                    "--issue-key",
                    "SDLC-1",
                    "--requested-by",
                    "test",
                    "--sync-mode",
                    "real",
                ]
            )
            self.assertEqual(_settings_from_args(default_args).sync_mode, "mock")
            self.assertEqual(_settings_from_args(explicit_args).sync_mode, "real")

    def test_real_sync_recovers_remote_ids_and_resync_keeps_them(self) -> None:
        settings, store, run_id, artifacts = self._fixture(sync_mode="real")
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "GET" and "/wiki/api/v2/pages?" in url:
                return {"results": [{"id": "20004", "title": "SDLC Harness - SDLC-1 - Code Review Checkpoint"}]}
            if method == "GET" and url.endswith("/wiki/api/v2/pages/20004"):
                return {"id": "20004", "version": {"number": 3}}
            if method == "PUT" and url.endswith("/wiki/api/v2/pages/20004"):
                return {"id": "20004", "_links": {"webui": "/spaces/SDLC/pages/20004/Code-Review"}}
            if method == "GET" and "/comment?maxResults=100" in url:
                return {
                    "comments": [
                        {
                            "id": "10004",
                            "body": self._adf("SDLC-HARNESS-CHECKPOINT::code_review::jira_issue:SDLC-1"),
                        }
                    ]
                }
            if method == "PUT" and url.endswith("/comment/10004"):
                return {"id": "10004"}
            self.fail(f"Unexpected request: {method} {url}")

        first = real_sync_code_review_checkpoint(
            store=store,
            settings=settings,
            run_id=run_id,
            source_ref="jira_issue:SDLC-1",
            artifact_paths=artifacts,
            request_json=fake_request,
        )
        second = real_sync_code_review_checkpoint(
            store=store,
            settings=settings,
            run_id=run_id,
            source_ref="jira_issue:SDLC-1",
            artifact_paths=artifacts,
            request_json=fake_request,
        )

        self.assertEqual(first["status"], "synced")
        self.assertEqual(second["status"], "synced")
        for result in (first, second):
            ids = {target["tool"]: target["external_id"] for target in result["targets"]}
            self.assertEqual(ids["jira"], "10004")
            self.assertEqual(ids["confluence"], "20004")
            self.assertEqual(ids["github"], "7")
        self.assertFalse(any(method == "POST" for method, _, _ in calls))

    def test_ambiguous_remote_checkpoint_fails_closed(self) -> None:
        settings, store, run_id, artifacts = self._fixture(sync_mode="real", confluence=False)

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            self.assertEqual(method, "GET")
            return {
                "comments": [
                    {"id": "1", "body": self._adf("SDLC-HARNESS-CHECKPOINT::code_review::jira_issue:SDLC-1")},
                    {"id": "2", "body": self._adf("SDLC-HARNESS-CHECKPOINT::code_review::jira_issue:SDLC-1")},
                ]
            }

        result = real_sync_code_review_checkpoint(
            store=store,
            settings=settings,
            run_id=run_id,
            source_ref="jira_issue:SDLC-1",
            artifact_paths=artifacts,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "failed")
        jira = next(target for target in result["targets"] if target["tool"] == "jira")
        self.assertEqual(jira["error_code"], "remote_sync_ambiguous")

    def test_legacy_confluence_page_is_migrated_only_when_unique(self) -> None:
        settings, _, _, _ = self._fixture(sync_mode="real")

        def unique_request(method: str, url: str, payload: dict[str, object] | None):
            if "/wiki/api/v2/pages?" in url:
                return {"results": []}
            return {
                "results": [
                    {"id": "legacy-1", "title": "Code Review Checkpoint - eTour UC1 - run_old"}
                ]
            }

        matched = _find_remote_confluence_checkpoint(
            settings,
            unique_request,
            "jira_issue:SDLC-1",
            "code_review",
        )
        self.assertEqual(matched, "legacy-1")

        def ambiguous_request(method: str, url: str, payload: dict[str, object] | None):
            if "/wiki/api/v2/pages?" in url:
                return {"results": []}
            return {
                "results": [
                    {"id": "legacy-1", "title": "Code Review Checkpoint - eTour UC1 - run_old"},
                    {"id": "legacy-2", "title": "Code Review Checkpoint - eTour UC1 - run_older"},
                ]
            }

        with self.assertRaises(HarnessError) as context:
            _find_remote_confluence_checkpoint(
                settings,
                ambiguous_request,
                "jira_issue:SDLC-1",
                "code_review",
            )
        self.assertEqual(context.exception.error_code, "remote_sync_ambiguous")

    def test_workflow_keeps_evidence_on_partial_failure_then_retries_to_ready(self) -> None:
        settings, store, run_id, artifacts = self._fixture(sync_mode="mock")
        store.write_state(
            {
                "ok": True,
                "run_id": run_id,
                "source_ref": "jira_issue:SDLC-1",
                "status": "code_review_published",
                "current_phase": "code_review_publishing",
                "latest_artifacts": artifacts,
                "quality_reports": {},
                "pending_checkpoint": None,
                "allowed_next_actions": ["continue_run", "get_status", "cancel_run"],
            }
        )
        RunWorkspaceManager(settings).create_for_run(run_id)
        workflow = PhaseFourWorkflow(settings)
        failed_sync = {
            "phase": "code_review",
            "status": "partial_failed",
            "mode": "real",
            "report": "sync/tool_sync_report_v4.json",
            "targets": [],
        }
        passed_sync = {
            "phase": "code_review",
            "status": "synced",
            "mode": "real",
            "report": "sync/tool_sync_report_v4.json",
            "targets": [],
        }

        with patch("sdlc_harness.workflows.phase4.sync_code_review_checkpoint", return_value=failed_sync):
            failed = workflow.continue_run(run_id=run_id)
        with patch("sdlc_harness.workflows.phase4.sync_code_review_checkpoint", return_value=passed_sync):
            ready = workflow.sync_code_review(run_id=run_id)

        self.assertEqual(failed["status"], "code_review_sync_failed")
        self.assertIn("github_code_review", failed["latest_artifacts"])
        self.assertEqual(ready["status"], "code_review_ready")
        self.assertEqual(ready["pending_checkpoint"]["type"], "code_review")
        self.assertEqual(ready["allowed_next_actions"], ["get_status", "sync_run", "cancel_run"])

    def _fixture(self, *, sync_mode: str, confluence: bool = True):
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": sync_mode,
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "1" if confluence else "0",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
                "SDLC_TARGET_REPO_PATH": str(self.target_repo),
                "SDLC_TARGET_REPO_REMOTE": str(self.remote_path.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
            },
        )
        store = RuntimeStore(repo_root=settings.repo_root, runtime_root=self.runtime_root)
        run_id = "run_20261015_000000_4a8b0000"
        artifact_dir = store.run_dir(run_id) / "artifacts"
        sync_dir = store.run_dir(run_id) / "sync"
        artifact_dir.mkdir(parents=True)
        sync_dir.mkdir(parents=True)
        payloads = {
            "test_report": "# Test Report\n\nStatus: passed\n",
            "requirement_design_code_trace": json.dumps(
                {"ok": True, "stats": {"mapping_count": 12, "missing_count": 0}}
            ),
            "qa_test_execution": "# QA Execution\n\nStatus: passed\n",
            "qa_test_report": "# QA Report\n\nSummary: 8 acceptance scenarios passed.\n",
        }
        artifacts: dict[str, str] = {}
        for name, content in payloads.items():
            suffix = ".json" if name == "requirement_design_code_trace" else ".md"
            path = artifact_dir / f"{name}_v1{suffix}"
            path.write_text(content, encoding="utf-8")
            artifacts[name] = store.relative(path)
        github_path = sync_dir / "github_code_review_v1.json"
        github_path.write_text(
            json.dumps(
                {
                    "status": "published",
                    "repository": "example/target",
                    "branch": "feature/sdlc-1",
                    "commit": {"sha": "abc123", "url": "https://github.com/example/target/commit/abc123"},
                    "pull_request": {
                        "number": 7,
                        "operation": "created",
                        "isDraft": True,
                        "url": "https://github.com/example/target/pull/7",
                    },
                }
            ),
            encoding="utf-8",
        )
        artifacts["github_code_review"] = store.relative(github_path)
        return settings, store, run_id, artifacts

    @staticmethod
    def _adf(text: str) -> dict[str, object]:
        return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
