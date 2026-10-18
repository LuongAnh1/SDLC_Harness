"""Single-worker durable operation queue for the HTTP write API."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from queue import Empty, Full, Queue
from threading import Lock, Thread
from typing import Any, Callable

from ..application import ApplicationCommand, ApplicationService
from ..core.errors import HarnessError
from .store import OperationStore, utc_now_iso


_LOGGER = logging.getLogger("sdlc_harness.operations")
_STOP = object()


@dataclass(frozen=True)
class OperationWorkItem:
    operation_id: str
    command: ApplicationCommand


class OperationManager:
    """Persist before acknowledgement, then execute commands with one worker."""

    def __init__(
        self,
        *,
        store: OperationStore,
        application_service: ApplicationService,
        queue_limit: int,
        public_value: Callable[[Any], Any] | None = None,
    ):
        if queue_limit < 1:
            raise HarnessError("api_config_invalid", "API queue limit must be at least one.")
        self.store = store
        self.application_service = application_service
        self.queue_limit = queue_limit
        self.public_value = public_value or (lambda value: value)
        self._queue: Queue[OperationWorkItem | object] = Queue(maxsize=queue_limit)
        self._lock = Lock()
        self._accepting = True
        self._stopped = False
        self._running_operation_id: str | None = None
        self._active_by_run: dict[str, str] = {}
        self.store.ensure_layout()
        self.store.rebuild_idempotency_indexes()
        self.store.interrupt_incomplete(
            message="Operation was interrupted by an API process restart and was not replayed."
        )
        self._worker = Thread(target=self._worker_loop, name="sdlc-operation-worker", daemon=True)
        self._worker.start()

    def submit(
        self,
        *,
        command: ApplicationCommand,
        public_command: str,
        run_id: str | None,
        requested_by: str,
        sync_mode: str,
        idempotency_key: str,
        canonical_request: dict[str, object],
    ) -> dict[str, object]:
        key = idempotency_key.strip()
        if not key:
            raise HarnessError("idempotency_key_required", "Idempotency-Key header is required.")
        if len(key) > 256:
            raise HarnessError("idempotency_key_invalid", "Idempotency-Key must not exceed 256 characters.")
        key_hash = sha256_text(key)
        request_hash = canonical_json_hash(canonical_request)
        with self._lock:
            existing_index = self.store.read_idempotency(key_hash)
            if existing_index is not None:
                if existing_index.get("request_hash") != request_hash:
                    raise HarnessError(
                        "idempotency_conflict",
                        "Idempotency-Key was already used with a different request.",
                        run_id=run_id,
                    )
                existing = self.store.read_operation(str(existing_index.get("operation_id") or ""))
                return self._receipt(existing, replayed=True)
            if not self._accepting:
                raise HarnessError("operation_manager_stopping", "Operation manager is shutting down.")
            if run_id and run_id in self._active_by_run:
                raise HarnessError(
                    "run_operation_in_progress",
                    f"Run already has a nonterminal operation: {run_id}",
                    run_id=run_id,
                )
            if self._queue.full():
                raise HarnessError("operation_queue_full", "Operation queue is full.")
            record = self.store.create_operation(
                command=public_command,
                run_id=run_id,
                requested_by=requested_by,
                sync_mode=sync_mode,
                request_hash=request_hash,
                idempotency_key_hash=key_hash,
            )
            operation_id = str(record["operation_id"])
            try:
                self.store.write_idempotency(
                    key_hash=key_hash,
                    request_hash=request_hash,
                    operation_id=operation_id,
                    created_at=str(record["created_at"]),
                )
            except HarnessError:
                self._interrupt_record(
                    record,
                    message="Operation could not be enqueued after persistence.",
                )
                raise
            try:
                self._queue.put_nowait(OperationWorkItem(operation_id=operation_id, command=command))
            except Full as exc:
                self._interrupt_record(
                    record,
                    message="Operation queue became full after persistence.",
                )
                raise HarnessError("operation_queue_full", "Operation queue is full.") from exc
            if run_id:
                self._active_by_run[run_id] = operation_id
            return self._receipt(record, replayed=False)

    def get_public_operation(self, operation_id: str) -> dict[str, object]:
        return self._public_operation(self.store.read_operation(operation_id), include_terminal=True)

    def list_public_summaries(self, *, limit: int = 20) -> list[dict[str, object]]:
        return [
            self._public_operation(record, include_terminal=False)
            for record in self.store.list_operations(limit=limit)
        ]

    def queue_status(self) -> dict[str, object]:
        with self._lock:
            return {
                "available": self._accepting,
                "status": "ready" if self._accepting else "stopping",
                "queued": self._queue.qsize(),
                "running": 1 if self._running_operation_id else 0,
                "capacity": self.queue_limit,
                "workers": 1,
            }

    def shutdown(self, *, timeout_seconds: int) -> None:
        with self._lock:
            if self._stopped:
                return
            self._accepting = False
            while True:
                try:
                    item = self._queue.get_nowait()
                except Empty:
                    break
                if isinstance(item, OperationWorkItem):
                    record = self.store.read_operation(item.operation_id)
                    self._interrupt_record(
                        record,
                        message="Queued operation was interrupted during API shutdown.",
                    )
                    self._release_run(record)
                self._queue.task_done()
            self._queue.put_nowait(_STOP)
        self._worker.join(timeout=max(0, timeout_seconds))
        with self._lock:
            if self._worker.is_alive() and self._running_operation_id:
                record = self.store.read_operation(self._running_operation_id)
                if record.get("status") == "running":
                    self._interrupt_record(
                        record,
                        message="Running operation exceeded the API shutdown timeout.",
                    )
                    self._release_run(record)
            self._stopped = True

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                assert isinstance(item, OperationWorkItem)
                self._execute(item)
            finally:
                self._queue.task_done()

    def _execute(self, item: OperationWorkItem) -> None:
        with self._lock:
            record = self.store.read_operation(item.operation_id)
            if record.get("status") != "queued":
                self._release_run(record)
                return
            now = utc_now_iso()
            record.update({"status": "running", "started_at": now, "updated_at": now})
            self.store.write_operation(record)
            self._running_operation_id = item.operation_id
        try:
            result = self.application_service.execute(item.command)
        except HarnessError as exc:
            self._finish(item.operation_id, status="failed", result=None, error=exc.to_response())
        except Exception:
            _LOGGER.exception("Unhandled operation execution error: %s", item.operation_id)
            self._finish(
                item.operation_id,
                status="failed",
                result=None,
                error={"ok": False, "error_code": "internal_error", "message": "Internal server error."},
            )
        else:
            if isinstance(result, dict):
                self._finish(item.operation_id, status="succeeded", result=result, error=None)
            else:
                self._finish(
                    item.operation_id,
                    status="failed",
                    result=None,
                    error={
                        "ok": False,
                        "error_code": "invalid_application_result",
                        "message": "Application service returned a non-object result.",
                    },
                )

    def _finish(
        self,
        operation_id: str,
        *,
        status: str,
        result: dict[str, object] | None,
        error: dict[str, object] | None,
    ) -> None:
        with self._lock:
            record = self.store.read_operation(operation_id)
            if record.get("status") != "running":
                self._running_operation_id = None
                self._release_run(record)
                return
            now = utc_now_iso()
            result_run_id = str((result or {}).get("run_id") or "").strip()
            if result_run_id:
                record["run_id"] = result_run_id
            record.update(
                {
                    "status": status,
                    "updated_at": now,
                    "finished_at": now,
                    "result": result,
                    "error": error,
                }
            )
            self.store.write_operation(record)
            self._running_operation_id = None
            self._release_run(record)

    def _interrupt_record(self, record: dict[str, object], *, message: str) -> None:
        now = utc_now_iso()
        record.update(
            {
                "status": "interrupted",
                "updated_at": now,
                "finished_at": now,
                "result": None,
                "error": {
                    "ok": False,
                    "error_code": "operation_interrupted",
                    "message": message,
                },
            }
        )
        self.store.write_operation(record)

    def _release_run(self, record: dict[str, object]) -> None:
        run_id = str(record.get("run_id") or "")
        operation_id = str(record.get("operation_id") or "")
        if run_id and self._active_by_run.get(run_id) == operation_id:
            self._active_by_run.pop(run_id, None)

    def _receipt(self, record: dict[str, object], *, replayed: bool) -> dict[str, object]:
        operation_id = str(record.get("operation_id") or "")
        return {
            "ok": True,
            "operation_id": operation_id,
            "operation_status": record.get("status"),
            "run_id": record.get("run_id"),
            "status_url": f"/api/v1/operations/{operation_id}",
            "idempotency_replayed": replayed,
        }

    def _public_operation(
        self,
        record: dict[str, object],
        *,
        include_terminal: bool,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": True,
            "operation_id": record.get("operation_id"),
            "operation_status": record.get("status"),
            "command": record.get("command"),
            "run_id": record.get("run_id"),
            "requested_by": record.get("requested_by"),
            "sync_mode": record.get("sync_mode"),
            "created_at": record.get("created_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
        }
        if include_terminal:
            payload["result"] = self.public_value(record.get("result"))
            payload["error"] = self.public_value(record.get("error"))
        else:
            error = record.get("error")
            payload["error_code"] = error.get("error_code") if isinstance(error, dict) else None
        return self.public_value(payload)


def canonical_json_hash(value: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HarnessError("invalid_request", "Request cannot be canonicalized.") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
