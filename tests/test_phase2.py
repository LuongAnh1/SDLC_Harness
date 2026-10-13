"""Test Phase 2 V0 cho contract approve checkpoint -> design checkpoint."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.designer import DesignDraft, DesignerAgent, parse_design_model
from sdlc_harness.agents.validator import TraceabilityResult, ValidatorAgent, build_requirement_graph
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmClient, fake_design_model, fake_refined_requirement
from sdlc_harness.sync.atlassian import real_sync_design_checkpoint
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow


class StaticLlmClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.responses:
            return "[]"
        return self.responses.pop(0)


class RepairingDesigner:
    def __init__(self) -> None:
        self.update_calls = 0
        self.last_audit_report = ""

    def draft(self, *, requirement_packet: str, formal_use_case: str = "") -> DesignDraft:
        return DesignDraft(analysis_report="test architecture", design_model=fake_design_model())

    def update_design(
        self,
        *,
        requirement_packet: str,
        formal_use_case: str,
        current_design: str,
        audit_report: str,
    ) -> str:
        self.update_calls += 1
        self.last_audit_report = audit_report
        return fake_design_model()


class SequencedTraceabilityValidator:
    def __init__(self, missing_by_call: list[bool]):
        self.missing_by_call = missing_by_call
        self.calls = 0

    def construct_traceability(
        self,
        *,
        requirement_packet: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
    ) -> TraceabilityResult:
        graph = build_requirement_graph(requirement_packet)
        requirements = graph["requirements"]
        has_missing = self.missing_by_call[min(self.calls, len(self.missing_by_call) - 1)]
        self.calls += 1

        class_trace = []
        sequence_trace = []
        for index, requirement in enumerate(requirements):
            class_links = [] if has_missing and index == 0 else [{"target": "TestTarget", "reason": "test"}]
            sequence_links = [{"target": "TestMessage", "reason": "test"}]
            class_trace.append(
                {
                    "requirement": requirement["id"],
                    "content": requirement["content"],
                    "links": class_links,
                }
            )
            sequence_trace.append(
                {
                    "requirement": requirement["id"],
                    "content": requirement["content"],
                    "links": sequence_links,
                }
            )

        missing_links = {"class_diagram": [], "sequence_diagram": []}
        if has_missing:
            missing_requirement = requirements[0]
            missing_links["class_diagram"] = [
                {
                    "requirement": missing_requirement["id"],
                    "content": missing_requirement["content"],
                }
            ]
        return TraceabilityResult(
            requirement_graph=graph,
            class_trace=class_trace,
            sequence_trace=sequence_trace,
            missing_links=missing_links,
        )


class PhaseTwoTest(unittest.TestCase):
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

    def _create_phase1_run(self) -> dict[str, object]:
        return PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

    def test_settings_reads_design_repair_limit(self) -> None:
        self.assertEqual(self.settings.design_repair_limit, 2)
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_DESIGN_REPAIR_LIMIT": "0"},
        )

        self.assertEqual(settings.design_repair_limit, 0)

    def test_designer_fake_mode_creates_analysis_and_design_model(self) -> None:
        draft = DesignerAgent(LlmClient(self.settings)).draft(
            requirement_packet="# Requirement Packet V1\n\n## Goal\nDelete cultural heritage.",
            formal_use_case="UsecaseName: Delete Cultural Heritage",
        )

        self.assertIn("layered architecture", draft.analysis_report.lower())
        self.assertIn("## Class Diagram", draft.design_model)
        self.assertIn("## Sequence Diagrams", draft.design_model)
        self.assertIn("@startuml", draft.design_model)

    def test_designer_update_design_fake_mode_returns_parseable_design(self) -> None:
        updated_design = DesignerAgent(LlmClient(self.settings)).update_design(
            requirement_packet=fake_refined_requirement(""),
            formal_use_case="UsecaseName: Delete Cultural Heritage",
            current_design=fake_design_model(),
            audit_report="# Design Quality Report\n\n- Missing design links: 1",
        )

        parsed = parse_design_model(updated_design)
        self.assertTrue(parsed.class_graph["nodes"])
        self.assertTrue(parsed.sequence_graph["sequenceDiagrams"])

    def test_parse_design_model_reads_class_and_sequence_graph(self) -> None:
        parsed = parse_design_model(fake_design_model())

        class_names = {node["className"] for node in parsed.class_graph["nodes"]}
        self.assertIn("DeleteCulturalHeritageBoundary", class_names)
        self.assertIn("DeleteCulturalHeritageService", class_names)
        self.assertGreaterEqual(len(parsed.class_graph["relationships"]), 2)

        sequence_diagram = parsed.sequence_graph["sequenceDiagrams"][0]
        messages = [message["message"] for message in sequence_diagram["messages"]]
        self.assertIn("requestDeletion(heritageId)", messages)
        self.assertIn("showResult(success message)", messages)

    def test_parse_design_model_rejects_missing_sequence_diagram(self) -> None:
        with self.assertRaises(HarnessError) as ctx:
            parse_design_model(
                """## Class Diagram
