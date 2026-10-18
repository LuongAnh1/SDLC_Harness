"""Durable JSON storage for asynchronous API operations and idempotency."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from threading import RLock
import time
from uuid import uuid4

from ..core.errors import HarnessError


NONTERMINAL_OPERATION_STATUSES = frozenset({"queued", "running"})
TERMINAL_OPERATION_STATUSES = frozenset({"succeeded", "failed", "interrupted"})
_OPERATION_ID = re.compile(r"^op_[A-Za-z0-9_]+$")
_HASH_VALUE = re.compile(r"^sha256:([0-9a-f]{64})$")


class OperationStore:
    """Persist operation records without storing raw requests or plaintext keys."""

    def __init__(self, *, runtime_root: Path):
        self.runtime_root = runtime_root.resolve()
        self.operations_dir = self.runtime_root / "operations"
        self.idempotency_dir = self.runtime_root / "idempotency"
        self._io_lock = RLock()

    def ensure_layout(self) -> None:
        try:
            self.operations_dir.mkdir(parents=True, exist_ok=True)
            self.idempotency_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError("operation_store_failed", "Operation storage is unavailable.") from exc

    def new_operation_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"op_{stamp}_{uuid4().hex[:8]}"

    def create_operation(
        self,
        *,
        command: str,
        run_id: str | None,
        requested_by: str,
        sync_mode: str,
        request_hash: str,
        idempotency_key_hash: str,
    ) -> dict[str, object]:
        now = utc_now_iso()
        record: dict[str, object] = {
            "version": "operation_v1",
            "operation_id": self.new_operation_id(),
            "status": "queued",
            "command": command,
            "run_id": run_id,
            "requested_by": requested_by,
            "sync_mode": sync_mode,
            "request_hash": request_hash,
            "idempotency_key_hash": idempotency_key_hash,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        self.write_operation(record)
        return record

    def read_operation(self, operation_id: str) -> dict[str, object]:
        path = self.operation_path(operation_id)
        if not path.is_file():
            raise HarnessError("operation_not_found", f"Operation not found: {operation_id}")
        return self._read_json(path, error_code="operation_state_corrupted")

    def write_operation(self, record: dict[str, object]) -> None:
        operation_id = str(record.get("operation_id") or "")
        status = str(record.get("status") or "")
        if status not in NONTERMINAL_OPERATION_STATUSES | TERMINAL_OPERATION_STATUSES:
            raise HarnessError("operation_state_invalid", f"Invalid operation status: {status}")
        self._write_json(self.operation_path(operation_id), record)

    def list_operations(self, *, limit: int | None = None) -> list[dict[str, object]]:
        if limit is not None and limit < 1:
            raise HarnessError("invalid_query", "Operation list limit must be at least 1.")
        if not self.operations_dir.exists():
            return []
        records = [
            self.read_operation(path.stem)
            for path in self.operations_dir.glob("op_*.json")
            if path.is_file()
        ]
        records.sort(
            key=lambda item: (str(item.get("created_at") or ""), str(item.get("operation_id") or "")),
            reverse=True,
        )
        return records if limit is None else records[:limit]

    def read_idempotency(self, key_hash: str) -> dict[str, object] | None:
        path = self.idempotency_path(key_hash)
        if not path.is_file():
            return None
        return self._read_json(path, error_code="idempotency_state_corrupted")

    def write_idempotency(
        self,
        *,
        key_hash: str,
        request_hash: str,
        operation_id: str,
        created_at: str,
    ) -> None:
        record = {
            "version": "idempotency_v1",
            "idempotency_key_hash": key_hash,
            "request_hash": request_hash,
            "operation_id": operation_id,
            "created_at": created_at,
        }
        self._write_json(self.idempotency_path(key_hash), record)

    def rebuild_idempotency_indexes(self) -> None:
        """Repair a crash between operation persistence and index persistence."""

        for operation in self.list_operations():
            key_hash = str(operation.get("idempotency_key_hash") or "")
            request_hash = str(operation.get("request_hash") or "")
            operation_id = str(operation.get("operation_id") or "")
            existing = self.read_idempotency(key_hash)
            if existing is not None:
                if (
                    existing.get("request_hash") != request_hash
                    or existing.get("operation_id") != operation_id
                ):
                    raise HarnessError(
                        "idempotency_state_corrupted",
                        "Idempotency index conflicts with persisted operation state.",
                    )
                continue
            self.write_idempotency(
                key_hash=key_hash,
                request_hash=request_hash,
                operation_id=operation_id,
                created_at=str(operation.get("created_at") or utc_now_iso()),
            )

    def interrupt_incomplete(self, *, message: str) -> list[str]:
        interrupted: list[str] = []
        now = utc_now_iso()
        for record in self.list_operations():
            if record.get("status") not in NONTERMINAL_OPERATION_STATUSES:
                continue
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
            self.write_operation(record)
            interrupted.append(str(record["operation_id"]))
        return interrupted

    def operation_path(self, operation_id: str) -> Path:
        if not _OPERATION_ID.fullmatch(operation_id):
            raise HarnessError("invalid_path_parameter", "Invalid operation id.")
        return self.operations_dir / f"{operation_id}.json"

    def idempotency_path(self, key_hash: str) -> Path:
        match = _HASH_VALUE.fullmatch(key_hash)
        if not match:
            raise HarnessError("operation_state_invalid", "Invalid idempotency key hash.")
        return self.idempotency_dir / f"{match.group(1)}.json"

    def _read_json(self, path: Path, *, error_code: str) -> dict[str, object]:
        try:
            with self._io_lock:
                value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(error_code, f"Invalid persisted operation data: {path.name}") from exc
        if not isinstance(value, dict):
            raise HarnessError(error_code, f"Invalid persisted operation data: {path.name}")
        return value

    def _write_json(self, path: Path, value: dict[str, object]) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with self._io_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.write_text(
                    json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                for attempt in range(20):
                    try:
                        temp_path.replace(path)
                        break
                    except PermissionError:
                        if attempt == 19:
                            raise
                        time.sleep(0.01)
        except OSError as exc:
            raise HarnessError("operation_store_failed", "Operation state could not be persisted.") from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
