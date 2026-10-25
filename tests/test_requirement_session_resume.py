"""Kiểm thử Requirement session tiếp tục đúng cursor, budget và frozen source."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sdlc_harness.agents.requirements import RequirementModelV2Refiner
from sdlc_harness.agents.requirements.session_v1 import (
    persist_requirement_action_intent,
    read_requirement_session,
    write_requirement_session,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.core.config import load_settings
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.sources.fixtures import UseCaseSource
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


class ScriptedClient:
    def __init__(self, completions: list[object]):
        self.completions = list(completions)
        self.calls = 0
        self.settings = SimpleNamespace(
            llm_structured_max_tokens=4096,
            llm_mode="fake",
            llm_model="session-test",
        )

    def complete_with_metadata(self, **_: object) -> LlmCompletion:
        self.calls += 1
        value = self.completions.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        assert isinstance(value, LlmCompletion)
        return value


class RequirementSessionResumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        source_path = root / "fixtures" / "etour" / "UC2.txt"
        cls.source = UseCaseSource(
            dataset="eTour",
            use_case="UC2",
            source_ref="dataset_use_case:eTour:UC2",
            source_path=source_path,
            text=source_path.read_text(encoding="utf-8").strip(),
        )
        cls.payload = json.loads(
            (root / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8")
        )

    @staticmethod
    def action(name: str, arguments: dict[str, object]) -> LlmCompletion:
        return LlmCompletion(
            content=json.dumps({"action": name, "arguments": arguments, "summary": name}),
            finish_reason="stop",
        )

    def candidate(self) -> LlmCompletion:
        payload = deepcopy(self.payload)
        for evidence in payload["evidence"]:
            evidence["observation_ref"] = "OBS-1"
        return self.action("complete", {"requirement_model": payload})

    def test_transport_resume_keeps_budget_and_does_not_repeat_source_actions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            source_ref = ".runtime/runs/run_resume/artifacts/source.json"
            first_client = ScriptedClient(
                [
                    self.action("source.list", {}),
                    self.action("source.read", {"source_id": "SRC-1"}),
                    HarnessError("llm_transport_failed", "temporary network failure"),
                ]
            )
            with self.assertRaises(HarnessError) as paused:
                RequirementModelV2Refiner(first_client, store=store).refine(
                    self.source,
                    run_id="run_resume",
                    source_artifact_ref=source_ref,
                )
            self.assertEqual(paused.exception.error_code, "llm_transport_failed")
            before = read_requirement_session(store=store, run_id="run_resume")
            self.assertEqual(before["budgets"]["action_used"], 3)
            self.assertEqual(before["working"]["observation_count"], 1)

            second_client = ScriptedClient([self.candidate()])
            result = RequirementModelV2Refiner(second_client, store=store).refine(
                self.source,
                run_id="run_resume",
                source_artifact_ref=source_ref,
                resume=True,
            )

            after = read_requirement_session(store=store, run_id="run_resume")
            journal_path = root / result.source_journal_path
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertTrue(result.final_report.ok)
            self.assertEqual(second_client.calls, 1)
            self.assertEqual(after["budgets"]["action_used"], 4)
            self.assertEqual(after["budgets"]["candidate_used"], 1)
            self.assertEqual(after["resume_count"], 1)
            self.assertEqual([item["call_id"] for item in journal["calls"]], ["RST-1", "RST-2", "RST-3", "RST-4"])
            self.assertEqual(journal["calls"][2]["tool_result"]["status"], "interrupted")

    def test_historical_run_without_session_is_not_auto_migrated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            with self.assertRaises(HarnessError) as raised:
                RequirementModelV2Refiner(ScriptedClient([]), store=store).refine(
                    self.source,
                    run_id="run_historical",
                    source_artifact_ref=".runtime/runs/run_historical/artifacts/source.json",
                    resume=True,
                )
            self.assertEqual(raised.exception.error_code, "requirement_session_not_resumable")
            self.assertFalse(
                store.artifact_path("run_historical", "requirement_source_manifest_v1.json").exists()
            )

    def test_phase_one_resumes_jira_run_from_frozen_snapshot_without_refetch(self) -> None:
        with TemporaryDirectory() as directory:
            project_root = Path(__file__).resolve().parents[1]
            runtime_root = Path(directory) / ".runtime"
            settings = load_settings(
                root=project_root,
                runtime_root=runtime_root,
                overrides={
                    "SDLC_LLM_MODE": "fake",
                    "SDLC_LLM_MODEL": "session-test",
                    "SDLC_SYNC_MODE": "mock",
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                },
            )
            jira_source = UseCaseSource(
                dataset="jira",
                use_case="SDLC-77",
                source_ref="jira_issue:SDLC-77",
                source_path=None,
                text=self.source.text,
                source_type="jira",
                locator="jira:SDLC-77",
                revision="rev-1",
            )
            first = PhaseOneWorkflow(settings)
            first.refiner_v2 = RequirementModelV2Refiner(
                ScriptedClient(
                    [
                        self.action("source.list", {}),
                        self.action("source.read", {"source_id": "SRC-1"}),
                        HarnessError("llm_transport_failed", "temporary network failure"),
                    ]
                ),
                store=first.store,
            )
            with patch("sdlc_harness.workflows.phase1.load_jira_issue_source", return_value=jira_source) as loader:
                paused = first.start_from_jira(issue_key="SDLC-77", requested_by="test")
            loader.assert_called_once()
            self.assertEqual(paused["status"], "requirement_paused")
            self.assertIn("continue_run", paused["allowed_next_actions"])
            self.assertNotIn("requirement_loop_session", paused["latest_artifacts"])
            self.assertEqual(paused["requirement_execution"]["action_budget"]["used"], 3)

            resumed = PhaseOneWorkflow(settings)
            resumed.refiner_v2 = RequirementModelV2Refiner(
                ScriptedClient([self.candidate()]),
                store=resumed.store,
            )
            with patch(
                "sdlc_harness.workflows.phase1.load_jira_issue_source",
                side_effect=AssertionError("Jira must not be fetched during resume"),
            ):
                completed = resumed.continue_requirement_run(run_id=str(paused["run_id"]))

            self.assertIn(completed["status"], {"waiting_requirement_clarification", "waiting_requirement_approval"})
            self.assertEqual(completed["requirement_execution"]["status"], "completed")
            self.assertEqual(completed["requirement_execution"]["resume_count"], 1)

    def test_persisted_action_is_executed_without_another_llm_call(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            source_ref = ".runtime/runs/run_intent/artifacts/source.json"
            first_client = ScriptedClient(
                [
                    self.action("source.list", {}),
                    self.action("source.read", {"source_id": "SRC-1"}),
                    HarnessError("llm_transport_failed", "temporary network failure"),
                ]
            )
            with self.assertRaises(HarnessError):
                RequirementModelV2Refiner(first_client, store=store).refine(
                    self.source, run_id="run_intent", source_artifact_ref=source_ref,
                )
            session = read_requirement_session(store=store, run_id="run_intent")
            candidate = json.loads(self.candidate().content)
            persist_requirement_action_intent(
                store=store,
                run_id="run_intent",
                session=session,
                action=candidate,
                completion_metadata={
                    "finish_reason": "stop",
                    "usage": {},
                    "duration_seconds": 0.1,
                    "max_tokens": 4096,
                },
                reserve_candidate=True,
            )

            no_call_client = ScriptedClient([])
            result = RequirementModelV2Refiner(no_call_client, store=store).refine(
                self.source,
                run_id="run_intent",
                source_artifact_ref=source_ref,
                resume=True,
            )

            self.assertTrue(result.final_report.ok)
            self.assertEqual(no_call_client.calls, 0)
            self.assertEqual(read_requirement_session(store=store, run_id="run_intent")["budgets"]["action_used"], 3)

    def test_stale_cursor_after_completed_journal_does_not_repeat_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            source_ref = ".runtime/runs/run_stale/artifacts/source.json"
            first_client = ScriptedClient(
                [
                    self.action("source.list", {}),
                    self.action("source.read", {"source_id": "SRC-1"}),
                    self.candidate(),
                ]
            )
            RequirementModelV2Refiner(first_client, store=store).refine(
                self.source, run_id="run_stale", source_artifact_ref=source_ref,
            )
            session = read_requirement_session(store=store, run_id="run_stale")
            intent_path = store.artifact_path("run_stale", "requirement_action_intent_v1_RST-3.json")
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            session["status"] = "paused"
            session["cursor"] = {
                "next_action_index": 3,
                "pending_call": {
                    "call_id": "RST-3",
                    "action_index": 3,
                    "status": "action_persisted",
                    "intent_ref": store.relative(intent_path),
                    "intent_sha256": intent["action_sha256"],
                    "candidate_reserved": True,
                    "started_at": session["created_at"],
                    "completion": intent["completion"],
                },
            }
            write_requirement_session(store=store, run_id="run_stale", session=session)

            no_call_client = ScriptedClient([])
            result = RequirementModelV2Refiner(no_call_client, store=store).refine(
                self.source,
                run_id="run_stale",
                source_artifact_ref=source_ref,
                resume=True,
            )

            self.assertTrue(result.final_report.ok)
            self.assertEqual(no_call_client.calls, 0)
            journal = json.loads((root / result.source_journal_path).read_text(encoding="utf-8"))
            self.assertEqual([call["call_id"] for call in journal["calls"]].count("RST-3"), 1)


if __name__ == "__main__":
    unittest.main()
