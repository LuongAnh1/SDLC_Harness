"""Schema và quy tắc xác thực cấu hình runtime do dashboard quản lý."""

from __future__ import annotations

from typing import Any

from ..profiles import list_project_profile_ids
from .errors import HarnessError


MASKED_SECRET_VALUE = "********"
_PROJECT_PROFILE_OPTIONS = list_project_profile_ids()


def _field(
    name: str,
    label: str,
    kind: str,
    default: object,
    help_text: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    options: tuple[str, ...] = (),
    secret: bool = False,
    editable: bool = True,
    restart_required: bool = False,
    bootstrap: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "name": name,
        "label": label,
        "kind": kind,
        "default": default,
        "help": help_text,
        "secret": secret,
        "editable": editable,
        "restart_required": restart_required,
        "bootstrap": bootstrap,
    }
    if minimum is not None:
        value["minimum"] = minimum
    if maximum is not None:
        value["maximum"] = maximum
    if options:
        value["options"] = list(options)
    return value


CONFIG_SECTIONS: tuple[dict[str, object], ...] = (
    {
        "id": "api",
        "label": "API",
        "description": "Bootstrap và giới hạn vận hành của HTTP API.",
        "fields": (
            _field("SDLC_API_HOST", "API host", "string", "127.0.0.1", "Địa chỉ bind khi khởi động API.", editable=False, restart_required=True, bootstrap=True),
            _field("SDLC_API_PORT", "API port", "integer", 7788, "Cổng HTTP khi khởi động API.", minimum=1, maximum=65535, editable=False, restart_required=True, bootstrap=True),
            _field("SDLC_API_TOKEN", "API token", "secret", "", "Bearer token bảo vệ API.", secret=True, editable=False, restart_required=True, bootstrap=True),
            _field("SDLC_RUNTIME_CONFIG_FILE", "Runtime config file", "path", ".runtime/config.json", "Vị trí file cấu hình runtime.", editable=False, restart_required=True, bootstrap=True),
            _field("SDLC_API_QUEUE_LIMIT", "Queue limit", "integer", 32, "Số operation tối đa đang chờ.", minimum=1, restart_required=True),
            _field("SDLC_API_SHUTDOWN_TIMEOUT_SECONDS", "Shutdown timeout", "integer", 30, "Số giây tối đa chờ worker dừng.", minimum=1, restart_required=True),
        ),
    },
    {
        "id": "llm",
        "label": "LLM",
        "description": "Provider tương thích OpenAI và ngân sách context/output.",
        "fields": (
            _field("SDLC_LLM_MODE", "LLM mode", "enum", "fake", "Chọn fake hoặc provider tương thích OpenAI.", options=("fake", "deepseek", "openai_compatible")),
            _field("SDLC_LLM_API_KEY", "LLM API key", "secret", "", "Secret dùng để gọi model.", secret=True),
            _field("SDLC_LLM_BASE_URL", "Base URL", "string", "https://api.deepseek.com", "URL gốc, không gồm /chat/completions."),
            _field("SDLC_LLM_MODEL", "Model", "string", "deepseek-flash", "Tên model gửi trong request."),
            _field(
                "SDLC_LLM_THINKING_MODE",
                "Thinking mode",
                "enum",
                "disabled",
                "Chế độ suy luận của DeepSeek; tắt mặc định để giữ hành vi non-thinking cũ.",
                options=("disabled", "enabled"),
            ),
            _field("SDLC_LLM_TIMEOUT_SECONDS", "Timeout", "integer", 90, "Timeout mỗi request LLM, tính bằng giây.", minimum=1),
            _field("SDLC_LLM_STRUCTURED_MAX_TOKENS", "Structured max tokens", "integer", 8192, "Giới hạn output cho response có cấu trúc.", minimum=1024),
            _field("SDLC_LLM_CONTEXT_WINDOW_TOKENS", "Context window", "integer", 128000, "Cửa sổ context của model.", minimum=8192),
        ),
    },
    {
        "id": "validation",
        "label": "Developer & Validation",
        "description": "Giới hạn action, repair và semantic validation.",
        "fields": (
            _field("SDLC_REQUIREMENT_MAX_TOOL_ACTIONS", "Requirement tool actions", "integer", 12, "Số action tối đa cho Requirement source loop.", minimum=3),
            _field("SDLC_REQUIREMENT_MAX_ACTION_RETRIES", "Requirement action retries", "integer", 2, "Số lần thử lại action Requirement sai hoặc bị cắt.", minimum=0),
            _field("SDLC_DEVELOPER_MAX_ACTIONS_PER_ITEM", "Actions per item", "integer", 24, "Số action tối đa cho mỗi work item.", minimum=1),
            _field("SDLC_DEVELOPER_MAX_ACTION_RETRIES", "Action retries", "integer", 2, "Số lần thử lại action lỗi hoặc bị cắt.", minimum=0),
            _field("SDLC_DESIGN_REPAIR_LIMIT", "Design repair limit", "integer", 2, "Số vòng Designer sửa design.", minimum=0),
            _field("SDLC_DESIGN_VALIDATION_TRIALS", "Design validation trials", "integer", 3, "Số lượt semantic validator chấm cùng design.", minimum=1),
            _field("SDLC_DESIGN_LLM_CALL_BUDGET", "Design LLM call budget", "integer", 16, "Ngân sách call LLM cho design validation.", minimum=1),
            _field("SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT", "Plan repair limit", "integer", 2, "Số vòng sửa implementation plan.", minimum=0),
            _field("SDLC_IMPLEMENTATION_REPAIR_LIMIT", "Implementation repair limit", "integer", 2, "Số vòng Developer, Verification và Validator.", minimum=0),
            _field("SDLC_IMPLEMENTATION_VALIDATION_TRIALS", "Implementation validation trials", "integer", 3, "Số semantic trial của Code Validator.", minimum=2),
            _field("SDLC_IMPLEMENTATION_VALIDATION_CALL_BUDGET", "Implementation call budget", "integer", 6, "Ngân sách call cho Code Validator.", minimum=2),
            _field("SDLC_QA_REPAIR_LIMIT", "QA repair limit", "integer", 1, "Số vòng sửa code sau QA.", minimum=0),
        ),
    },
    {
        "id": "target",
        "label": "Target repository",
        "description": "Repository đích, worktree và quy ước branch.",
        "fields": (
            _field(
                "SDLC_PROJECT_PROFILE",
                "Project profile",
                "enum",
                "typescript_fullstack_v1",
                "Profile bắt buộc cho run mới; TypeScript full-stack là target hiện tại.",
                options=_PROJECT_PROFILE_OPTIONS,
            ),
            _field("SDLC_TARGET_REPO_PATH", "Repository path", "path", "../SDLC_Target_TypeScript", "Đường dẫn repository nhận code."),
            _field("SDLC_TARGET_WORKTREE_ROOT", "Worktree root", "path", "", "Để trống để dùng thư mục sibling mặc định."),
            _field("SDLC_TARGET_REPO_REMOTE", "Remote URL", "string", "https://github.com/LuongAnh1/SDLC_Target_TypeScript.git", "Remote dùng để chuẩn bị và kiểm tra repository."),
            _field("SDLC_TARGET_BASE_BRANCH", "Base branch", "string", "main", "Nhánh gốc tạo worktree."),
            _field("SDLC_IMPLEMENTATION_BRANCH_PREFIX", "Branch prefix", "string", "feature", "Prefix của branch triển khai."),
        ),
    },
    {
        "id": "sync",
        "label": "Sync",
        "description": "Jira, Confluence và chế độ ghi ra công cụ cộng tác.",
        "fields": (
            _field("SDLC_SYNC_MODE", "Default sync mode", "enum", "mock", "Mặc định an toàn cho CLI; API action vẫn mặc định mock.", options=("mock", "real", "both")),
            _field("ATLASSIAN_BASE_URL", "Atlassian base URL", "string", "", "URL gốc Atlassian Cloud."),
            _field("ATLASSIAN_EMAIL", "Atlassian email", "string", "", "Email của tài khoản API."),
            _field("ATLASSIAN_API_TOKEN", "Atlassian API token", "secret", "", "Secret dùng cho Jira và Confluence.", secret=True),
            _field("JIRA_SYNC_ENABLED", "Enable Jira sync", "boolean", False, "Cho phép adapter Jira chạy khi sync mode yêu cầu."),
            _field("JIRA_SYNC_ISSUE_KEY", "Jira issue key", "string", "", "Issue nhận comment/checkpoint mặc định."),
            _field("CONFLUENCE_SYNC_ENABLED", "Enable Confluence sync", "boolean", False, "Cho phép adapter Confluence chạy khi sync mode yêu cầu."),
            _field("CONFLUENCE_SYNC_SPACE_ID", "Confluence space ID", "string", "", "ID của space đích."),
            _field("CONFLUENCE_SYNC_PARENT_PAGE_ID", "Confluence parent page ID", "string", "", "Page ID cha chứa artifact."),
        ),
    },
)


