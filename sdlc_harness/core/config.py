"""Cấu hình runtime cho SDLC Harness.

Phase 1 ưu tiên CLI chạy local nên cấu hình được đọc từ `.env` ở repo root và
OS environment. `.env` chỉ chứa thông tin máy cá nhân/API key, không commit.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Các giá trị cần cho Requirement Refiner và runtime local."""

    repo_root: Path
    runtime_root: Path
    llm_mode: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: int


def repo_root() -> Path:
    """Trả về root của repo, tính từ `sdlc_harness/core/config.py`."""

    return Path(__file__).resolve().parents[2]


def _read_dotenv(path: Path) -> dict[str, str]:
    """Đọc `.env` dạng KEY=VALUE tối giản, đủ cho demo local."""

    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(
    *,
    root: Path | None = None,
    runtime_root: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> Settings:
    """Gộp `.env`, OS env và override CLI thành cấu hình chạy.

    Thứ tự ưu tiên: `.env` -> OS env -> CLI overrides. CLI override đứng cuối để
    test/smoke có thể ép `--llm-mode fake` mà không sửa file cấu hình thật.
    """

    root_path = (root or repo_root()).resolve()
    merged = _read_dotenv(root_path / ".env")
    for key in (
        "SDLC_LLM_MODE",
        "SDLC_LLM_API_KEY",
        "SDLC_LLM_BASE_URL",
        "SDLC_LLM_MODEL",
        "SDLC_LLM_TIMEOUT_SECONDS",
    ):
        if key in os.environ:
            merged[key] = os.environ[key]
    if overrides:
        merged.update({key: value for key, value in overrides.items() if value is not None})

    timeout_raw = merged.get("SDLC_LLM_TIMEOUT_SECONDS", "90")
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError:
        timeout_seconds = 90

    runtime_path = (runtime_root or root_path / ".runtime").resolve()
    return Settings(
        repo_root=root_path,
        runtime_root=runtime_path,
        llm_mode=merged.get("SDLC_LLM_MODE", "fake").strip().lower() or "fake",
        llm_api_key=merged.get("SDLC_LLM_API_KEY", ""),
        llm_base_url=merged.get("SDLC_LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=merged.get("SDLC_LLM_MODEL", "deepseek-chat"),
        llm_timeout_seconds=timeout_seconds,
    )

