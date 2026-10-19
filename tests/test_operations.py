"""Kiểm thử độ bền, idempotency và vòng đời operation của Slice 3."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest

from sdlc_harness.application import ApplicationCommand
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.operations import OperationManager, OperationStore, canonical_json_hash, sha256_text


class BlockingService:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.commands: list[ApplicationCommand] = []

    def execute(self, command: ApplicationCommand) -> dict[str, object]:
        self.commands.append(command)
        self.started.set()
        self.release.wait(timeout=5)
        return {"ok": True, "run_id": command.arguments.get("run_id") or "run_created"}


class FailingService:
    def execute(self, command: ApplicationCommand) -> dict[str, object]:
        raise HarnessError("workflow_failed", "Workflow failed.", run_id=str(command.arguments.get("run_id") or ""))


class OperationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.store = OperationStore(runtime_root=self.runtime_root)
        self.service = BlockingService()
        self.manager = OperationManager(
            store=self.store,
            application_service=self.service,  # type: ignore[arg-type]
            queue_limit=1,
        )

    def tearDown(self) -> None:
        self.service.release.set()
        self.manager.shutdown(timeout_seconds=2)
        self.temp_dir.cleanup()

    def submit(
        self,
        *,
        key: str,
        run_id: str = "run_1",
        request: dict[str, object] | None = None,
        answer: str = "private clarification answer",
    ) -> dict[str, object]:
        return self.manager.submit(
            command=ApplicationCommand(
                name="answer-clarification",
                arguments={
                    "run_id": run_id,
                    "question_id": "Q-1",
                    "answer": answer,
                    "answered_by": "owner",
                },
                runtime_root=self.runtime_root,
            ),
            public_command="answer_clarification",
            run_id=run_id,
            requested_by="telegram:owner",
            sync_mode="mock",
            idempotency_key=key,
            canonical_request=request or {"route": "run_action", "run_id": run_id, "answer": answer},
        )

    def wait_status(self, operation_id: str, statuses: set[str]) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = self.store.read_operation(operation_id)
            if str(record.get("status")) in statuses:
                return record
            time.sleep(0.01)
        self.fail(f"Operation {operation_id} did not reach {statuses}")

    def test_submit_persists_before_receipt_without_plaintext_key_or_raw_arguments(self) -> None:
        receipt = self.submit(key="plain-idempotency-key")
        operation_id = str(receipt["operation_id"])
        self.assertTrue(self.store.operation_path(operation_id).is_file())
        persisted = self.store.operation_path(operation_id).read_text(encoding="utf-8")

        self.assertNotIn("plain-idempotency-key", persisted)
        self.assertNotIn("private clarification answer", persisted)
        self.assertIn(sha256_text("plain-idempotency-key"), persisted)
        self.assertFalse(receipt["idempotency_replayed"])

    def test_same_idempotency_key_replays_and_changed_request_conflicts(self) -> None:
        first = self.submit(key="same-key", request={"route": "same", "value": 1})
        replay = self.submit(key="same-key", request={"value": 1, "route": "same"})

        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertTrue(replay["idempotency_replayed"])
        with self.assertRaises(HarnessError) as context:
            self.submit(key="same-key", request={"route": "same", "value": 2})
        self.assertEqual(context.exception.error_code, "idempotency_conflict")

    def test_same_run_is_rejected_and_bounded_queue_reports_full(self) -> None:
        first = self.submit(key="key-1", run_id="run_1")
        self.assertTrue(self.service.started.wait(timeout=2))

        with self.assertRaises(HarnessError) as same_run:
            self.submit(key="key-2", run_id="run_1")
        self.assertEqual(same_run.exception.error_code, "run_operation_in_progress")

        second = self.submit(key="key-3", run_id="run_2")
        with self.assertRaises(HarnessError) as full:
            self.submit(key="key-4", run_id="run_3")
        self.assertEqual(full.exception.error_code, "operation_queue_full")

        self.service.release.set()
        self.wait_status(str(first["operation_id"]), {"succeeded"})
        self.wait_status(str(second["operation_id"]), {"succeeded"})
        self.assertEqual([command.arguments["run_id"] for command in self.service.commands], ["run_1", "run_2"])

    def test_worker_persists_harness_error(self) -> None:
        self.service.release.set()
        self.manager.shutdown(timeout_seconds=2)
        self.manager = OperationManager(
            store=self.store,
            application_service=FailingService(),  # type: ignore[arg-type]
            queue_limit=1,
        )

        receipt = self.submit(key="failing-key", run_id="run_failed")
        record = self.wait_status(str(receipt["operation_id"]), {"failed"})

        self.assertEqual(record["error"]["error_code"], "workflow_failed")
        self.assertIsNone(record["result"])

    def test_shutdown_interrupts_running_and_queued_without_late_overwrite(self) -> None:
        running = self.submit(key="running", run_id="run_1")
        self.assertTrue(self.service.started.wait(timeout=2))
        queued = self.submit(key="queued", run_id="run_2")

        self.manager.shutdown(timeout_seconds=0)
        self.assertEqual(self.store.read_operation(str(running["operation_id"]))["status"], "interrupted")
        self.assertEqual(self.store.read_operation(str(queued["operation_id"]))["status"], "interrupted")

        self.service.release.set()
        self.manager._worker.join(timeout=2)
        self.assertEqual(self.store.read_operation(str(running["operation_id"]))["status"], "interrupted")


class OperationRecoveryTests(unittest.TestCase):
    def test_concurrent_polling_does_not_race_atomic_replace_on_windows(self) -> None:
        with TemporaryDirectory() as directory:
            store = OperationStore(runtime_root=Path(directory) / ".runtime")
            record = store.create_operation(
                command="continue_run",
                run_id="run_concurrent",
                requested_by="owner",
                sync_mode="mock",
                request_hash=canonical_json_hash({"request": "concurrent"}),
                idempotency_key_hash=sha256_text("concurrent-key"),
            )
            errors: list[BaseException] = []

            def write_repeatedly() -> None:
                try:
                    for index in range(200):
                        updated = dict(record)
                        updated["updated_at"] = f"2026-10-18T00:00:{index % 60:02d}Z"
                        store.write_operation(updated)
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            def read_repeatedly() -> None:
                try:
                    for _ in range(400):
                        store.read_operation(str(record["operation_id"]))
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            writer = Thread(target=write_repeatedly)
            reader = Thread(target=read_repeatedly)
            writer.start()
            reader.start()
            writer.join(timeout=5)
            reader.join(timeout=5)

            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])

    def test_startup_rebuilds_indexes_and_interrupts_queued_and_running(self) -> None:
        with TemporaryDirectory() as directory:
            runtime_root = Path(directory) / ".runtime"
            store = OperationStore(runtime_root=runtime_root)
            queued = store.create_operation(
                command="continue_run",
                run_id="run_1",
                requested_by="owner",
                sync_mode="mock",
                request_hash=canonical_json_hash({"request": 1}),
                idempotency_key_hash=sha256_text("queued-key"),
            )
            running = store.create_operation(
                command="continue_run",
                run_id="run_2",
                requested_by="owner",
                sync_mode="mock",
                request_hash=canonical_json_hash({"request": 2}),
                idempotency_key_hash=sha256_text("running-key"),
            )
            running["status"] = "running"
            store.write_operation(running)
            service = BlockingService()

            manager = OperationManager(
                store=store,
                application_service=service,  # type: ignore[arg-type]
                queue_limit=2,
            )
            try:
                for operation in (queued, running):
                    recovered = store.read_operation(str(operation["operation_id"]))
                    self.assertEqual(recovered["status"], "interrupted")
                    self.assertEqual(recovered["error"]["error_code"], "operation_interrupted")
                    self.assertIsNotNone(store.read_idempotency(str(operation["idempotency_key_hash"])))
                self.assertEqual(service.commands, [])
            finally:
                manager.shutdown(timeout_seconds=2)


if __name__ == "__main__":
    unittest.main()
