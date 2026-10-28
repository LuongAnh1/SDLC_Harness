"""Focused tests for persisted Phase 3 LLM exchange review."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sdlc_harness.llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.storage.runtime import RuntimeStore


class _FakeClient:
    def __init__(self, outputs: list[str]):
        self.settings = SimpleNamespace(llm_mode="fake")
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.outputs.pop(0)


class PhaseThreeStepReviewTest(unittest.TestCase):
    def test_scoped_review_uses_design_validator_identity_and_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            client = ReviewableLlmClient(
                _FakeClient(['{"ok":true,"findings":[]}']),
                store=store,
                run_id="run-design",
                call_prefix="design-validator",
                execution_dir="design_validator_execution/design_v2",
                error_prefix="design_validator",
                label="Design Validator",
            )

            with self.assertRaises(LlmStepReviewRequired) as paused:
                client.complete(system="validator", prompt="review design")

            self.assertEqual(paused.exception.error_code, "design_validator_step_review_required")
            self.assertEqual(paused.exception.progress["call_id"], "design-validator-0001-a01")
            self.assertTrue(
                (store.run_dir("run-design") / "design_validator_execution" / "design_v2" / "step_review_session_v1.json").exists()
            )

    def test_approved_exchange_is_replayed_without_calling_provider_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":1}', '{"review":2}'])
            first = ReviewableLlmClient(base, store=store, run_id="run-1")
            with self.assertRaises(LlmStepReviewRequired) as paused:
                first.complete(system="planner", prompt="build plan")

            resumed = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-1",
                review={
                    "call_id": paused.exception.progress["call_id"],
                    "decision": "approve",
                },
            )
            self.assertEqual(resumed.complete(system="planner", prompt="build plan"), '{"plan":1}')
            with self.assertRaises(LlmStepReviewRequired) as second_pause:
                resumed.complete(system="validator", prompt="review plan")

            self.assertEqual(len(base.prompts), 2)
            self.assertEqual(second_pause.exception.progress["call_id"], "implementation-plan-0002-a01")

    def test_rejection_calls_provider_again_with_operator_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"bad"}', '{"plan":"fixed"}'])
            first = ReviewableLlmClient(base, store=store, run_id="run-2")
            with self.assertRaises(LlmStepReviewRequired) as paused:
                first.complete(system="planner", prompt="build plan")

            rejected = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-2",
                review={
                    "call_id": paused.exception.progress["call_id"],
                    "decision": "reject",
                    "note": "Keep the UI and API in one vertical work unit.",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as next_pause:
                rejected.complete(system="planner", prompt="build plan")

            self.assertEqual(next_pause.exception.progress["call_id"], "implementation-plan-0001-a02")
            self.assertIn("Keep the UI and API", base.prompts[-1])
            session = json.loads(
                (store.run_dir("run-2") / "implementation_plan_execution" / "step_review_session_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([item["status"] for item in session["calls"]], ["rejected", "pending"])
            self.assertTrue(session["calls"][1]["resume_prompt_hash"].startswith("sha256:"))

    def test_rejected_retry_can_be_approved_and_replayed_against_base_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"bad"}', '{"plan":"fixed"}'])
            first = ReviewableLlmClient(base, store=store, run_id="run-retry-approve")
            with self.assertRaises(LlmStepReviewRequired) as first_pause:
                first.complete(system="planner", prompt="build plan")

            rejected = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-retry-approve",
                review={
                    "call_id": first_pause.exception.progress["call_id"],
                    "decision": "reject",
                    "note": "Fix the approved target binding.",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as retry_pause:
                rejected.complete(system="planner", prompt="build plan")

            approved = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-retry-approve",
                review={
                    "call_id": retry_pause.exception.progress["call_id"],
                    "decision": "approve",
                },
            )
            self.assertEqual(
                approved.complete(system="planner", prompt="build plan"),
                '{"plan":"fixed"}',
            )

            replayed = ReviewableLlmClient(base, store=store, run_id="run-retry-approve")
            self.assertEqual(
                replayed.complete(system="planner", prompt="build plan"),
                '{"plan":"fixed"}',
            )
            self.assertEqual(len(base.prompts), 2)

    def test_rejection_replaces_a_pending_exchange_after_prompt_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"old"}', '{"plan":"new"}'])
            first = ReviewableLlmClient(base, store=store, run_id="run-drift-reject")
            with self.assertRaises(LlmStepReviewRequired) as paused:
                first.complete(system="planner", prompt="old prompt")

            rejected = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-drift-reject",
                review={
                    "call_id": paused.exception.progress["call_id"],
                    "decision": "reject",
                    "note": "Use the new Harness-owned targets.",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as next_pause:
                rejected.complete(system="planner", prompt="new prompt")

            self.assertEqual(next_pause.exception.progress["call_id"], "implementation-plan-0001-a02")
            self.assertIn("new prompt", base.prompts[-1])
            self.assertNotIn("old prompt", base.prompts[-1])
            session = json.loads(
                (store.run_dir("run-drift-reject") / "implementation_plan_execution" / "step_review_session_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(session["calls"][0]["replacement_prompt_hash"].startswith("sha256:"))

    def test_approval_still_fails_closed_after_prompt_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"old"}'])
            first = ReviewableLlmClient(base, store=store, run_id="run-drift-approve")
            with self.assertRaises(LlmStepReviewRequired) as paused:
                first.complete(system="planner", prompt="old prompt")

            resumed = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-drift-approve",
                review={
                    "call_id": paused.exception.progress["call_id"],
                    "decision": "approve",
                },
            )
            with self.assertRaises(HarnessError) as drift:
                resumed.complete(system="planner", prompt="new prompt")

            self.assertEqual(drift.exception.error_code, "implementation_plan_step_review_drift")
            self.assertEqual(len(base.prompts), 1)


if __name__ == "__main__":
    unittest.main()
