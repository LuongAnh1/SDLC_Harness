"""Focused tests for persisted Phase 3 LLM exchange review."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from sdlc_harness.llm.step_review import (
    LlmStepReviewRequired,
    ReviewableLlmClient,
    _filesystem_path,
    _parsed_output,
)
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
            reasoning_content="private provider reasoning",
        )


class PhaseThreeStepReviewTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows extended paths only")
    def test_step_review_supports_exchange_paths_longer_than_260_characters(self) -> None:
        temp = tempfile.mkdtemp()
        try:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            execution_dir = "step_review/" + "/".join(["x" * 60] * 3)
            base = _FakeClient(['{"ok":true}'])
            first = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-long-path",
                call_prefix="code-validation-boundary-cross-scenario",
                execution_dir=execution_dir,
            )

            with self.assertRaises(LlmStepReviewRequired) as paused:
                first.complete(system="validator", prompt="review boundary")

            exchange_path = root / str(paused.exception.progress["exchange_path"])
            self.assertGreater(len(str(exchange_path)), 260)
            filesystem_exchange = _filesystem_path(exchange_path)
            filesystem_exchange.unlink()
            recovered = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-long-path",
                call_prefix="code-validation-boundary-cross-scenario",
                execution_dir=execution_dir,
            )
            with self.assertRaises(LlmStepReviewRequired):
                recovered.complete(system="validator", prompt="review boundary")
            self.assertTrue(filesystem_exchange.exists())
            resumed = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-long-path",
                review={"call_id": paused.exception.progress["call_id"], "decision": "approve"},
                call_prefix="code-validation-boundary-cross-scenario",
                execution_dir=execution_dir,
            )
            self.assertEqual(
                resumed.complete(system="validator", prompt="review boundary"),
                '{"ok":true}',
            )
            self.assertEqual(len(base.prompts), 1)
        finally:
            shutil.rmtree(_filesystem_path(Path(temp)), ignore_errors=True)

    def test_parsed_output_accepts_validator_json_not_only_tool_actions(self) -> None:
        self.assertEqual(
            _parsed_output('{"ok":true,"findings":[]}'),
            {"ok": True, "findings": []},
        )

    def test_parsed_output_does_not_extract_nested_object_from_malformed_root(self) -> None:
        raw = (
            '{"action":"execution.complete_unit","arguments":{"summary":"bar\n\nrels",'
            '"path_dispositions":[{"path":"src/client.ts","status":"verified_unchanged"}]}}'
        )

        self.assertIsNone(_parsed_output(raw))

    def test_parsed_output_still_accepts_json_after_plain_text(self) -> None:
        self.assertEqual(
            _parsed_output('Result follows:\n{"ok":true,"findings":[]}'),
            {"ok": True, "findings": []},
        )

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
                    "note": "Keep the approved acceptance binding explicit.",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as output_pause:
                approved_input.complete(system="planner", prompt="build plan")

            self.assertEqual(output_pause.exception.progress["review_stage"], "output")
            self.assertEqual(len(base.prompts), 1)
            self.assertIn("Keep the approved acceptance binding explicit.", base.prompts[0])
            self.assertTrue(base.prompts[0].endswith("build plan"))

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
            self.assertEqual(exchange["reasoning_content"], "private provider reasoning")
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
            self.assertEqual(completion.reasoning_content, "private provider reasoning")
            self.assertEqual(completion.finish_reason, "stop")
            self.assertEqual(len(base.prompts), 1)

    def test_image_references_are_auditable_and_part_of_the_replay_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            first_image = root / "first.png"
            second_image = root / "second.png"
            first_image.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
            second_image.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
            first_ref = {
                "label": "desktop",
                "path": str(first_image),
                "sha256": hashlib.sha256(first_image.read_bytes()).hexdigest(),
            }
            second_ref = {
                "label": "desktop",
                "path": str(second_image),
                "sha256": hashlib.sha256(second_image.read_bytes()).hexdigest(),
            }
            base = _FakeClient(['{"ok":true}'])
            first = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-images",
                review={"review_input": True},
            )
            with self.assertRaises(LlmStepReviewRequired) as input_pause:
                first.complete_with_metadata(
                    system="validator", prompt="review", images=[first_ref], accepts_images=True
                )

            exchange_path = root / str(input_pause.exception.progress["exchange_path"])
            serialized = exchange_path.read_text(encoding="utf-8")
            exchange = json.loads(serialized)
            self.assertNotIn("base64", serialized)
            self.assertEqual(exchange["images"][0]["bytes"], first_image.stat().st_size)
            self.assertEqual(exchange["images"], exchange["request_options"]["images"])

            approved_input = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-images",
                review={
                    "review_input": True,
                    "call_id": input_pause.exception.progress["call_id"],
                    "decision": "approve",
                },
            )
            with self.assertRaises(LlmStepReviewRequired) as output_pause:
                approved_input.complete_with_metadata(
                    system="validator", prompt="review", images=[first_ref], accepts_images=True
                )
            approved_output = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-images",
                review={"call_id": output_pause.exception.progress["call_id"], "decision": "approve"},
            )
            self.assertEqual(
                approved_output.complete_with_metadata(
                    system="validator", prompt="review", images=[first_ref], accepts_images=True
                ).content,
                '{"ok":true}',
            )
            # G1.5: another image is another input; the approved answer is superseded, never replayed.
            with self.assertRaises(LlmStepReviewRequired) as changed:
                ReviewableLlmClient(
                    base, store=store, run_id="run-images", review={"review_input": True}
                ).complete_with_metadata(system="validator", prompt="review", images=[second_ref], accepts_images=True)
            self.assertEqual(changed.exception.progress["call_id"], "implementation-plan-0001-a02-input")
            self.assertEqual(len(base.prompts), 1)
            old = json.loads(exchange_path.read_text(encoding="utf-8"))
            self.assertEqual((old["status"], old["superseded_reason"]), ("superseded", "input_changed"))

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

    def test_approving_an_outdated_exchange_opens_a_new_one_for_the_changed_prompt(self) -> None:
        # G1.5: the approval names the old input; the changed prompt gets its own exchange and its own review.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"old"}', '{"plan":"new"}'])
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
            with self.assertRaises(LlmStepReviewRequired) as changed:
                resumed.complete(system="planner", prompt="new prompt")

            self.assertEqual(changed.exception.progress["call_id"], "implementation-plan-0001-a02")
            self.assertEqual(base.prompts, ["old prompt", "new prompt"])
            execution = store.run_dir("run-drift-approve") / "implementation_plan_execution"
            old = json.loads((execution / "exchange_implementation-plan-0001-a01.json").read_text(encoding="utf-8"))
            new = json.loads((execution / "exchange_implementation-plan-0001-a02.json").read_text(encoding="utf-8"))
            self.assertEqual((old["status"], old["superseded_from_status"]), ("superseded", "pending"))
            self.assertEqual(new["supersedes"]["call_id"], "implementation-plan-0001-a01")
            self.assertEqual((new["supersedes"]["reason"], new["supersedes"]["first_difference"]), ("input_changed", 0))

            # The new exchange is approved on its own and then replays without another provider call.
            approved = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-drift-approve",
                review={"call_id": "implementation-plan-0001-a02", "decision": "approve"},
            )
            self.assertEqual(approved.complete(system="planner", prompt="new prompt"), '{"plan":"new"}')
            replay = ReviewableLlmClient(base, store=store, run_id="run-drift-approve")
            self.assertEqual(replay.complete(system="planner", prompt="new prompt"), '{"plan":"new"}')
            self.assertEqual(len(base.prompts), 2)

    def test_an_approved_exchange_whose_input_changed_is_asked_again_under_input_review(self) -> None:
        # The r9 group-A recoveries (prompt or contract changed mid-run) without a recovery: no provider call
        # runs until the owner approves the new input.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"plan":"old"}'])
            review = {"review_input": True}
            with self.assertRaises(LlmStepReviewRequired) as input_pause:
                ReviewableLlmClient(base, store=store, run_id="run-changed", review=review).complete(system="s", prompt="v1")
            with self.assertRaises(LlmStepReviewRequired) as output_pause:
                ReviewableLlmClient(
                    base, store=store, run_id="run-changed",
                    review={**review, "call_id": input_pause.exception.progress["call_id"], "decision": "approve"},
                ).complete(system="s", prompt="v1")
            ReviewableLlmClient(
                base, store=store, run_id="run-changed",
                review={**review, "call_id": output_pause.exception.progress["call_id"], "decision": "approve"},
            ).complete(system="s", prompt="v1")

            with self.assertRaises(LlmStepReviewRequired) as changed:
                ReviewableLlmClient(base, store=store, run_id="run-changed", review=review).complete(system="s", prompt="v2")

            self.assertEqual(changed.exception.progress["review_stage"], "input")
            self.assertEqual(changed.exception.progress["call_id"], "implementation-plan-0001-a02-input")
            self.assertEqual(base.prompts, ["v1"])

    def test_approved_output_note_reaches_the_next_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"action":1}', '{"action":2}'])
            first = ReviewableLlmClient(base, store=store, run_id="run-approve-note")
            with self.assertRaises(LlmStepReviewRequired) as first_pause:
                first.complete(system="developer", prompt="first decision")

            resumed = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-approve-note",
                review={
                    "call_id": first_pause.exception.progress["call_id"],
                    "decision": "approve",
                    "note": "The direct probe is authoritative; do not infer status from the UI label.",
                },
            )
            self.assertEqual(resumed.complete(system="developer", prompt="first decision"), '{"action":1}')
            with self.assertRaises(LlmStepReviewRequired):
                resumed.complete(system="developer", prompt="second decision")

            self.assertIn("The direct probe is authoritative", base.prompts[-1])
            self.assertTrue(base.prompts[-1].endswith("second decision"))

    def test_approved_output_note_can_be_audit_only_for_independent_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")
            base = _FakeClient(['{"scenario":1}', '{"scenario":2}'])
            first = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-audit-only-note",
                carry_approved_output_notes=False,
            )
            with self.assertRaises(LlmStepReviewRequired) as first_pause:
                first.complete(system="validator", prompt="scenario one")

            resumed = ReviewableLlmClient(
                base,
                store=store,
                run_id="run-audit-only-note",
                review={
                    "call_id": first_pause.exception.progress["call_id"],
                    "decision": "approve",
                    "note": "Scenario one routes to the API unit.",
                },
                carry_approved_output_notes=False,
            )
            self.assertEqual(resumed.complete(system="validator", prompt="scenario one"), '{"scenario":1}')
            with self.assertRaises(LlmStepReviewRequired):
                resumed.complete(system="validator", prompt="scenario two")

            self.assertEqual(base.prompts[-1], "scenario two")
            session = json.loads(
                (
                    store.run_dir("run-audit-only-note")
                    / "implementation_plan_execution"
                    / "step_review_session_v1.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(session["calls"][0]["review_note_delivery"], "audit_only")


if __name__ == "__main__":
    unittest.main()
