"""Tests for the isolated progressive design-validator experiment."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.agents.validator import TraceabilityResult, build_requirement_graph_v2
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmClient, LlmCompletion, fake_design_model
from sdlc_harness.workflows.design_experiment import (
    BudgetedLlmClient,
    ProgressiveDesignExperiment,
    SemanticCandidate,
    _aggregate_assessments,
    _candidate_feedback,
    _validate_assessments,
)
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


class ProgressiveFakeClient:
    def __init__(self, settings, *, bad_design: bool = False):
        self.settings = settings
        self.delegate = LlmClient(settings)
        self.bad_design = bad_design
        self.systems: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.systems.append(system)
        if self.bad_design and "Designer.act" in system:
            return fake_design_model().replace(
                "Boundary -> Service: validateDeletion(heritageId)",
                "Boundary -> Service: missingOperation(heritageId)",
            )
        return self.delegate.complete(system=system, prompt=prompt)

    def complete_with_metadata(self, *, system: str, prompt: str, json_mode=False, max_tokens=None):
        self.systems.append(system)
        if "Progressive Design Semantic Judge" in system:
            context = json.loads(prompt.split("INPUT:\n", 1)[1])
            assessments = [
                {
                    "claim_id": item["claim_id"],
                    "verdict": "satisfied",
                    "category": "none",
                    "message": "Covered by the traced design.",
                    "expected_change": "",
                    "design_refs": item["allowed_design_refs"][:1],
                }
                for item in context["claims"]
            ]
            return LlmCompletion(content=json.dumps({"assessments": assessments}), finish_reason="fake")
        return self.delegate.complete_with_metadata(
            system=system,
            prompt=prompt,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )

    def complete_messages(self, *, messages, json_mode=False, max_tokens=None):
        return self.delegate.complete_messages(messages=messages, json_mode=json_mode, max_tokens=max_tokens)


class RetrySemanticClient(ProgressiveFakeClient):
    def __init__(self, settings, *, invalid_semantic_calls: int):
        super().__init__(settings)
        self.invalid_semantic_calls = invalid_semantic_calls
        self.semantic_calls = 0

    def complete_with_metadata(self, *, system: str, prompt: str, json_mode=False, max_tokens=None):
        if "Progressive Design Semantic Judge" not in system:
            return super().complete_with_metadata(
                system=system,
                prompt=prompt,
                json_mode=json_mode,
                max_tokens=max_tokens,
            )
        self.systems.append(system)
        self.semantic_calls += 1
        context = json.loads(prompt.split("INPUT:\n", 1)[1])
        assessments = []
        for item in context["claims"]:
            refs = item["allowed_design_refs"][:1]
            if self.semantic_calls <= self.invalid_semantic_calls:
                refs = ["Invented.ref"]
            assessments.append(
                {
                    "claim_id": item["claim_id"],
                    "verdict": "satisfied",
                    "category": "none",
                    "message": "Covered by the traced design.",
                    "expected_change": "",
                    "design_refs": refs,
                }
            )
        return LlmCompletion(content=json.dumps({"assessments": assessments}), finish_reason="fake")


class MissingTraceValidator:
    def construct_traceability(self, *, canonical_requirement, **kwargs):
        graph = build_requirement_graph_v2(canonical_requirement)
        first = graph["requirements"][0]
        return TraceabilityResult(
            requirement_graph=graph,
            class_trace=[],
            sequence_trace=[],
            missing_links={
                "class_diagram": [{"requirement": first["id"], "content": first["content"]}],
                "sequence_diagram": [],
            },
            mode="test",
        )


class ProgressiveDesignExperimentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp.name)
        self.runtime_root = self.temp_root / ".runtime"
        self.experiment_root = self.temp_root / ".experiments" / "design-validator"
        self.settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={
                "SDLC_LLM_MODE": "fake",
                "SDLC_SYNC_MODE": "mock",
                "SDLC_DESIGN_REPAIR_LIMIT": "3",
            },
        )
        created = PhaseOneWorkflow(self.settings).start_or_get_run(
            dataset="eTour", use_case="UC1", requested_by="experiment-test"
        )
        self.source_run_id = str(created["run_id"])
        store = PhaseOneWorkflow(self.settings).store
        state = store.read_state(self.source_run_id)
        state["approved_artifacts"] = dict(state["latest_artifacts"])
        store.write_state(state)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_clean_run_passes_outside_runtime_without_sync(self) -> None:
        before = _tree_hashes(self.runtime_root)
        client = ProgressiveFakeClient(self.settings)
        result = ProgressiveDesignExperiment(
            self.settings,
            experiment_root=self.experiment_root,
            llm_call_budget=16,
            llm_client=client,
        ).run(source_run_id=self.source_run_id)

        self.assertEqual(result["status"], "design_experiment_passed")
        self.assertEqual(result["experiment"]["llm_calls"]["used"], 7)
        self.assertTrue(result["experiment"]["source_integrity"]["unchanged"])
        self.assertEqual(_tree_hashes(self.runtime_root), before)
        run_dir = self.experiment_root / "runs" / str(result["run_id"])
        self.assertTrue((run_dir / "artifacts" / "experiment_report.json").is_file())
        self.assertFalse((run_dir / "sync").exists())
        self.assertFalse((self.experiment_root / "index" / "active_runs.json").exists())

    def test_preflight_failure_skips_traceability_and_semantic_calls(self) -> None:
        zero_repair_settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_LLM_MODE": "fake", "SDLC_DESIGN_REPAIR_LIMIT": "0"},
        )
        client = ProgressiveFakeClient(zero_repair_settings, bad_design=True)
        result = ProgressiveDesignExperiment(
            zero_repair_settings,
            experiment_root=self.experiment_root,
            llm_client=client,
        ).run(source_run_id=self.source_run_id)

        self.assertEqual(result["status"], "design_experiment_exhausted")
        self.assertEqual(result["experiment"]["trajectory"][0]["stage"], "deterministic_preflight")
        self.assertFalse(any("traceability" in value.casefold() for value in client.systems))
        self.assertFalse(any("semantic" in value.casefold() for value in client.systems))

    def test_traceability_failure_skips_semantic_calls(self) -> None:
        zero_repair_settings = load_settings(
            root=self.repo_root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_LLM_MODE": "fake", "SDLC_DESIGN_REPAIR_LIMIT": "0"},
        )
        client = ProgressiveFakeClient(zero_repair_settings)
        workflow = ProgressiveDesignExperiment(
            zero_repair_settings,
            experiment_root=self.experiment_root,
            llm_client=client,
        )
        workflow.trace_validator = MissingTraceValidator()
        result = workflow.run(source_run_id=self.source_run_id)

        self.assertEqual(result["status"], "design_experiment_exhausted")
        self.assertEqual(result["experiment"]["trajectory"][0]["stage"], "traceability")
        self.assertFalse(any("semantic" in value.casefold() for value in client.systems))

    def test_two_of_three_votes_confirm_and_one_vote_remains_disputed(self) -> None:
        assessments = [
            ("trial-1", _assessment("CLAIM-AC-1", "terminal_state")),
            ("trial-2", _assessment("CLAIM-AC-1", "terminal_state")),
            ("trial-3", _assessment("CLAIM-BR-1", "data_consistency")),
        ]
        candidates = _aggregate_assessments(assessments)

        support = {item.key: item.support_count for item in candidates}
        self.assertEqual(support["CLAIM-AC-1|terminal_state"], 2)
        self.assertEqual(support["CLAIM-BR-1|data_consistency"], 1)

    def test_semantic_trial_must_cover_exact_fixed_claim_set(self) -> None:
        claims = [
            {"claim_id": "CLAIM-AC-1", "allowed_design_refs": ["Service.finish"]},
            {"claim_id": "CLAIM-BR-1", "allowed_design_refs": ["Repository.save"]},
        ]
        with self.assertRaises(HarnessError):
            _validate_assessments(
                {"assessments": [_assessment("CLAIM-AC-1", "terminal_state")]},
                claims,
            )

    def test_semantic_trial_rejects_evidence_outside_fixed_claim(self) -> None:
        claims = [{"claim_id": "CLAIM-AC-1", "allowed_design_refs": ["Service.finish"]}]
        assessment = _assessment("CLAIM-AC-1", "terminal_state")
        assessment["design_refs"] = ["Invented.ref"]
        with self.assertRaises(HarnessError):
            _validate_assessments({"assessments": [assessment]}, claims)

    def test_semantic_trials_retry_invalid_evidence_without_counting_its_vote(self) -> None:
        client = RetrySemanticClient(self.settings, invalid_semantic_calls=1)
        workflow = ProgressiveDesignExperiment(
            self.settings,
            experiment_root=self.experiment_root,
            llm_call_budget=16,
            llm_client=client,
        )

        confirmed, disputed, report = workflow.semantic_trials(
            claims=[
                {
                    "claim_id": "CLAIM-AC-1",
                    "requirement_id": "AC-1",
                    "allowed_design_refs": ["Service.finish"],
                }
            ],
            requirement_model={},
            design_model="design",
            class_graph={},
            sequence_graph={},
        )

        self.assertEqual(confirmed, [])
        self.assertEqual(disputed, [])
        self.assertTrue(report["complete"])
        self.assertEqual(report["valid_trials"], 3)
        self.assertEqual(report["attempt_count"], 4)
        self.assertEqual(report["invalid_trial_count"], 1)
        self.assertIn("CLAIM-AC-1", report["invalid_trials"][0]["message"])
        self.assertEqual(workflow.llm.used, 4)

    def test_semantic_trials_stop_inconclusive_when_valid_quorum_cannot_fit_budget(self) -> None:
        client = RetrySemanticClient(self.settings, invalid_semantic_calls=10)
        workflow = ProgressiveDesignExperiment(
            self.settings,
            experiment_root=self.experiment_root,
            llm_call_budget=6,
            llm_client=client,
        )

        _, _, report = workflow.semantic_trials(
            claims=[
                {
                    "claim_id": "CLAIM-AC-1",
                    "requirement_id": "AC-1",
                    "allowed_design_refs": ["Service.finish"],
                }
            ],
            requirement_model={},
            design_model="design",
            class_graph={},
            sequence_graph={},
        )

        self.assertFalse(report["complete"])
        self.assertEqual(report["valid_trials"], 0)
        self.assertEqual(report["invalid_trial_count"], 4)
        self.assertEqual(workflow.llm.remaining, 2)

    def test_adjudicator_cannot_introduce_candidate(self) -> None:
        class AdjudicationClient(ProgressiveFakeClient):
            def complete_with_metadata(self, **kwargs):
                return LlmCompletion(
                    content=json.dumps(
                        {
                            "decisions": [
                                {"candidate_id": "CAND-001", "decision": "refuted"},
                                {"candidate_id": "CAND-NEW", "decision": "supported"},
                            ]
                        }
                    )
                )

        workflow = ProgressiveDesignExperiment(
            self.settings,
            experiment_root=self.experiment_root,
            llm_client=AdjudicationClient(self.settings),
        )
        candidate = _candidate("CAND-001")
        with self.assertRaises(HarnessError):
            workflow.adjudicate([candidate], requirement_model={}, design_model="design")

    def test_feedback_and_budget_are_bounded(self) -> None:
        candidates = [_candidate(f"CAND-{index:03d}") for index in range(1, 5)]
        feedback = _candidate_feedback(candidates)
        self.assertIn("CAND-001", feedback)
        self.assertIn("CAND-002", feedback)
        self.assertNotIn("CAND-003", feedback)

        client = BudgetedLlmClient(ProgressiveFakeClient(self.settings), limit=2)
        client.complete(system="Designer.analysis", prompt="one")
        client.complete(system="Designer.analysis", prompt="two")
        with self.assertRaises(HarnessError) as caught:
            client.complete(system="Designer.analysis", prompt="three")
        self.assertEqual(caught.exception.error_code, "design_llm_budget_exhausted")


def _assessment(claim_id: str, category: str) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "verdict": "violated",
        "category": category,
        "message": "Contradiction",
        "expected_change": "Correct it",
        "design_refs": [],
    }


def _candidate(candidate_id: str) -> SemanticCandidate:
    return SemanticCandidate(
        candidate_id=candidate_id,
        claim_id="CLAIM-AC-1",
        requirement_id="AC-1",
        category="terminal_state",
        message="Wrong terminal state",
        expected_change="Fix terminal state",
        design_refs=["finish"],
        support_count=1,
        trial_ids=["trial-1"],
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


if __name__ == "__main__":
    unittest.main()
