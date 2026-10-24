"""Kiểm thử đọc kép và traceability Phase 2 cho Requirement Contract V2."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sdlc_harness.agents.designer import DesignDraft, DesignerAgent
from sdlc_harness.agents.requirements import normalize_and_validate_requirement_model_v2
from sdlc_harness.agents.validator import TraceabilityResult, ValidatorAgent, build_requirement_graph_v2
from sdlc_harness.artifacts import (
    CanonicalRequirementInput,
    read_approved_requirement_input,
    read_frozen_requirement_input,
    write_requirement_v2_revision_artifacts,
    write_source_snapshot_v2,
)
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import fake_design_model
from sdlc_harness.sources.fixtures import UseCaseSource
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow


class CaptureClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class CompleteTraceClient:
    def complete(self, *, system: str, prompt: str) -> str:
        marker = "Requirements:\n"
        start = prompt.index(marker) + len(marker)
        decoder = json.JSONDecoder()
        requirements, _ = decoder.raw_decode(prompt[start:].lstrip())
        target = "DeleteCulturalHeritageService" if "traceability_class" in system else "requestDeletion(heritageId)"
        return json.dumps(
            [
                {
                    "requirement": item["id"],
                    "links": [{"target": target, "reason": "covered"}],
                }
                for item in requirements
            ]
        )


class CanonicalRepairingDesigner:
    def __init__(self) -> None:
        self.draft_models = []
        self.repair_models = []

    def draft(self, *, requirement_packet: str, formal_use_case: str, canonical_requirement=None) -> DesignDraft:
        self.draft_models.append(canonical_requirement)
        return DesignDraft(analysis_report="V2 architecture", design_model=fake_design_model())

    def update_design(
        self,
        *,
        requirement_packet: str,
        formal_use_case: str,
        current_design: str,
        audit_report: str,
        canonical_requirement=None,
    ) -> str:
        self.repair_models.append(canonical_requirement)
        return fake_design_model()


class CanonicalRepairValidator:
    def __init__(self) -> None:
        self.calls = 0
        self.requirement_ids_by_call: list[list[str]] = []

    def construct_traceability(
        self,
        *,
        requirement_packet: str,
        class_graph: dict[str, object],
        sequence_graph: dict[str, object],
        canonical_requirement=None,
        attempt_recorder=None,
    ) -> TraceabilityResult:
        graph = build_requirement_graph_v2(canonical_requirement)
        requirements = graph["requirements"]
        self.requirement_ids_by_call.append([item["id"] for item in requirements])
        has_missing = self.calls == 0
        self.calls += 1
        class_trace = []
        sequence_trace = []
        for index, requirement in enumerate(requirements):
            class_links = [] if has_missing and index == 0 else [{"target": "TestTarget", "reason": "test"}]
            sequence_links = [{"target": "TestMessage", "reason": "test"}]
            class_trace.append(
                {"requirement": requirement["id"], "content": requirement["content"], "links": class_links}
            )
            sequence_trace.append(
                {"requirement": requirement["id"], "content": requirement["content"], "links": sequence_links}
            )
        missing_links = {"class_diagram": [], "sequence_diagram": []}
        if has_missing:
            missing_links["class_diagram"] = [
                {"requirement": requirements[0]["id"], "content": requirements[0]["content"]}
            ]
        return TraceabilityResult(
            requirement_graph=graph,
            class_trace=class_trace,
            sequence_trace=sequence_trace,
            missing_links=missing_links,
            mode="traceability_v2",
        )


class RequirementInputV2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.source_text = (cls.root / "fixtures" / "etour" / "UC2.txt").read_text(encoding="utf-8").strip()
        cls.base_payload = json.loads(
            (cls.root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8")
        )

    def _valid_payload(self) -> dict[str, object]:
        payload = deepcopy(self.base_payload)
        payload["open_questions"] = []
        for item in payload["flows"]["alternatives"] + payload["flows"]["exceptions"]:
            item["open_question_refs"] = []
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

    def _v2_bundle(self, directory: str, *, valid: bool = True):
        runtime_root = Path(directory) / ".runtime"
        store = RuntimeStore(repo_root=self.root, runtime_root=runtime_root)
        run_id = "run_v2_input"
        source = UseCaseSource(
            dataset="eTour",
            use_case="UC2",
            source_ref="dataset_use_case:eTour:UC2",
            source_path=self.root / "fixtures" / "etour" / "UC2.txt",
            text=self.source_text,
        )
        snapshot_ref = write_source_snapshot_v2(store=store, run_id=run_id, source=source)
        payload = self._valid_payload() if valid else deepcopy(self.base_payload)
        payload.update(
            {
                "schema_version": "requirement_model_v2",
                "revision": 1,
                "run_id": run_id,
                "source": {
                    "source_ref": source.source_ref,
                    "artifact_ref": snapshot_ref,
                    "fingerprint": "sha256:" + hashlib.sha256(self.source_text.encode("utf-8")).hexdigest(),
                },
                "decisions": [],
                "validation": {"status": "invalid", "errors": []},
            }
        )
        validation = normalize_and_validate_requirement_model_v2(payload, self.source_text)
        refs = write_requirement_v2_revision_artifacts(
            store=store,
            run_id=run_id,
            model=validation.model,
            validation_report=validation.report,
            audit={"test": True},
        )
        state = {
            "run_id": run_id,
            "source_ref": source.source_ref,
            "requirement_contract_version": "v2",
            "status": "waiting_requirement_approval",
            "current_phase": "waiting_requirement_approval",
            "latest_artifacts": refs,
            "pending_checkpoint": {
                "checkpoint_id": f"checkpoint_requirement_{run_id}",
                "type": "requirement_approval",
                "artifact_ref": "requirement_model_v2_r1",
                "artifact_path": refs["requirement_model"],
            },
        }
        store.write_state(state)
        return store, state, refs, validation.model

    def test_reader_rejects_missing_contract_version(self) -> None:
        with TemporaryDirectory() as directory:
            store = RuntimeStore(repo_root=self.root, runtime_root=Path(directory) / ".runtime")
            with self.assertRaises(HarnessError) as caught:
                read_approved_requirement_input(
                    store=store,
                    run_id="run_without_contract",
                    state={"latest_artifacts": {}},
                )
            self.assertEqual(caught.exception.error_code, "approved_requirement_not_found")

    def test_reader_rejects_explicit_v1_contract(self) -> None:
        with TemporaryDirectory() as directory:
            store = RuntimeStore(repo_root=self.root, runtime_root=Path(directory) / ".runtime")
            with self.assertRaises(HarnessError) as caught:
                read_approved_requirement_input(
                    store=store,
                    run_id="run_v1",
                    state={"requirement_contract_version": "v1", "latest_artifacts": {}},
                )
            self.assertEqual(caught.exception.error_code, "requirement_contract_unsupported")

    def test_v2_reader_verifies_canonical_revision_and_projections(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, refs, model = self._v2_bundle(directory)
            result = read_approved_requirement_input(store=store, state=state, run_id=model.run_id)
            self.assertIsInstance(result, CanonicalRequirementInput)
            self.assertEqual(result.canonical_model.revision, 1)
            self.assertEqual(result.approved_artifacts["requirement_model"], refs["requirement_model"])

    def test_frozen_reader_uses_approved_artifacts_without_current_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, refs, model = self._v2_bundle(directory)
            state["approved_artifacts"] = dict(refs)
            state["latest_artifacts"] = {"requirement_packet": "later-phase-artifact.md"}
            state["pending_checkpoint"] = {
                "checkpoint_id": "checkpoint_design",
                "type": "design_approval",
            }

            result = read_frozen_requirement_input(store=store, state=state, run_id=model.run_id)

            self.assertIsInstance(result, CanonicalRequirementInput)
            self.assertEqual(result.canonical_model.revision, 1)
            self.assertEqual(result.approved_artifacts["requirement_model"], refs["requirement_model"])

    def test_v2_reader_rejects_stale_checkpoint_revision(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, _, model = self._v2_bundle(directory)
            state["pending_checkpoint"]["artifact_ref"] = "requirement_model_v2_r2"
            with self.assertRaises(HarnessError) as caught:
                read_approved_requirement_input(store=store, state=state, run_id=model.run_id)
            self.assertEqual(caught.exception.error_code, "requirement_revision_mismatch")

    def test_v2_reader_reports_missing_canonical_artifact(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, refs, model = self._v2_bundle(directory)
            Path(refs["requirement_model"]).unlink()
            with self.assertRaises(HarnessError) as caught:
                read_approved_requirement_input(store=store, state=state, run_id=model.run_id)
            self.assertEqual(caught.exception.error_code, "approved_requirement_not_found")

    def test_v2_reader_rejects_projection_tampering_before_phase2_mutates_state(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, refs, model = self._v2_bundle(directory)
            Path(refs["requirement_packet"]).write_text("tampered", encoding="utf-8")
            settings = load_settings(
                root=self.root,
                runtime_root=store.runtime_root,
                overrides={"SDLC_LLM_MODE": "fake", "SDLC_SYNC_MODE": "mock"},
            )
            with self.assertRaises(HarnessError) as caught:
                PhaseTwoWorkflow(settings).approve_checkpoint(run_id=model.run_id)
            self.assertEqual(caught.exception.error_code, "requirement_projection_mismatch")
            persisted = store.read_state(model.run_id)
            self.assertEqual(persisted["status"], "waiting_requirement_approval")
            self.assertNotIn("approved_artifacts", persisted)

    def test_v2_reader_rejects_source_snapshot_tampering(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, _, model = self._v2_bundle(directory)
            snapshot_path = Path(str(model.source["artifact_ref"]))
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["text"] = "tampered source"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            with self.assertRaises(HarnessError) as caught:
                read_approved_requirement_input(store=store, state=state, run_id=model.run_id)
            self.assertEqual(caught.exception.error_code, "source_snapshot_corrupted")

    def test_v2_reader_rejects_blocking_model(self) -> None:
        with TemporaryDirectory() as directory:
            store, state, _, model = self._v2_bundle(directory, valid=False)
            with self.assertRaises(HarnessError) as caught:
                read_approved_requirement_input(store=store, state=state, run_id=model.run_id)
            self.assertEqual(caught.exception.error_code, "requirement_approval_blocked")

    def test_v2_graph_preserves_ids_and_excludes_unknown_items(self) -> None:
        with TemporaryDirectory() as directory:
            _, _, _, model = self._v2_bundle(directory)
            graph = build_requirement_graph_v2(model)
            ids = [item["id"] for item in graph["requirements"]]
            self.assertEqual(ids[:3], ["PRE-1", "MF-1", "MF-2"])
            self.assertIn("AF-1", ids)
            self.assertIn("EF-2", ids)
            self.assertIn("POST-1", ids)
            self.assertIn("BR-1", ids)
            self.assertIn("AC-1", ids)
            self.assertNotIn("GOAL-1", ids)
            self.assertNotIn("ACT-1", ids)
            self.assertEqual(graph["revision"], 1)

    def test_designer_and_validator_use_canonical_v2_ids(self) -> None:
        with TemporaryDirectory() as directory:
            _, _, _, model = self._v2_bundle(directory)
            designer_client = CaptureClient(["architecture", fake_design_model()])
            DesignerAgent(designer_client).draft(
                requirement_packet="review packet",
                formal_use_case="review formal",
                canonical_requirement=model,
            )
            prompt = designer_client.prompts[0]
            self.assertLess(prompt.index("Canonical Requirement Model V2"), prompt.index("Requirement Packet"))
            self.assertIn('"id": "MF-1"', prompt)

            from sdlc_harness.agents.designer import parse_design_model

            parsed = parse_design_model(fake_design_model())
            trace = ValidatorAgent(CompleteTraceClient()).construct_traceability(
                requirement_packet="ignored projection",
                canonical_requirement=model,
                class_graph=parsed.class_graph,
                sequence_graph=parsed.sequence_graph,
            )
            self.assertTrue(trace.ok)
            self.assertEqual(trace.mode, "traceability_v2")
            self.assertEqual(trace.class_trace[0]["requirement"], "PRE-1")

    def test_phase2_v2_repair_keeps_canonical_model_and_stable_ids(self) -> None:
        with TemporaryDirectory() as directory:
            store, _, _, model = self._v2_bundle(directory)
            settings = load_settings(
                root=self.root,
                runtime_root=store.runtime_root,
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_DESIGN_REPAIR_LIMIT": "1",
                },
            )
            workflow = PhaseTwoWorkflow(settings)
            designer = CanonicalRepairingDesigner()
            validator = CanonicalRepairValidator()
            workflow.designer = designer
            workflow.validator = validator

            response = workflow.approve_checkpoint(run_id=model.run_id)

            self.assertEqual(response["status"], "waiting_design_approval")
            self.assertTrue(response["latest_artifacts"]["design_artifact"].endswith("design_artifact_v2.md"))
            self.assertEqual(response["quality_reports"]["design_traceability"]["mode"], "traceability_v2")
            self.assertEqual(response["quality_reports"]["design_traceability"]["versions"], ["v1", "v2"])
            self.assertEqual(validator.calls, 2)
            self.assertEqual(validator.requirement_ids_by_call[0], validator.requirement_ids_by_call[1])
            self.assertIn("MF-1", validator.requirement_ids_by_call[1])
            self.assertIs(designer.draft_models[0], designer.repair_models[0])
            self.assertEqual(designer.draft_models[0].to_dict(), model.to_dict())

            first_trace = json.loads(
                (store.run_dir(model.run_id) / "artifacts" / "requirement_design_trace_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            repaired_trace = json.loads(
                (store.run_dir(model.run_id) / "artifacts" / "requirement_design_trace_v2.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(first_trace["mode"], "traceability_v2")
            self.assertEqual(repaired_trace["mode"], "traceability_v2")
            self.assertEqual(first_trace["requirement_revision"], 1)
            self.assertEqual(repaired_trace["requirement_revision"], 1)


if __name__ == "__main__":
    unittest.main()
