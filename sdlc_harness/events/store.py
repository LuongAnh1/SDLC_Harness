"""Kho event chỉ ghi nối, giới hạn dữ liệu riêng tư cho một workflow run."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import RLock
import time
from typing import Any
from uuid import uuid4

from ..core.errors import HarnessError
from ..storage.locks import RuntimeActionLock
from ..storage.runtime_readme import ensure_runtime_readme


DEFAULT_EVENT_LIMIT = 100
MAX_EVENT_LIMIT = 200
MAX_EVENT_DATA_BYTES = 4096
_SAFE_RUN_ID = re.compile(r"^run_[A-Za-z0-9_-]+$")
_LOCAL_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|/(?:home|users|tmp|var|opt)/)")
_CREDENTIAL_URL = re.compile(r"(?i)https?://[^/\s:@]+:[^/\s@]+@")
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt", "output", "content", "answer", "stdout", "stderr", "command",
        "cwd", "path", "url", "token", "secret", "password", "authorization",
        "exception", "message", "raw_response", "api_key", "api_token",
    }
)
_EVENT_MESSAGES = {
    "operation_queued": "Operation queued.",
    "operation_started": "Operation started.",
    "operation_completed": "Operation completed.",
    "operation_failed": "Operation failed.",
    "operation_interrupted": "Operation interrupted.",
    "workflow_created": "Workflow run created.",
    "workflow_state_changed": "Workflow state changed.",
    "checkpoint_opened": "Checkpoint opened.",
    "checkpoint_approved": "Checkpoint approved.",
    "llm_call_started": "LLM call started.",
    "llm_call_completed": "LLM call completed.",
    "llm_call_failed": "LLM call failed.",
    "repair_iteration_started": "Repair iteration started.",
    "repair_iteration_completed": "Repair iteration completed.",
    "verification_completed": "Verification completed.",
    "validator_completed": "Validator completed.",
    "qa_completed": "QA completed.",
    "sync_completed": "Synchronization completed.",
}
_DATA_ALLOWLIST: dict[str, frozenset[str]] = {
    "operation_queued": frozenset({"operation_command", "sync_mode"}),
    "operation_started": frozenset({"operation_command", "sync_mode"}),
    "operation_completed": frozenset({"operation_command", "sync_mode", "duration_seconds"}),
    "operation_failed": frozenset({"operation_command", "sync_mode", "duration_seconds", "error_code"}),
    "operation_interrupted": frozenset({"operation_command", "sync_mode", "error_code"}),
    "workflow_created": frozenset({"current_phase", "current_status"}),
    "workflow_state_changed": frozenset({"previous_phase", "current_phase", "previous_status", "current_status"}),
    "checkpoint_opened": frozenset({"checkpoint_id", "checkpoint_type"}),
    "checkpoint_approved": frozenset({"checkpoint_id", "checkpoint_type"}),
    "llm_call_started": frozenset({"mode", "model", "json_mode"}),
    "llm_call_completed": frozenset({"mode", "model", "json_mode", "finish_reason", "duration_seconds", "prompt_tokens", "completion_tokens", "total_tokens"}),
    "llm_call_failed": frozenset({"mode", "model", "json_mode", "duration_seconds", "error_code"}),
    "repair_iteration_started": frozenset({"repair_phase", "iteration", "limit"}),
    "repair_iteration_completed": frozenset({"repair_phase", "iteration", "result"}),
    "verification_completed": frozenset({"status", "exit_code", "timed_out", "duration_seconds", "discovered_tests", "executed_tests"}),
    "validator_completed": frozenset({"validator", "status", "blockers", "warnings", "repair_iterations"}),
    "qa_completed": frozenset({"status", "scenario_count", "passed_count", "failed_count", "repair_iterations"}),
    "sync_completed": frozenset({"sync_phase", "mode", "status", "targets"}),
}


class EventStore:
    """Store and page public event records under a run directory."""

    def __init__(self, *, runtime_root: Path):
        self.runtime_root = runtime_root.resolve()
        self.runs_dir = self.runtime_root / "runs"
        self._io_lock = RLock()

    def ensure_layout(self) -> None:
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            ensure_runtime_readme(self.runtime_root)
        except OSError as exc:
            raise HarnessError("event_store_failed", "Event storage is unavailable.") from exc

    def event_path(self, run_id: str) -> Path:
        run_id = _validated_run_id(run_id)
        return self.runs_dir / run_id / "events.jsonl"

    def append(
        self,
        *,
        run_id: str,
        category: str,
        event_type: str,
        phase: str | None,
        status: str | None,
        message: str,
        data: dict[str, object] | None = None,
        operation_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, object]:
        path = self.event_path(run_id)
        clean_data = _sanitize_data(event_type, data or {})
        self.ensure_layout()
        with self._io_lock:
            with self._event_lock(run_id):
                existing = self._read_complete_records(path, recover_tail=True)
                sequence = len(existing) + 1
                record = {
                    "version": "event_v1",
                    "event_id": f"evt_{uuid4().hex}",
                    "sequence": sequence,
                    "timestamp": timestamp or utc_now_iso(),
                    "run_id": run_id,
                    "operation_id": operation_id,
                    "category": _bounded_text(category, 64),
                    "type": _bounded_text(event_type, 96),
                    "phase": _optional_text(phase, 96),
                    "status": _optional_text(status, 96),
                    "message": _EVENT_MESSAGES.get(event_type, "Event recorded."),
                    "data": clean_data,
                }
                encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("ab") as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as exc:
                    raise HarnessError("event_store_failed", "Event could not be persisted.", run_id=run_id) from exc
                return record

    def read(self, *, run_id: str, after: int = 0, limit: int = DEFAULT_EVENT_LIMIT) -> dict[str, object]:
        run_id = _validated_run_id(run_id)
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise HarnessError("invalid_query", "after must be an integer >= 0.", run_id=run_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_EVENT_LIMIT:
            raise HarnessError("invalid_query", f"limit must be an integer from 1 to {MAX_EVENT_LIMIT}.", run_id=run_id)
        run_dir = self.runs_dir / run_id
        if not (run_dir / "state.json").is_file():
            raise HarnessError("run_not_found", f"Run not found: {run_id}", run_id=run_id)
        records = self._read_complete_records(run_dir / "events.jsonl", recover_tail=False)
        eligible = [item for item in records if int(item["sequence"]) > after]
        page = eligible[:limit]
        next_cursor = int(page[-1]["sequence"]) if page else after
        return {
            "ok": True,
            "run_id": run_id,
            "events": page,
            "count": len(page),
            "after": after,
            "next_cursor": next_cursor,
            "has_more": len(eligible) > len(page),
        }

    def _event_lock(self, run_id: str) -> RuntimeActionLock:
        lock = RuntimeActionLock(
            self.runtime_root / "locks" / f"event-{run_id}.lock",
            run_id=run_id,
            error_code="event_write_locked",
            locked_message=f"Another process is writing events for run {run_id}.",
        )
        for attempt in range(101):
            try:
                lock.__enter__()
                return _EnteredLock(lock)
            except HarnessError as exc:
                if exc.error_code != "event_write_locked" or attempt == 100:
                    raise
                time.sleep(0.01)
        raise AssertionError("unreachable")

    @staticmethod
    def _read_complete_records(path: Path, *, recover_tail: bool) -> list[dict[str, object]]:
        if not path.is_file():
            return []
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise HarnessError("event_store_failed", "Event log could not be read.") from exc
        last_newline = raw.rfind(b"\n")
        complete = raw[: last_newline + 1] if last_newline >= 0 else b""
        if recover_tail and complete != raw:
            try:
                with path.open("r+b") as handle:
                    handle.truncate(len(complete))
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise HarnessError("event_store_failed", "Incomplete event tail could not be repaired.") from exc
        records: list[dict[str, object]] = []
        for index, line in enumerate(complete.splitlines(), start=1):
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HarnessError("event_log_corrupted", f"Event log contains invalid JSON at sequence {index}.") from exc
            if not isinstance(value, dict) or value.get("version") != "event_v1" or value.get("sequence") != index:
                raise HarnessError("event_log_corrupted", f"Event log sequence {index} is invalid.")
            records.append(value)
        return records


class _EnteredLock:
    def __init__(self, lock: RuntimeActionLock):
        self.lock = lock

    def __enter__(self) -> RuntimeActionLock:
        return self.lock

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.lock.__exit__(exc_type, exc, traceback)  # type: ignore[arg-type]


def _sanitize_data(event_type: str, data: dict[str, object]) -> dict[str, object]:
    allowed = _DATA_ALLOWLIST.get(event_type, frozenset())
    result: dict[str, object] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        if key not in allowed or key.lower() in _FORBIDDEN_KEYS:
            continue
        result[key] = _safe_value(value)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EVENT_DATA_BYTES:
        raise HarnessError("event_data_too_large", "Event metadata exceeds the size limit.")
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _LOCAL_PATH.search(value) or _CREDENTIAL_URL.search(value):
            return "<redacted>"
        return _bounded_text(value, 512)
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in list(value)[:50] if isinstance(item, (str, bool, int, float)) or item is None]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _safe_value(item)
            for key, item in list(value.items())[:50]
            if str(key).lower() not in _FORBIDDEN_KEYS and (isinstance(item, (str, bool, int, float)) or item is None)
        }
    return _bounded_text(type(value).__name__, 64)


def _validated_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not _SAFE_RUN_ID.fullmatch(value):
        raise HarnessError("invalid_path_parameter", "Invalid run id.")
    return value


def _bounded_text(value: object, limit: int) -> str:
    return str(value)[:limit]


def _optional_text(value: object | None, limit: int) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _bounded_text(value, limit)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