@startuml
class OnlyClass {
  +run() : void
}
@enduml"""
            )

        self.assertEqual(ctx.exception.error_code, "design_parser_failed")

    def test_build_requirement_graph_creates_stable_requirement_ids(self) -> None:
        graph = build_requirement_graph(fake_refined_requirement(""))

        ids = [item["id"] for item in graph["requirements"]]
        self.assertIn("REQ-PRE-001", ids)
        self.assertIn("REQ-MAIN-001", ids)
        self.assertIn("REQ-AC-001", ids)
        self.assertIn("REQ-NFR-001", ids)
        self.assertEqual(graph["count"], len(graph["requirements"]))

    def test_validator_fake_mode_creates_traceability_without_missing_links(self) -> None:
        parsed = parse_design_model(fake_design_model())
        result = ValidatorAgent(LlmClient(self.settings)).construct_traceability(
            requirement_packet=fake_refined_requirement(""),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.missing_links["class_diagram"], [])
        self.assertEqual(result.missing_links["sequence_diagram"], [])
        self.assertEqual(len(result.class_trace), result.requirement_graph["count"])
        self.assertEqual(len(result.sequence_trace), result.requirement_graph["count"])

    def test_validator_rejects_invalid_traceability_json(self) -> None:
        parsed = parse_design_model(fake_design_model())

        with self.assertRaises(HarnessError) as ctx:
            ValidatorAgent(StaticLlmClient(["not json"])).construct_traceability(
                requirement_packet=fake_refined_requirement(""),
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )

        self.assertEqual(ctx.exception.error_code, "validator_traceability_failed")

    def test_validator_rejects_traceability_targets_not_in_graph(self) -> None:
        parsed = parse_design_model(fake_design_model())
        first_requirement = build_requirement_graph(fake_refined_requirement(""))["requirements"][0]
        bad_trace = json.dumps(
            [
                {
                    "requirement": first_requirement["id"],
                    "content": first_requirement["content"],
                    "links": [{"target": "GhostClass.missingMethod", "reason": "bad"}],
                }
            ]
        )

        with self.assertRaises(HarnessError) as ctx:
            ValidatorAgent(StaticLlmClient([bad_trace])).construct_traceability(
                requirement_packet=fake_refined_requirement(""),
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )

        self.assertEqual(ctx.exception.error_code, "validator_traceability_failed")

    def test_validator_accepts_class_attribute_target(self) -> None:
        parsed = parse_design_model(fake_design_model())
        first_requirement = build_requirement_graph(fake_refined_requirement(""))["requirements"][0]
        class_trace = json.dumps(
            [
                {
                    "requirement": first_requirement["id"],
                    "content": first_requirement["content"],
                    "links": [
                        {
                            "target": "CulturalHeritage.id",
                            "reason": "valid attribute target from class graph",
                        }
                    ],
                }
            ]
        )
        sequence_trace = json.dumps(
            [
                {
                    "requirement": first_requirement["id"],
                    "content": first_requirement["content"],
                    "links": [
                        {
                            "target": "requestDeletion(heritageId)",
                            "reason": "valid sequence target",
                        }
                    ],
                }
            ]
        )

        result = ValidatorAgent(StaticLlmClient([class_trace, sequence_trace])).construct_traceability(
            requirement_packet=fake_refined_requirement(""),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
        )

        self.assertEqual(result.class_trace[0]["links"][0]["target"], "CulturalHeritage.id")

    def test_validator_accepts_sequence_return_arrow_target(self) -> None:
        parsed = parse_design_model(fake_design_model())
        first_requirement = build_requirement_graph(fake_refined_requirement(""))["requirements"][0]
        class_trace = json.dumps(
            [
                {
                    "requirement": first_requirement["id"],
                    "content": first_requirement["content"],
                    "links": [
                        {
                            "target": "DeleteCulturalHeritageService.deleteHeritage",
                            "reason": "valid class target",
                        }
                    ],
                }
            ]
        )
        sequence_trace = json.dumps(
            [
                {
                    "requirement": first_requirement["id"],
                    "content": first_requirement["content"],
                    "links": [
                        {
                            "target": "Repository --> Service: CulturalHeritage",
                            "reason": "valid return message target",
                        }
                    ],
                }
            ]
        )

        result = ValidatorAgent(StaticLlmClient([class_trace, sequence_trace])).construct_traceability(
            requirement_packet=fake_refined_requirement(""),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
        )

        self.assertEqual(result.sequence_trace[0]["links"][0]["target"], "Repository --> Service: CulturalHeritage")

    def test_validator_reports_missing_links_when_trace_links_are_empty(self) -> None:
        parsed = parse_design_model(fake_design_model())
        graph = build_requirement_graph(fake_refined_requirement(""))
        empty_trace = json.dumps(
            [
                {"requirement": item["id"], "content": item["content"], "links": []}
                for item in graph["requirements"]
            ]
        )

        result = ValidatorAgent(StaticLlmClient([empty_trace, empty_trace])).construct_traceability(
            requirement_packet=fake_refined_requirement(""),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
        )

        self.assertFalse(result.ok)
        self.assertEqual(len(result.missing_links["class_diagram"]), graph["count"])
        self.assertEqual(len(result.missing_links["sequence_diagram"]), graph["count"])

    def test_approve_checkpoint_creates_design_checkpoint(self) -> None:
        created = self._create_phase1_run()
        response = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertTrue(response["ok"])
        self.assertFalse(response["created_new_run"])
        self.assertEqual(response["current_phase"], "design_review")
        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertEqual(response["pending_checkpoint"]["type"], "design_approval")
        self.assertEqual(response["last_sync"]["phase"], "design_checkpoint")
        self.assertEqual(response["last_sync"]["status"], "mock_synced")
        self.assertIn("design_artifact", response["latest_artifacts"])
        self.assertIn("requirement_graph", response["latest_artifacts"])
        self.assertIn("requirement_design_trace", response["latest_artifacts"])
        self.assertTrue(response["quality_reports"]["design_traceability"]["ok"])
        self.assertEqual(response["quality_reports"]["design_traceability"]["mode"], "traceability_v1")
        self.assertIn("không phát hiện missing link", response["quality_reports"]["design_traceability"]["summary"])

        run_dir = self.runtime_root / "runs" / str(created["run_id"])
        self.assertTrue((run_dir / "artifacts" / "designer_analysis_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "design_artifact_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "class_diagram_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "requirement_design_trace_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "missing_design_links_v1.json").exists())
        self.assertTrue((run_dir / "artifacts" / "design_quality_report_v1.md").exists())
        self.assertTrue((run_dir / "sync" / "design-jira-comment.md").exists())
        self.assertTrue((run_dir / "sync" / "design-confluence-page.md").exists())
        class_graph = json.loads((run_dir / "artifacts" / "class_diagram_graph_v1.json").read_text(encoding="utf-8"))
        class_names = {node["className"] for node in class_graph["nodes"]}
        self.assertIn("DeleteCulturalHeritageBoundary", class_names)
        sequence_graph = json.loads((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").read_text(encoding="utf-8"))
        first_sequence = sequence_graph["sequenceDiagrams"][0]
        self.assertTrue(
            any(message["message"] == "requestDeletion(heritageId)" for message in first_sequence["messages"])
        )
        trace = json.loads((run_dir / "artifacts" / "requirement_design_trace_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["mode"], "traceability_v1")
        self.assertIn("class_trace", trace)
        self.assertIn("sequence_trace", trace)
        missing = json.loads((run_dir / "artifacts" / "missing_design_links_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(missing["mode"], "traceability_v1")
        self.assertEqual(missing["missing_count"], 0)
        self.assertIn("class_diagram", missing["missing_links"])
        report = json.loads((run_dir / "sync" / "tool_sync_report_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "tool_sync_report_v2")
        self.assertEqual(report["phase"], "design_checkpoint")
        self.assertEqual(report["checkpoint"]["type"], "design_approval")

    def test_repair_loop_passes_after_one_repair(self) -> None:
        created = self._create_phase1_run()
        workflow = PhaseTwoWorkflow(self.settings)
        designer = RepairingDesigner()
        validator = SequencedTraceabilityValidator([True, False])
        workflow.designer = designer
        workflow.validator = validator

        response = workflow.approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertEqual(response["current_phase"], "design_review")
        self.assertEqual(designer.update_calls, 1)
        self.assertEqual(validator.calls, 2)
        self.assertTrue(response["latest_artifacts"]["design_artifact"].endswith("design_artifact_v2.md"))
        self.assertEqual(response["pending_checkpoint"]["artifact_ref"], "design_artifact_v2")
        self.assertEqual(response["quality_reports"]["design_traceability"]["versions"], ["v1", "v2"])
        self.assertTrue(response["quality_reports"]["design_traceability"]["ok"])
        self.assertIn("Missing design links: 1", designer.last_audit_report)

        run_dir = self.runtime_root / "runs" / str(created["run_id"])
        self.assertTrue((run_dir / "artifacts" / "design_artifact_v1.md").exists())
        self.assertTrue((run_dir / "artifacts" / "design_artifact_v2.md").exists())
        quality_v2 = (run_dir / "artifacts" / "design_quality_report_v2.md").read_text(encoding="utf-8")
        self.assertIn("Repair iterations: 1", quality_v2)
        sync_report = json.loads((run_dir / "sync" / "tool_sync_report_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(sync_report["checkpoint"]["artifact_ref"], "design_artifact_v2")

    def test_repair_loop_blocks_after_limit(self) -> None:
        created = self._create_phase1_run()
        workflow = PhaseTwoWorkflow(self.settings)
        designer = RepairingDesigner()
        validator = SequencedTraceabilityValidator([True, True, True])
        workflow.designer = designer
        workflow.validator = validator

        response = workflow.approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["current_phase"], "design_review")
        self.assertIsNone(response["pending_checkpoint"])
        self.assertEqual(response["last_error"]["error_code"], "design_traceability_blocked")
        self.assertEqual(designer.update_calls, 2)
        self.assertEqual(validator.calls, 3)
        self.assertTrue(response["latest_artifacts"]["design_artifact"].endswith("design_artifact_v3.md"))
        self.assertEqual(response["quality_reports"]["design_traceability"]["versions"], ["v1", "v2", "v3"])
        self.assertFalse(response["quality_reports"]["design_traceability"]["ok"])

        run_dir = self.runtime_root / "runs" / str(created["run_id"])
        self.assertTrue((run_dir / "artifacts" / "design_artifact_v3.md").exists())
        quality_v3 = (run_dir / "artifacts" / "design_quality_report_v3.md").read_text(encoding="utf-8")
        self.assertIn("Repair iterations: 2", quality_v3)
        self.assertFalse((run_dir / "sync" / "tool_sync_report_v2.json").exists())

    def test_repair_loop_respects_configured_zero_limit(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_REPAIR_LIMIT": "0",
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")
        workflow = PhaseTwoWorkflow(settings)
        designer = RepairingDesigner()
        validator = SequencedTraceabilityValidator([True, False])
        workflow.designer = designer
        workflow.validator = validator

        response = workflow.approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "blocked")
        self.assertEqual(designer.update_calls, 0)
        self.assertEqual(validator.calls, 1)
        self.assertEqual(response["quality_reports"]["design_traceability"]["versions"], ["v1"])
        self.assertTrue(response["latest_artifacts"]["design_artifact"].endswith("design_artifact_v1.md"))
        run_dir = self.runtime_root / "runs" / str(created["run_id"])
        self.assertFalse((run_dir / "sync" / "tool_sync_report_v2.json").exists())

    def test_sync_run_resyncs_design_checkpoint(self) -> None:
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        synced = PhaseOneWorkflow(self.settings).sync_run(run_id=str(approved["run_id"]))

        self.assertEqual(synced["run_id"], approved["run_id"])
        self.assertEqual(synced["last_sync"]["phase"], "design_checkpoint")
        self.assertEqual(synced["last_sync"]["status"], "mock_synced")
        self.assertIn("review design", synced["summary_for_user"])

    def test_real_sync_design_writes_jira_and_confluence_targets(self) -> None:
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
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        artifact_paths = {str(key): str(value) for key, value in approved["latest_artifacts"].items()}
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if "/comment" in url:
                return {"id": "10002"}
            if url.endswith("/wiki/api/v2/pages"):
                return {"id": "20002", "_links": {"webui": "/spaces/SDLC/pages/20002/Design"}}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_design_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(approved["run_id"]),
            source_ref=str(approved["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["phase"], "design_checkpoint")
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        jira_payload = calls[0][2]
        self.assertIsNotNone(jira_payload)
        self.assertIn("Design review", jira_payload["body"]["content"][0]["content"][0]["text"])
        jira_blocks = jira_payload["body"]["content"]
        self.assertTrue(any(block["type"] == "codeBlock" for block in jira_blocks))
        self.assertTrue(
            any(
                block["type"] == "heading" and block["content"][0]["text"] == "Diagrams"
                for block in jira_blocks
            )
        )
        confluence_payload = calls[1][2]
        self.assertIsNotNone(confluence_payload)
        confluence_body = confluence_payload["body"]["value"]
        self.assertIn("<h1>Design Checkpoint - eTour UC1</h1>", confluence_body)
        self.assertIn("<h2>Design Diagrams</h2>", confluence_body)
        self.assertIn("<h3>Class Diagram</h3>", confluence_body)
        self.assertIn("<h3>Sequence Diagram</h3>", confluence_body)
        self.assertIn("<ac:adf-extension>", confluence_body)
        self.assertIn("plantuml-confluence", confluence_body)
        self.assertIn('key="diagram-code"', confluence_body)
        self.assertIn('ac:name="code"', confluence_body)
        self.assertNotIn("<ac:image", confluence_body)
        self.assertIn("@startuml", confluence_body)
        self.assertIn("<h2>Quality Report</h2>", confluence_body)
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "tool_sync_report_v2")
        self.assertEqual(report["phase"], "design_checkpoint")
        self.assertEqual(report["mode"], "real")
        self.assertEqual(report["targets"][0]["operation"], "created")
        self.assertEqual(report["targets"][1]["operation"], "created")
        self.assertNotIn("diagram_images", report["targets"][1])

    def test_real_sync_design_updates_existing_phase1_targets(self) -> None:
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
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        run_id = str(approved["run_id"])
        artifact_paths = {str(key): str(value) for key, value in approved["latest_artifacts"].items()}
        sync_dir = self.runtime_root / "runs" / run_id / "sync"
        (sync_dir / "tool_sync_report_v1.json").write_text(
            json.dumps(
                {
                    "targets": [
                        {"tool": "jira", "status": "synced", "external_id": "10001"},
                        {"tool": "confluence", "status": "synced", "external_id": "20001"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, str, dict[str, object] | None]] = []

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            calls.append((method, url, payload))
            if method == "PUT" and "/comment/10001" in url:
                return {"id": "10001"}
            if method == "GET" and url.endswith("/wiki/api/v2/pages/20001"):
                return {"id": "20001", "version": {"number": 2}}
            if method == "PUT" and url.endswith("/wiki/api/v2/pages/20001"):
                return {"id": "20001", "_links": {"webui": "/spaces/SDLC/pages/20001/Design"}}
            self.fail(f"Unexpected request: {method} {url}")

        result = real_sync_design_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=run_id,
            source_ref=str(approved["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "synced")
        self.assertEqual([call[0] for call in calls], ["PUT", "GET", "PUT"])
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["targets"][0]["operation"], "updated")
        self.assertEqual(report["targets"][1]["operation"], "updated")
        self.assertEqual(calls[2][2]["version"]["number"], 3)

    def test_real_sync_design_records_partial_failure(self) -> None:
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
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        artifact_paths = {str(key): str(value) for key, value in approved["latest_artifacts"].items()}

        def fake_request(method: str, url: str, payload: dict[str, object] | None):
            if "/comment" in url:
                return {"id": "10002"}
            raise RuntimeError("boom")

        result = real_sync_design_checkpoint(
            store=PhaseOneWorkflow(self.settings).store,
            settings=settings,
            run_id=str(approved["run_id"]),
            source_ref=str(approved["source_ref"]),
            artifact_paths=artifact_paths,
            request_json=fake_request,
        )

        self.assertEqual(result["status"], "partial_failed")

    def test_approve_checkpoint_rejects_wrong_checkpoint_id(self) -> None:
        created = self._create_phase1_run()

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(
                run_id=str(created["run_id"]),
                checkpoint_id="checkpoint_requirement_wrong",
            )

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_approve_checkpoint_rejects_missing_pending_checkpoint(self) -> None:
        created = self._create_phase1_run()
        phase1 = PhaseOneWorkflow(self.settings)
        state = phase1.store.read_state(str(created["run_id"]))
        state["pending_checkpoint"] = None
        phase1.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "no_pending_checkpoint")

    def test_approve_checkpoint_rejects_non_requirement_status(self) -> None:
        created = self._create_phase1_run()
        phase2 = PhaseTwoWorkflow(self.settings)
        phase2.approve_checkpoint(run_id=str(created["run_id"]))

        with self.assertRaises(HarnessError) as ctx:
            phase2.approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "checkpoint_not_current")

    def test_approve_checkpoint_rejects_missing_requirement_artifact(self) -> None:
        created = self._create_phase1_run()
        phase1 = PhaseOneWorkflow(self.settings)
        state = phase1.store.read_state(str(created["run_id"]))
        latest = dict(state["latest_artifacts"])
        latest.pop("requirement_packet")
        state["latest_artifacts"] = latest
        phase1.store.write_state(state)

        with self.assertRaises(HarnessError) as ctx:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(ctx.exception.error_code, "approved_requirement_not_found")

    def test_cli_approve_checkpoint_returns_json(self) -> None:
        created = self._create_phase1_run()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "approve-checkpoint",
                    "--run-id",
                    str(created["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "waiting_design_approval")
        self.assertEqual(payload["last_sync"]["phase"], "design_checkpoint")


if __name__ == "__main__":
    unittest.main()
