"""Chọn adapter sync theo runtime config."""

from __future__ import annotations

from ..core.config import Settings
from ..storage.runtime import RuntimeStore
from .atlassian import (
    real_sync_design_checkpoint,
    real_sync_implementation_plan_checkpoint,
    real_sync_requirement_checkpoint,
)
from .mock import (
    mock_sync_design_checkpoint,
    mock_sync_implementation_plan_checkpoint,
    mock_sync_requirement_checkpoint,
)


def sync_requirement_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    """Sync requirement checkpoint bằng mock, real hoặc cả hai."""

    if settings.sync_mode == "mock":
        return mock_sync_requirement_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
        )
    if settings.sync_mode == "both":
        mock_sync_requirement_checkpoint(
            store=store,
            run_id=run_id,
            source_ref=source_ref,
            artifact_paths=artifact_paths,
        )
    return real_sync_requirement_checkpoint(
        store=store,
        settings=settings,
        run_id=run_id,
        source_ref=source_ref,
        artifact_paths=artifact_paths,
    )


def sync_design_checkpoint(
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


def sync_implementation_plan_checkpoint(
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
