"""Schema và quy tắc xác thực cấu hình runtime do dashboard quản lý."""

from __future__ import annotations

from typing import Any

from ..profiles import list_project_profile_ids
from .errors import HarnessError


MASKED_SECRET_VALUE = "********"
_PROJECT_PROFILE_OPTIONS = list_project_profile_ids()
PHASE1_LLM_PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    "refiner_list": {"label": "Refiner source list", "effort": "none", "max_tokens": 2048, "timeout": 90},
    "refiner_navigation": {"label": "Refiner source navigation", "effort": "low", "max_tokens": 4096, "timeout": 90},
    "refiner_synthesis": {"label": "Refiner synthesis", "effort": "high", "max_tokens": 32768, "timeout": 180},
    "refiner_repair": {"label": "Refiner repair", "effort": "high", "max_tokens": 32768, "timeout": 180},
    "refiner_final_repair": {"label": "Refiner final repair", "effort": "max", "max_tokens": 65536, "timeout": 300},
    "coverage": {"label": "Requirement coverage", "effort": "high", "max_tokens": 24576, "timeout": 180},
    "clarification": {"label": "Clarification update", "effort": "high", "max_tokens": 16384, "timeout": 180},
    "clarification_repair": {"label": "Clarification repair", "effort": "max", "max_tokens": 65536, "timeout": 300},
}
PHASE2_LLM_PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    "designer_investigation": {
        "label": "Designer investigation",
        "effort": "low",
        "max_tokens": 8192,
        "timeout": 180,
    },
    "designer_delta": {
        "label": "Designer graph delta",
        "effort": "high",
        "max_tokens": 32768,
        "timeout": 300,
    },
    "designer_repair": {
        "label": "Designer repair",
        "effort": "max",
        "max_tokens": 65536,
        "timeout": 600,
    },
    "design_validator": {
        "label": "Design semantic validator",
        "effort": "high",
        "max_tokens": 32768,
        "timeout": 300,
    },
}
PHASE3_LLM_PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    "developer_plan": {
        "label": "Implementation plan developer",
        "effort": "high",
        "max_tokens": 49152,
        "timeout": 420,
    },
    "plan_validator": {
        "label": "Implementation plan semantic validator",
        "effort": "high",
        "max_tokens": 32768,
        "timeout": 300,
    },
    "developer_repair": {
        "label": "Implementation plan developer repair",
        "effort": "max",
        "max_tokens": 65536,
        "timeout": 600,
    },
}
PHASE4_LLM_PROFILE_DEFAULTS: dict[str, dict[str, object]] = {
    "developer_action": {
        "label": "Developer action",
        "model": "deepseek-flash",
        "effort": "high",
        "max_tokens": 32768,
        "timeout": 600,
    },
    "developer_repair": {
        "label": "Developer repair action",
        "model": "deepseek-flash",
        "effort": "max",
        "max_tokens": 32768,
        "timeout": 600,
    },
}


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


def _phase1_llm_fields() -> tuple[dict[str, object], ...]:
    fields: list[dict[str, object]] = []
    for profile_name, defaults in PHASE1_LLM_PROFILE_DEFAULTS.items():
        prefix = f"SDLC_PHASE1_{profile_name.upper()}"
        label = str(defaults["label"])
        fields.extend(
            (
                _field(
                    f"{prefix}_MODEL",
                    f"{label} model",
                    "string",
                    "",
                    "Để trống để dùng SDLC_LLM_MODEL; có thể chọn model riêng cho call này.",
                ),
                _field(
                    f"{prefix}_THINKING_EFFORT",
                    f"{label} thinking",
                    "enum",
                    defaults["effort"],
                    "Mức reasoning riêng cho call Phase 1.",
                    options=("none", "low", "high", "max"),
                ),
                _field(
                    f"{prefix}_MAX_TOKENS",
                    f"{label} max tokens",
                    "integer",
                    defaults["max_tokens"],
                    "Trần output gồm cả reasoning và JSON cuối.",
                    minimum=1024,
                    maximum=384000,
                ),
                _field(
                    f"{prefix}_TIMEOUT_SECONDS",
                    f"{label} timeout",
                    "integer",
                    defaults["timeout"],
                    "Timeout riêng cho call Phase 1, tính bằng giây.",
                    minimum=1,
                    maximum=1800,
                ),
            )
        )
    return tuple(fields)


