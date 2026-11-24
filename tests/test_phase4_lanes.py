"""D2 pilot: a Phase 4 lane object runs against a small host, without building the whole workflow."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from sdlc_harness.storage.runtime import RuntimeStore
from sdlc_harness.workflows.phase4_final_verification import FinalVerificationLane


class _Host:
    """Only what this path of the lane may touch; anything else fails the test."""

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        return {**state, "created_new_run": created_new_run}

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the lane used an undeclared host member: {name}")


class FinalVerificationLaneTest(unittest.TestCase):
    def test_blocks_before_any_check_when_documentation_is_not_approved(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = RuntimeStore(repo_root=Path(root), runtime_root=Path(root) / ".runtime")
            state = {"run_id": "run_lane", "status": "documentation_approved", "documentation_closure": {"status": "waiting_review"}}
            store.write_state(state)

            response = FinalVerificationLane(_Host(store))._continue_final_verification(state=state, run_id="run_lane")

            self.assertEqual(response["status"], "blocked")
            self.assertEqual(response["last_error"]["error_code"], "final_verification_documentation_not_approved")
            self.assertEqual(response["allowed_next_actions"], ["get_status", "cancel_run"])
            self.assertEqual(store.read_state("run_lane")["final_verification"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
