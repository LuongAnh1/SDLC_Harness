"""H-OC Phase 2 Markdown design flow with a fake OpenCode worker: draft, frame fix, parallel review, revision 1,
scoped re-review in the same sessions, final unreviewed revision 2, JSON conversion, step review and resume."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion
from sdlc_harness.llm.step_review import LlmStepReviewRequired
from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows import design_markdown_flow as flow
from tests.test_design_trace_v1 import DESIGN, FILES, TRACE as BASE_TRACE

RUN_ID = "run_20261126_120000_abcdef12"
TRACE = {
    **BASE_TRACE,
    "acceptance_criteria": [
        {"id": "AC-1", "covers": ["UC-1"], "text": "Lists items.", "api": "200", "web": "a list"},
        {"id": "AC-2", "covers": ["UC-2"], "text": "Hides drafts.", "api": "404", "web": "redirect"},
    ],
}
UNFRAMED = DESIGN.replace("- Uses: U-3\n`/items`", "`/items`")  # U-4 lacks Uses
REVISED = DESIGN.replace("Reads published items.", "Reads published items with a row lock.")
FINAL = REVISED.replace("`GET /api/items` answers 200;", "`GET /api/items` answers 200 sorted by date;")
CONVERTED = {
    "schema_version": "design_trace_v1",
    "units": [
        {"id": "U-1", "kind": "data", "status": "modify", "files": ["db/schema.prisma"], "serves": ["IMP-1"], "decided_by": ["Q-1"], "uses": []},
        {"id": "U-2", "kind": "query", "status": "new", "files": ["db/items.ts"], "serves": ["AC-1", "AC-2"], "decided_by": ["DD-1"], "uses": ["U-1"]},
        {"id": "U-3", "kind": "endpoint", "status": "modify", "files": ["api/items.ts"], "serves": ["AC-1", "AC-2"], "decided_by": ["Q-2"], "uses": ["U-2"]},
        {"id": "U-4", "kind": "screen", "status": "modify", "files": ["web/Items.tsx"], "serves": ["AC-1"], "decided_by": [], "uses": ["U-3"]},
    ],
    "scenario_paths": [{"scenario": "SC-1", "units": ["U-4", "U-3", "U-2"]}, {"scenario": "SC-2", "units": ["U-3", "U-2"]}],
    "endpoint_branches": [],
    "design_decisions": [{"id": "DD-1", "decision": "filter in the query", "reason": "one place"}],
}


class FakeWorker:
    """Stands in for OpenCodeWorker: files live in a temp worktree; each turn writes what the scripted agent would."""

    root: Path
    calls: list[tuple[str, str]] = []
    outside: list[str] = []
    lock = threading.Lock()

    def __init__(self, settings, *, run_id: str, model: str):
        self.model = model
        self.counter = 0

    def stage(self, label: str, suffix: str, text: str) -> str:
        ref = f"docs/sdlc-local/{label}-{sha256(text.encode()).hexdigest()[:16]}{suffix}"
        (self.root / ref).write_text(text, encoding="utf-8")
        return ref

    def local_ref(self, name: str) -> str:
        return f"docs/sdlc-local/{name}"

    def reserve(self, prefix: str, suffix: str) -> str:
        with FakeWorker.lock:
            FakeWorker.counter_all = getattr(FakeWorker, "counter_all", 0) + 1
            return f"docs/sdlc-local/{prefix}-{FakeWorker.counter_all}{suffix}"

    def read(self, ref: str) -> str:
        return (self.root / ref).read_text(encoding="utf-8")

    def write(self, ref: str, text: str) -> None:
        (self.root / ref).write_text(text, encoding="utf-8")

    def remove(self, ref: str) -> None:
        (self.root / ref).unlink(missing_ok=True)

    def worktree(self) -> Path:
        return self.root

    def repo_file_checker(self):
        return lambda path: path in FILES

    def paths_outside_worktree(self, session_id: str) -> list[str]:
        return list(FakeWorker.outside)

    def turn(self, *, task, timeout, role=None, output_ref=None, editable_refs=(), allow_ok=False, exclusive=True):
        design = self.root / "docs/sdlc-local/design.md"
        out = self.root / output_ref
        answer = output_ref
        if "# Design the change" in task:
            kind = "draft"
            design.write_text(UNFRAMED, encoding="utf-8")
        elif "checked the frame of your design" in task:
            kind = "frame"
            design.write_text(DESIGN, encoding="utf-8")
        elif "resolves these findings: F-1" in task:
            kind = "revise-1"
            assert "not reviewed again" not in task
            design.write_text(REVISED, encoding="utf-8")
            out.write_text("- F-1: fixed - U-2 takes a row lock\n", encoding="utf-8")
        elif "resolves these findings: F-2" in task:
            kind = "revise-2"
            assert "not reviewed again" in task
            design.write_text(FINAL, encoding="utf-8")
            out.write_text("- F-2: fixed - U-3 sorts by date\n", encoding="utf-8")
        elif "This turn is group 1" in task:
            kind = "group"
            out.write_text(
                "## Group 1 (SC-1, SC-2)\n| AC | Branches | Verdict | Evidence |\n| --- | --- | --- | --- |\n"
                "| AC-1 (list) | U-4, U-3, U-2 | problem: G1-1 | x |\n| AC-2 | U-3 | ok | x |\n\nFindings:\n1. G1-1 race\n",
                encoding="utf-8",
            )
        elif "This turn checks what no single scenario owns" in task:
            kind = "global"
            out.write_text(
                "# Review — Global\n| UC | Units | Verdict | Evidence |\n| --- | --- | --- | --- |\n"
                "| UC-2 | U-2 | ok | x |\n| UC-1 | U-1 | ok | x |\n\nFindings: none\n",
                encoding="utf-8",
            )
        elif "All groups and the global checks are in" in task:
            kind = "final-1"
            out.write_text("### F-1 race on the list query (minor)\n", encoding="utf-8")
        elif "Your earlier verdicts for the acceptance criteria" in task:
            kind = "regroup"
            assert "- AC-1: ledger cites U-2" in task and "AC-2" not in task.split("Criteria to check again")[1].split("Write")[0]
            out.write_text("## Group 1 round 2\n| AC | B | Verdict | E |\n| --- | --- | --- | --- |\n| AC-1 | U-2 | ok | x |\n\nFindings: none\n",
                           encoding="utf-8")
        elif "Your earlier verdicts for the rows" in task:
            kind = "reglobal"
            assert "- UC-2 (cites U-2)" in task
            out.write_text("## Global round 2\n| Row | Units | Verdict | Evidence |\n| --- | --- | --- | --- |\n| UC-2 | U-2 | ok | x |\n",
                           encoding="utf-8")
        elif "The re-checks of this round are in" in task:
            kind = "final-2"
            out.write_text("## Fix checks\n| Finding | Status | Evidence |\n| --- | --- | --- |\n| F-1 | fixed | lock |\n\n"
                           "## Findings\n### F-2 list order unstated (minor)\n", encoding="utf-8")
        elif "Convert the design to the trace JSON" in task:
            kind = "convert"
            out.write_text("{not json", encoding="utf-8")
        else:
            kind = "json-fix"
            assert "failed these Harness checks" in task
            out.write_text(json.dumps(CONVERTED), encoding="utf-8")
        with FakeWorker.lock:
            FakeWorker.calls.append((kind, role))
        return LlmCompletion(content=answer, finish_reason="stop"), f"ses_{role.replace('-', '')}"


class DesignFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        FakeWorker.root = base / "worktree"
        (FakeWorker.root / "docs/sdlc-local").mkdir(parents=True)
        FakeWorker.calls = []
        FakeWorker.outside = []
        self.store = RuntimeStore(repo_root=base, runtime_root=base / "runtime")
        self.store.run_dir(RUN_ID).mkdir(parents=True)
        self.patch = patch.object(flow, "OpenCodeWorker", FakeWorker)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def run_flow(self, review: dict[str, object]):
        return flow.run_markdown_design(
            settings=None, store=self.store, run_id=RUN_ID, model="deepseek/deepseek-flash#high",
            reviewer_model="deepseek/deepseek-v4-pro#high", requirement_markdown="# Requirements\n", requirement_trace=TRACE,
            component_roots={}, step_review=review,
        )

    def test_the_whole_flow_with_step_review(self) -> None:
        mode = {"enabled": True, "review_input": False, "review_output": True}
        pauses: list[str] = []
        result = None
        for _ in range(60):
            try:
                result = self.run_flow({**mode, **({"call_id": pauses[-1], "decision": "approve"} if pauses else {})})
                break
            except LlmStepReviewRequired as pause:
                pauses.append(pause.progress["call_id"])
        self.assertIsNotNone(result, pauses)
        kinds = [kind for kind, _ in FakeWorker.calls]
        # Every turn ran once: a pending output is re-raised without a new call until it is approved.
        self.assertEqual(sorted(kinds), sorted([
            "draft", "frame", "group", "global", "final-1", "revise-1", "regroup", "reglobal", "final-2", "revise-2",
            "convert", "json-fix",
        ]))
        roles = dict(FakeWorker.calls)
        self.assertEqual((roles["group"], roles["regroup"], roles["global"], roles["reglobal"]),
                         ("reviewer-g1", "reviewer-g1", "reviewer-global", "reviewer-global"))
        self.assertEqual((roles["final-1"], roles["final-2"], roles["revise-2"]), ("reviewer-final", "reviewer-final", "designer"))
        self.assertEqual(len(pauses), len(kinds))
        self.assertEqual(result.open_findings, ["F-2"])
        self.assertEqual(result.unreviewed_revision["revision"], 2)
        self.assertFalse(result.unreviewed_revision["reviewed"])
        self.assertEqual([u["decision_refs"] for u in result.trace["units"]], [["D-1"], [], ["D-2"], []])
        self.assertIn("U3 --> U2", (self.store.repo_root / result.diagram_artifact).read_text(encoding="utf-8"))
        record = json.loads(self.store.artifact_path(RUN_ID, flow.FLOW_RECORD).read_text(encoding="utf-8"))
        self.assertEqual([item["kind"] for item in record["drafts"]], ["draft", "frame_fix"])
        self.assertEqual(record["review"]["final"]["findings"], ["F-1"])
        self.assertEqual(record["rereview"]["scope"]["groups"][0]["recheck_acs"], {"AC-1": "ledger cites U-2"})
        self.assertEqual(record["rereview"]["remaining"], ["F-2"])
        self.assertEqual(len(record["revisions"]), 2)
        self.assertEqual((self.store.repo_root / result.markdown_artifact).read_text(encoding="utf-8"), FINAL)
        # Resuming a finished flow calls nothing again.
        before = len(FakeWorker.calls)
        self.run_flow({"enabled": False})
        self.assertEqual(len(FakeWorker.calls), before)

    def test_a_path_outside_the_worktree_fails_closed(self) -> None:
        FakeWorker.outside = ["D:/other-clone/design.md"]
        with self.assertRaises(HarnessError) as raised:
            self.run_flow({"enabled": False})
        self.assertEqual(raised.exception.error_code, "design_read_outside_worktree")


if __name__ == "__main__":
    unittest.main()
