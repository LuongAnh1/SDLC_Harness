"""Chính sách retirement cho run Java cũ không có profile snapshot."""

from __future__ import annotations

from collections.abc import Mapping

from ..core.errors import HarnessError
from .snapshot import PROFILE_SNAPSHOT_STATE_KEY, read_profile_snapshot


# Kept local to avoid importing the Designer package from config/profile bootstrap.
DESIGNER_CONTRACT_STATE_KEY = "designer_contract_version"
CURRENT_DESIGNER_CONTRACT_VERSION = "vertical_design_graph_v2"
SUPPORTED_DESIGNER_CONTRACT_VERSIONS = frozenset(
    {"vertical_design_graph_v1", "vertical_design_graph_v2"}
)

LEGACY_RETIRED_ALLOWED_COMMANDS = frozenset(
    {
        "get-status",
        "sync-run",
        "cleanup-run-workspace",
    }
)
LEGACY_RETIRED_VISIBLE_ACTIONS = frozenset({"get_status", "sync_run"})


def require_project_profile_for_start(*, project_profile_id: str | None) -> None:
    """Không cho tạo run mới nếu chưa chọn execution profile hiện hành."""

    if project_profile_id:
        return
    raise HarnessError(
        "project_profile_required",
        "SDLC_PROJECT_PROFILE must be selected before starting a new run.",
    )


def require_run_command_allowed(
    state: dict[str, object],
    *,
    command_name: str,
    run_id: str,
) -> None:
    """Chặn command sinh artifact trên run legacy tại application boundary."""

    snapshot = read_profile_snapshot(state, run_id=run_id)
    if snapshot is None:
        if command_name in LEGACY_RETIRED_ALLOWED_COMMANDS:
            return
        raise HarnessError(
            "legacy_execution_profile_retired",
            (
                f"Run {run_id} uses the retired legacy_java_v1 execution profile; "
                "only read, existing-artifact sync, and workspace cleanup remain available."
            ),
            run_id=run_id,
        )
    if _has_retired_designer_contract(state):
        if command_name in LEGACY_RETIRED_ALLOWED_COMMANDS:
            return
        raise HarnessError(
            "legacy_designer_contract_retired",
            (
                f"Run {run_id} has no current Vertical Design contract; "
                "only read, existing-artifact sync, and workspace cleanup remain available."
            ),
            run_id=run_id,
        )
    if not _has_retired_developer_executor(state) or command_name in LEGACY_RETIRED_ALLOWED_COMMANDS:
        return
    raise HarnessError(
        "legacy_developer_executor_retired",
        (
            f"Run {run_id} has a Developer session bound to the retired legacy executor; "
            "only read, existing-artifact sync, and workspace cleanup remain available."
        ),
        run_id=run_id,
    )


def public_allowed_actions(
    state: Mapping[str, object],
    actions: object,
) -> list[str]:
    """Ẩn action đã retire khỏi projection nhưng không sửa persisted state."""

    items = actions if isinstance(actions, (list, tuple)) else []
    normalized = [str(action) for action in items if action]
    if (
        not _is_legacy_projection(state)
        and not _has_retired_designer_contract(state)
        and not _has_retired_developer_executor(state)
    ):
        if str(state.get("status") or "") == "design_paused" and "restart_design" not in normalized:
            insert_at = normalized.index("continue_run") + 1 if "continue_run" in normalized else 0
            normalized.insert(insert_at, "restart_design")
        return normalized
    return [action for action in normalized if action in LEGACY_RETIRED_VISIBLE_ACTIONS]


def public_run_summary(
    state: Mapping[str, object],
    summary: object | None = None,
) -> object:
    """Không để status legacy tiếp tục mời gọi action đã bị retire."""

    if (
        not _is_legacy_projection(state)
        and not _has_retired_designer_contract(state)
        and not _has_retired_developer_executor(state)
    ):
        return state.get("summary_for_user") if summary is None else summary
    run_id = str(state.get("run_id") or "run này")
    if not _is_legacy_projection(state) and _has_retired_designer_contract(state):
        return (
            f"Run {run_id} không có Vertical Design contract hiện hành; chỉ còn "
            "read/audit, sync artifact đã tồn tại và workspace cleanup."
        )
    if _has_retired_developer_executor(state):
        return (
            f"Run {run_id} có Developer session dùng legacy executor đã retire; chỉ còn "
            "read/audit, sync artifact đã tồn tại và workspace cleanup."
        )
    return (
        f"Run {run_id} dùng legacy_java_v1 đã retire; chỉ còn read/audit, "
        "sync artifact đã tồn tại và workspace cleanup."
    )


def _is_legacy_projection(state: Mapping[str, object]) -> bool:
    """Nhận diện legacy từ persisted state hoặc public operation payload."""

    metadata = state.get("execution_profile")
    if isinstance(metadata, Mapping):
        classification = metadata.get("classification")
        if classification == "legacy_java_v1":
            return True
        if classification in {"profiled", "invalid"}:
            return False
    return state.get(PROFILE_SNAPSHOT_STATE_KEY) is None


def _has_retired_developer_executor(state: Mapping[str, object]) -> bool:
    execution = state.get("developer_execution")
    return isinstance(execution, Mapping) and execution.get("executor") == "legacy"


def _has_retired_designer_contract(state: Mapping[str, object]) -> bool:
    return state.get(DESIGNER_CONTRACT_STATE_KEY) not in SUPPORTED_DESIGNER_CONTRACT_VERSIONS
