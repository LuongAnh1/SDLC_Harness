"""Cấu hình runtime cho SDLC Harness.

Giai đoạn hiện tại ưu tiên CLI chạy local nên cấu hình được đọc từ `.env` ở repo
root và OS environment. `.env` chỉ chứa thông tin máy cá nhân/API key, không
commit.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Cấu hình chung cho LLM, runtime state và tool sync."""

    repo_root: Path
    runtime_root: Path
    llm_mode: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: int
    llm_structured_max_tokens: int
    llm_context_window_tokens: int
    developer_execution_mode: str
    developer_max_actions_per_item: int
    developer_max_action_retries: int
    requirement_contract_version: str
    design_repair_limit: int
    design_validation_trials: int
    design_llm_call_budget: int
    implementation_plan_repair_limit: int
    implementation_repair_limit: int
    implementation_validation_trials: int
    implementation_validation_call_budget: int
    qa_repair_limit: int
    sync_mode: str
    atlassian_base_url: str
    atlassian_email: str
    atlassian_api_token: str
    jira_sync_enabled: bool
    jira_sync_issue_key: str
    confluence_sync_enabled: bool
    confluence_sync_space_id: str
    confluence_sync_parent_page_id: str
    target_repo_path: Path
    target_worktree_root: Path
    target_repo_remote: str
    target_base_branch: str
    implementation_branch_prefix: str
    api_host: str
    api_port: int
    api_token: str
    api_shutdown_timeout_seconds: int


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


