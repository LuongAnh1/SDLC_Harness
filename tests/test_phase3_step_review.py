"""Focused tests for persisted Phase 3 LLM exchange review."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sdlc_harness.llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from sdlc_harness.llm.client import LlmCompletion
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

    def complete_with_metadata(self, *, system: str, prompt: str, **_: object) -> LlmCompletion:
        return LlmCompletion(
            content=self.complete(system=system, prompt=prompt),
            finish_reason="stop",
            usage={"completion_tokens": 7},
        )


class PhaseThreeStepReviewTest(unittest.TestCase):
    def test_input_review_pauses_before_provider_then_keeps_output_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"action":"repo.read","arguments":{"path":"package.json"}}'])

            first = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-input-review",
                review={"review_input": True},
            )
            with self.assertRaises(LlmStepReviewRequired) as input_pause:
                first.complete(system="planner", prompt="build plan")

            self.assertEqual(input_pause.exception.progress["review_stage"], "input")
            self.assertTrue(str(input_pause.exception.progress["call_id"]).endswith("-input"))
            self.assertEqual(base.prompts, [])
            exchange = json.loads(
                (root / str(input_pause.exception.progress["exchange_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(exchange["status"], "input_pending")
            self.assertEqual(exchange["raw_completion"], "")

            approved_input = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-input-review",
                review={
                    "review_input": True,
                    "call_id": input_pause.exception.progress["call_id"],
                    "decision": "approve",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as output_pause:
                approved_input.complete(system="planner", prompt="build plan")

            self.assertEqual(output_pause.exception.progress["review_stage"], "output")
            self.assertEqual(base.prompts, ["build plan"])

            approved_output = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-input-review",
                review={
                    "review_input": True,
                    "call_id": output_pause.exception.progress["call_id"],
                    "decision": "approve",
                },
            )
            self.assertIn("package.json", approved_output.complete(system="planner", prompt="build plan"))
            self.assertEqual(len(base.prompts), 1)

    def test_rejected_input_creates_new_input_checkpoint_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"unused"}'])
            first = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-input-reject",
                review={"review_input": True},
            )
            with self.assertRaises(LlmStepReviewRequired) as first_pause:
                first.complete(system="planner", prompt="build plan")

            rejected = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-input-reject",
                review={
                    "review_input": True,
                    "call_id": first_pause.exception.progress["call_id"],
                    "decision": "reject",
                    "note": "Include the approved acceptance binding.",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as retry_pause:
                rejected.complete(system="planner", prompt="build plan")

            self.assertEqual(retry_pause.exception.progress["review_stage"], "input")
            self.assertIn("-a02-input", str(retry_pause.exception.progress["call_id"]))
            self.assertEqual(base.prompts, [])
            session = json.loads(
                (store.run_dir("run-input-reject") / "implementation_plan_execution" / "step_review_session_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual([item["status"] for item in session["calls"]], ["input_rejected", "input_pending"])

    def test_metadata_exchange_persists_and_replays_input_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(
                [
                    '{"action":"repo.read","arguments":{"path":"src/main.ts"}}\n'
                    "Wait — use the package manifest instead.\n"
                    '<｜end▁of▁thinking｜>{"action":"repo.read","arguments":'
                    '{"path":"package.json"}}'
                ]
            )
            first = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-developer",
                call_prefix="developer-wu-001",
                execution_dir="developer_execution/act_v1/step_review/wu-001",
                error_prefix="developer",
                label="Developer WU-001",
            )

            with self.assertRaises(LlmStepReviewRequired) as paused:
                first.complete_with_metadata(
                    system="developer system",
                    prompt="developer prompt",
                    json_mode=True,
                    max_tokens=2048,
                )

            exchange = json.loads(
                (root / str(paused.exception.progress["exchange_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(exchange["system"], "developer system")
            self.assertEqual(exchange["prompt"], "developer prompt")
            self.assertEqual(exchange["parsed_output"]["action"], "repo.read")
            self.assertEqual(exchange["parsed_output"]["arguments"], {"path": "package.json"})
            self.assertEqual(exchange["request_options"], {"json_mode": True, "max_tokens": 2048})

            resumed = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-developer",
                review={"call_id": exchange["call_id"], "decision": "approve"},
                call_prefix="developer-wu-001",
                execution_dir="developer_execution/act_v1/step_review/wu-001",
                error_prefix="developer",
                label="Developer WU-001",
            )
            completion = resumed.complete_with_metadata(
                system="developer system",
                prompt="developer prompt",
                json_mode=True,
                max_tokens=2048,
            )
            self.assertEqual(completion.content, exchange["raw_completion"])
            self.assertEqual(completion.finish_reason, "stop")
            self.assertEqual(len(base.prompts), 1)

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
