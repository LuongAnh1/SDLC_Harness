"""Loader nguồn use case cho Phase 1.

Niko không đọc fixture hay Jira trực tiếp. Mọi source đầu vào của workflow SDLC
đi qua module này để SDLC Harness giữ quyền sở hữu source_ref và idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.errors import HarnessError


_SUPPORTED_DATASETS = {"etour": "etour"}


@dataclass(frozen=True)
class UseCaseSource:
    """Use case thô đã được load, trước khi gọi Requirement Refiner."""

    dataset: str
    use_case: str
    source_ref: str
    source_path: Path
    text: str


def source_ref_for(dataset: str, use_case: str) -> str:
    """Tạo source_ref ổn định để tìm active run cho cùng một use case."""

    return f"dataset_use_case:{dataset}:{use_case}"


def load_use_case(*, dataset: str, use_case: str, repo_root: Path) -> UseCaseSource:
    """Load fixture Phase 1 và trả lỗi có mã nếu source chưa hỗ trợ."""

    dataset_key = dataset.strip().lower()
    if dataset_key not in _SUPPORTED_DATASETS:
        raise HarnessError(
            "source_not_supported",
            f"Dataset is not supported in Phase 1: {dataset}",
        )

    fixture_dir = repo_root / "fixtures" / _SUPPORTED_DATASETS[dataset_key]
    fixture_path = fixture_dir / f"{use_case}.txt"
    if not fixture_path.exists():
        raise HarnessError(
            "source_not_found",
            f"Use case fixture not found: {dataset}/{use_case}",
        )

    text = fixture_path.read_text(encoding="utf-8").strip()
    if not text:
        raise HarnessError(
            "source_not_found",
            f"Use case fixture is empty: {dataset}/{use_case}",
        )

    return UseCaseSource(
        dataset=dataset,
        use_case=use_case,
        source_ref=source_ref_for(dataset, use_case),
        source_path=fixture_path,
        text=text,
    )

