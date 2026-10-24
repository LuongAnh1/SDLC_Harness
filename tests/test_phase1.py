"""Kiểm thử Phase 1 cho vertical slice use case -> requirement checkpoint."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main
from sdlc_harness.core.config import load_settings, repo_root
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_profile_snapshot
from sdlc_harness.sources.fixtures import UseCaseSource, load_use_case
from sdlc_harness.sources.jira import load_jira_issue_source
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.sync.atlassian import real_sync_requirement_checkpoint
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


class PhaseOneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def profile_snapshot(self) -> dict[str, object]:
        return build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote=self.settings.target_repo_remote,
            target_base_branch=self.settings.target_base_branch,
        ).to_dict()

    def test_repo_root_points_to_project_root(self) -> None:
        self.assertEqual(repo_root(), self.repo_root)
        self.assertTrue((repo_root() / "fixtures" / "etour" / "UC1.txt").exists())

    def test_fixture_loader_reads_etour_uc1(self) -> None:
        source = load_use_case(dataset="eTour", use_case="UC1", repo_root=self.repo_root)
        self.assertEqual(source.source_id, "SRC-1")
        self.assertEqual(source.source_type, "dataset")
        self.assertEqual(source.source_ref, "dataset_use_case:eTour:UC1")
        self.assertEqual(source.locator, "fixtures/etour/UC1.txt")
        self.assertIsNone(source.revision)
        self.assertIn("DeleteCulturalHeritage", source.text)

    def test_fixture_loader_rejects_unsupported_dataset(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            load_use_case(dataset="unknown", use_case="UC1", repo_root=self.repo_root)
        self.assertEqual(ctx.exception.error_code, "source_not_supported")

    def test_fixture_loader_returns_source_not_found(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            load_use_case(dataset="eTour", use_case="missing", repo_root=self.repo_root)
        self.assertEqual(ctx.exception.error_code, "source_not_found")

    def test_jira_source_loader_reads_issue_context(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
            },
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            return {
                "key": "SDLC-1",
                "fields": {
                    "updated": "2026-10-24T12:34:56.000+0000",
                    "summary": "eTour UC1 - Delete cultural heritage",
                    "description": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Delete a cultural object in the system."}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "issuetype": {"name": "Task"},
                    "labels": ["etour", "uc1"],
                    "comment": {
                        "comments": [
                            {
                                "author": {"displayName": "Reviewer"},
                                "body": {
                                    "type": "doc",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Keep cancellation as an open question."}],
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                },
            }

        source = load_jira_issue_source(settings=settings, issue_key="sdlc-1", request_json=fake_request)

        self.assertEqual(source.dataset, "jira")
        self.assertEqual(source.use_case, "SDLC-1")
        self.assertEqual(source.source_ref, "jira_issue:SDLC-1")
        self.assertEqual(source.source_id, "SRC-1")
        self.assertEqual(source.source_type, "jira")
        self.assertEqual(source.locator, "SDLC-1")
        self.assertEqual(source.revision, "2026-10-24T12:34:56.000+0000")
        self.assertIsNone(source.source_path)
        self.assertIn("Delete a cultural object", source.text)
        self.assertIn("Reviewer", source.text)
        self.assertEqual(calls[0][0], "GET")
        self.assertIn("/rest/api/3/issue/SDLC-1", calls[0][1])

    def test_jira_source_loader_rejects_empty_issue(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
            },
        )

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            return {"fields": {"summary": "Only a title", "description": None, "comment": {"comments": []}}}

        with self.assertRaises(HarnessError) as ctx:
            load_jira_issue_source(settings=settings, issue_key="SDLC-1", request_json=fake_request)
        self.assertEqual(ctx.exception.error_code, "jira_source_empty")

    def test_jira_source_loader_filters_harness_comments_but_keeps_user_comments(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
            },
        )

        def paragraph(text: str) -> dict[str, object]:
            return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            return {
                "fields": {
                    "summary": "Delete cultural heritage",
                    "description": paragraph("Delete the selected object."),
                    "comment": {
                        "comments": [
                            {
                                "author": {"displayName": "SDLC Harness"},
                                "body": paragraph(
                                    "SDLC Harness checkpoint: Design review "
                                    "Source: jira_issue:SDLC-1 Run: run_old"
                                ),
                            },
                            {
                                "author": {"displayName": "SDLC Harness"},
                                "body": paragraph(
                                    "SDLC-HARNESS-CHECKPOINT::code_review::jira_issue:SDLC-1"
                                ),
                            },
                            {
                                "author": {"displayName": "Reviewer"},
                                "body": paragraph("Keep the cancellation flow."),
                            },
                        ]
                    },
                }
            }

        source = load_jira_issue_source(settings=settings, issue_key="SDLC-1", request_json=fake_request)

        self.assertIn("Keep the cancellation flow", source.text)
        self.assertNotIn("run_old", source.text)
        self.assertNotIn("SDLC-HARNESS-CHECKPOINT", source.text)

    def test_trace_dev_role_prompts_are_available(self) -> None:
        prompt_paths = [
            self.repo_root / "sdlc_harness" / "agents" / "designer" / "prompts" / "designer.txt",
            self.repo_root / "sdlc_harness" / "agents" / "validator" / "prompts" / "validator.txt",
            self.repo_root / "sdlc_harness" / "agents" / "developer" / "prompts" / "developer.txt",
            self.repo_root / "sdlc_harness" / "agents" / "tester" / "prompts" / "tester.txt",
        ]
        for path in prompt_paths:
            self.assertTrue(path.exists(), str(path))
            self.assertGreater(len(path.read_text(encoding="utf-8")), 100)

    def test_start_or_get_run_creates_runtime_files(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        response = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertTrue(response["ok"])
        self.assertTrue(response["created_new_run"])
        self.assertEqual(response["current_phase"], "waiting_requirement_approval")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertIn("formal_use_case", response["latest_artifacts"])
        self.assertEqual(response["requirement_contract_version"], "v2")
        self.assertIn("requirement_model", response["latest_artifacts"])
        self.assertIn("requirement_gap_report", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["requirement_model_v2"]["final_report"]["ok"])
        self.assertEqual(
            response["quality_reports"]["requirement_gaps"]["status"],
            "clear",
        )

        run_id = str(response["run_id"])
        run_dir = self.runtime_root / "runs" / run_id
        self.assertTrue((run_dir / "state.json").exists())
        self.assertTrue((run_dir / "artifacts" / "use_case_packet_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_model_v2_r1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "formal_use_case_v2_r1.txt").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_packet_v2_r1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_validation_v2_r1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_decisions_v2_r1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_gap_report_v1.json").exists())
        self.assertTrue((run_dir / "sync" / "tool_sync_report_v1.json").exists())

    def test_source_failure_keeps_failed_state_and_persists_classified_gap(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        with patch.object(
            workflow.refiner_v2,
            "refine",
            side_effect=HarnessError(
                "source_snapshot_corrupted",
                "The immutable source snapshot is corrupted.",
            ),
        ):
            with self.assertRaises(HarnessError) as context:
                workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        run_id = str(context.exception.run_id)
        state = workflow.store.read_state(run_id)
        report = json.loads(
            (self.runtime_root / "runs" / run_id / "artifacts" / "requirement_gap_report_v1.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_error"]["error_code"], "source_snapshot_corrupted")
        self.assertEqual(state["quality_reports"]["requirement_gaps"]["status"], "failed")
        self.assertEqual(report["gaps"][0]["type"], "source_unavailable")
        self.assertEqual(report["gaps"][0]["route"], "fail")

    def test_failed_requirement_delta_remains_visible_in_internal_run_state(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)

        def reject_delta(*args: object, run_id: str, **kwargs: object) -> object:
            workflow.store.write_json_artifact(
                run_id,
                "requirement_delta_v1_a1.json",
                {
                    "schema_version": "requirement_delta_v1",
                    "run_id": run_id,
                    "status": "rejected",
                },
            )
            raise HarnessError(
                "requirement_delta_policy_violation",
                "The proposed repair delta violates Python-owned policy.",
                run_id=run_id,
            )

        with patch.object(workflow.refiner_v2, "refine", side_effect=reject_delta):
            with self.assertRaises(HarnessError) as context:
                workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        state = workflow.store.read_state(str(context.exception.run_id))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(
            state["latest_artifacts"]["requirement_delta"],
            workflow.store.relative(
                workflow.store.artifact_path(
                    str(context.exception.run_id),
                    "requirement_delta_v1_a1.json",
                )
            ),
        )

    def test_start_from_jira_creates_runtime_files(self) -> None:
        source = UseCaseSource(
            dataset="jira",
            use_case="SDLC-1",
            source_ref="jira_issue:SDLC-1",
            source_path=None,
            text="Jira Issue: SDLC-1\nSummary: Delete cultural heritage\n\nDescription:\nDelete a cultural object.",
        )
        workflow = PhaseOneWorkflow(self.settings)
        with patch("sdlc_harness.workflows.phase1.load_jira_issue_source", return_value=source):
            response = workflow.start_from_jira(issue_key="SDLC-1", requested_by="test")
            second = workflow.start_from_jira(issue_key="SDLC-1", requested_by="test")

        self.assertTrue(response["ok"])
        self.assertTrue(response["created_new_run"])
        self.assertFalse(second["created_new_run"])
        self.assertEqual(response["run_id"], second["run_id"])
        self.assertEqual(response["source_ref"], "jira_issue:SDLC-1")
        run_id = str(response["run_id"])
        use_case_packet = (self.runtime_root / "runs" / run_id / "artifacts" / "use_case_packet_v1.md").read_text(encoding="utf-8")
        self.assertIn("Source location: `jira_issue:SDLC-1`", use_case_packet)

    def test_sync_run_updates_existing_run_with_mock_report(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        synced = workflow.sync_run(run_id=str(created["run_id"]))

        self.assertEqual(synced["run_id"], created["run_id"])
        self.assertEqual(synced["last_sync"]["status"], "mock_synced")

    def test_start_or_get_run_is_idempotent_for_active_source_ref(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        first = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        second = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertTrue(first["created_new_run"])
        self.assertFalse(second["created_new_run"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_clean_default_start_snapshots_typescript_profile_in_fake_mock_mode(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sdlc_harness.core.config._read_dotenv", return_value={}),
        ):
            settings = load_settings(root=self.repo_root, runtime_root=self.runtime_root)

        self.assertEqual(settings.llm_mode, "fake")
        self.assertEqual(settings.sync_mode, "mock")
        response = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="cutover-smoke",
        )
        state = RuntimeStore(
            repo_root=self.repo_root,
            runtime_root=self.runtime_root,
        ).read_state(str(response["run_id"]))

        self.assertEqual(
            state["profile_snapshot"]["project_profile_id"],
            "typescript_fullstack_v1",
        )
        self.assertEqual(
            state["profile_snapshot"]["target_remote"],
            "https://github.com/LuongAnh1/SDLC_Target_TypeScript.git",
        )

    def test_profile_snapshot_is_persisted_before_requirement_refiner_runs(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_TARGET_REPO_REMOTE": "https://github.com/example/typescript-target.git",
                "SDLC_TARGET_BASE_BRANCH": "main",
            },
        )
        workflow = PhaseOneWorkflow(settings)
        observed: dict[str, object] = {}

        def stop_after_state(*args: object, run_id: str, **kwargs: object) -> object:
            observed.update(workflow.store.read_state(run_id))
            raise HarnessError("test_stop", "Stop after observing initial state.")

        with patch.object(workflow.refiner_v2, "refine", side_effect=stop_after_state):
            with self.assertRaises(HarnessError) as context:
                workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertEqual(context.exception.error_code, "test_stop")
        snapshot = observed["profile_snapshot"]
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["version"], "profile_snapshot_v1")
        self.assertEqual(snapshot["project_profile_id"], "typescript_fullstack_v1")
        self.assertEqual(snapshot["language_profile_id"], "typescript_v1")
        self.assertEqual(snapshot["target_remote"], "https://github.com/example/typescript-target.git")

    def test_profiled_run_status_exposes_only_minimal_profile_metadata(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_TARGET_REPO_REMOTE": "https://github.com/example/typescript-target.git",
            },
        )
        workflow = PhaseOneWorkflow(settings)

        response = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertEqual(
            response["execution_profile"],
            {
                "classification": "profiled",
                "snapshot_version": "profile_snapshot_v1",
                "project_profile_id": "typescript_fullstack_v1",
                "language_profile_id": "typescript_v1",
            },
        )
        self.assertNotIn("target_remote", response["execution_profile"])

    def test_blank_profile_starts_typescript_run_and_keeps_frozen_snapshot(self) -> None:
        initial_settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "",
            },
        )
        initial_workflow = PhaseOneWorkflow(initial_settings)
        first = initial_workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        profiled_settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_TARGET_REPO_REMOTE": "https://github.com/example/typescript-target.git",
            },
        )

        second = PhaseOneWorkflow(profiled_settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        state = initial_workflow.store.read_state(str(first["run_id"]))

        self.assertFalse(second["created_new_run"])
        self.assertEqual(state["profile_snapshot"]["project_profile_id"], "typescript_fullstack_v1")
        self.assertEqual(
            state["profile_snapshot"]["target_remote"],
            "https://github.com/LuongAnh1/SDLC_Target_TypeScript.git",
        )
        self.assertEqual(second["execution_profile"]["classification"], "profiled")

    def test_status_reports_invalid_snapshot_without_breaking_audit_read(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        run_id = "run_invalid_profile_snapshot"
        workflow.store.write_state(
            {
                "run_id": run_id,
                "status": "blocked",
                "latest_artifacts": {},
                "allowed_next_actions": ["get_status"],
                "profile_snapshot": {"version": "profile_snapshot_v2"},
            }
        )

        response = workflow.get_status(run_id=run_id)

        self.assertEqual(response["execution_profile"]["classification"], "invalid")
        self.assertIsNone(response["execution_profile"]["project_profile_id"])

    def test_start_or_get_run_ignores_failed_active_run(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        first = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        state = workflow.store.read_state(str(first["run_id"]))
        state["status"] = "failed"
        workflow.store.write_state(state)

        second = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertTrue(second["created_new_run"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_get_status_reads_existing_run(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        status = workflow.get_status(run_id=str(created["run_id"]))

        self.assertEqual(status["run_id"], created["run_id"])
        self.assertEqual(status["pending_checkpoint"]["type"], "requirement_approval")
        self.assertIn("summary_for_user", status)
        self.assertIn("quality_reports", status)

    def test_get_status_normalizes_legacy_code_review_ready_without_mutating_state(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        run_id = "run_20261013_160421_f661babf"
        legacy_state = {
            "ok": True,
            "run_id": run_id,
            "source_ref": "jira_issue:SDLC-1",
            "current_phase": "code_review",
            "status": "code_review_ready",
            "pending_checkpoint": {
                "checkpoint_id": f"checkpoint_code_review_{run_id}",
                "type": "code_review",
                "pull_request_url": "https://github.com/example/target/pull/1",
            },
            "allowed_next_actions": ["get_status", "sync_run", "cancel_run"],
            "summary_for_user": "Legacy summary without approval action.",
        }
        workflow.store.write_state(legacy_state)
        state_path = workflow.store.state_path(run_id)
        persisted_before = state_path.read_text(encoding="utf-8")

        response = workflow.get_status(run_id=run_id)

        self.assertEqual(
            response["allowed_next_actions"],
            ["approve_checkpoint", "get_status", "sync_run", "cancel_run"],
        )
        self.assertIn("Draft PR: https://github.com/example/target/pull/1", response["summary_for_user"])
        self.assertIn("Ready for review", response["summary_for_user"])
        self.assertEqual(state_path.read_text(encoding="utf-8"), persisted_before)

    def test_get_status_keeps_current_code_review_ready_contract_without_mutating_state(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        run_id = "run_20261015_150000_c0decafe"
        current_summary = (
            f"Checkpoint Code Review đã sẵn sàng cho run {run_id}. "
            "Draft PR: https://github.com/example/target/pull/2. "
            "Anh có thể approve checkpoint để chuyển Draft PR thành Ready for review."
        )
        current_state = {
            "ok": True,
            "run_id": run_id,
            "source_ref": "jira_issue:SDLC-2",
            "current_phase": "code_review",
            "status": "code_review_ready",
            "pending_checkpoint": {
                "checkpoint_id": f"checkpoint_code_review_{run_id}",
                "type": "code_review",
                "pull_request_url": "https://github.com/example/target/pull/2",
            },
            "allowed_next_actions": ["approve_checkpoint", "get_status", "sync_run", "cancel_run"],
            "summary_for_user": current_summary,
            "profile_snapshot": self.profile_snapshot(),
        }
        workflow.store.write_state(current_state)
        state_path = workflow.store.state_path(run_id)
        persisted_before = state_path.read_text(encoding="utf-8")

        response = workflow.get_status(run_id=run_id)

        self.assertEqual(response["allowed_next_actions"], current_state["allowed_next_actions"])
        self.assertEqual(response["summary_for_user"], current_summary)
        self.assertEqual(state_path.read_text(encoding="utf-8"), persisted_before)

    def test_get_status_advertises_sync_retry_for_partial_checkpoint_sync(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        run_id = "run_20261016_072017_ae0bac6b"
        state = {
            "ok": True,
            "run_id": run_id,
            "source_ref": "jira_issue:SDLC-2",
            "current_phase": "design_review",
            "status": "waiting_design_approval",
            "pending_checkpoint": {
                "checkpoint_id": f"checkpoint_design_{run_id}",
                "type": "design_approval",
                "artifact_ref": "design_artifact_v1",
            },
            "allowed_next_actions": [
                "approve_checkpoint",
                "request_change",
                "cancel_run",
                "get_status",
            ],
            "last_sync": {
                "phase": "design_checkpoint",
                "status": "partial_failed",
                "mode": "real",
            },
            "profile_snapshot": self.profile_snapshot(),
        }
        workflow.store.write_state(state)
        state_path = workflow.store.state_path(run_id)
        persisted_before = state_path.read_text(encoding="utf-8")

        response = workflow.get_status(run_id=run_id)

        self.assertEqual(
            response["allowed_next_actions"],
            [
                "revalidate_design",
                "approve_checkpoint",
                "request_change",
                "sync_run",
                "cancel_run",
                "get_status",
            ],
        )
        self.assertEqual(state_path.read_text(encoding="utf-8"), persisted_before)

    def test_get_status_returns_run_not_found(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        with self.assertRaises(HarnessError) as ctx:
            workflow.get_status(run_id="run_missing")
        self.assertEqual(ctx.exception.error_code, "run_not_found")

    def test_cli_error_is_json(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main([
                "get-status",
                "--run-id",
                "run_missing",
                "--runtime-root",
                str(self.runtime_root),
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "run_not_found")

    def test_cli_code_review_approval_success_is_json_with_zero_exit(self) -> None:
        run_id = "run_20261015_150000_c0decafe"
        PhaseOneWorkflow(self.settings).store.write_state(
            {"run_id": run_id, "profile_snapshot": self.profile_snapshot()}
        )
        response = {
            "ok": True,
            "run_id": run_id,
            "status": "code_review_approved",
            "pending_checkpoint": None,
            "allowed_next_actions": ["get_status", "sync_run", "cancel_run"],
        }
        stdout = io.StringIO()

        with patch("sdlc_harness.application.service.CheckpointApprovalWorkflow") as workflow_type:
            workflow_type.return_value.approve_checkpoint.return_value = response
            with contextlib.redirect_stdout(stdout):
                code = cli_main(
                    [
                        "approve-checkpoint",
                        "--run-id",
                        run_id,
                        "--runtime-root",
                        str(self.runtime_root),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), response)
        workflow_type.return_value.approve_checkpoint.assert_called_once_with(
            run_id=run_id,
            checkpoint_id=None,
        )

    def test_cli_code_review_approval_business_error_keeps_json_contract(self) -> None:
        run_id = "run_20261015_150000_c0decafe"
        PhaseOneWorkflow(self.settings).store.write_state(
            {"run_id": run_id, "profile_snapshot": self.profile_snapshot()}
        )
        stdout = io.StringIO()
        failure = HarnessError(
            "github_pr_identity_mismatch",
            "Pull request identity does not match the run workspace.",
            run_id=run_id,
        )

        with patch("sdlc_harness.application.service.CheckpointApprovalWorkflow") as workflow_type:
            workflow_type.return_value.approve_checkpoint.side_effect = failure
            with contextlib.redirect_stdout(stdout):
                code = cli_main(
                    [
                        "approve-checkpoint",
                        "--run-id",
                        run_id,
                        "--runtime-root",
                        str(self.runtime_root),
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], "github_pr_identity_mismatch")
        self.assertEqual(payload["run_id"], run_id)

    def test_cli_sync_run_reads_existing_run(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        stdout = io.StringIO()
        with patch.dict(os.environ, {"SDLC_SYNC_MODE": "mock"}), contextlib.redirect_stdout(stdout):
            code = cli_main([
                "sync-run",
                "--run-id",
                str(created["run_id"]),
                "--runtime-root",
                str(self.runtime_root),
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["last_sync"]["status"], "mock_synced")

    def test_cli_start_from_jira_returns_json(self) -> None:
        source = UseCaseSource(
            dataset="jira",
            use_case="SDLC-1",
            source_ref="jira_issue:SDLC-1",
            source_path=None,
            text="Jira Issue: SDLC-1\nSummary: Delete cultural heritage\n\nDescription:\nDelete a cultural object.",
        )
        stdout = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                },
            ),
            patch("sdlc_harness.workflows.phase1.load_jira_issue_source", return_value=source),
            contextlib.redirect_stdout(stdout),
        ):
            code = cli_main([
                "start-from-jira",
                "--issue-key",
                "SDLC-1",
                "--requested-by",
                "test",
                "--llm-mode",
                "fake",
                "--runtime-root",
                str(self.runtime_root),
            ])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source_ref"], "jira_issue:SDLC-1")

    def test_real_sync_writes_jira_and_confluence_targets(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "1",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
            },
        )
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        run_id = str(created["run_id"])
        artifact_paths = {str(key): str(value) for key, value in created["latest_artifacts"].items()}
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if "/comment" in url:
                return {"id": "10001", "self": url + "/10001"}
            if url.endswith("/wiki/api/v2/pages"):
                return {"id": "20001", "_links": {"webui": "/spaces/SDLC/pages/20001/Requirement"}}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_requirement_checkpoint(
            store=workflow.store,
            settings=settings,
            run_id=run_id,
            source_ref=str(created["source_ref"]),
            artifact_paths=artifact_paths,
            checkpoint_context={
                "status": "waiting_requirement_clarification",
                "remaining_blocking_count": 2,
                "current_question": {"id": "Q-4", "text": "Which fields define a duplicate?"},
                "decision_summary": {"id": "D-1", "question_id": "Q-3", "status": "accepted"},
            },
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        jira_payload = calls[0][2]
        self.assertIsNotNone(jira_payload)
        jira_body = jira_payload["body"]
        self.assertEqual(jira_body["content"][0]["type"], "heading")
        self.assertIn("Requirement review", jira_body["content"][0]["content"][0]["text"])
        self.assertTrue(any(block["type"] == "bulletList" for block in jira_body["content"]))
        jira_text = json.dumps(jira_body, ensure_ascii=False)
        self.assertIn("waiting_requirement_clarification", jira_text)
        self.assertIn("Which fields define a duplicate?", jira_text)
        self.assertIn("Remaining blocking questions: 2", jira_text)
        self.assertIn("D-1 / Q-3 / accepted", jira_text)
        confluence_payload = calls[1][2]
        self.assertIsNotNone(confluence_payload)
        confluence_body = confluence_payload["body"]["value"]
        self.assertIn("<h1>SDLC Harness - UC1 - Requirement Checkpoint</h1>", confluence_body)
        self.assertIn("SDLC-HARNESS-CHECKPOINT::requirement::dataset_use_case:eTour:UC1", confluence_body)
        self.assertIn("<table>", confluence_body)
        self.assertIn("<h2>Requirement Packet</h2>", confluence_body)
        self.assertIn("<h3>Alternative / Exception Flows</h3>", confluence_body)
        self.assertIn("<h3>Acceptance Criteria</h3>", confluence_body)
        self.assertIn("waiting_requirement_clarification", confluence_body)
        self.assertIn("Which fields define a duplicate?", confluence_body)
        self.assertIn("Remaining blocking questions</th><td>2", confluence_body)
        self.assertIn("D-1 / Q-3 / accepted", confluence_body)
        self.assertNotIn("<pre>", confluence_body)
        report_path = self.repo_root / str(result["report"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["mode"], "real")
        self.assertEqual(report["targets"][0]["external_id"], "10001")
        self.assertEqual(report["targets"][1]["external_id"], "20001")

    def test_real_sync_updates_existing_confluence_page(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "0",
                "CONFLUENCE_SYNC_ENABLED": "1",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
            },
        )
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        run_id = str(created["run_id"])
        artifact_paths = {str(key): str(value) for key, value in created["latest_artifacts"].items()}
        sync_dir = self.runtime_root / "runs" / run_id / "sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / "tool_sync_report_v1.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "confluence", "status": "synced", "external_id": "20001"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "GET":
                return {"id": "20001", "version": {"number": 3}}
            if method == "PUT":
                return {"id": "20001", "_links": {"webui": "/spaces/SDLC/pages/20001/Requirement"}}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_requirement_checkpoint(
            store=workflow.store,
            settings=settings,
            run_id=run_id,
            source_ref=str(created["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["GET", "PUT"])
        self.assertEqual(calls[1][2]["version"]["number"], 4)

    def test_real_sync_updates_existing_jira_comment(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "0",
            },
        )
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        run_id = str(created["run_id"])
        artifact_paths = {str(key): str(value) for key, value in created["latest_artifacts"].items()}
        sync_dir = self.runtime_root / "runs" / run_id / "sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / "tool_sync_report_v1.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "jira", "status": "synced", "external_id": "10001"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "PUT" and url.endswith("/rest/api/3/issue/SDLC-1/comment/10001"):
                return {"id": "10001"}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_requirement_checkpoint(
            store=workflow.store,
            settings=settings,
            run_id=run_id,
            source_ref=str(created["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["PUT"])
        self.assertEqual(result["targets"][0]["external_id"], "10001")

    def test_real_sync_records_partial_failure(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "real",
                "ATLASSIAN_BASE_URL": "https://example.atlassian.net",
                "ATLASSIAN_EMAIL": "test@example.com",
                "ATLASSIAN_API_TOKEN": "secret",
                "JIRA_SYNC_ENABLED": "1",
                "JIRA_SYNC_ISSUE_KEY": "SDLC-1",
                "CONFLUENCE_SYNC_ENABLED": "1",
                "CONFLUENCE_SYNC_SPACE_ID": "98315",
                "CONFLUENCE_SYNC_PARENT_PAGE_ID": "294923",
            },
        )
        workflow = PhaseOneWorkflow(self.settings)
        created = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        artifact_paths = {str(key): str(value) for key, value in created["latest_artifacts"].items()}

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            if "/comment" in url:
                return {"id": "10001"}
            raise RuntimeError("boom")

        result = real_sync_requirement_checkpoint(
            store=workflow.store,
            settings=settings,
            run_id=str(created["run_id"]),
            source_ref=str(created["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "partial_failed")
        self.assertEqual(result["targets"][0]["status"], "synced")
        self.assertEqual(result["targets"][1]["status"], "failed")

    def test_status_does_not_auto_migrate_legacy_developer_truncation(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
            },
        )
        workflow = PhaseOneWorkflow(settings)
        workflow.store.write_state(
            {
                "run_id": "run_legacy_developer_truncation",
                "source_ref": "jira_issue:SDLC-2",
                "current_phase": "implementation_writing",
                "status": "blocked",
                "latest_artifacts": {"developer_work_packet": "packet.json"},
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": {"error_code": "developer_act_failed", "message": "truncated"},
                "developer_execution": {},
                "profile_snapshot": self.profile_snapshot(),
            }
        )

        response = workflow.get_status(run_id="run_legacy_developer_truncation")

        self.assertNotIn("continue_run", response["allowed_next_actions"])
        self.assertEqual(response["developer_execution"], {})

    def test_status_exposes_safe_retry_for_blocked_repair_selection_policy_drift(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        run_id = "run_repair_item_scope_mismatch"
        workflow.store.write_state(
            {
                "run_id": run_id,
                "source_ref": "jira_issue:SDLC-2",
                "current_phase": "implementation_repair",
                "status": "blocked",
                "latest_artifacts": {
                    "implementation_repair_feedback": "feedback.json",
                    "developer_repair_execution_manifest": "manifest.json",
                    "developer_repair_execution_journal": "journal.json",
                },
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": {
                    "error_code": "developer_input_drift",
                    "message": "Persisted repair selection differs from the current selection policy.",
                },
                "developer_execution": {"mode": "implementation_repair"},
                "profile_snapshot": self.profile_snapshot(),
            }
        )

        response = workflow.get_status(run_id=run_id)

        self.assertEqual(response["allowed_next_actions"][0], "continue_run")

    def test_status_exposes_safe_retry_for_profiled_qa_blocked_by_legacy_java_path_guard(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        run_id = "run_profiled_qa_legacy_path_guard"
        workflow.store.write_state(
            {
                "run_id": run_id,
                "source_ref": "jira_issue:SDLC-2",
                "current_phase": "qa_testing",
                "status": "blocked",
                "latest_artifacts": {"developer_work_packet": "packet.json"},
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": {
                    "error_code": "developer_act_unsafe_path",
                    "message": (
                        "Unsafe or unsupported Developer.act path: "
                        "packages/contracts/src/CulturalHeritage.ts"
                    ),
                },
                "profile_snapshot": self.profile_snapshot(),
            }
        )

        response = workflow.get_status(run_id=run_id)

        self.assertEqual(response["allowed_next_actions"][0], "continue_run")
