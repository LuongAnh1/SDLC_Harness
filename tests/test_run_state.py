from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sdlc_harness.core import run_state
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.storage.runtime import RuntimeStore


class RunStateSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = RuntimeStore(repo_root=root, runtime_root=root / ".runtime")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _stored(self, state: dict[str, object]) -> None:
        path = self.store.state_path(str(state["run_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_a_state_written_before_versioning_reads_as_layout_one_and_writes_are_stamped(self) -> None:
        self._stored({"run_id": "run_old", "status": "blocked"})

        state = self.store.read_state("run_old")
        self.assertEqual(state["state_schema_version"], 1)

        self.store.write_state({"run_id": "run_new", "status": "requirement_drafting"})
        on_disk = json.loads(self.store.state_path("run_new").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["state_schema_version"], run_state.STATE_SCHEMA_VERSION)

    def test_a_newer_layout_fails_closed_and_registered_migrations_run_in_order(self) -> None:
        self._stored({"run_id": "run_future", "state_schema_version": run_state.STATE_SCHEMA_VERSION + 1})
        with self.assertRaises(HarnessError) as caught:
            self.store.read_state("run_future")
        self.assertEqual(caught.exception.error_code, "state_schema_unsupported")

        def rename(state: dict[str, object]) -> dict[str, object]:
            old = state.pop("old")
            return {**state, "renamed": old}

        steps = {1: rename, 2: lambda state: {**state, "added": True}}
        self._stored({"run_id": "run_v1", "old": "value"})
        with patch.object(run_state, "STATE_SCHEMA_VERSION", 3), patch.dict(run_state.MIGRATIONS, steps):
            state = self.store.read_state("run_v1")
        self.assertEqual((state["renamed"], state["added"], state["state_schema_version"]), ("value", True, 3))
        self.assertNotIn("old", state)


if __name__ == "__main__":
    unittest.main()
