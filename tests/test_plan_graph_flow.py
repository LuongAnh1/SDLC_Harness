"""H-OC Phase 3 plan graph flow with a fake OpenCode worker, and its wiring into Phase 3 approval (no LLM, no git)."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.llm.step_review import LlmStepReviewRequired
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.repo_context.plan_facts import RepoFacts
from sdlc_harness.workflows import plan_graph_flow as flow
from sdlc_harness.workflows.approval import CheckpointApprovalWorkflow
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow
from sdlc_harness.workspaces.manager import RunWorkspaceManager

FIXTURES = Path(__file__).parent / "fixtures" / "hoc_plan_r4"
RUN_ID = "run_20261127_120000_abcdef12"
TRACE = json.loads((FIXTURES / "design_units.json").read_text(encoding="utf-8"))
FACTS = RepoFacts.from_dict(json.loads((FIXTURES / "repo_facts.json").read_text(encoding="utf-8")))
GOOD = (FIXTURES / "planner_output.md").read_text(encoding="utf-8")
MISSING_ONE = GOOD.replace("| U-25 -> U-26 | drop | U-25 promises 200 `{ tags: string[] }` computed from U-15 (DD-1); "
                           "read-only, no error response. |\n", "")


class FakeWorker:
    """Stands in for OpenCodeWorker: the first answer misses a candidate, the format fix completes it."""

    root: Path
    answers: list[str] = []
    calls: list[tuple[str, str]] = []
    outside: list[str] = []

    def __init__(self, settings, *, run_id: str, model: str):
        self.model = model

    def stage(self, label: str, suffix: str, text: str) -> str:
        ref = f"docs/sdlc-local/{label}-{sha256(text.encode()).hexdigest()[:16]}{suffix}"
        (self.root / ref).write_text(text, encoding="utf-8")
        return ref

    def local_ref(self, name: str) -> str:
        return f"docs/sdlc-local/{name}"

    def read(self, ref: str) -> str:
        return (self.root / ref).read_text(encoding="utf-8")

    def remove(self, ref: str) -> None:
        (self.root / ref).unlink(missing_ok=True)

    def worktree(self) -> Path:
        return self.root

    def paths_outside_worktree(self, session_id: str) -> list[str]:
        return list(FakeWorker.outside)

    def turn(self, *, task, timeout, role=None, output_ref=None, editable_refs=(), allow_ok=False, exclusive=True):
        kind = "fix" if "checked your answer" in task else "first"
        if kind == "fix":
            assert "candidate U-25 -> U-26 is not answered" in task
        else:
            assert "- Option 0: base U-1, U-3, U-4, U-26" in task
        (self.root / output_ref).write_text(FakeWorker.answers.pop(0), encoding="utf-8")
        FakeWorker.calls.append((kind, role))
        return LlmCompletion(content=output_ref, finish_reason="stop"), "ses_planner"


class FlowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        FakeWorker.root = base / "worktree"
        (FakeWorker.root / "docs/sdlc-local").mkdir(parents=True)
        FakeWorker.answers = [MISSING_ONE, GOOD]
        FakeWorker.calls = []
        FakeWorker.outside = []
        self.base = base
        self.store = RuntimeStore(repo_root=base, runtime_root=base / "runtime")
        self.store.run_dir(RUN_ID).mkdir(parents=True)
        self.patches = [
            patch.object(flow, "OpenCodeWorker", FakeWorker),
            patch.object(flow, "repo_facts_for_profile", return_value=FACTS),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()


class PlanFlowTests(FlowCase):
    def run_flow(self, review: dict[str, object]):
        return flow.run_plan_graph(
            settings=None, store=self.store, run_id=RUN_ID, model="deepseek/deepseek-flash#high", design_trace=TRACE,
            design_markdown="# Design\n", project_profile_id="typescript_fullstack_v1", k=3, step_review=review,
        )

    def test_planner_turn_format_fix_and_final_plan_with_step_review(self) -> None:
        mode = {"enabled": True, "review_input": False, "review_output": True}
        pauses: list[str] = []
        result = None
        for _ in range(10):
            try:
                result = self.run_flow({**mode, **({"call_id": pauses[-1], "decision": "approve"} if pauses else {})})
                break
            except LlmStepReviewRequired as pause:
                pauses.append(pause.progress["call_id"])
        self.assertIsNotNone(result, pauses)
        self.assertEqual(FakeWorker.calls, [("first", "planner"), ("fix", "planner")])  # each turn ran once
        self.assertEqual(len(pauses), 2)
        plan = result.plan
        self.assertEqual(plan["base"]["units"], ["U-1", "U-3", "U-4", "U-26"])
        self.assertEqual(len(plan["groups"]), 8)
        self.assertEqual((plan["planner"]["kept"], plan["planner"]["dropped"], plan["planner"]["attempts"]), (4, 17, 2))
        self.assertEqual(plan["checks"]["issues"], [])
        for key in ("plan_graph", "plan_graph_report", "plan_task_graph", "plan_repo_facts", "plan_graph_candidates"):
            self.assertTrue((self.base / result.artifacts[key]).is_file(), key)
        report = (self.base / result.artifacts["plan_graph_report"]).read_text(encoding="utf-8")
        self.assertIn("## Planner", report)
        # A second run replays the record: no new turn, same artifacts.
        again = self.run_flow({"enabled": False})
        self.assertEqual(len(FakeWorker.calls), 2)
        self.assertEqual(again.artifacts, result.artifacts)

    def test_answer_still_invalid_after_two_fixes_fails_closed(self) -> None:
        FakeWorker.answers = [MISSING_ONE, MISSING_ONE, MISSING_ONE]
        with self.assertRaises(HarnessError) as error:
            self.run_flow({"enabled": False})
        self.assertEqual(error.exception.error_code, "plan_planner_invalid")
        self.assertEqual(len(FakeWorker.calls), 3)

    def test_paths_outside_the_worktree_fail_closed(self) -> None:
        FakeWorker.outside = ["C:/elsewhere/secret.md"]
        with self.assertRaises(HarnessError) as error:
            self.run_flow({"enabled": False})
        self.assertEqual(error.exception.error_code, "plan_read_outside_worktree")

    def test_a_mechanical_check_failure_stops_before_any_llm_call(self) -> None:
        trace = {"units": [*TRACE["units"], {"id": "U-36", "files": ["package.json"], "uses": []}]}
        with self.assertRaises(HarnessError) as error:
            flow.run_plan_graph(
                settings=None, store=self.store, run_id=RUN_ID, model="m/x", design_trace=trace,
                design_markdown="# Design\n", project_profile_id="typescript_fullstack_v1", k=3,
                step_review={"enabled": False},
            )
        self.assertEqual(error.exception.error_code, "plan_graph_invalid")
        self.assertEqual(FakeWorker.calls, [])


class PlanWiringTests(FlowCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings = load_settings(root=self.base, runtime_root=self.base / "runtime",
                                      overrides={"SDLC_LLM_MODE": "fake", "SDLC_HOC_PARALLEL_AGENTS": "3"})
        artifacts = self.store.run_dir(RUN_ID) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "design_trace_v1_r1.json").write_text(json.dumps(TRACE), encoding="utf-8")
        (artifacts / "design_markdown_rev2_a1.md").write_text("# Design\n", encoding="utf-8")
        self.store.write_state(
            {
                "run_id": RUN_ID,
                "status": "waiting_design_approval",
                "current_phase": "design_review",
                "design_opencode_flow": "design_markdown_v1",
                "design_opencode_model": "deepseek/deepseek-flash#high",
                "profile_snapshot": {"project_profile_id": "typescript_fullstack_v1"},
                "approved_artifacts": {"requirement_trace": "x.json"},
                "latest_artifacts": {
                    "design_trace": f"runtime/runs/{RUN_ID}/artifacts/design_trace_v1_r1.json",
                    "design_markdown": f"runtime/runs/{RUN_ID}/artifacts/design_markdown_rev2_a1.md",
                },
                "pending_checkpoint": {"checkpoint_id": f"checkpoint_design_{RUN_ID}", "type": "design_approval"},
            }
        )
        self.workspace = patch.object(RunWorkspaceManager, "create_for_run", return_value=None)
        self.workspace.start()

    def tearDown(self) -> None:
        self.workspace.stop()
        super().tearDown()

    def test_design_approval_starts_the_plan_and_opens_a_plan_checkpoint_that_cannot_be_approved_yet(self) -> None:
        response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=RUN_ID)
        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        state = self.store.read_state(RUN_ID)
        self.assertEqual(state["plan_opencode_flow"], "plan_graph_v1")
        self.assertEqual(state["plan_parallel_agents"], 3)
        self.assertEqual(state["approved_artifacts"]["requirement_trace"], "x.json")
        self.assertTrue(state["approved_artifacts"]["design_trace"].endswith("design_trace_v1_r1.json"))
        checkpoint = state["pending_checkpoint"]
        self.assertEqual((checkpoint["type"], checkpoint["k"]), ("implementation_plan_review", 3))
        self.assertEqual(checkpoint["counts"]["groups"], 8)
        self.assertTrue(state["latest_artifacts"]["plan_graph"].endswith("plan_graph_v1.json"))
        with self.assertRaises(HarnessError) as error:
            CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=RUN_ID)
        self.assertEqual(error.exception.error_code, "plan_hoc_execution_unavailable")

    def test_step_review_pauses_and_continue_resumes_the_plan(self) -> None:
        review = {"enabled": True, "review_input": False, "review_output": True}
        response = PhaseThreeWorkflow(self.settings, plan_step_review=review).approve_design_checkpoint(run_id=RUN_ID)
        self.assertEqual(response["status"], "implementation_plan_step_review_required")
        for _ in range(4):
            call_id = self.store.read_state(RUN_ID)["pending_step_review"]["call_id"]
            response = PhaseThreeWorkflow(
                self.settings, plan_step_review={"call_id": call_id, "decision": "approve"}
            ).continue_implementation_plan_step_review(run_id=RUN_ID)
            if response["status"] != "implementation_plan_step_review_required":
                break
        self.assertEqual(response["status"], "waiting_implementation_plan_review")
        self.assertEqual(FakeWorker.calls, [("first", "planner"), ("fix", "planner")])

    def test_a_transport_failure_stays_resumable(self) -> None:
        with patch.object(FakeWorker, "turn", side_effect=HarnessError("llm_transport_failed", "down")):
            response = CheckpointApprovalWorkflow(self.settings).approve_checkpoint(run_id=RUN_ID)
        self.assertEqual(response["status"], "implementation_planning")
        self.assertIn("continue_run", response["allowed_next_actions"])
        response = PhaseThreeWorkflow(self.settings).continue_implementation_plan_step_review(run_id=RUN_ID)
        self.assertEqual(response["status"], "waiting_implementation_plan_review")


if __name__ == "__main__":
    unittest.main()