def config_field_index() -> dict[str, dict[str, object]]:
    return {
        str(field["name"]): field
        for section in CONFIG_SECTIONS
        for field in section["fields"]  # type: ignore[index]
    }


def config_section_index() -> dict[str, dict[str, object]]:
    return {str(section["id"]): section for section in CONFIG_SECTIONS}


def serialize_config_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def normalize_config_value(field: dict[str, object], value: object) -> object:
    name = str(field["name"])
    kind = str(field["kind"])
    if kind == "integer":
        if isinstance(value, bool):
            raise HarnessError("config_invalid", f"{name} must be an integer.")
        try:
            normalized = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise HarnessError("config_invalid", f"{name} must be an integer.") from exc
        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if minimum is not None and normalized < int(minimum):
            raise HarnessError("config_invalid", f"{name} must be >= {minimum}.")
        if maximum is not None and normalized > int(maximum):
            raise HarnessError("config_invalid", f"{name} must be <= {maximum}.")
        return normalized
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise HarnessError("config_invalid", f"{name} must be a boolean.")
    if value is None:
        raise HarnessError("config_invalid", f"{name} must be a string.")
    normalized = str(value).strip()
    if kind == "enum":
        options = tuple(str(item) for item in field.get("options", ()))
        normalized = normalized.lower()
        if normalized not in options:
            raise HarnessError("config_invalid", f"{name} must be one of: {', '.join(options)}.")
    return normalized


def typed_config_value(field: dict[str, object], value: str) -> object:
    return normalize_config_value(field, value)
