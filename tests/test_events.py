"""Kiểm thử lưu event, context, instrumentation và bố cục runtime của Slice 4."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sdlc_harness.events import EventStore, bind_current_run, emit_current, event_scope
from sdlc_harness.llm import LlmClient
from sdlc_harness.operations import OperationManager, OperationStore
from sdlc_harness.storage import RUNTIME_README, RuntimeStore
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.verification import VerificationRunner


class EventFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runtime_root = self.root / ".runtime"
        self.run_id = "run_20261018_events"
        self.run_dir = self.runtime_root / "runs" / self.run_id
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "state.json").write_text(
            json.dumps({"run_id": self.run_id, "status": "running"}) + "\n",
            encoding="utf-8",
        )
        self.events = EventStore(runtime_root=self.runtime_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def append(self, event_type: str = "workflow_state_changed", **data: object) -> dict[str, object]:
        return self.events.append(
            run_id=self.run_id,
            category="workflow",
            event_type=event_type,
            phase="design",
            status="running",
            message="Event recorded.",
            data=dict(data),
        )


class EventStoreTests(EventFixture):
    def test_sequence_pagination_restart_and_empty_cursor(self) -> None:
        for index in range(5):
            self.append(
                previous_phase=f"p{index}",
                current_phase=f"p{index + 1}",
                previous_status="running",
                current_status="running",
            )
        page = EventStore(runtime_root=self.runtime_root).read(run_id=self.run_id, after=1, limit=2)
        self.assertEqual([item["sequence"] for item in page["events"]], [2, 3])
        self.assertEqual(page["next_cursor"], 3)
        self.assertTrue(page["has_more"])
        empty = self.events.read(run_id=self.run_id, after=99, limit=10)
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["next_cursor"], 99)

    def test_concurrent_append_has_no_duplicate_or_missing_sequence(self) -> None:
        def append_one(index: int) -> None:
            EventStore(runtime_root=self.runtime_root).append(
                run_id=self.run_id,
                category="workflow",
                event_type="workflow_state_changed",
                phase="design",
                status="running",
                message="Concurrent event.",
                data={
                    "previous_phase": str(index),
                    "current_phase": str(index + 1),
                    "previous_status": "running",
                    "current_status": "running",
                },
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(append_one, range(24)))
        result = self.events.read(run_id=self.run_id, limit=100)
        self.assertEqual([item["sequence"] for item in result["events"]], list(range(1, 25)))

    def test_partial_tail_is_recovered_but_middle_corruption_is_rejected(self) -> None:
        self.append(
            previous_phase="one",
            current_phase="two",
            previous_status="running",
            current_status="running",
        )
        path = self.events.event_path(self.run_id)
        with path.open("ab") as handle:
            handle.write(b'{"partial"')
        second = self.append(
            previous_phase="two",
            current_phase="three",
            previous_status="running",
            current_status="running",
        )
        self.assertEqual(second["sequence"], 2)
        with path.open("ab") as handle:
            handle.write(b"{not-json}\n")
        with self.assertRaises(HarnessError) as context:
            self.append(
                previous_phase="three",
                current_phase="four",
                previous_status="running",
                current_status="running",
            )
        self.assertEqual(context.exception.error_code, "event_log_corrupted")

    def test_allowlist_redacts_paths_and_drops_private_fields(self) -> None:
        self.events.append(
            run_id=self.run_id,
            category="llm",
            event_type="llm_call_completed",
            phase="requirement",
            status="running",
            message="private message secret",
            data={
                "mode": "fake",
                "model": r"C:\private\model",
                "json_mode": True,
                "finish_reason": "stop",
                "duration_seconds": 0.1,
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
                "prompt": "secret prompt",
                "output": "secret output",
                "token": "secret-token",
            },
        )
        encoded = self.events.event_path(self.run_id).read_text(encoding="utf-8")
        self.assertNotIn("private message secret", encoded)
        self.assertNotIn("secret prompt", encoded)
        self.assertNotIn("secret output", encoded)
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("C:\\private", encoded)
        self.assertIn("<redacted>", encoded)

    def test_invalid_run_id_and_query_are_rejected_without_mutation(self) -> None:
        with self.assertRaises(HarnessError):
            self.events.event_path("../outside")
        before = self.events.event_path(self.run_id)
        before_bytes = before.read_bytes() if before.exists() else b""
        for kwargs in ({"after": -1}, {"limit": 0}, {"limit": 201}):
            with self.assertRaises(HarnessError):
                self.events.read(run_id=self.run_id, **kwargs)
        after_bytes = before.read_bytes() if before.exists() else b""
        self.assertEqual(after_bytes, before_bytes)


class RuntimeEventIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runtime_root = self.root / ".runtime"
        self.store = RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_runtime_readme_is_created_updated_and_not_rewritten_when_equal(self) -> None:
        self.store.ensure_layout()
        path = self.runtime_root / "README.md"
        self.assertEqual(path.read_text(encoding="utf-8"), RUNTIME_README)
        first_mtime = path.stat().st_mtime_ns
        time.sleep(0.002)
        self.store.ensure_layout()
        self.assertEqual(path.stat().st_mtime_ns, first_mtime)
        path.write_text("old layout", encoding="utf-8")
        self.store.ensure_layout()
        self.assertEqual(path.read_text(encoding="utf-8"), RUNTIME_README)

    def test_state_writes_emit_workflow_checkpoint_repair_validator_and_qa_events(self) -> None:
        run_id = "run_20261018_state"
        state = {
            "run_id": run_id,
            "source_ref": "jira:TEST-1",
            "current_phase": "implementation",
            "status": "implementation_repair_needed",
            "pending_checkpoint": {"id": "cp-1", "type": "implementation"},
            "implementation_repair_iterations": 0,
            "quality_reports": {},
        }
        self.store.write_state(state)
        updated = {
            **state,
            "status": "qa_tested",
            "pending_checkpoint": None,
            "implementation_repair_iterations": 1,
            "quality_reports": {
                "implementation_validation": {
                    "status": "passed",
                    "blocking_count": 0,
                    "warning_count": 1,
                    "repair_iterations": 1,
                },
                "qa_acceptance": {
                    "status": "pass",
                    "scenarios": [{"result": "pass"}, {"result": "fail"}],
                },
            },
        }
        self.store.write_state(updated)
        event_types = [
            item["type"]
            for item in EventStore(runtime_root=self.runtime_root).read(run_id=run_id, limit=100)["events"]
        ]
        for expected in (
            "workflow_created",
            "checkpoint_opened",
            "workflow_state_changed",
            "checkpoint_approved",
            "repair_iteration_started",
            "repair_iteration_completed",
            "validator_completed",
            "qa_completed",
        ):
            self.assertIn(expected, event_types)

    def test_code_review_approved_is_terminal_for_active_source_lookup(self) -> None:
        run_id = "run_20261018_terminal"
        state = {
            "run_id": run_id,
            "source_ref": "jira:TEST-2",
            "current_phase": "code_review",
            "status": "code_review_approved",
            "pending_checkpoint": None,
        }
        self.store.write_state(state)
        self.store.register_active_run("jira:TEST-2", run_id)
        self.assertIsNone(self.store.find_active_run("jira:TEST-2"))

    def test_create_run_context_flushes_buffer_with_operation_id(self) -> None:
        run_id = "run_20261018_buffer"
        with event_scope(
            runtime_root=self.runtime_root,
            command="start-from-jira",
            operation_id="op_20261018_buffer",
        ):
            emit_current(
                category="operation",
                event_type="operation_queued",
                message="Operation queued.",
                data={"operation_command": "start_from_jira", "sync_mode": "mock"},
            )
            (self.runtime_root / "runs" / run_id).mkdir(parents=True)
            (self.runtime_root / "runs" / run_id / "state.json").write_text(
                json.dumps({"run_id": run_id}) + "\n",
                encoding="utf-8",
            )
            bind_current_run(run_id)
        events = EventStore(runtime_root=self.runtime_root).read(run_id=run_id)["events"]
        self.assertEqual(events[0]["operation_id"], "op_20261018_buffer")

    def test_fake_llm_and_verification_events_exclude_inputs_and_outputs(self) -> None:
        run_id = "run_20261018_boundaries"
        (self.runtime_root / "runs" / run_id).mkdir(parents=True)
        (self.runtime_root / "runs" / run_id / "state.json").write_text(
            json.dumps({"run_id": run_id}) + "\n",
            encoding="utf-8",
        )
        settings = SimpleNamespace(llm_mode="fake", llm_model="fixture-model")
        with event_scope(runtime_root=self.runtime_root, command="test", run_id=run_id):
            LlmClient(settings).complete(system="private system", prompt="private prompt")  # type: ignore[arg-type]
            VerificationRunner(timeout_seconds=5).run(
                target_path=self.root,
                work_packet={"test_intents": [{"command": "echo verification-ok"}]},
            )
        encoded = (self.runtime_root / "runs" / run_id / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("llm_call_started", encoded)
        self.assertIn("llm_call_completed", encoded)
        self.assertIn("verification_completed", encoded)
        self.assertNotIn("private system", encoded)
        self.assertNotIn("private prompt", encoded)
        self.assertNotIn("verification-ok", encoded)


class _StateWritingService:
    def __init__(self, *, root: Path, runtime_root: Path, run_id: str):
        self.root = root
        self.runtime_root = runtime_root
        self.run_id = run_id

    def execute(self, command: object) -> dict[str, object]:
        RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root).write_state(
            {
                "run_id": self.run_id,
                "source_ref": "jira:TEST-3",
                "current_phase": "requirement",
                "status": "waiting_requirement_approval",
                "pending_checkpoint": {"id": "cp-3", "type": "requirement"},
            }
        )
        return {"ok": True, "run_id": self.run_id, "status": "waiting_requirement_approval"}


class _FailingService:
    def execute(self, command: object) -> dict[str, object]:
        raise HarnessError("test_failure", "private failure detail", run_id="run_20261018_failure")


class OperationEventTests(unittest.TestCase):
    def test_create_operation_orders_buffered_events_and_terminal_event(self) -> None:
        from sdlc_harness.application import ApplicationCommand

        with TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / ".runtime"
            run_id = "run_20261018_operation"
            operation_store = OperationStore(runtime_root=runtime_root)
            manager = OperationManager(
                store=operation_store,
                application_service=_StateWritingService(root=root, runtime_root=runtime_root, run_id=run_id),  # type: ignore[arg-type]
                queue_limit=2,
            )
            receipt = manager.submit(
                command=ApplicationCommand(name="start-from-jira", arguments={"issue_key": "TEST-3"}),
                public_command="start_from_jira",
                run_id=None,
                requested_by="test",
                sync_mode="mock",
                idempotency_key="operation-events",
                canonical_request={"source": "TEST-3"},
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                record = operation_store.read_operation(str(receipt["operation_id"]))
                if record["status"] == "succeeded":
                    break
                time.sleep(0.01)
            else:
                self.fail("operation did not finish")
            manager.shutdown(timeout_seconds=1)
            events = EventStore(runtime_root=runtime_root).read(run_id=run_id, limit=100)["events"]
            event_types = [item["type"] for item in events]
            self.assertEqual(event_types[:2], ["operation_queued", "operation_started"])
            self.assertIn("workflow_created", event_types)
            self.assertEqual(event_types[-1], "operation_completed")
            self.assertTrue(all(item["operation_id"] == receipt["operation_id"] for item in events))

    def test_failed_and_interrupted_operations_emit_terminal_events(self) -> None:
        from sdlc_harness.application import ApplicationCommand

        with TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / ".runtime"
            for run_id in ("run_20261018_failure", "run_20261018_interrupted"):
                run_dir = runtime_root / "runs" / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "state.json").write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
            operation_store = OperationStore(runtime_root=runtime_root)
            manager = OperationManager(
                store=operation_store,
                application_service=_FailingService(),  # type: ignore[arg-type]
                queue_limit=2,
            )
            receipt = manager.submit(
                command=ApplicationCommand(
                    name="sync-run",
                    arguments={"run_id": "run_20261018_failure"},
                ),
                public_command="sync_run",
                run_id="run_20261018_failure",
                requested_by="test",
                sync_mode="mock",
                idempotency_key="operation-failure",
                canonical_request={"run_id": "run_20261018_failure"},
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                record = operation_store.read_operation(str(receipt["operation_id"]))
                if record["status"] == "failed":
                    break
                time.sleep(0.01)
            else:
                self.fail("failed operation did not finish")
            interrupted = operation_store.create_operation(
                command="continue_run",
                run_id="run_20261018_interrupted",
                requested_by="test",
                sync_mode="mock",
                request_hash="sha256:" + "1" * 64,
                idempotency_key_hash="sha256:" + "2" * 64,
            )
            manager._interrupt_record(interrupted, message="private interruption detail")
            manager.shutdown(timeout_seconds=1)
            failed_types = [
                item["type"]
                for item in EventStore(runtime_root=runtime_root).read(run_id="run_20261018_failure")["events"]
            ]
            interrupted_events = EventStore(runtime_root=runtime_root).read(run_id="run_20261018_interrupted")["events"]
            self.assertEqual(failed_types, ["operation_queued", "operation_started", "operation_failed"])
            self.assertEqual(interrupted_events[-1]["type"], "operation_interrupted")
            self.assertNotIn("private interruption detail", json.dumps(interrupted_events))

    def test_mock_sync_emits_only_bounded_target_metadata(self) -> None:
        from sdlc_harness.sync.runner import sync_requirement_checkpoint

        with TemporaryDirectory() as temp:
            runtime_root = Path(temp) / ".runtime"
            run_id = "run_20261018_sync"
            run_dir = runtime_root / "runs" / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "state.json").write_text(json.dumps({"run_id": run_id}) + "\n", encoding="utf-8")
            settings = SimpleNamespace(sync_mode="mock")
            with event_scope(runtime_root=runtime_root, command="sync-run", run_id=run_id):
                with patch(
                    "sdlc_harness.sync.runner._sync_requirement_checkpoint",
                    return_value={
                        "status": "success",
                        "jira": {"status": "success", "url": "https://private.example"},
                        "confluence": {"status": "skipped", "token": "secret"},
                    },
                ):
                    sync_requirement_checkpoint(
                        store=object(),
                        settings=settings,
                        run_id=run_id,
                        source_ref="jira:TEST-4",
                        artifact_paths={},
                    )
            events = EventStore(runtime_root=runtime_root).read(run_id=run_id)["events"]
            sync_event = events[-1]
            self.assertEqual(sync_event["type"], "sync_completed")
            self.assertEqual(sync_event["data"]["targets"], {"jira": "success", "confluence": "skipped"})
            encoded = json.dumps(sync_event)
            self.assertNotIn("private.example", encoded)
            self.assertNotIn("secret", encoded)


if __name__ == "__main__":
    unittest.main()
