"""Mô hình command dùng chung cho các transport của SDLC Harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ApplicationCommand:
    """Một yêu cầu application độc lập với argparse và HTTP."""

    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    settings_overrides: Mapping[str, str] = field(default_factory=dict)
    runtime_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))
        object.__setattr__(
            self,
            "settings_overrides",
            MappingProxyType({str(key): str(value) for key, value in self.settings_overrides.items()}),
        )
        if self.runtime_root is not None:
            object.__setattr__(self, "runtime_root", Path(self.runtime_root))
