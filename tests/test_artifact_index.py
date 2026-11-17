"""G1.3: `RuntimeStore.write_state` indexes every changed artifact pointer with its node and inputs."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from sdlc_harness.storage.artifact_index import (
    artifact_scope,
    artifacts_at,
    index_path,
    last_visit,
    read_artifact_index,
    record_read,
    sha256_file,
)
from sdlc_harness.storage.runtime import RuntimeStore

RUN_ID = "run_20261117_000000_abcdef12"


class ArtifactIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = RuntimeStore(repo_root=self.root, runtime_root=self.root / ".runtime")
        self.store.ensure_layout()

    def _artifact(self, name: str, content: str) -> str:
        path = self.store.artifact_path(RUN_ID, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.store.relative(path)

    def _write(self, status: str, latest: dict[str, str]) -> None:
        self.store.write_state({"run_id": RUN_ID, "status": status, "latest_artifacts": dict(latest)})

    def _entries(self) -> list[dict[str, object]]:
        return read_artifact_index(self.store.run_dir(RUN_ID))

    def test_a_write_outside_a_scope_is_indexed_with_untracked_inputs(self) -> None:
        plan = self._artifact("plan_v1.json", "{}")
        self._write("planning", {})
        self._write("planned", {"plan": plan})

        [entry] = self._entries()
        self.assertEqual((entry["key"], entry["ref"], entry["version"]), ("plan", plan, 1))
        self.assertEqual(entry["sha256"], sha256_file(self.root / plan))
        self.assertEqual(entry["transition"], {"from": "planning", "to": "planned"})
        self.assertIsNone(entry["node"])
        self.assertIsNone(entry["inputs"])
        self.assertNotIn("worktree_fingerprint", entry)

    def test_a_scope_records_node_reads_and_fingerprint_and_skips_unchanged_pointers(self) -> None:
        plan = self._artifact("plan_v1.json", '{"a": 1}')
        self._write("planned", {"plan": plan})
        report = self._artifact("report_v2.json", "{}")
        with artifact_scope(
            RUN_ID, node="planned", handler="_verify", repo_root=self.root, fingerprint=lambda: {"sha256": "sha256:t"}
        ):
            record_read(self.root / plan)
            record_read(self.root / report)  # a node re-reading its own output is not its input
            record_read(self.root / "missing.json")
            self._write("verified", {"plan": plan, "report": report})

        entry = self._entries()[-1]
        self.assertEqual(len(self._entries()), 2)  # `plan` kept its ref: no second line
        self.assertEqual((entry["key"], entry["node"], entry["handler"]), ("report", "planned", "_verify"))
        self.assertEqual(entry["inputs"], [{"key": "plan", "ref": plan, "sha256": sha256_file(self.root / plan)}])
        self.assertEqual(entry["worktree_fingerprint"], {"sha256": "sha256:t"})

    def test_a_scope_for_another_run_is_ignored(self) -> None:
        plan = self._artifact("plan_v1.json", "{}")
        with artifact_scope("run_other", node="x", handler="_x", repo_root=self.root):
            self._write("planned", {"plan": plan})

        self.assertIsNone(self._entries()[0]["inputs"])

    def test_replaying_the_index_rebuilds_pointers_and_finds_the_last_visit_of_a_node(self) -> None:
        plan, report_1, report_2 = (self._artifact(f"{name}.json", "{}") for name in ("plan_v1", "report_v1", "report_v2"))
        for node, latest in (
            ("planning", {"plan": plan}),
            ("verifying", {"plan": plan, "report": report_1}),
            ("reviewing", {"plan": plan}),  # the report pointer is dropped
            ("verifying", {"plan": plan, "report": report_2}),
        ):
            with artifact_scope(RUN_ID, node=node, handler="_h", repo_root=self.root):
                self._write(node, latest)
        entries = self._entries()

        self.assertEqual((entries[2]["key"], entries[2]["ref"], entries[2]["previous_ref"]), ("report", None, report_1))
        self.assertEqual(last_visit(entries, "verifying"), 3)
        self.assertEqual(last_visit(entries, "verifying", excluded=[(3, 4)]), 1)  # line 3 is an abandoned branch
        self.assertIsNone(last_visit(entries, "reviewing", excluded=[(2, 4)]))
        self.assertIsNone(last_visit(entries, "publishing"))
        self.assertEqual(artifacts_at(entries, 2), {"plan": plan, "report": report_1})
        self.assertEqual(artifacts_at(entries, 3), {"plan": plan})

    def test_a_torn_last_line_is_skipped_and_dropped_before_the_next_append(self) -> None:
        self._write("planned", {"plan": self._artifact("plan_v1.json", "{}")})
        with index_path(self.store.run_dir(RUN_ID)).open("a", encoding="utf-8") as handle:
            handle.write('{"key": "torn')
        self.assertEqual(len(self._entries()), 1)

        self._write("verified", {"plan": self._artifact("plan_v2.json", "{}")})

        self.assertEqual([entry["version"] for entry in self._entries()], [1, 2])


if __name__ == "__main__":
    unittest.main()
