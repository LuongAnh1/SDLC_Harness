"""Chọn mock/real adapter cho từng checkpoint theo runtime config.

CLI truyền sync mode rõ ràng vào đây; runner chỉ điều phối adapter và hợp nhất
report, không tự quyết định ghi lên tool cộng tác.
"""

from __future__ import annotations

from ..core.config import Settings
from ..events import emit_current
from ..storage.runtime import RuntimeStore
from .atlassian import (
    real_sync_code_review_checkpoint,
    real_sync_design_checkpoint,
    real_sync_implementation_plan_checkpoint,
    real_sync_requirement_checkpoint,
)
from .mock import (
    mock_sync_code_review_checkpoint,
    mock_sync_design_checkpoint,
    mock_sync_implementation_plan_checkpoint,
    mock_sync_requirement_checkpoint,
)


def _sync_requirement_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    checkpoint_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Sync requirement checkpoint bằng mock, real hoặc cả hai."""

    if settings.sync_mode == "mock":
        return mock_sync_requirement_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
            checkpoint_context=checkpoint_context,
        )
    if settings.sync_mode == "both":
        mock_sync_requirement_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
            checkpoint_context=checkpoint_context,
        )
    return real_sync_requirement_checkpoint(
        store=store,
        settings=settings,
        run_id=run_id,
        source_ref=source_ref,
        artifact_paths=artifact_paths,
        checkpoint_context=checkpoint_context,
    )


def _sync_design_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    """Sync design checkpoint Phase 2 bằng mock, real hoặc cả hai."""

    if settings.sync_mode == "mock":
        return mock_sync_design_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
        )
    if settings.sync_mode == "both":
        mock_sync_design_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
        )
    return real_sync_design_checkpoint(
        store=store,
        settings=settings,
        run_id=run_id,
        source_ref=source_ref,
        artifact_paths=artifact_paths,
    )


def _sync_implementation_plan_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    """Sync checkpoint Phase 3 bằng mock, real hoặc cả hai."""

    if settings.sync_mode == "mock":
        return mock_sync_implementation_plan_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
        )
    if settings.sync_mode == "both":
        mock_sync_implementation_plan_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
        )
    return real_sync_implementation_plan_checkpoint(
        store=store,
        settings=settings,
        run_id=run_id,
        source_ref=source_ref,
        artifact_paths=artifact_paths,
    )


def _sync_code_review_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    review_status: str = "code_review_ready",
) -> dict[str, object]:
    """Sync checkpoint Code Review bằng mock, real hoặc cả hai."""

    if review_status not in {"code_review_ready", "code_review_approved"}:
        raise ValueError(f"Unsupported Code Review status: {review_status}")

    if settings.sync_mode == "mock":
        return mock_sync_code_review_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
            review_status=review_status,
        )
    if settings.sync_mode == "both":
        mock_sync_code_review_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
            review_status=review_status,
        )
    return real_sync_code_review_checkpoint(
        store=store,
        settings=settings,
        run_id=run_id,
        source_ref=source_ref,
        artifact_paths=artifact_paths,
        review_status=review_status,
    )

def _sync_targets(report: dict[str, object]) -> dict[str, object]:
    targets: dict[str, object] = {}
    for key in ("jira", "confluence", "github", "mock"):
        value = report.get(key)
        if isinstance(value, dict):
            targets[key] = str(value.get("status") or value.get("result") or "unknown")
    return targets


def _record_sync(*, phase: str, settings: Settings, report: dict[str, object]) -> dict[str, object]:
    status = str(report.get("status") or report.get("overall_status") or "unknown")
    emit_current(
        category="sync",
        event_type="sync_completed",
        message="Synchronization completed.",
        data={
            "sync_phase": phase,
            "mode": settings.sync_mode,
            "status": status,
            "targets": _sync_targets(report),
        },
    )
    return report


def sync_requirement_checkpoint(**kwargs: object) -> dict[str, object]:
    settings = kwargs["settings"]
    return _record_sync(phase="requirement", settings=settings, report=_sync_requirement_checkpoint(**kwargs))  # type: ignore[arg-type]


def sync_design_checkpoint(**kwargs: object) -> dict[str, object]:
    settings = kwargs["settings"]
    return _record_sync(phase="design", settings=settings, report=_sync_design_checkpoint(**kwargs))  # type: ignore[arg-type]


def sync_implementation_plan_checkpoint(**kwargs: object) -> dict[str, object]:
    settings = kwargs["settings"]
    return _record_sync(phase="implementation_plan", settings=settings, report=_sync_implementation_plan_checkpoint(**kwargs))  # type: ignore[arg-type]


def sync_code_review_checkpoint(**kwargs: object) -> dict[str, object]:
    settings = kwargs["settings"]
    return _record_sync(phase="code_review", settings=settings, report=_sync_code_review_checkpoint(**kwargs))  # type: ignore[arg-type]
