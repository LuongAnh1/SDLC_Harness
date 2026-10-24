"""Bộ nạp use case từ fixture cho Phase 1.

Niko không đọc fixture trực tiếp. Fixture eTour đi qua module này để
SDLC_Harness giữ quyền sở hữu source_ref và idempotency; Jira issue thật nằm ở
adapter source riêng nhưng cùng trả về `UseCaseSource`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from ..core.errors import HarnessError


_SUPPORTED_DATASETS = {"etour": "etour"}


@dataclass(frozen=True)
class UseCaseSource:
    """Use case thô đã được load, trước khi gọi Requirement Refiner."""

    dataset: str
    use_case: str
    source_ref: str
    source_path: Path | None
    text: str
    source_id: str = "SRC-1"
    source_type: str = "dataset"
    locator: str = ""
    revision: str | None = None

    def __post_init__(self) -> None:
        """Chuẩn hóa metadata tối thiểu cho các caller legacy trong test nội bộ."""

        if not self.locator:
            object.__setattr__(self, "locator", self.source_ref)
        if self.source_ref.startswith("jira_issue:") and self.source_type == "dataset":
            object.__setattr__(self, "source_type", "jira")


def source_provenance_entry(*, source: UseCaseSource, artifact_ref: str) -> dict[str, object]:
    """Tạo provenance entry chuẩn tắc từ metadata do source adapter sở hữu."""

    return {
        "id": source.source_id,
        "type": source.source_type,
        "source_ref": source.source_ref,
        "locator": source.locator,
        "revision": source.revision,
        "artifact_ref": artifact_ref,
        "fingerprint": "sha256:" + hashlib.sha256(source.text.encode("utf-8")).hexdigest(),
    }


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
        source_type="dataset",
        locator=fixture_path.relative_to(repo_root).as_posix(),
    )

