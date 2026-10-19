"""Application service đọc và cập nhật cấu hình runtime an toàn."""

from __future__ import annotations

import os
from pathlib import Path

from ..core.config import resolve_config, repo_root as default_repo_root
from ..core.config_schema import (
    CONFIG_SECTIONS,
    MASKED_SECRET_VALUE,
    config_field_index,
    config_section_index,
    normalize_config_value,
    typed_config_value,
)
from ..core.errors import HarnessError
from ..core.runtime_config import RuntimeConfigStore


class ConfigurationService:
    """Expose schema-derived config without leaking secret values."""

    def __init__(self, *, root: Path | None = None, runtime_root: Path | None = None):
        self.root = (root or default_repo_root()).resolve()
        self.runtime_root = (runtime_root or self.root / ".runtime").resolve()

    def snapshot(self) -> dict[str, object]:
        resolution = resolve_config(root=self.root, runtime_root=self.runtime_root)
        sections: list[dict[str, object]] = []
        for section in CONFIG_SECTIONS:
            public_fields: list[dict[str, object]] = []
            for raw_field in section["fields"]:  # type: ignore[index]
                field = dict(raw_field)
                name = str(field["name"])
                source = resolution.sources[name]
                raw_value = resolution.values[name]
                runtime_value = resolution.runtime_values.get(name)
                secret = bool(field.get("secret"))
                editable = bool(field.get("editable")) and source != "os"
                value = typed_config_value(field, raw_value)
                if secret:
                    value = MASKED_SECRET_VALUE if raw_value else ""
                    displayed_runtime = MASKED_SECRET_VALUE if runtime_value not in {None, ""} else None
                elif field.get("kind") == "path":
                    value = self._public_path(value)
                    displayed_runtime = self._public_path(runtime_value) if runtime_value is not None else None
                else:
                    displayed_runtime = runtime_value
                public_fields.append(
                    {
                        **field,
                        "value": value,
                        "source": source,
                        "editable": editable,
                        "runtime_value": displayed_runtime,
                        "is_set": bool(raw_value),
                        "resettable": runtime_value is not None and editable,
                    }
                )
            sections.append(
                {
                    "id": section["id"],
                    "label": section["label"],
                    "description": section["description"],
                    "fields": public_fields,
                }
            )
        return {
            "ok": True,
            "version": "runtime_config_v1",
            "path": self._public_path(resolution.runtime_config_label),
            "precedence": ["default", ".env", "runtime", "os", "override"],
            "sections": sections,
        }

    def update(self, values: dict[str, object]) -> dict[str, object]:
        if not isinstance(values, dict):
            raise HarnessError("config_invalid", "values must be a JSON object.")
        resolution = resolve_config(root=self.root, runtime_root=self.runtime_root)
        fields = config_field_index()
        normalized: dict[str, object] = {}
        for raw_name, value in values.items():
            name = str(raw_name)
            field = fields.get(name)
            self._require_editable(name, field, resolution.sources.get(name))
            assert field is not None
            if field.get("secret") and (value == "" or value == MASKED_SECRET_VALUE):
                continue
            normalized[name] = normalize_config_value(field, value)
        before = resolution.runtime_values
        changed = sorted(name for name, value in normalized.items() if before.get(name) != value)
        if normalized:
            RuntimeConfigStore(resolution.runtime_config_path).update(normalized)
        return self._mutation_snapshot(changed)

    def reset(self, *, keys: list[str] | None = None, section: str | None = None) -> dict[str, object]:
        if not keys and not section:
            raise HarnessError("config_invalid", "reset requires keys, section, or both.")
        resolution = resolve_config(root=self.root, runtime_root=self.runtime_root)
        fields = config_field_index()
        selected: set[str] = set()
        for raw_name in keys or []:
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise HarnessError("config_invalid", "reset keys must be non-empty strings.")
            name = raw_name.strip()
            field = fields.get(name)
            self._require_editable(name, field, resolution.sources.get(name))
            selected.add(name)
        if section:
            section_spec = config_section_index().get(section)
            if section_spec is None:
                raise HarnessError("config_invalid", f"Unknown config section: {section}")
            for field in section_spec["fields"]:  # type: ignore[index]
                name = str(field["name"])
                if name not in resolution.runtime_values:
                    continue
                if field.get("editable") and resolution.sources.get(name) != "os":
                    selected.add(name)
        changed = sorted(name for name in selected if name in resolution.runtime_values)
        if selected:
            RuntimeConfigStore(resolution.runtime_config_path).reset(selected)
        return self._mutation_snapshot(changed)

    def _mutation_snapshot(self, changed: list[str]) -> dict[str, object]:
        result = self.snapshot()
        fields = config_field_index()
        restart_keys = sorted(name for name in changed if fields[name].get("restart_required"))
        result["mutation"] = {
            "changed_keys": changed,
            "restart_required": bool(restart_keys),
            "restart_keys": restart_keys,
        }
        return result

    @staticmethod
    def _public_path(value: object) -> object:
        if not isinstance(value, str) or not value:
            return value
        return "<local-path>" if Path(value).expanduser().is_absolute() else value.replace("\\", "/")

    @staticmethod
    def _require_editable(
        name: str,
        field: dict[str, object] | None,
        source: str | None,
    ) -> None:
        if field is None:
            raise HarnessError("config_invalid", f"Unsupported config key: {name}")
        if not field.get("editable") or field.get("bootstrap"):
            raise HarnessError("config_read_only", f"{name} is a read-only bootstrap setting.")
        if source == "os" or name in os.environ:
            raise HarnessError("config_locked", f"{name} is locked by the OS environment.")
