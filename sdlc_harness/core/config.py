"""Nạp cấu hình cho SDLC Harness.

Thiết lập workflow dùng thứ tự ưu tiên: mặc định trong code, `.env` ở thư mục
gốc, cấu hình runtime, môi trường OS thật, rồi override từ invocation/action.
Các trường bootstrap API không bao giờ lấy từ cấu hình runtime vì thay đổi
chúng đòi hỏi khởi động lại server.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ..profiles import get_project_profile
from .config_schema import CONFIG_SECTIONS, config_field_index, serialize_config_value
from .runtime_config import RuntimeConfigStore


@dataclass(frozen=True)
class Settings:
    """Cấu hình chung cho LLM, runtime state và tool sync."""

    repo_root: Path
    runtime_root: Path
    runtime_config_path: Path
    llm_mode: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_thinking_mode: str
    llm_timeout_seconds: int
    llm_structured_max_tokens: int
    llm_context_window_tokens: int
    requirement_max_tool_actions: int
    requirement_max_action_retries: int
    developer_max_actions_per_item: int
    developer_max_action_retries: int
    requirement_contract_version: str
    design_repair_limit: int
    design_validation_trials: int
    design_llm_call_budget: int
    vertical_trace_mode: str
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
    project_profile_id: str | None
    target_repo_path: Path
    target_worktree_root: Path
    target_repo_remote: str
    target_base_branch: str
    implementation_branch_prefix: str
    api_host: str
    api_port: int
    api_token: str
    api_queue_limit: int
    api_shutdown_timeout_seconds: int


@dataclass(frozen=True)
class ConfigResolution:
    values: dict[str, str]
    sources: dict[str, str]
    runtime_values: dict[str, object]
    runtime_config_path: Path
    runtime_config_label: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _bool_value(raw: str | None, *, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_value(raw: str | None, *, default: int, minimum: int = 0) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, value)


def resolve_config(
    *,
    root: Path | None = None,
    runtime_root: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> ConfigResolution:
    root_path = (root or repo_root()).resolve()
    runtime_path = (runtime_root or root_path / ".runtime").resolve()
    fields = config_field_index()
    dotenv = _read_dotenv(root_path / ".env")

    locator_raw = os.environ.get(
        "SDLC_RUNTIME_CONFIG_FILE",
        dotenv.get("SDLC_RUNTIME_CONFIG_FILE", ""),
    ).strip()
    if locator_raw:
        config_path = Path(locator_raw).expanduser()
        if not config_path.is_absolute():
            config_path = root_path / config_path
        config_label = locator_raw.replace("\\", "/")
    else:
        config_path = runtime_path / "config.json"
        try:
            config_label = str(config_path.relative_to(root_path)).replace("\\", "/")
        except ValueError:
            config_label = ".runtime/config.json"
    config_path = config_path.resolve()
    runtime_values = RuntimeConfigStore(config_path).read()

    values = {name: serialize_config_value(field.get("default", "")) for name, field in fields.items()}
    sources = {name: "default" for name in fields}

    def apply(source_values: dict[str, object], source: str, *, allow_bootstrap: bool) -> None:
        for name, value in source_values.items():
            field = fields.get(name)
            if field is None or (field.get("bootstrap") and not allow_bootstrap):
                continue
            values[name] = serialize_config_value(value)
            sources[name] = source

    apply(dotenv, ".env", allow_bootstrap=True)
    apply(runtime_values, "runtime", allow_bootstrap=False)
    apply({name: os.environ[name] for name in fields if name in os.environ}, "os", allow_bootstrap=True)
    if overrides:
        apply({name: value for name, value in overrides.items() if value is not None}, "override", allow_bootstrap=True)

    # A4 cutover treats the retired blank profile value as the current default.
    # Keep the reported source so operators can still see where the stale value lives.
    if not values["SDLC_PROJECT_PROFILE"].strip():
        values["SDLC_PROJECT_PROFILE"] = "typescript_fullstack_v1"

    # The implicit location follows runtime_root in tests and alternate local runtimes.
    if not locator_raw:
        values["SDLC_RUNTIME_CONFIG_FILE"] = config_label
        sources["SDLC_RUNTIME_CONFIG_FILE"] = "default"
    return ConfigResolution(
        values=values,
        sources=sources,
        runtime_values=runtime_values,
        runtime_config_path=config_path,
        runtime_config_label=config_label,
    )


def load_settings(
    *,
    root: Path | None = None,
    runtime_root: Path | None = None,
    overrides: dict[str, str] | None = None,
) -> Settings:
    """Resolve settings without mutating process environment."""

    resolution = resolve_config(root=root, runtime_root=runtime_root, overrides=overrides)
    merged = resolution.values
    root_path = (root or repo_root()).resolve()
    runtime_path = (runtime_root or root_path / ".runtime").resolve()
    sync_mode = merged.get("SDLC_SYNC_MODE", "mock").strip().lower() or "mock"
    if sync_mode not in {"mock", "real", "both"}:
        sync_mode = "mock"
    llm_thinking_mode = merged.get("SDLC_LLM_THINKING_MODE", "disabled").strip().lower()
    if llm_thinking_mode not in {"disabled", "enabled"}:
        llm_thinking_mode = "disabled"
    project_profile_raw = merged.get("SDLC_PROJECT_PROFILE", "typescript_fullstack_v1").strip()
    vertical_trace_mode = merged.get("SDLC_VERTICAL_TRACE_MODE", "enforce").strip().lower() or "enforce"
    if vertical_trace_mode not in {"shadow", "enforce"}:
        vertical_trace_mode = "enforce"
    project_profile_id = get_project_profile(
        project_profile_raw or "typescript_fullstack_v1"
    ).profile_id
    target_repo_path = Path(merged.get("SDLC_TARGET_REPO_PATH", str(root_path.parent / "SDLC_Target_TypeScript")))
    if not target_repo_path.is_absolute():
        target_repo_path = root_path / target_repo_path
    target_repo_path = target_repo_path.resolve()
    worktree_root_raw = merged.get("SDLC_TARGET_WORKTREE_ROOT", "").strip()
    target_worktree_root = Path(worktree_root_raw) if worktree_root_raw else target_repo_path.parent / f"{target_repo_path.name}-worktrees"
    if not target_worktree_root.is_absolute():
        target_worktree_root = root_path / target_worktree_root
    return Settings(
        repo_root=root_path,
        runtime_root=runtime_path,
        runtime_config_path=resolution.runtime_config_path,
        llm_mode=merged.get("SDLC_LLM_MODE", "fake").strip().lower() or "fake",
        llm_api_key=merged.get("SDLC_LLM_API_KEY", ""),
        llm_base_url=merged.get("SDLC_LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=merged.get("SDLC_LLM_MODEL", "deepseek-flash"),
        llm_thinking_mode=llm_thinking_mode,
        llm_timeout_seconds=_int_value(merged.get("SDLC_LLM_TIMEOUT_SECONDS"), default=90, minimum=1),
        llm_structured_max_tokens=_int_value(merged.get("SDLC_LLM_STRUCTURED_MAX_TOKENS"), default=8192, minimum=1024),
        llm_context_window_tokens=_int_value(merged.get("SDLC_LLM_CONTEXT_WINDOW_TOKENS"), default=128000, minimum=8192),
        requirement_max_tool_actions=_int_value(merged.get("SDLC_REQUIREMENT_MAX_TOOL_ACTIONS"), default=12, minimum=3),
        requirement_max_action_retries=_int_value(merged.get("SDLC_REQUIREMENT_MAX_ACTION_RETRIES"), default=2, minimum=0),
        developer_max_actions_per_item=_int_value(merged.get("SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM"), default=24, minimum=1),
        developer_max_action_retries=_int_value(merged.get("SDLC_DEVELOPER_MAX_ACTION_RETRIES"), default=2, minimum=0),
        requirement_contract_version="v2",
        design_repair_limit=_int_value(merged.get("SDLC_DESIGN_REPAIR_LIMIT"), default=3, minimum=0),
        design_validation_trials=_int_value(merged.get("SDLC_DESIGN_VALIDATION_TRIALS"), default=3, minimum=1),
        design_llm_call_budget=_int_value(merged.get("SDLC_DESIGN_LLM_CALL_BUDGET"), default=28, minimum=1),
        vertical_trace_mode=vertical_trace_mode,
        implementation_plan_repair_limit=_int_value(merged.get("SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT"), default=2, minimum=0),
        implementation_repair_limit=_int_value(merged.get("SDLC_IMPLEMENTATION_REPAIR_LIMIT"), default=2, minimum=0),
        implementation_validation_trials=_int_value(merged.get("SDLC_IMPLEMENTATION_VALIDATION_TRIALS"), default=3, minimum=2),
        implementation_validation_call_budget=_int_value(merged.get("SDLC_IMPLEMENTATION_VALIDATION_CALL_BUDGET"), default=6, minimum=2),
        qa_repair_limit=_int_value(merged.get("SDLC_QA_REPAIR_LIMIT"), default=1, minimum=0),
        sync_mode=sync_mode,
        atlassian_base_url=merged.get("ATLASSIAN_BASE_URL", "").rstrip("/"),
        atlassian_email=merged.get("ATLASSIAN_EMAIL", ""),
        atlassian_api_token=merged.get("ATLASSIAN_API_TOKEN", ""),
        jira_sync_enabled=_bool_value(merged.get("JIRA_SYNC_ENABLED")),
        jira_sync_issue_key=merged.get("JIRA_SYNC_ISSUE_KEY", ""),
        confluence_sync_enabled=_bool_value(merged.get("CONFLUENCE_SYNC_ENABLED")),
        confluence_sync_space_id=merged.get("CONFLUENCE_SYNC_SPACE_ID", ""),
        confluence_sync_parent_page_id=merged.get("CONFLUENCE_SYNC_PARENT_PAGE_ID", ""),
        project_profile_id=project_profile_id,
        target_repo_path=target_repo_path,
        target_worktree_root=target_worktree_root.resolve(),
        target_repo_remote=merged.get("SDLC_TARGET_REPO_REMOTE", "https://github.com/LuongAnh1/SDLC_Target_TypeScript.git"),
        target_base_branch=merged.get("SDLC_TARGET_BASE_BRANCH", "main").strip() or "main",
        implementation_branch_prefix=(merged.get("SDLC_IMPLEMENTATION_BRANCH_PREFIX", "feature").strip().strip("/") or "feature"),
        api_host=merged.get("SDLC_API_HOST", "127.0.0.1").strip() or "127.0.0.1",
        api_port=_int_value(merged.get("SDLC_API_PORT"), default=7788, minimum=1),
        api_token=merged.get("SDLC_API_TOKEN", ""),
        api_queue_limit=_int_value(merged.get("SDLC_API_QUEUE_LIMIT"), default=32, minimum=1),
        api_shutdown_timeout_seconds=_int_value(merged.get("SDLC_API_SHUTDOWN_TIMEOUT_SECONDS"), default=30, minimum=1),
    )
