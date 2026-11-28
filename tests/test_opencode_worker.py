"""OpenCode worker: per-role sessions, parallel (non-exclusive) turns, the external-directory rule and the audit."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from sdlc_harness.llm import opencode
from sdlc_harness.llm.opencode import OpenCodeWorker, outside_paths, session_artifact


class SessionAndConfigTests(unittest.TestCase):
    def test_phase1_roles_keep_their_files_and_new_roles_get_one_each(self) -> None:
        self.assertEqual(session_artifact("refiner"), "requirement_opencode_refiner_session_v1.json")
        self.assertEqual(session_artifact("reviewer"), "requirement_opencode_reviewer_session_v1.json")
        self.assertEqual(session_artifact("reviewer-g3"), "opencode_reviewer_g3_session_v1.json")
        self.assertEqual(session_artifact("designer"), "opencode_designer_session_v1.json")
        with self.assertRaises(ValueError):
            session_artifact("../escape")

    def test_nothing_outside_the_worktree_is_the_last_rule(self) -> None:
        rules = json.loads(opencode._worker_config(("docs/sdlc-local/design.md",)))["agents"][opencode._AGENT]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        self.assertIn({"action": "edit", "resource": "docs/sdlc-local/design.md", "effect": "allow"}, rules)
        self.assertEqual(rules[-1], {"action": "external_directory", "resource": "*", "effect": "deny"})


class OutsidePathTests(unittest.TestCase):
    def test_only_the_last_turn_and_only_paths_outside_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "worktree"
            root.mkdir()
            outside = str(Path(raw) / "other-clone" / "design.md")
            tool = lambda name, given: {"type": "tool", "tool": name, "input": given}  # noqa: E731
            messages = [
                {"type": "user"},
                {"type": "assistant", "content": [tool("read", {"path": outside})]},  # an earlier turn: ignored
                {"type": "user"},
                {"type": "assistant", "content": [
                    tool("read", {"path": "docs/sdlc-local/design.md"}),
                    tool("read", {"path": str(root / "apps" / "x.ts")}),
                    tool("grep", {"pattern": "draft", "path": outside}),
                    tool("glob", {"pattern": str(Path(raw) / "*.md")}),
                    tool("read", {"path": "../sibling/file.ts"}),
                ]},
            ]
            self.assertEqual(
                outside_paths(messages, root),
                [outside, str(Path(raw) / "*.md"), "../sibling/file.ts"],
            )


class ParallelTurnTests(unittest.TestCase):
    def run_turn(self, *, exclusive: bool) -> list[bool]:
        held = threading.Event()
        seen: list[bool] = []
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "docs/sdlc-local").mkdir(parents=True)
            output = "docs/sdlc-local/review.md"

            @contextmanager
            def fake_root(self_):
                held.set()
                try:
                    yield root
                finally:
                    held.clear()

            def fake_run(args, **kwargs):
                seen.append(held.is_set())
                (root / output).write_text("1. finding\n", encoding="utf-8")
                events = [{"type": "step_start", "sessionID": "ses_test"}, {"type": "text", "part": {"text": output}},
                          {"type": "step_finish", "part": {"reason": "stop"}}]
                return subprocess.CompletedProcess(args, 0, "\n".join(map(json.dumps, events)), "")

            worker = object.__new__(OpenCodeWorker)
            worker.run_id, worker.model = "run_x", "deepseek/deepseek-v4-pro#high"
            with (
                patch.object(OpenCodeWorker, "_root", fake_root),
                patch.object(OpenCodeWorker, "_read_session", return_value=None),
                patch.object(OpenCodeWorker, "_remember_session", return_value="ses_test"),
                patch("sdlc_harness.llm.opencode._find_opencode", return_value="fake-opencode"),
                patch("sdlc_harness.llm.opencode.subprocess.run", side_effect=fake_run),
                patch("sdlc_harness.llm.opencode.emit_current"),
            ):
                completion, session = worker.turn(
                    task="review", timeout=10, role="reviewer-g1", output_ref=output, editable_refs=(output,),
                    exclusive=exclusive,
                )
            self.assertEqual((completion.content, session), (output, "ses_test"))
        return seen

    def test_a_reviewer_turn_runs_without_the_workspace_lock(self) -> None:
        self.assertEqual(self.run_turn(exclusive=False), [False])

    def test_an_exclusive_turn_keeps_the_lock_while_opencode_runs(self) -> None:
        self.assertEqual(self.run_turn(exclusive=True), [True])


if __name__ == "__main__":
    unittest.main()