def _phase2_llm_fields() -> tuple[dict[str, object], ...]:
    fields: list[dict[str, object]] = []
    for profile_name, defaults in PHASE2_LLM_PROFILE_DEFAULTS.items():
        prefix = f"SDLC_PHASE2_{profile_name.upper()}"
        label = str(defaults["label"])
        fields.extend(
            (
                _field(
                    f"{prefix}_MODEL",
                    f"{label} model",
                    "string",
                    "",
                    "Để trống để dùng SDLC_LLM_MODEL; có thể chọn model riêng cho call này.",
                ),
                _field(
                    f"{prefix}_THINKING_EFFORT",
                    f"{label} thinking",
                    "enum",
                    defaults["effort"],
                    "Mức reasoning riêng cho call Phase 2.",
                    options=("none", "low", "high", "max"),
                ),
                _field(
                    f"{prefix}_MAX_TOKENS",
                    f"{label} max tokens",
                    "integer",
                    defaults["max_tokens"],
                    "Trần output gồm cả reasoning và JSON cuối.",
                    minimum=1024,
                    maximum=384000,
                ),
                _field(
                    f"{prefix}_TIMEOUT_SECONDS",
                    f"{label} timeout",
                    "integer",
                    defaults["timeout"],
                    "Timeout riêng cho call Phase 2, tính bằng giây.",
                    minimum=1,
                    maximum=1800,
                ),
            )
        )
    return tuple(fields)


def _phase3_llm_fields() -> tuple[dict[str, object], ...]:
    fields: list[dict[str, object]] = []
    for profile_name, defaults in PHASE3_LLM_PROFILE_DEFAULTS.items():
        prefix = f"SDLC_PHASE3_{profile_name.upper()}"
        label = str(defaults["label"])
        fields.extend(
            (
                _field(
                    f"{prefix}_MODEL",
                    f"{label} model",
                    "string",
                    "",
                    "Để trống để dùng SDLC_LLM_MODEL; có thể chọn model riêng cho call này.",
                ),
                _field(
                    f"{prefix}_THINKING_EFFORT",
                    f"{label} thinking",
                    "enum",
                    defaults["effort"],
                    "Mức reasoning riêng cho call Phase 3.",
                    options=("none", "low", "high", "max"),
                ),
                _field(
                    f"{prefix}_MAX_TOKENS",
                    f"{label} max tokens",
                    "integer",
                    defaults["max_tokens"],
                    "Trần output gồm cả reasoning và JSON cuối.",
                    minimum=1024,
                    maximum=384000,
                ),
                _field(
                    f"{prefix}_TIMEOUT_SECONDS",
                    f"{label} timeout",
                    "integer",
                    defaults["timeout"],
                    "Timeout riêng cho call Phase 3, tính bằng giây.",
                    minimum=1,
                    maximum=1800,
                ),
            )
        )
    return tuple(fields)


