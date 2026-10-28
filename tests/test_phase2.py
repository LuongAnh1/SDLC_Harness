"""Kiểm thử Phase 2 V0 cho contract approve checkpoint -> design checkpoint."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main
from sdlc_harness.agents.designer import DesignDraft, DesignerAgent, parse_design_model
from sdlc_harness.agents.validator import (
    DesignFinding,
    DesignValidationResult,
    DesignValidator,
    TraceabilityResult,
    ValidatorAgent,
    build_requirement_graph,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmClient
from sdlc_harness.llm.fake import fake_design_model, fake_refined_requirement
from sdlc_harness.sync.atlassian import real_sync_design_checkpoint
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow, _design_repair_stalled
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow
from tests._support import GitRepositoryTemplate, RuntimeDirectoryTemplate, populate_typescript_target


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

    def draft(self, *, requirement_packet: str, formal_use_case: str = "", canonical_requirement=None) -> DesignDraft:
        return DesignDraft(analysis_report="test architecture", design_model=fake_design_model())

    def update_design(
        self,
        *,
        requirement_packet: str,
        formal_use_case: str,
        current_design: str,
        audit_report: str,
        canonical_requirement=None,
    ) -> str:
        self.update_calls += 1
        self.last_audit_report = audit_report
        return fake_design_model()


class PreflightRepairDesigner(RepairingDesigner):
    def draft(self, *, requirement_packet: str, formal_use_case: str = "", canonical_requirement=None) -> DesignDraft:
        broken = fake_design_model().replace(
            "Boundary -> Service: validateDeletion(heritageId)",
            "Boundary -> Service: missingOperation(heritageId)",
        )
        return DesignDraft(analysis_report="test progressive architecture", design_model=broken)


class ComponentRoutingRepairDesigner(RepairingDesigner):
    def __init__(self) -> None:
        super().__init__()
        self.draft_requirement_packet = ""
        self.repair_requirement_packet = ""

    def draft(self, *, requirement_packet: str, formal_use_case: str = "", canonical_requirement=None) -> DesignDraft:
        self.draft_requirement_packet = requirement_packet
        broken = fake_design_model().replace("class CulturalHeritage <<contracts>>", "class CulturalHeritage")
        return DesignDraft(analysis_report="test component routing", design_model=broken)

    def update_design(
        self,
        *,
        requirement_packet: str,
        formal_use_case: str,
        current_design: str,
        audit_report: str,
        canonical_requirement=None,
    ) -> str:
        self.repair_requirement_packet = requirement_packet
        return super().update_design(
            requirement_packet=requirement_packet,
            formal_use_case=formal_use_case,
            current_design=current_design,
            audit_report=audit_report,
            canonical_requirement=canonical_requirement,
        )


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
        canonical_requirement=None,
        attempt_recorder=None,
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


class SequencedDesignValidator:
    def __init__(self, findings_by_call: list[list[DesignFinding]]):
        self.findings_by_call = findings_by_call
        self.calls = 0

    def review(self, *, traceability: TraceabilityResult, repair_iterations: int = 0, **kwargs) -> DesignValidationResult:
        findings = self.findings_by_call[min(self.calls, len(self.findings_by_call) - 1)]
        self.calls += 1
        return DesignValidationResult(
            traceability=traceability,
            blocking_findings=findings,
            warnings=[],
            rule_review={"ok": not findings, "findings": []},
            llm_review={"ok": not findings, "summary": "test", "findings": []},
            repair_iterations=repair_iterations,
        )


def design_finding(name: str, *, excerpt: str = "") -> DesignFinding:
    return DesignFinding(
        finding_id=f"SEM-{name}",
        category="cross_diagram_contract",
        severity="blocking",
        message=f"Problem {name}",
        expected_change=f"Fix {name}",
        requirement_ids=["REQ-1"],
        diagram_ids=["sequence_diagram_1"],
        evidence=[{"source": "design", "ref": f"contract:{name}", "excerpt": excerpt}],
    )


class PhaseTwoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.target_repo_template = GitRepositoryTemplate(populate_typescript_target)
        cls.runtime_template_temp = tempfile.TemporaryDirectory()
        cls.phase1_target = cls.target_repo_template.materialize(
            Path(cls.runtime_template_temp.name) / "target"
        )
        runtime_root = Path(cls.runtime_template_temp.name) / ".runtime"
        settings = load_settings(
            root=Path(__file__).resolve().parents[1],
            runtime_root=runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_DESIGN_REPAIR_LIMIT": "2",
                "SDLC_DESIGN_VALIDATION_TRIALS": "3",
                "SDLC_DESIGN_LLM_CALL_BUDGET": "16",
                "SDLC_TARGET_REPO_PATH": str(cls.phase1_target.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(cls.phase1_target.remote.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_TARGET_WORKTREE_ROOT": str(Path(cls.runtime_template_temp.name) / "worktrees"),
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )
        cls.phase1_run_id = str(created["run_id"])
        cls.phase1_runtime_template = RuntimeDirectoryTemplate(runtime_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.phase1_runtime_template.close()
        cls.runtime_template_temp.cleanup()
        cls.target_repo_template.close()

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.target = self.target_repo_template.materialize(Path(self.temp_dir.name) / "target")
        suffix = hashlib.sha256(self._testMethodName.encode("utf-8")).hexdigest()[:8]
        self.run_id = f"run_20261020_000000_{suffix}"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                "SDLC_DESIGN_REPAIR_LIMIT": "2",
                "SDLC_DESIGN_VALIDATION_TRIALS": "3",
                "SDLC_DESIGN_LLM_CALL_BUDGET": "16",
                "SDLC_TARGET_REPO_PATH": str(self.target.canonical),
                "SDLC_TARGET_REPO_REMOTE": str(self.target.remote.resolve()),
                "SDLC_TARGET_BASE_BRANCH": "main",
                "SDLC_TARGET_WORKTREE_ROOT": str(Path(self.temp_dir.name) / "worktrees"),
            },
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_phase1_run(self) -> dict[str, object]:
        self.phase1_runtime_template.materialize(
            self.runtime_root,
            replacements={
                self.phase1_run_id: self.run_id,
                str(self.phase1_target.canonical): str(self.target.canonical),
                self.phase1_target.canonical.as_posix(): self.target.canonical.as_posix(),
                str(self.phase1_target.remote.resolve()): str(self.target.remote.resolve()),
                self.phase1_target.remote.resolve().as_posix(): self.target.remote.resolve().as_posix(),
            },
        )
        return PhaseOneWorkflow(self.settings).get_status(run_id=self.run_id)

    def _create_phase1_run_fresh(self) -> dict[str, object]:
        return PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour",
            use_case="UC1",
            requested_by="test",
        )

    def _target_overrides(self) -> dict[str, str]:
        return {
            "SDLC_TARGET_REPO_PATH": str(self.target.canonical),
            "SDLC_TARGET_REPO_REMOTE": str(self.target.remote.resolve()),
            "SDLC_TARGET_BASE_BRANCH": "main",
            "SDLC_TARGET_WORKTREE_ROOT": str(Path(self.temp_dir.name) / "worktrees"),
        }

    def test_settings_reads_design_repair_limit(self) -> None:
        self.assertEqual(self.settings.design_repair_limit, 2)
        self.assertEqual(self.settings.design_validation_trials, 3)
        self.assertEqual(self.settings.design_llm_call_budget, 16)
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_DESIGN_REPAIR_LIMIT": "0"},
        )
        self.assertEqual(settings.design_repair_limit, 0)

    def test_step_review_decision_survives_design_runtime_reset(self) -> None:
        review = {
            "enabled": True,
            "call_id": "designer-0001",
            "decision": "approve",
            "note": "",
        }
        workflow = PhaseTwoWorkflow(self.settings, designer_step_review=review)

        workflow._reset_design_runtime()

        self.assertEqual(workflow.designer_tool_loop.step_review, review)

    def test_production_flow_blocks_safely_when_tight_budget_cannot_converge(self) -> None:
        settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                **self._target_overrides(),
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_VALIDATION_TRIALS": "3",
                "SDLC_DESIGN_LLM_CALL_BUDGET": "2",
            },
        )
        created = PhaseOneWorkflow(settings).start_or_get_run(dataset="eTour", use_case="UC1", requested_by="test")

        response = PhaseTwoWorkflow(settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "design_blocked")
        self.assertEqual(response["last_error"]["error_code"], "design_repair_stalled")
        self.assertIn("revalidate_design", response["allowed_next_actions"])
        self.assertIsNone(response["pending_checkpoint"])

    def test_repair_stalls_when_canonical_blockers_repeat_after_graph_change(self) -> None:
        self.assertTrue(
            _design_repair_stalled(
                prior_digest="sha256:v1",
                current_digest="sha256:v2",
                prior_blocker_keys=frozenset({"branch_parent_ambiguous|sc-4"}),
                current_blocker_keys=frozenset({"branch_parent_ambiguous|sc-4"}),
            )
        )
        self.assertFalse(
            _design_repair_stalled(
                prior_digest="sha256:v1",
                current_digest="sha256:v2",
                prior_blocker_keys=frozenset({"branch_parent_ambiguous|sc-4"}),
                current_blocker_keys=frozenset({"terminal_state|sc-4"}),
            )
        )


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
        self.assertIn("CulturalHeritage", class_names)
        self.assertIn("DeleteCulturalHeritageService", class_names)
        self.assertGreaterEqual(len(parsed.class_graph["relationships"]), 2)

        sequence_diagram = parsed.sequence_graph["sequenceDiagrams"][0]
        messages = [message["message"] for message in sequence_diagram["messages"]]
        self.assertIn("requestDeletion(heritageId)", messages)
        self.assertIn("showResult(success message)", messages)
        self.assertEqual(
            [event["order"] for event in sequence_diagram["events"]],
            list(range(1, len(sequence_diagram["events"]) + 1)),
        )

    def test_sequence_parser_preserves_nested_control_path_and_reports_unclosed_fragment(self) -> None:
        design = fake_design_model().replace(
            "AO -> Boundary: requestDeletion(heritageId)",
            "alt selected\nopt confirmed\nAO -> Boundary: requestDeletion(heritageId)\nend",
        )

        parsed = parse_design_model(design)
        sequence = parsed.sequence_graph["sequenceDiagrams"][0]
        request = next(item for item in sequence["messages"] if item["message"] == "requestDeletion(heritageId)")

        self.assertEqual(len(request["controlPath"]), 2)
        self.assertEqual(sequence["parseIssues"][0]["code"], "unclosed_fragment")

    def test_design_validator_blocks_sequence_operation_missing_from_class_contract(self) -> None:
        design = fake_design_model().replace("delete(heritageId)", "deleteUnknown(heritageId)")
        parsed = parse_design_model(design)
        traceability = TraceabilityResult(
            requirement_graph={"requirements": [], "count": 0},
            class_trace=[],
            sequence_trace=[],
            missing_links={"class_diagram": [], "sequence_diagram": []},
        )
        semantic = json.dumps({"ok": True, "summary": "semantic pass", "findings": []})

        result = DesignValidator(StaticLlmClient([semantic])).review(
            requirement_packet="approved requirement",
            design_artifact=design,
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
            traceability=traceability,
        )

        self.assertFalse(result.ok)
        self.assertTrue(any(item.category == "cross_diagram_contract" for item in result.blocking_findings))

    def test_semantic_validator_downgrades_unsupported_blocker_without_requirement_id(self) -> None:
        parsed = parse_design_model(fake_design_model())
        traceability = TraceabilityResult(
            requirement_graph={"requirements": [], "count": 0},
            class_trace=[],
            sequence_trace=[],
            missing_links={"class_diagram": [], "sequence_diagram": []},
        )
        semantic = json.dumps(
            {
                "ok": False,
                "summary": "transaction suggestion",
                "findings": [
                    {
                        "id": "SEM-001",
                        "category": "transaction_boundary",
                        "severity": "blocking",
                        "message": "Consider a transaction.",
                        "expected_change": "Document transaction behavior.",
                        "requirement_ids": [],
                        "diagram_ids": ["sequence_diagram_1"],
                        "evidence": [],
                    }
                ],
            }
        )

        result = DesignValidator(StaticLlmClient([semantic])).review(
            requirement_packet="approved requirement",
            design_artifact=fake_design_model(),
            class_graph=parsed.class_graph,
            sequence_graph=parsed.sequence_graph,
            traceability=traceability,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.warnings[0].severity, "warning")

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
            ValidatorAgent(StaticLlmClient(["not json", "still not json"])).construct_traceability(
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

    def test_validator_accepts_sequence_note_target_from_control_flow(self) -> None:
        parsed = parse_design_model(fake_design_model())
        parsed.sequence_graph["sequenceDiagrams"][0].setdefault("controlFlows", []).append(
            {
                "type": "note",
                "condition": "note right of Controller: Duplicate submissions are blocked",
            }
        )
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
                            "target": "note right of Controller: Duplicate submissions are blocked\nwhile confirmation is pending",
                            "reason": "valid note target from sequence control flow",
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

        self.assertEqual(
            result.sequence_trace[0]["links"][0]["target"],
            "note right of Controller: Duplicate submissions are blocked",
        )

    def test_validator_accepts_sequence_alt_condition_target_from_control_flow(self) -> None:
        parsed = parse_design_model(fake_design_model())
        parsed.sequence_graph["sequenceDiagrams"][0].setdefault("controlFlows", []).append(
            {
                "type": "alt",
                "condition": "Duplicate submission detected",
            }
        )
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
                            "target": "Duplicate submission detected",
                            "reason": "valid alt/else condition from sequence control flow",
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

        self.assertEqual(result.sequence_trace[0]["links"][0]["target"], "Duplicate submission detected")

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
        created = self._create_phase1_run_fresh()
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
        self.assertEqual(response["quality_reports"]["design_traceability"]["mode"], "traceability_v2")
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
        mock_jira = (run_dir / "sync" / "design-jira-comment.md").read_text(encoding="utf-8")
        self.assertIn("## Repository Landscape", mock_jira)
        self.assertIn("Inventory: 5 component(s)", mock_jira)
        class_graph = json.loads((run_dir / "artifacts" / "class_diagram_graph_v1.json").read_text(encoding="utf-8"))
        class_names = {node["className"] for node in class_graph["nodes"]}
        self.assertIn("CulturalHeritage", class_names)
        sequence_graph = json.loads((run_dir / "artifacts" / "sequence_diagram_graph_v1.json").read_text(encoding="utf-8"))
        first_sequence = sequence_graph["sequenceDiagrams"][0]
        self.assertTrue(first_sequence["nodes"])
        trace = json.loads((run_dir / "artifacts" / "requirement_design_trace_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(trace["mode"], "traceability_v2")
        self.assertIn("class_trace", trace)
        self.assertIn("sequence_trace", trace)
        missing = json.loads((run_dir / "artifacts" / "missing_design_links_v1.json").read_text(encoding="utf-8"))
        self.assertEqual(missing["mode"], "traceability_v2")
        self.assertEqual(missing["missing_count"], 0)
        self.assertIn("class_diagram", missing["missing_links"])
        report = json.loads((run_dir / "sync" / "tool_sync_report_v2.json").read_text(encoding="utf-8"))
        self.assertEqual(report["version"], "tool_sync_report_v2")
        self.assertEqual(report["phase"], "design_checkpoint")
        self.assertEqual(report["checkpoint"]["type"], "design_approval")

    def test_repo_discovery_pause_can_resume_before_design(self) -> None:
        created = self._create_phase1_run_fresh()
        run_id = str(created["run_id"])
        workflow = PhaseTwoWorkflow(self.settings)
        real_create = workflow.repo_discovery.workspace_manager.create_for_run
        attempts = 0

        def flaky_create(current_run_id: str):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise HarnessError("workspace_fetch_failed", "temporary fetch failure", run_id=current_run_id)
            return real_create(current_run_id)

        with patch.object(
            workflow.repo_discovery.workspace_manager,
            "create_for_run",
            side_effect=flaky_create,
        ):
            paused = workflow.approve_checkpoint(run_id=run_id)
            resumed = workflow.continue_repo_discovery(run_id=run_id)

        self.assertEqual(paused["status"], "repo_discovery_paused")
        self.assertEqual(paused["allowed_next_actions"], ["continue_run", "get_status"])
        self.assertEqual(paused["last_error"]["error_code"], "workspace_fetch_failed")
        self.assertEqual(resumed["status"], "waiting_design_approval")
        self.assertIn("repo_landscape", resumed["latest_artifacts"])
        self.assertIn("run_workspace", resumed["latest_artifacts"])
        self.assertEqual(resumed["quality_reports"]["repo_discovery"]["status"], "passed")




    def test_projection_tampering_is_rejected_before_design_state_mutation(self) -> None:
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        state_path = self.runtime_root / "runs" / run_id / "state.json"
        state_before = json.loads(state_path.read_text(encoding="utf-8"))
        requirement_path = self.repo_root / str(state_before["latest_artifacts"]["requirement_packet"])
        requirement_path.write_text(
            "# Requirement Packet V2\n\n## Notes\nTampered projection.",
            encoding="utf-8",
        )

        with self.assertRaises(HarnessError) as caught:
            PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=run_id)

        self.assertEqual(caught.exception.error_code, "requirement_projection_mismatch")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), state_before)
        self.assertFalse((self.runtime_root / "runs" / run_id / "sync" / "tool_sync_report_v2.json").exists())



    def test_sync_run_resyncs_design_checkpoint(self) -> None:
        created = self._create_phase1_run()
        approved = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        self.assertIn("sync_run", approved["allowed_next_actions"])

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
        self.assertNotIn(
            str(self.runtime_root).replace("\\", "\\\\"),
            json.dumps(jira_payload, ensure_ascii=False),
        )
        self.assertIn("Design review", jira_payload["body"]["content"][0]["content"][0]["text"])
        jira_blocks = jira_payload["body"]["content"]
        self.assertTrue(any(block["type"] == "codeBlock" for block in jira_blocks))
        self.assertTrue(
            any(
                block["type"] == "heading" and block["content"][0]["text"] == "Repository landscape"
                for block in jira_blocks
            )
        )
        self.assertTrue(
            any(
                block["type"] == "heading" and block["content"][0]["text"] == "Diagrams"
                for block in jira_blocks
            )
        )
        confluence_payload = calls[1][2]
        self.assertIsNotNone(confluence_payload)
        confluence_body = confluence_payload["body"]["value"]
        self.assertIn("<h1>SDLC Harness - UC1 - Design Checkpoint</h1>", confluence_body)
        self.assertIn("SDLC-HARNESS-CHECKPOINT::design::dataset_use_case:eTour:UC1", confluence_body)
        self.assertIn("<h2>Phụ lục UML</h2>", confluence_body)
        self.assertIn("<h2>Repository Landscape</h2>", confluence_body)
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

    def test_real_sync_design_creates_targets_even_when_phase1_report_exists(self) -> None:
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
            if method == "POST" and "/comment" in url:
                return {"id": "10002"}
            if method == "POST" and url.endswith("/wiki/api/v2/pages"):
                return {"id": "20002", "_links": {"webui": "/spaces/SDLC/pages/20002/Design"}}
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
        self.assertEqual([call[0] for call in calls], ["POST", "POST"])
        report = json.loads((self.repo_root / str(result["report"])).read_text(encoding="utf-8"))
        self.assertEqual(report["targets"][0]["operation"], "created")
        self.assertEqual(report["targets"][1]["operation"], "created")

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

    def test_revalidate_design_creates_next_version_without_restarting_requirement(self) -> None:
        created = self._create_phase1_run()
        initial = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))

        response = PhaseTwoWorkflow(self.settings).revalidate_design(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertTrue(response["latest_artifacts"]["design_artifact"].endswith("design_artifact_v2.md"))
        self.assertTrue(response["latest_artifacts"]["design_validation_report"].endswith("design_validation_report_v2.json"))
        self.assertEqual(response["quality_reports"]["design_validation"]["versions"], ["v1", "v2"])
        self.assertNotEqual(initial["pending_checkpoint"]["artifact_ref"], response["pending_checkpoint"]["artifact_ref"])

    def test_design_validator_step_review_pauses_and_replays_approved_output(self) -> None:
        settings = replace(self.settings, design_validation_trials=1)
        created = self._create_phase1_run()
        run_id = str(created["run_id"])
        PhaseTwoWorkflow(settings).approve_checkpoint(run_id=run_id)

        paused = PhaseTwoWorkflow(
            settings,
            designer_step_review={"enabled": True},
        ).revalidate_design(run_id=run_id)

        self.assertEqual(paused["status"], "design_paused", paused)
        self.assertEqual(paused["last_error"]["error_code"], "design_validator_step_review_required")
        state = PhaseTwoWorkflow(settings).store.read_state(run_id)
        pending = state["designer_execution"]["validator_step_review"]
        self.assertEqual(pending["call_id"], "design-validator-0001-a01")
        resumed = PhaseTwoWorkflow(
            settings,
            designer_step_review={
                "enabled": True,
                "call_id": pending["call_id"],
                "decision": "approve",
                "note": "",
            },
        ).continue_design(run_id=run_id)

        self.assertEqual(resumed["status"], "waiting_design_approval")
        self.assertTrue(resumed["latest_artifacts"]["design_artifact"].endswith("design_artifact_v2.md"))

    def test_revalidate_design_recovers_feedback_from_latest_finding_ledger(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        run_id = str(created["run_id"])
        state = workflow.store.read_state(run_id)
        state["designer_repair_feedback"] = []
        ledger_ref = state["latest_artifacts"]["design_finding_ledger"]
        ledger_path = self.repo_root / str(ledger_ref)
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["blocking_findings"] = [
            {
                "id": "CONS-RETRY",
                "category": "behavior_order",
                "severity": "blocking",
                "message": "The previous semantic blocker must reach Designer revalidation.",
            }
        ]
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        feedback = workflow._persisted_design_feedback(state=state, run_id=run_id)

        self.assertEqual([item["id"] for item in feedback], ["CONS-RETRY"])

    def test_revalidate_design_recovers_failed_phase3_component_routing(self) -> None:
        created = self._create_phase1_run()
        designed = PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        state = workflow.store.read_state(str(created["run_id"]))
        design_path = self.repo_root / str(designed["latest_artifacts"]["design_artifact"])
        design_path.write_text(
            design_path.read_text(encoding="utf-8").replace(
                "class CulturalHeritage <<contracts>>",
                "class CulturalHeritage",
            ),
            encoding="utf-8",
        )
        state.update(
            {
                "current_phase": "implementation_planning",
                "status": "failed",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": HarnessError(
                    "implementation_target_unclassified",
                    "CulturalHeritage has no component.",
                    run_id=str(created["run_id"]),
                ).to_response(),
            }
        )
        workflow.store.write_state(state)

        response = workflow.revalidate_design(run_id=str(created["run_id"]))

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertIsNone(response["last_error"])
        self.assertEqual(response["designer_contract_version"], self.settings.designer_contract_version)
        self.assertIn("revalidate_design", response["allowed_next_actions"])

    def test_revalidate_design_supersedes_pending_phase3_exchange(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        run_id = str(created["run_id"])
        state = workflow.store.read_state(run_id)
        state.update(
            {
                "current_phase": "implementation_planning",
                "status": "implementation_plan_step_review_required",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
            }
        )
        workflow.store.write_state(state)
        execution_root = workflow.store.run_dir(run_id) / "implementation_plan_execution"
        call = {
            "call_id": "implementation-plan-0001-a01",
            "ordinal": 1,
            "attempt": 1,
            "status": "pending",
            "raw_completion": "{}",
        }
        execution_root.mkdir(parents=True, exist_ok=True)
        (execution_root / "step_review_session_v1.json").write_text(
            json.dumps({"calls": [call]}, ensure_ascii=False), encoding="utf-8"
        )
        (execution_root / "exchange_implementation-plan-0001-a01.json").write_text(
            json.dumps(call, ensure_ascii=False), encoding="utf-8"
        )

        response = workflow.revalidate_design(run_id=run_id)

        self.assertEqual(response["status"], "waiting_design_approval")
        session = json.loads(
            (execution_root / "step_review_session_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(session["calls"][0]["status"], "superseded")
        self.assertEqual(
            session["calls"][0]["superseded_reason"],
            "upstream_design_revalidation",
        )

    def test_revalidate_design_supersedes_unapproved_plan_checkpoint(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        workflow = PhaseTwoWorkflow(self.settings)
        run_id = str(created["run_id"])
        state = workflow.store.read_state(run_id)
        state["latest_artifacts"].update(
            {
                "implementation_plan": "artifacts/implementation_plan_v1.md",
                "implementation_plan_structured": "artifacts/implementation_plan_structured_v1.json",
                "implementation_plan_review": "artifacts/implementation_plan_review_v1.json",
            }
        )
        state["quality_reports"]["implementation_plan_review"] = {"ok": True}
        state.update(
            {
                "current_phase": "implementation_planning",
                "status": "waiting_implementation_plan_review",
                "pending_checkpoint": {
                    "checkpoint_id": f"checkpoint_implementation_plan_{run_id}",
                    "type": "implementation_plan_review",
                },
            }
        )
        workflow.store.write_state(state)
        execution_root = workflow.store.run_dir(run_id) / "implementation_plan_execution"
        call = {
            "call_id": "implementation-plan-0001-a01",
            "ordinal": 1,
            "attempt": 1,
            "status": "approved",
            "raw_completion": "{}",
        }
        execution_root.mkdir(parents=True, exist_ok=True)
        (execution_root / "step_review_session_v1.json").write_text(
            json.dumps({"calls": [call]}, ensure_ascii=False), encoding="utf-8"
        )
        (execution_root / "exchange_implementation-plan-0001-a01.json").write_text(
            json.dumps(call, ensure_ascii=False), encoding="utf-8"
        )

        response = workflow.revalidate_design(run_id=run_id)

        self.assertEqual(response["status"], "waiting_design_approval")
        self.assertEqual(response["pending_checkpoint"]["type"], "design_approval")
        self.assertNotIn("implementation_plan", response["latest_artifacts"])
        self.assertNotIn("implementation_plan_structured", response["latest_artifacts"])
        self.assertNotIn("implementation_plan_review", response["latest_artifacts"])
        self.assertNotIn("implementation_plan_review", response["quality_reports"])
        session = json.loads(
            (execution_root / "step_review_session_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(session["calls"][0]["status"], "superseded")
        self.assertEqual(
            session["calls"][0]["superseded_reason"],
            "upstream_design_revalidation",
        )


    def test_cli_revalidate_design_returns_json_contract(self) -> None:
        created = self._create_phase1_run()
        PhaseTwoWorkflow(self.settings).approve_checkpoint(run_id=str(created["run_id"]))
        stdout = io.StringIO()

        with patch.dict(os.environ, self._target_overrides()), contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "revalidate-design",
                    "--run-id",
                    str(created["run_id"]),
                    "--llm-mode",
                    "fake",
                    "--sync-mode",
                    "mock",
                    "--runtime-root",
                    str(self.runtime_root),
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "waiting_design_approval")
        self.assertIn("revalidate_design", payload["allowed_next_actions"])


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
        with patch.dict(os.environ, self._target_overrides()), contextlib.redirect_stdout(stdout):
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
