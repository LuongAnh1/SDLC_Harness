"""Kiểm thử chọn, cập nhật clarification Requirement V2 và nối vào Phase 1."""

from __future__ import annotations

from copy import deepcopy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sdlc_harness.agents.requirements import (
    RequirementClarificationUpdater,
    RequirementModelV2,
    RequirementModelV2RefinementResult,
    normalize_and_validate_requirement_model_v2,
    select_current_requirement_question,
)
from sdlc_harness.artifacts import (
    read_source_snapshot_v2,
    write_requirement_v2_artifacts,
    write_requirement_v2_revision_artifacts,
    write_source_snapshot_v2,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.interfaces.cli import build_parser, main as cli_main
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.sources.fixtures import UseCaseSource, source_provenance_entry
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


class ScriptedClient:
    def __init__(self, payloads: list[dict[str, object] | str]):
        self.payloads = list(payloads)
        self.settings = SimpleNamespace(llm_structured_max_tokens=4096)
        self.calls: list[dict[str, object]] = []

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        value = self.payloads.pop(0)
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return LlmCompletion(content=content, finish_reason="stop", usage={})


class StubV2Refiner:
    def __init__(self, test: "RequirementClarificationV2Test", model_payload: dict[str, object] | None = None):
        self.test = test
        self.model_payload = model_payload

    def refine(self, source, *, run_id: str, source_artifact_ref: str, source_catalog=None):
        return self.test.refinement_result(
            run_id=run_id,
            source_ref=source.source_ref,
            source_text=source.text,
            source_artifact_ref=source_artifact_ref,
            model_payload=self.model_payload,
        )


class RequirementClarificationV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.source_text = (cls.root / "fixtures" / "etour" / "UC2.txt").read_text(encoding="utf-8").strip()
        cls.payload = json.loads(
            (cls.root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8")
        )

    def refinement_result(
        self,
        *,
        run_id: str = "run_clarification",
        source_ref: str = "dataset_use_case:eTour:UC2",
        source_text: str | None = None,
        source_artifact_ref: str = "source_snapshot_SRC-1_v2.json",
        model_payload: dict[str, object] | None = None,
    ) -> RequirementModelV2RefinementResult:
        text = source_text or self.source_text
        payload = deepcopy(model_payload if model_payload is not None else self.payload)
        fingerprint = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_entry = {
            "id": "SRC-1",
            "type": "jira" if source_ref.startswith("jira_issue:") else "dataset",
            "source_ref": source_ref,
            "locator": "fixtures/etour/UC2.txt",
            "revision": None,
            "artifact_ref": source_artifact_ref,
            "fingerprint": fingerprint,
        }
        payload.update(
            {
                "schema_version": "requirement_model_v2",
                "revision": 1,
                "run_id": run_id,
                "source": {
                    "source_id": "SRC-1",
                    "source_ref": source_ref,
                    "artifact_ref": source_artifact_ref,
                    "fingerprint": fingerprint,
                },
                "provenance_ledger": {
                    "schema_version": "requirement_provenance_v1",
                    "sources": [source_entry],
                },
                "decisions": [],
                "validation": {"status": "invalid", "errors": []},
            }
        )
        normalized = normalize_and_validate_requirement_model_v2(payload, {"SRC-1": text})
        assert normalized.model is not None
        assert normalized.report.ok
        return RequirementModelV2RefinementResult(
            model=normalized.model,
            initial_report=normalized.report,
            final_report=normalized.report,
            repaired=False,
            attempts=[],
        )

    def payload_without_open_questions(self) -> dict[str, object]:
        payload = deepcopy(self.payload)
        payload["open_questions"] = []
        for item in payload["flows"]["alternatives"] + payload["flows"]["exceptions"]:
            item["open_question_refs"] = []
            item["steps"] = [f"Execute the documented {item['id']} branch behavior."]
            item["outcome"] = f"The {item['id']} branch reaches an observable terminal state."
        payload["business_rules"][0]["open_question_refs"] = []
        resolved_acceptance = {
            "AC-1": ("The system rejects a duplicate cultural heritage record.", ["E-13"]),
            "AC-3": ("The operator can cancel the insertion operation.", ["E-10"]),
            "AC-4": ("Invalid or insufficient data activates the Errored use case.", ["E-7"]),
            "AC-5": ("A connection interruption is handled as an insertion exception.", ["E-11"]),
        }
        for item in payload["acceptance_criteria"]:
            item["open_question_refs"] = []
            if item["id"] in resolved_acceptance:
                item["text"], item["evidence_refs"] = resolved_acceptance[item["id"]]
                item["status"] = "explicit"
        for scenario in payload["scenario_contract"]["scenarios"]:
            scenario["open_question_refs"] = []
            for surface in scenario["surfaces"].values():
                surface["open_question_refs"] = []
        return payload

    def payload_with_only_non_blocking_question(self) -> dict[str, object]:
        payload = self.payload_without_open_questions()
        payload["assumptions"] = [
            {
                "id": "ASM-1",
                "text": "The reviewer may choose a display label later.",
                "status": "assumption",
                "evidence_refs": ["E-1"],
                "open_question_refs": ["Q-1"],
                "reason": "Display wording is not specified by the source.",
                "affects": [],
            }
        ]
        payload["open_questions"] = [
            {
                "id": "Q-1",
                "question": "Which display label should the review page use?",
                "category": "documentation",
                "proposed_blocking": False,
                "effective_blocking": False,
                "affects": ["ASM-1"],
                "depends_on": [],
                "status": "open",
                "answer_guidance": None,
                "evidence_refs": ["E-1"],
            }
        ]
        return payload

    @staticmethod
    def accepted_q4() -> dict[str, object]:
        return {
            "resolution": "accepted",
            "follow_up_text": None,
            "surface_updates": [],
            "updates": [
                {
                    "requirement_id": "BR-1",
                    "fields": {"text": "Duplicate identity uses normalized heritage name and location."},
                },
                {
                    "requirement_id": "AC-1",
                    "fields": {
                        "text": "A record with the same normalized heritage name and location is rejected.",
                        "verifies": ["BR-1"],
                    },
                },
            ],
        }

    def test_selector_uses_category_priority_then_question_id(self) -> None:
        model = self.refinement_result().model
        selection = select_current_requirement_question(model)
        self.assertIsNotNone(selection.current_question)
        self.assertEqual(selection.current_question.id, "Q-4")
        self.assertEqual(selection.remaining_blocking_count, 4)

    def test_v2_without_blocking_questions_goes_directly_to_approval(self) -> None:
        with TemporaryDirectory() as directory:
            settings = load_settings(
                root=self.root,
                runtime_root=Path(directory),
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                },
            )
            workflow = PhaseOneWorkflow(settings)
            workflow.refiner_v2 = StubV2Refiner(self, self.payload_without_open_questions())
            response = workflow.start_or_get_run(dataset="eTour", use_case="UC2", requested_by="owner")
            self.assertEqual(response["status"], "waiting_requirement_approval")
            self.assertEqual(response["pending_checkpoint"]["type"], "requirement_approval")
            self.assertNotIn("answer_clarification", response["allowed_next_actions"])

    def test_non_blocking_question_is_rendered_but_does_not_block_approval(self) -> None:
        payload = self.payload_with_only_non_blocking_question()
        normalized = self.refinement_result(model_payload=payload)
        self.assertEqual(normalized.final_report.status, "valid")
        self.assertEqual(normalized.model.open_questions[0]["effective_blocking"], False)
        with TemporaryDirectory() as directory:
            settings = load_settings(
                root=self.root,
                runtime_root=Path(directory),
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                },
            )
            workflow = PhaseOneWorkflow(settings)
            workflow.refiner_v2 = StubV2Refiner(self, payload)
            response = workflow.start_or_get_run(dataset="eTour", use_case="UC2", requested_by="owner")
            self.assertEqual(response["status"], "waiting_requirement_approval")
            packet = (self.root / str(response["latest_artifacts"]["requirement_packet"])).read_text(
                encoding="utf-8"
            )
            self.assertIn("Which display label should the review page use?", packet)
            gap_report = json.loads(
                Path(response["latest_artifacts"]["requirement_gap_report"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(gap_report["status"], "clear")
            self.assertEqual(gap_report["gaps"][0]["route"], "record_only")

    def test_accepted_answer_creates_revision_and_decision(self) -> None:
        client = ScriptedClient([self.accepted_q4()])
        original = self.refinement_result().model
        result = RequirementClarificationUpdater(client).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=original,
            question_id="Q-4",
            answer="Use normalized heritage name and location.",
            answered_by="owner",
            answered_at="2026-10-16T00:00:00Z",
        )
        self.assertEqual(result.model.revision, 2)
        self.assertEqual(result.resolution, "accepted")
        self.assertEqual(result.decision["id"], "D-1")
        self.assertTrue(str(result.decision["submission_hash"]).startswith("sha256:"))
        self.assertEqual(result.model.open_questions[3]["status"], "resolved")
        self.assertEqual(result.final_report.blocking_question_ids, ["Q-1", "Q-2", "Q-3"])
        self.assertEqual(result.model.provenance_ledger, original.provenance_ledger)
        self.assertEqual(result.model.source, original.source)

    def test_accepted_answer_resolves_a_clarification_required_surface(self) -> None:
        payload = deepcopy(self.payload)
        ui = payload["scenario_contract"]["scenarios"][4]["surfaces"]["ui"]
        ui["status"] = "clarification_required"
        proposal = self.accepted_q4()
        proposal["surface_updates"] = [
            {
                "scenario_id": "SC-5",
                "surface": "ui",
                "status": "not_applicable",
                "reason": "The owner's duplicate rule does not require a separate UI behavior.",
            }
        ]
        result = RequirementClarificationUpdater(ScriptedClient([proposal])).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=self.refinement_result(model_payload=payload).model,
            question_id="Q-4",
            answer="Use normalized heritage name and location; no separate UI behavior is required.",
            answered_by="owner",
        )
        updated_ui = result.model.scenario_contract["scenarios"][4]["surfaces"]["ui"]
        self.assertEqual(updated_ui["status"], "not_applicable")
        self.assertEqual(updated_ui["requirement_refs"], [])
        self.assertEqual(updated_ui["open_question_refs"], [])
        self.assertNotIn("Q-4", result.model.scenario_contract["scenarios"][4]["open_question_refs"])

    def test_accepted_answer_may_refine_a_required_surface_linked_to_question(self) -> None:
        proposal = self.accepted_q4()
        proposal["surface_updates"] = [
            {
                "scenario_id": "SC-5",
                "surface": "ui",
                "status": "required",
                "reason": "The operator sees the answer-backed duplicate rejection result.",
            }
        ]
        result = RequirementClarificationUpdater(ScriptedClient([proposal])).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=self.refinement_result().model,
            question_id="Q-4",
            answer="Use normalized heritage name and location.",
            answered_by="owner",
        )
        updated_ui = result.model.scenario_contract["scenarios"][4]["surfaces"]["ui"]
        self.assertEqual(updated_ui["status"], "required")
        self.assertEqual(updated_ui["reason"], "The operator sees the answer-backed duplicate rejection result.")
        self.assertEqual(updated_ui["open_question_refs"], [])

    def test_missing_required_surface_update_gets_one_bounded_repair(self) -> None:
        payload = deepcopy(self.payload)
        payload["scenario_contract"]["scenarios"][4]["surfaces"]["ui"]["status"] = "clarification_required"
        repaired = self.accepted_q4()
        repaired["surface_updates"] = [
            {
                "scenario_id": "SC-5",
                "surface": "ui",
                "status": "required",
                "reason": "The duplicate decision remains observable through the existing required UI surface.",
            }
        ]
        client = ScriptedClient([self.accepted_q4(), repaired])
        result = RequirementClarificationUpdater(client).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=self.refinement_result(model_payload=payload).model,
            question_id="Q-4",
            answer="Use normalized heritage name and location.",
            answered_by="owner",
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result.attempts[0].parse_status, "invalid_update")
        self.assertEqual(result.model.scenario_contract["scenarios"][4]["surfaces"]["ui"]["status"], "required")

    def test_insufficient_answer_is_audited_without_requirement_mutation(self) -> None:
        original = self.refinement_result().model
        client = ScriptedClient(
            [{"resolution": "insufficient", "follow_up_text": "Please list every identity field.", "updates": [], "surface_updates": []}]
        )
        result = RequirementClarificationUpdater(client).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=original,
            question_id="Q-4",
            answer="Use a unique key.",
            answered_by="owner",
        )
        self.assertEqual(result.model.revision, 2)
        self.assertEqual(result.model.business_rules, original.business_rules)
        self.assertEqual(result.model.acceptance_criteria, original.acceptance_criteria)
        self.assertEqual(result.model.open_questions[3]["status"], "open")
        self.assertEqual(result.decision["status"], "insufficient")

        accepted = RequirementClarificationUpdater(ScriptedClient([self.accepted_q4()])).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=result.model,
            question_id="Q-4",
            answer="Use normalized heritage name and location.",
            answered_by="owner",
        )
        self.assertEqual(accepted.model.revision, 3)
        self.assertEqual(accepted.model.decisions[0]["status"], "superseded")
        self.assertEqual(accepted.model.decisions[1]["supersedes_decision_id"], "D-1")

    def test_out_of_scope_update_gets_one_bounded_repair(self) -> None:
        invalid = {
            "resolution": "accepted",
            "follow_up_text": None,
            "surface_updates": [],
            "updates": [{"requirement_id": "G-1", "fields": {"text": "changed"}}],
        }
        client = ScriptedClient([invalid, self.accepted_q4()])
        result = RequirementClarificationUpdater(client).apply(
            source_text_by_id={"SRC-1": self.source_text},
            model=self.refinement_result().model,
            question_id="Q-4",
            answer="Use normalized heritage name and location.",
            answered_by="owner",
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result.attempts[0].parse_status, "invalid_update")
        self.assertEqual(result.attempts[1].purpose, "repair")

    def test_wrong_question_and_empty_answer_fail_with_stable_errors(self) -> None:
        updater = RequirementClarificationUpdater(ScriptedClient([]))
        model = self.refinement_result().model
        with self.assertRaises(HarnessError) as wrong_question:
            updater.apply(
                source_text_by_id={"SRC-1": self.source_text},
                model=model,
                question_id="Q-1",
                answer="Stop without saving.",
                answered_by="owner",
            )
        self.assertEqual(wrong_question.exception.error_code, "requirement_question_not_current")
        with self.assertRaises(HarnessError) as empty_answer:
            updater.apply(
                source_text_by_id={"SRC-1": self.source_text},
                model=model,
                question_id="Q-4",
                answer="   ",
                answered_by="owner",
            )
        self.assertEqual(empty_answer.exception.error_code, "requirement_answer_empty")

    def test_phase1_v2_answer_is_idempotent_and_updates_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            settings = load_settings(
                root=self.root,
                runtime_root=Path(directory),
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                },
            )
            workflow = PhaseOneWorkflow(settings)
            workflow.refiner_v2 = StubV2Refiner(self)
            start = workflow.start_or_get_run(dataset="eTour", use_case="UC2", requested_by="owner")
            self.assertEqual(start["status"], "waiting_requirement_clarification")
            self.assertEqual(start["current_phase"], "requirement_clarification")
            self.assertEqual(start["requirement_contract_version"], "v2")
            self.assertEqual(start["pending_checkpoint"]["current_question"]["id"], "Q-4")
            mock_comment = (
                Path(directory) / "runs" / str(start["run_id"]) / "sync" / "jira-comment.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Current clarification question", mock_comment)
            self.assertIn("Remaining blocking questions: 4", mock_comment)

            client = ScriptedClient([self.accepted_q4()])
            workflow.clarification_updater = RequirementClarificationUpdater(client)
            updated = workflow.answer_clarification(
                run_id=str(start["run_id"]),
                question_id="Q-4",
                answer="Use normalized heritage name and location.",
                answered_by="owner",
            )
            self.assertEqual(updated["pending_checkpoint"]["current_question"]["id"], "Q-1")
            self.assertTrue(updated["latest_artifacts"]["requirement_model"].endswith("_r2.json"))
            updated_gaps = json.loads(
                Path(updated["latest_artifacts"]["requirement_gap_report"]).read_text(
                    encoding="utf-8"
                )
            )
            q4_gap = next(gap for gap in updated_gaps["gaps"] if gap["question_ref"] == "Q-4")
            self.assertEqual(q4_gap["status"], "resolved")
            self.assertEqual(q4_gap["route"], "record_only")

            repeated = workflow.answer_clarification(
                run_id=str(start["run_id"]),
                question_id="Q-4",
                answer="  USE normalized heritage name and location.  ",
                answered_by="owner",
            )
            self.assertEqual(repeated["latest_artifacts"], updated["latest_artifacts"])
            self.assertEqual(len(client.calls), 1)

            remaining = ScriptedClient(
                [
                    {
                        "resolution": "accepted",
                        "follow_up_text": None,
                        "surface_updates": [],
                        "updates": [
                            {
                                "requirement_id": "AF-1",
                                "fields": {
                                    "steps": ["Stop the insertion flow."],
                                    "outcome": "Stop without saving."
                                },
                            },
                            {"requirement_id": "AC-3", "fields": {"text": "Cancelling stops without saving.", "verifies": ["AF-1"]}},
                        ],
                    },
                    {
                        "resolution": "accepted",
                        "follow_up_text": None,
                        "surface_updates": [],
                        "updates": [
                            {"requirement_id": "EF-1", "fields": {"outcome": "Show validation errors."}},
                            {"requirement_id": "AC-4", "fields": {"text": "Invalid data shows validation errors.", "verifies": ["EF-1"]}},
                        ],
                    },
                    {
                        "resolution": "accepted",
                        "follow_up_text": None,
                        "surface_updates": [],
                        "updates": [
                            {
                                "requirement_id": "EF-2",
                                "fields": {
                                    "steps": ["Report the interruption and preserve entered data."],
                                    "outcome": "Keep the form for retry."
                                },
                            },
                            {"requirement_id": "AC-5", "fields": {"text": "A connection interruption keeps the form for retry.", "verifies": ["EF-2"]}},
                        ],
                    },
                ]
            )
            workflow.clarification_updater = RequirementClarificationUpdater(remaining)
            for question_id, answer in (
                ("Q-1", "Stop without saving."),
                ("Q-2", "Show validation errors."),
                ("Q-3", "Keep the form for retry."),
            ):
                completed = workflow.answer_clarification(
                    run_id=str(start["run_id"]),
                    question_id=question_id,
                    answer=answer,
                    answered_by="owner",
                )
            self.assertEqual(completed["status"], "waiting_requirement_approval")
            self.assertTrue(completed["latest_artifacts"]["requirement_model"].endswith("_r5.json"))
            completed_gaps = json.loads(
                Path(completed["latest_artifacts"]["requirement_gap_report"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(completed_gaps["status"], "clear")
            self.assertEqual(completed["quality_reports"]["requirement_gaps"]["unresolved"], 0)
            design = CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id=str(start["run_id"]))
            self.assertEqual(design["status"], "waiting_design_approval")
            self.assertEqual(design["requirement_contract_version"], "v2")
            self.assertEqual(design["quality_reports"]["design_traceability"]["mode"], "traceability_v2")
            self.assertTrue(design["latest_artifacts"]["requirement_graph"].endswith("requirement_graph_v1.json"))
            trace = json.loads(
                (workflow.store.repo_root / design["latest_artifacts"]["requirement_design_trace"]).read_text(
                    encoding="utf-8"
                )
            )
            graph = json.loads(
                (workflow.store.repo_root / design["latest_artifacts"]["requirement_graph"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(trace["mode"], "traceability_v2")
            self.assertEqual(trace["requirement_contract_version"], "v2")
            self.assertEqual(trace["requirement_revision"], 5)
            self.assertTrue(trace["inputs"]["requirement_model"].endswith("_r5.json"))
            graph_ids = {item["id"] for item in graph["requirements"]}
            self.assertIn("MF-1", graph_ids)
            self.assertIn("AC-1", graph_ids)
            persisted = workflow.store.read_state(str(start["run_id"]))
            self.assertIn("requirement_model", persisted["approved_artifacts"])
            self.assertEqual(workflow.store.read_state(str(start["run_id"]))["status"], "waiting_design_approval")
            with self.assertRaises(HarnessError) as wrong_state:
                workflow.answer_clarification(
                    run_id=str(start["run_id"]),
                    question_id="Q-1",
                    answer="A different answer after clarification completed.",
                    answered_by="owner",
                )
            self.assertEqual(wrong_state.exception.error_code, "requirement_clarification_not_ready")

    def test_sync_failure_after_answer_keeps_revision_for_retry_without_llm(self) -> None:
        with TemporaryDirectory() as directory:
            settings = load_settings(
                root=self.root,
                runtime_root=Path(directory),
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                },
            )
            workflow = PhaseOneWorkflow(settings)
            workflow.refiner_v2 = StubV2Refiner(self)
            start = workflow.start_or_get_run(dataset="eTour", use_case="UC2", requested_by="owner")
            client = ScriptedClient([self.accepted_q4()])
            workflow.clarification_updater = RequirementClarificationUpdater(client)
            with patch(
                "sdlc_harness.workflows.phase1.sync_requirement_checkpoint",
                side_effect=HarnessError("runtime_write_failed", "simulated sync failure"),
            ):
                with self.assertRaises(HarnessError) as failed_sync:
                    workflow.answer_clarification(
                        run_id=str(start["run_id"]),
                        question_id="Q-4",
                        answer="Use normalized heritage name and location.",
                        answered_by="owner",
                    )
            self.assertEqual(failed_sync.exception.error_code, "runtime_write_failed")
            persisted = workflow.get_status(run_id=str(start["run_id"]))
            self.assertTrue(persisted["latest_artifacts"]["requirement_model"].endswith("_r2.json"))
            self.assertEqual(persisted["pending_checkpoint"]["current_question"]["id"], "Q-1")
            retried = workflow.sync_run(run_id=str(start["run_id"]))
            self.assertEqual(retried["last_sync"]["status"], "mock_synced")
            self.assertTrue(retried["latest_artifacts"]["requirement_model"].endswith("_r2.json"))
            self.assertEqual(len(client.calls), 1)

    def test_approval_is_blocked_while_clarification_is_current(self) -> None:
        with TemporaryDirectory() as directory:
            settings = load_settings(
                root=self.root,
                runtime_root=Path(directory),
                overrides={},
            )
            store = RuntimeStore(repo_root=self.root, runtime_root=Path(directory))
            store.write_state(
                {
                    "run_id": "run_blocked",
                    "status": "waiting_requirement_clarification",
                    "requirement_contract_version": "v2",
                }
            )
            with self.assertRaises(HarnessError) as context:
                CheckpointApprovalWorkflow(settings).approve_checkpoint(run_id="run_blocked")
            self.assertEqual(context.exception.error_code, "requirement_approval_blocked")

    def test_cli_exposes_start_and_answer_action(self) -> None:
        parser = build_parser()
        start = parser.parse_args(
            [
                "start-or-get-run",
                "--dataset",
                "eTour",
                "--use-case",
                "UC2",
                "--requested-by",
                "owner",
            ]
        )
        answer = parser.parse_args(
            [
                "answer-clarification",
                "--run-id",
                "run_1",
                "--question-id",
                "Q-4",
                "--answer",
                "name and location",
                "--answered-by",
                "owner",
            ]
        )
        self.assertEqual(start.command, "start-or-get-run")
        self.assertEqual(answer.command, "answer-clarification")

    def test_cli_v2_start_answer_and_status_sequence(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            settings = load_settings(
                root=self.root,
                runtime_root=runtime_root,
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                },
            )
            workflow = PhaseOneWorkflow(settings)
            workflow.refiner_v2 = StubV2Refiner(self)
            workflow.clarification_updater = RequirementClarificationUpdater(
                ScriptedClient([self.accepted_q4()])
            )

            with (
                patch.dict(os.environ, {"SDLC_PROJECT_PROFILE": "typescript_fullstack_v1"}),
                patch("sdlc_harness.application.service.PhaseOneWorkflow", return_value=workflow),
            ):
                start_stdout = io.StringIO()
                with contextlib.redirect_stdout(start_stdout):
                    start_code = cli_main(
                        [
                            "start-or-get-run",
                            "--dataset",
                            "eTour",
                            "--use-case",
                            "UC2",
                            "--requested-by",
                            "owner",
                            "--llm-mode",
                            "fake",
                            "--runtime-root",
                            str(runtime_root),
                        ]
                    )
                started = json.loads(start_stdout.getvalue())
                self.assertEqual(start_code, 0)
                self.assertEqual(started["pending_checkpoint"]["current_question"]["id"], "Q-4")

                answer_stdout = io.StringIO()
                with contextlib.redirect_stdout(answer_stdout):
                    answer_code = cli_main(
                        [
                            "answer-clarification",
                            "--run-id",
                            str(started["run_id"]),
                            "--question-id",
                            "Q-4",
                            "--answer",
                            "Use normalized heritage name and location.",
                            "--answered-by",
                            "owner",
                            "--runtime-root",
                            str(runtime_root),
                        ]
                    )
                answered = json.loads(answer_stdout.getvalue())
                self.assertEqual(answer_code, 0)
                self.assertEqual(answered["pending_checkpoint"]["current_question"]["id"], "Q-1")

                status_stdout = io.StringIO()
                with contextlib.redirect_stdout(status_stdout):
                    status_code = cli_main(
                        [
                            "get-status",
                            "--run-id",
                            str(started["run_id"]),
                            "--runtime-root",
                            str(runtime_root),
                        ]
                    )
                status = json.loads(status_stdout.getvalue())
                self.assertEqual(status_code, 0)
                self.assertEqual(status["latest_artifacts"], answered["latest_artifacts"])
                self.assertEqual(status["requirement_contract_version"], "v2")

    def test_requirement_contract_is_fixed_to_v2(self) -> None:
        with TemporaryDirectory() as directory:
            default = load_settings(root=self.root, runtime_root=Path(directory))
        self.assertEqual(default.requirement_contract_version, "v2")

    def test_run_lock_rejects_a_second_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            store = RuntimeStore(repo_root=self.root, runtime_root=Path(directory))
            with store.run_lock("run_locked"):
                with self.assertRaises(HarnessError) as context:
                    with store.run_lock("run_locked"):
                        pass
            self.assertEqual(context.exception.error_code, "run_action_locked")

    def test_source_snapshot_fingerprint_detects_tampering(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            source = UseCaseSource(
                dataset="eTour",
                use_case="UC2",
                source_ref="dataset_use_case:eTour:UC2",
                source_path=None,
                text=self.source_text,
            )
            artifact_ref = write_source_snapshot_v2(store=store, run_id="run_source", source=source)
            expected_source = source_provenance_entry(source=source, artifact_ref=artifact_ref)
            self.assertEqual(
                read_source_snapshot_v2(
                    store=store,
                    run_id="run_source",
                    expected_source=expected_source,
                ),
                self.source_text,
            )
            path = root / artifact_ref
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["locator"] = "tampered-locator"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(HarnessError) as identity_context:
                read_source_snapshot_v2(
                    store=store,
                    run_id="run_source",
                    expected_source=expected_source,
                )
            self.assertEqual(identity_context.exception.error_code, "source_snapshot_corrupted")

            payload["locator"] = expected_source["locator"]
            payload["fingerprint"] = "sha256:tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(HarnessError) as fingerprint_context:
                read_source_snapshot_v2(
                    store=store,
                    run_id="run_source",
                    expected_source=expected_source,
                )
            self.assertEqual(fingerprint_context.exception.error_code, "source_snapshot_corrupted")

            payload["fingerprint"] = expected_source["fingerprint"]
            payload["text"] += " tampered"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(HarnessError) as context:
                read_source_snapshot_v2(
                    store=store,
                    run_id="run_source",
                    expected_source=expected_source,
                )
            self.assertEqual(context.exception.error_code, "source_snapshot_corrupted")

    def test_revision_bundle_is_idempotent_but_rejects_conflicting_content(self) -> None:
        result = self.refinement_result()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            first = write_requirement_v2_artifacts(
                store=store,
                run_id=result.model.run_id,
                refinement_result=result,
            )
            repeated = write_requirement_v2_artifacts(
                store=store,
                run_id=result.model.run_id,
                refinement_result=result,
            )
            self.assertEqual(repeated, first)

            changed = result.model.to_dict()
            changed["use_case"]["name"] = "Conflicting revision content"
            with self.assertRaises(HarnessError) as conflict:
                write_requirement_v2_revision_artifacts(
                    store=store,
                    run_id=result.model.run_id,
                    model=RequirementModelV2.from_dict(changed),
                    validation_report=result.final_report,
                    audit=result.to_report_dict(),
                )
            self.assertEqual(conflict.exception.error_code, "requirement_revision_conflict")


if __name__ == "__main__":
    unittest.main()