def _phase4_llm_fields() -> tuple[dict[str, object], ...]:
    fields: list[dict[str, object]] = []
    for profile_name, defaults in PHASE4_LLM_PROFILE_DEFAULTS.items():
        prefix = f"SDLC_PHASE4_{profile_name.upper()}"
        label = str(defaults["label"])
        fields.extend(
            (
                _field(
                    f"{prefix}_MODEL",
                    f"{label} model",
                    "string",
                    defaults["model"],
                    "Model riêng cho call Developer Phase 4; có thể cấu hình lại ở runtime.",
                ),
                _field(
                    f"{prefix}_THINKING_EFFORT",
                    f"{label} thinking",
                    "enum",
                    defaults["effort"],
                    "Mức reasoning riêng cho call Developer Phase 4.",
                    options=("none", "low", "high", "max"),
                ),
                _field(
                    f"{prefix}_MAX_TOKENS",
                    f"{label} max tokens",
                    "integer",
                    defaults["max_tokens"],
                    "Trần output gồm cả reasoning và JSON action cuối.",
                    minimum=1024,
                    maximum=384000,
                ),
                _field(
                    f"{prefix}_TIMEOUT_SECONDS",
                    f"{label} timeout",
                    "integer",
                    defaults["timeout"],
                    "Timeout riêng cho call Developer Phase 4, tính bằng giây.",
                    minimum=1,
                    maximum=1800,
                ),
            )
        )
    return tuple(fields)


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
            _field("SDLC_DEVELOPER_VERIFICATION_REPAIR_ACTION_RESERVE", "Verification repair reserve", "integer", 6, "Số action dự phòng được mở một lần sau khi verification phát hiện lỗi implementation.", minimum=0),
            _field("SDLC_DEVELOPER_MAX_ACTION_RETRIES", "Action retries", "integer", 2, "Số lần thử lại action lỗi hoặc bị cắt.", minimum=0),
            _field("SDLC_DEVELOPER_PACKAGE_TOOL_TIMEOUT_SECONDS", "Package tool timeout", "integer", 120, "Timeout cho npm workspace dependency tool.", minimum=1),
            _field("SDLC_DEVELOPER_WORKING_MEMORY_EVIDENCE_CHARS", "Working-memory evidence chars", "integer", 96000, "Ngân sách ký tự cho source evidence trong mỗi Developer input.", minimum=8000),
            _field("SDLC_DESIGN_REPAIR_LIMIT", "Design repair limit", "integer", 3, "Số vòng Designer sửa design.", minimum=0),
            _field("SDLC_DESIGN_VALIDATION_TRIALS", "Design validation trials", "integer", 3, "Số lượt semantic validator chấm cùng design.", minimum=1),
            _field("SDLC_DESIGN_LLM_CALL_BUDGET", "Design LLM call hard cap", "integer", 128, "Cầu dao tổng cho call LLM của Phase 2; action Designer còn được giới hạn theo policy bên dưới.", minimum=1),
            _field("SDLC_DESIGNER_PARTITION_ACTION_BUDGET", "Designer actions per partition", "integer", 8, "Số action tối đa cho mỗi scenario/component partition; partition mới bắt đầu bộ đếm riêng.", minimum=1),
            _field("SDLC_DESIGNER_SCENARIO_REPAIR_ACTION_BUDGET", "Designer repair actions per scenario", "integer", 4, "Số action dành riêng cho nối graph và repair của mỗi scenario.", minimum=0),
            _field("SDLC_DESIGNER_FINALIZATION_ACTION_BUDGET", "Designer finalization actions", "integer", 4, "Số action dự phòng để hoàn tất toàn bộ Design sau các partition.", minimum=0),
            _field("SDLC_DESIGNER_GLOBAL_ACTION_HARD_CAP", "Designer global action hard cap", "integer", 128, "Chặn trước khi chạy nếu ngân sách tính từ số partition/scenario vượt ngưỡng vận hành này.", minimum=1),
            _field(
                "SDLC_DESIGNER_CONTRACT_VERSION",
                "Designer contract",
                "enum",
                "vertical_design_graph_v1",
                "Pin V1 mặc định; chỉ bật V2 có chủ đích cho controlled L3.8 run trước cutover.",
                options=("vertical_design_graph_v1", "vertical_design_graph_v2"),
            ),
            _field("SDLC_DESIGN_STEP_REVIEW_ENABLED", "Pause Design LLM steps", "boolean", False, "Dừng Phase 2 tại từng input/output LLM để người vận hành kiểm tra."),
            _field("SDLC_DESIGN_STEP_REVIEW_INPUT_ENABLED", "Review Design LLM input", "boolean", True, "Dừng trước khi gửi từng input LLM của Phase 2."),
            _field("SDLC_DESIGN_STEP_REVIEW_OUTPUT_ENABLED", "Review Design LLM output", "boolean", True, "Dừng sau khi nhận từng output LLM của Phase 2."),
            _field(
                "SDLC_VERTICAL_TRACE_MODE",
                "Vertical trace mode",
                "enum",
                "enforce",
                "Shadow chỉ ghi nhận; enforce chặn checkpoint khi Requirement-to-Design trace không đạt.",
                options=("shadow", "enforce"),
            ),
            _field("SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT", "Plan repair limit", "integer", 2, "Số vòng sửa implementation plan.", minimum=0),
            _field("SDLC_IMPLEMENTATION_PLAN_STEP_REVIEW_ENABLED", "Pause implementation-plan LLM steps", "boolean", False, "Dừng Phase 3 tại từng input/output LLM để người vận hành kiểm tra."),
            _field("SDLC_IMPLEMENTATION_PLAN_STEP_REVIEW_INPUT_ENABLED", "Review implementation-plan LLM input", "boolean", True, "Dừng trước khi gửi từng input LLM của Phase 3."),
            _field("SDLC_IMPLEMENTATION_PLAN_STEP_REVIEW_OUTPUT_ENABLED", "Review implementation-plan LLM output", "boolean", True, "Dừng sau khi nhận từng output LLM của Phase 3."),
            _field("SDLC_IMPLEMENTATION_REPAIR_LIMIT", "Implementation repair limit", "integer", 2, "Số vòng Developer, Verification và Validator.", minimum=0),
            _field("SDLC_IMPLEMENTATION_VALIDATION_TRIALS", "Implementation validation trials", "integer", 3, "Số semantic trial của Code Validator.", minimum=2),
            _field("SDLC_IMPLEMENTATION_VALIDATION_CALL_BUDGET", "Implementation call budget", "integer", 6, "Ngân sách call cho Code Validator.", minimum=2),
            _field("SDLC_QA_REPAIR_LIMIT", "QA repair limit", "integer", 1, "Số vòng sửa code sau QA.", minimum=0),
        ),
    },
    {
        "id": "phase1_llm",
        "label": "Phase 1 LLM",
        "description": "Model, thinking effort, output budget và timeout theo từng loại call Requirement.",
        "fields": _phase1_llm_fields(),
    },
    {
        "id": "phase2_llm",
        "label": "Phase 2 LLM",
        "description": "Model, thinking effort, output budget và timeout theo từng loại call Design.",
        "fields": _phase2_llm_fields(),
    },
    {
        "id": "phase3_llm",
        "label": "Phase 3 LLM",
        "description": "Model, thinking effort, output budget và timeout theo từng loại call Implementation Plan.",
        "fields": _phase3_llm_fields(),
    },
    {
        "id": "phase4_llm",
        "label": "Phase 4 Developer LLM",
        "description": "Model, thinking effort, output budget và timeout cho Developer action và repair.",
        "fields": _phase4_llm_fields(),
    },
    {
        "id": "phase2_tools",
        "label": "Phase 2 tools",
        "description": "CodeGraph và UI/UX guidance cho repo-aware Designer.",
        "fields": (
            _field(
                "SDLC_UIUX_SKILL_MODE",
                "UI/UX skill mode",
                "enum",
                "off",
                "Off giữ hành vi cũ; enforce bắt buộc guidance cho UI partition của run mới.",
                options=("off", "enforce"),
            ),
            _field(
                "SDLC_UIUX_SKILL_ROOT",
                "UI/UX skill root",
                "path",
                "",
                "Thư mục chứa scripts/search.py của ui-ux-pro-max; không được công khai trong artifact.",
            ),
            _field(
                "SDLC_UIUX_SKILL_TIMEOUT_SECONDS",
                "UI/UX skill timeout",
                "integer",
                30,
                "Timeout cho một lượt search read-only.",
                minimum=1,
                maximum=300,
            ),
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
