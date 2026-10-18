"""Atomic persistence for dashboard-managed runtime configuration."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
import time
from uuid import uuid4

from .config_schema import config_field_index, normalize_config_value
from .errors import HarnessError


class RuntimeConfigStore:
    """Read and atomically replace one allowlisted local config JSON file."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._lock = RLock()

    def read(self) -> dict[str, object]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HarnessError("config_corrupted", "Runtime config is not valid UTF-8 JSON.") from exc
            if not isinstance(payload, dict):
                raise HarnessError("config_corrupted", "Runtime config must be a JSON object.")
            fields = config_field_index()
            result: dict[str, object] = {}
            for raw_name, value in payload.items():
                name = str(raw_name)
                field = fields.get(name)
                if field is None:
                    raise HarnessError("config_corrupted", f"Runtime config contains unsupported key: {name}")
                if not field.get("editable") or field.get("bootstrap"):
                    raise HarnessError("config_corrupted", f"Runtime config contains read-only key: {name}")
                try:
                    result[name] = normalize_config_value(field, value)
                except HarnessError as exc:
                    raise HarnessError("config_corrupted", f"Runtime config value is invalid for {name}.") from exc
            return result

    def update(self, updates: dict[str, object]) -> dict[str, object]:
        with self._lock:
            values = self.read()
            values.update(updates)
            self._write(values)
            return dict(values)

    def reset(self, keys: set[str]) -> dict[str, object]:
        with self._lock:
            values = self.read()
            for key in keys:
                values.pop(key, None)
            self._write(values)
            return dict(values)

    def _write(self, values: dict[str, object]) -> None:
        temp_path = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            for attempt in range(20):
                try:
                    temp_path.replace(self.path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
        except OSError as exc:
            raise HarnessError("config_write_failed", "Runtime config could not be written.") from exc
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