def _bool_value(raw: str | None, *, default: bool = False) -> bool:
    """Đọc biến bật/tắt dạng 1/true/yes/on cho adapter local."""

    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_value(raw: str | None, *, default: int, minimum: int = 0) -> int:
    """Đọc số nguyên từ env/CLI và chặn giá trị âm cho cấu hình vòng lặp."""

    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, value)


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
        "SDLC_LLM_STRUCTURED_MAX_TOKENS",
        "SDLC_LLM_CONTEXT_WINDOW_TOKENS",
        "SDLC_DEVELOPER_EXECUTION_MODE",
        "SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM",
        "SDLC_DEVELOPER_MAX_ACTION_RETRIES",
        "SDLC_DESIGN_REPAIR_LIMIT",
        "SDLC_DESIGN_VALIDATION_TRIALS",
        "SDLC_DESIGN_LLM_CALL_BUDGET",
        "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT",
        "SDLC_IMPLEMENTATION_REPAIR_LIMIT",
        "SDLC_IMPLEMENTATION_VALIDATION_TRIALS",
        "SDLC_IMPLEMENTATION_VALIDATION_CALL_BUDGET",
        "SDLC_QA_REPAIR_LIMIT",
        "SDLC_SYNC_MODE",
        "ATLASSIAN_BASE_URL",
        "ATLASSIAN_EMAIL",
        "ATLASSIAN_API_TOKEN",
        "JIRA_SYNC_ENABLED",
        "JIRA_SYNC_ISSUE_KEY",
        "CONFLUENCE_SYNC_ENABLED",
        "CONFLUENCE_SYNC_SPACE_ID",
        "CONFLUENCE_SYNC_PARENT_PAGE_ID",
        "SDLC_TARGET_REPO_PATH",
        "SDLC_TARGET_WORKTREE_ROOT",
        "SDLC_TARGET_REPO_REMOTE",
        "SDLC_TARGET_BASE_BRANCH",
        "SDLC_IMPLEMENTATION_BRANCH_PREFIX",
        "SDLC_API_HOST",
        "SDLC_API_PORT",
        "SDLC_API_TOKEN",
        "SDLC_API_SHUTDOWN_TIMEOUT_SECONDS",
    ):
        if key in os.environ:
            merged[key] = os.environ[key]
    if overrides:
        merged.update({key: value for key, value in overrides.items() if value is not None})

    runtime_path = (runtime_root or root_path / ".runtime").resolve()
    sync_mode = merged.get("SDLC_SYNC_MODE", "mock").strip().lower() or "mock"
    if sync_mode not in {"mock", "real", "both"}:
        sync_mode = "mock"
    developer_execution_mode = merged.get("SDLC_DEVELOPER_EXECUTION_MODE", "legacy").strip().lower() or "legacy"
    if developer_execution_mode not in {"legacy", "tool_loop"}:
        developer_execution_mode = "legacy"
    target_repo_path = Path(merged.get("SDLC_TARGET_REPO_PATH", str(root_path.parent / "SDLC_Target_eTour")))
    if not target_repo_path.is_absolute():
        target_repo_path = root_path / target_repo_path
    target_repo_path = target_repo_path.resolve()
    worktree_root_raw = merged.get("SDLC_TARGET_WORKTREE_ROOT", "").strip()
    target_worktree_root = (
        Path(worktree_root_raw)
        if worktree_root_raw
        else target_repo_path.parent / f"{target_repo_path.name}-worktrees"
    )
    if not target_worktree_root.is_absolute():
        target_worktree_root = root_path / target_worktree_root
    return Settings(
        repo_root=root_path,
        runtime_root=runtime_path,
        llm_mode=merged.get("SDLC_LLM_MODE", "fake").strip().lower() or "fake",
        llm_api_key=merged.get("SDLC_LLM_API_KEY", ""),
        llm_base_url=merged.get("SDLC_LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=merged.get("SDLC_LLM_MODEL", "deepseek-chat"),
        llm_timeout_seconds=_int_value(merged.get("SDLC_LLM_TIMEOUT_SECONDS"), default=90, minimum=1),
        llm_structured_max_tokens=_int_value(
            merged.get("SDLC_LLM_STRUCTURED_MAX_TOKENS"),
            default=8192,
            minimum=1024,
        ),
        llm_context_window_tokens=_int_value(
            merged.get("SDLC_LLM_CONTEXT_WINDOW_TOKENS"),
            default=128000,
            minimum=8192,
        ),
        developer_execution_mode=developer_execution_mode,
        developer_max_actions_per_item=_int_value(
            merged.get("SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM"),
            default=24,
            minimum=1,
        ),
        developer_max_action_retries=_int_value(
            merged.get("SDLC_DEVELOPER_MAX_ACTION_RETRIES"),
            default=2,
            minimum=0,
        ),
        requirement_contract_version="v2",
        design_repair_limit=_int_value(merged.get("SDLC_DESIGN_REPAIR_LIMIT"), default=2, minimum=0),
        design_validation_trials=_int_value(
            merged.get("SDLC_DESIGN_VALIDATION_TRIALS"),
            default=3,
            minimum=1,
        ),
        design_llm_call_budget=_int_value(
            merged.get("SDLC_DESIGN_LLM_CALL_BUDGET"),
            default=16,
            minimum=1,
        ),
        implementation_plan_repair_limit=_int_value(
            merged.get("SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT"),
            default=2,
            minimum=0,
        ),
        implementation_repair_limit=_int_value(
            merged.get("SDLC_IMPLEMENTATION_REPAIR_LIMIT"),
            default=2,
            minimum=0,
        ),
        implementation_validation_trials=_int_value(
            merged.get("SDLC_IMPLEMENTATION_VALIDATION_TRIALS"),
            default=3,
            minimum=2,
        ),
        implementation_validation_call_budget=_int_value(
            merged.get("SDLC_IMPLEMENTATION_VALIDATION_CALL_BUDGET"),
            default=6,
            minimum=2,
        ),
        qa_repair_limit=_int_value(
            merged.get("SDLC_QA_REPAIR_LIMIT"),
            default=1,
            minimum=0,
        ),
        sync_mode=sync_mode,
        atlassian_base_url=merged.get("ATLASSIAN_BASE_URL", "").rstrip("/"),
        atlassian_email=merged.get("ATLASSIAN_EMAIL", ""),
        atlassian_api_token=merged.get("ATLASSIAN_API_TOKEN", ""),
        jira_sync_enabled=_bool_value(merged.get("JIRA_SYNC_ENABLED")),
        jira_sync_issue_key=merged.get("JIRA_SYNC_ISSUE_KEY", ""),
        confluence_sync_enabled=_bool_value(merged.get("CONFLUENCE_SYNC_ENABLED")),
        confluence_sync_space_id=merged.get("CONFLUENCE_SYNC_SPACE_ID", ""),
        confluence_sync_parent_page_id=merged.get("CONFLUENCE_SYNC_PARENT_PAGE_ID", ""),
        target_repo_path=target_repo_path,
        target_worktree_root=target_worktree_root.resolve(),
        target_repo_remote=merged.get(
            "SDLC_TARGET_REPO_REMOTE",
            "https://github.com/LuongAnh1/SDLC_Target_eTour.git",
        ),
        target_base_branch=merged.get("SDLC_TARGET_BASE_BRANCH", "main").strip() or "main",
        implementation_branch_prefix=(
            merged.get("SDLC_IMPLEMENTATION_BRANCH_PREFIX", "feature").strip().strip("/") or "feature"
        ),
        api_host=merged.get("SDLC_API_HOST", "127.0.0.1").strip() or "127.0.0.1",
        api_port=_int_value(merged.get("SDLC_API_PORT"), default=7788, minimum=1),
        api_token=merged.get("SDLC_API_TOKEN", ""),
        api_shutdown_timeout_seconds=_int_value(
            merged.get("SDLC_API_SHUTDOWN_TIMEOUT_SECONDS"),
            default=30,
            minimum=1,
        ),
    )

