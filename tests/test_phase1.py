"""Test Phase 1 cho vertical slice use case -> requirement checkpoint."""

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
from sdlc_harness.sources.fixtures import UseCaseSource, load_use_case
from sdlc_harness.sources.jira import load_jira_issue_source
from sdlc_harness.llm.client import LlmClient, fake_formal_use_case
from sdlc_harness.agents.requirements import FormalUseCaseValidator, RequirementRefiner, RequirementValidator
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
            overrides={"SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock"},
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repo_root_points_to_project_root(self) -> None:
        self.assertEqual(repo_root(), self.repo_root)
        self.assertTrue((repo_root() / "fixtures" / "etour" / "UC1.txt").exists())

    def test_fixture_loader_reads_etour_uc1(self) -> None:
        source = load_use_case(dataset="eTour", use_case="UC1", repo_root=self.repo_root)
        self.assertEqual(source.source_ref, "dataset_use_case:eTour:UC1")
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

    def test_requirement_prompt_templates_exist(self) -> None:
        prompt_root = self.repo_root / "sdlc_harness" / "agents" / "requirements" / "prompts"
        packet_prompt = (prompt_root / "requirement_packet_v1.md").read_text(encoding="utf-8")
        packet_repair_prompt = (prompt_root / "requirement_packet_repair_v1.md").read_text(encoding="utf-8")
        formal_prompt = (prompt_root / "formal_use_case_v1.txt").read_text(encoding="utf-8")
        repair_prompt = (prompt_root / "formal_use_case_repair_v1.txt").read_text(encoding="utf-8")

        self.assertIn("## Source Evidence", packet_prompt)
        self.assertIn("## Alternative / Exception Flows", packet_prompt)
        self.assertIn("## Quality / Non-functional Requirements", packet_prompt)
        self.assertIn("{formal_use_case}", packet_prompt)
        self.assertIn("{RequirementValidationReport}", packet_repair_prompt)
        self.assertIn("{RequirementPacket}", packet_repair_prompt)
        self.assertIn("Formal Syntactic Constraint", formal_prompt)
        self.assertIn("{OriginalUseCase}", formal_prompt)
        self.assertIn("{FormalCheckResult}", repair_prompt)
        self.assertIn("{ReconstructedUseCase}", repair_prompt)

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

    def test_formal_use_case_validator_passes_fake_formal_output(self) -> None:
        report = FormalUseCaseValidator().require_valid(fake_formal_use_case())
        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.flow_step_count, 1)

    def test_formal_use_case_validator_rejects_missing_grammar(self) -> None:
        report = FormalUseCaseValidator().validate("UsecaseName: Broken\nFlow of Events:\n1. Do something")
        self.assertFalse(report.ok)
        self.assertIn("Description:", " ".join(report.issues))

    def test_fake_refiner_output_passes_validators(self) -> None:
        source = load_use_case(dataset="eTour", use_case="UC1", repo_root=self.repo_root)
        refiner = RequirementRefiner(LlmClient(self.settings))
        formal_result = refiner.refine_formal_use_case(source)
        packet_result = refiner.refine_requirement_packet(source, formal_use_case=formal_result.final_use_case)
        packet = packet_result.final_packet
        report = packet_result.final_report

        self.assertTrue(formal_result.final_report.ok)
        self.assertFalse(formal_result.repaired)
        self.assertFalse(packet_result.repaired)
        self.assertTrue(report.ok)
        self.assertIn("## Main Flow", packet)
        self.assertIn("## Alternative / Exception Flows", packet)
        self.assertIn("## Quality / Non-functional Requirements", packet)

    def test_requirement_packet_refiner_repairs_validator_failure(self) -> None:
        source = UseCaseSource(
            dataset="jira",
            use_case="SDLC-1",
            source_ref="jira_issue:SDLC-1",
            source_path=None,
            text="Jira Issue: SDLC-1\nDescription: Delete a cultural object. Operator can cancel. Connection may fail.",
        )
        invalid_packet = _valid_requirement_packet().replace(
            "## Acceptance Criteria\n- Khi Agency Operator xác nhận, hệ thống xóa cultural heritage object đã chọn.",
            "## Acceptance Criteria\n- System handles connection interruption and cancellation.",
        )
        calls: list[str] = []

        class RepairingClient:
            def complete(self, *, system: str, prompt: str) -> str:
                calls.append(prompt)
                if "Repair Requirement Packet v1" in prompt:
                    return _valid_requirement_packet()
                return invalid_packet

        result = RequirementRefiner(RepairingClient()).refine_requirement_packet(
            source,
            formal_use_case=fake_formal_use_case(),
        )

        self.assertTrue(result.repaired)
        self.assertFalse(result.initial_report.ok)
        self.assertTrue(result.final_report.ok)
        self.assertIn("Requirement Validation Report", calls[-1])

    def test_requirement_validator_rejects_exception_in_postconditions(self) -> None:
        packet = _valid_requirement_packet().replace(
            "## Postconditions\n- Cultural heritage object đã chọn không còn khả dụng sau khi xóa thành công.",
            "## Postconditions\n- Agency Operator cancels the operation.",
        )

        report = RequirementValidator().validate(packet)

        self.assertFalse(report.ok)
        self.assertIn("Postconditions must describe successful final state", " ".join(report.issues))

    def test_requirement_validator_rejects_quality_in_acceptance_criteria(self) -> None:
        packet = _valid_requirement_packet().replace(
            "## Acceptance Criteria\n- Khi Agency Operator xác nhận, hệ thống xóa cultural heritage object đã chọn.",
            "## Acceptance Criteria\n- Hệ thống chặn multiple submissions trong khi chờ xác nhận.",
        )

        report = RequirementValidator().validate(packet)

        self.assertFalse(report.ok)
        self.assertIn("Acceptance Criteria must not contain quality", " ".join(report.issues))

    def test_requirement_validator_rejects_formal_text_as_source_evidence(self) -> None:
        packet = _valid_requirement_packet().replace(
            '- "Delete a cultural object in the system."',
            '- "UsecaseName: Delete CulturalHeritage"',
        )

        report = RequirementValidator().validate(packet)

        self.assertFalse(report.ok)
        self.assertIn("original source", " ".join(report.issues))

    def test_start_or_get_run_creates_runtime_files(self) -> None:
        workflow = PhaseOneWorkflow(self.settings)
        response = workflow.start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        self.assertTrue(response["ok"])
        self.assertTrue(response["created_new_run"])
        self.assertEqual(response["current_phase"], "waiting_requirement_approval")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertIn("formal_use_case", response["latest_artifacts"])
        self.assertIn("formal_check_report", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["formal_use_case"]["ok"])
        self.assertTrue(response["quality_reports"]["requirement_packet"]["final_report"]["ok"])

        run_id = str(response["run_id"])
        run_dir = self.runtime_root / "runs" / run_id
        self.assertTrue((run_dir / "state.json").exists())
        self.assertTrue((run_dir / "artifacts" / "use_case_packet_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "formal_use_case_v1.txt").exists())
        self.assertTrue((run_dir / "artifacts" / "formal_check_report_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_packet_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "trace_graph_v1.json").exists())
        self.assertTrue((run_dir / "sync" / "tool_sync_report_v1.json").exists())

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
            patch.dict(os.environ, {"SDLC_SYNC_MODE": "mock"}),
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
        confluence_payload = calls[1][2]
        self.assertIsNotNone(confluence_payload)
        confluence_body = confluence_payload["body"]["value"]
        self.assertIn("<h1>Requirement Checkpoint - eTour UC1</h1>", confluence_body)
        self.assertIn("<table>", confluence_body)
        self.assertIn("<h2>Requirement Packet</h2>", confluence_body)
        self.assertIn("<h3>Alternative / Exception Flows</h3>", confluence_body)
        self.assertIn("<h3>Acceptance Criteria</h3>", confluence_body)
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


def _valid_requirement_packet() -> str:
    return """# Requirement Packet V1

## Goal
Cho phép Agency Operator xóa một cultural heritage object đã chọn.

## Actors
- Agency Operator

## Preconditions
- Agency Operator đã đăng nhập.

## Main Flow
1. Agency Operator chọn một cultural heritage object.
2. Hệ thống yêu cầu xác nhận.
3. Agency Operator xác nhận.
4. Hệ thống xóa cultural heritage object đã chọn.

## Alternative / Exception Flows
- Agency Operator cancels the operation.
- Connection to the ETOUR server is interrupted.

## Postconditions
- Cultural heritage object đã chọn không còn khả dụng sau khi xóa thành công.

## Acceptance Criteria
- Khi Agency Operator xác nhận, hệ thống xóa cultural heritage object đã chọn.

## Quality / Non-functional Requirements
- Hệ thống chặn multiple submissions trong khi chờ xác nhận.

## Open Questions
- None.

## Source Evidence
- "Delete a cultural object in the system."
- "Operator Agency cancels the operation."
- "Interruption of connection to the server ETOUR."
""".strip()


if __name__ == "__main__":
    unittest.main()
