"""CLI JSON độc lập cho operator, debug và automation của SDLC Harness.

Quy tắc quan trọng: stdout chỉ có JSON. Nếu cần debug, ghi stderr/log file sau;
không trộn text tự do vào stdout để giữ contract automation ổn định. Niko hiện
gọi HTTP API, không gọi module này. CLI bao phủ start/status/sync/approve/continue,
workspace adoption/cleanup và
đường vận hành `revalidate-implementation` cho Phase 4 repair-limit runs. Các
lệnh có thể ghi tool cộng tác mặc định `--sync-mode mock`; real sync phải được
caller chọn rõ.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..application import ApplicationCommand, ApplicationService
from ..core.config import repo_root
from ..core.errors import HarnessError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlc-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start-or-get-run")
    start.add_argument("--dataset", required=True)
    start.add_argument("--use-case", required=True)
    start.add_argument("--requested-by", required=True)
    start.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    _add_sync_mode_argument(start)
    start.add_argument("--runtime-root", default=None)

    start_jira = subparsers.add_parser("start-from-jira")
    start_jira.add_argument("--issue-key", required=True)
    start_jira.add_argument("--requested-by", required=True)
    start_jira.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    _add_sync_mode_argument(start_jira)
    start_jira.add_argument("--runtime-root", default=None)

    status = subparsers.add_parser("get-status")
    status.add_argument("--run-id", required=True)
    status.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    status.add_argument("--runtime-root", default=None)

    sync = subparsers.add_parser("sync-run")
    sync.add_argument("--run-id", required=True)
    _add_sync_mode_argument(sync)
    sync.add_argument("--runtime-root", default=None)

    continue_run = subparsers.add_parser("continue-run")
    continue_run.add_argument("--run-id", required=True)
    continue_run.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    continue_run.add_argument("--implementation-repair-limit", default=None)
    continue_run.add_argument("--qa-repair-limit", default=None)
    continue_run.add_argument("--step-review", action="store_true")
    continue_run.add_argument("--step-review-input", action="store_true")
    continue_run.add_argument("--step-review-output", action="store_true")
    continue_run.add_argument("--step-review-call-id", default=None)
    continue_run.add_argument("--step-review-decision", choices=["approve", "reject"], default=None)
    continue_run.add_argument("--step-review-note", default=None)
    _add_sync_mode_argument(continue_run)
    continue_run.add_argument("--runtime-root", default=None)

    revalidate = subparsers.add_parser("revalidate-implementation")
    revalidate.add_argument("--run-id", required=True)
    revalidate.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    revalidate.add_argument("--implementation-repair-limit", default=None)
    revalidate.add_argument("--runtime-root", default=None)

    revalidate_plan = subparsers.add_parser("revalidate-implementation-plan")
    revalidate_plan.add_argument("--run-id", required=True)
    revalidate_plan.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    revalidate_plan.add_argument("--implementation-plan-repair-limit", default=None)
    _add_sync_mode_argument(revalidate_plan)
    revalidate_plan.add_argument("--runtime-root", default=None)

    revalidate_design = subparsers.add_parser("revalidate-design")
    revalidate_design.add_argument("--run-id", required=True)
    revalidate_design.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    revalidate_design.add_argument("--design-repair-limit", default=None)
    revalidate_design.add_argument("--design-validation-trials", default=None)
    revalidate_design.add_argument("--design-llm-call-budget", default=None)
    _add_sync_mode_argument(revalidate_design)
    revalidate_design.add_argument("--runtime-root", default=None)

    restart_design = subparsers.add_parser("restart-design")
    restart_design.add_argument("--run-id", required=True)
    restart_design.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    restart_design.add_argument("--design-repair-limit", default=None)
    restart_design.add_argument("--design-validation-trials", default=None)
    restart_design.add_argument("--design-llm-call-budget", default=None)
    restart_design.add_argument(
        "--designer-contract-version",
        choices=["vertical_design_graph_v1", "vertical_design_graph_v2"],
        default=None,
    )
    restart_design.add_argument("--uiux-skill-mode", choices=["off", "enforce"], default=None)
    restart_design.add_argument("--uiux-skill-root", default=None)
    restart_design.add_argument("--uiux-skill-timeout-seconds", default=None)
    restart_design.add_argument("--vertical-trace-mode", choices=["shadow", "enforce"], default=None)
    _add_sync_mode_argument(restart_design)
    restart_design.add_argument("--runtime-root", default=None)

    revalidate_requirement = subparsers.add_parser("revalidate-requirement")
    revalidate_requirement.add_argument("--run-id", required=True)
    revalidate_requirement.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    _add_sync_mode_argument(revalidate_requirement)
    revalidate_requirement.add_argument("--runtime-root", default=None)

    clarify_more = subparsers.add_parser("clarify-more")
    clarify_more.add_argument("--run-id", required=True)
    clarify_more.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    _add_sync_mode_argument(clarify_more)
    clarify_more.add_argument("--runtime-root", default=None)

    approve = subparsers.add_parser("approve-checkpoint")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--checkpoint-id", default=None)
    approve.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    approve.add_argument("--design-repair-limit", default=None)
    approve.add_argument("--design-validation-trials", default=None)
    approve.add_argument("--design-llm-call-budget", default=None)
    approve.add_argument("--implementation-plan-repair-limit", default=None)
    approve.add_argument("--implementation-repair-limit", default=None)
    _add_sync_mode_argument(approve)
    approve.add_argument("--runtime-root", default=None)

    answer_clarification = subparsers.add_parser("answer-clarification")
    answer_clarification.add_argument("--run-id", required=True)
    answer_clarification.add_argument("--question-id", required=True)
    answer_clarification.add_argument("--answer", required=True)
    answer_clarification.add_argument("--answered-by", required=True)
    answer_clarification.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    _add_sync_mode_argument(answer_clarification)
    answer_clarification.add_argument("--runtime-root", default=None)

    prepare_target = subparsers.add_parser("prepare-target-repo")
    prepare_target.add_argument("--init-codegraph", action="store_true")
    prepare_target.add_argument("--target-repo", default=None)
    prepare_target.add_argument("--target-remote", default=None)
    prepare_target.add_argument("--runtime-root", default=None)

    adopt_workspace = subparsers.add_parser("adopt-run-workspace")
    adopt_workspace.add_argument("--run-id", required=True)
    adopt_workspace.add_argument("--runtime-root", default=None)

    cleanup_workspace = subparsers.add_parser("cleanup-run-workspace")
    cleanup_workspace.add_argument("--run-id", required=True)
    cleanup_workspace.add_argument("--runtime-root", default=None)
    return parser


def _add_sync_mode_argument(parser: argparse.ArgumentParser) -> None:
    """Các lệnh có side effect tool mặc định mock; real phải được chỉ rõ."""

    parser.add_argument("--sync-mode", choices=["mock", "real", "both"], default="mock")


_COMMAND_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "start-or-get-run": ("dataset", "use_case", "requested_by"),
    "start-from-jira": ("issue_key", "requested_by"),
    "get-status": ("run_id",),
    "sync-run": ("run_id",),
    "continue-run": (
        "run_id",
        "step_review",
        "step_review_input",
        "step_review_output",
        "step_review_call_id",
        "step_review_decision",
        "step_review_note",
    ),
    "revalidate-implementation": ("run_id",),
    "revalidate-implementation-plan": ("run_id",),
    "revalidate-design": ("run_id",),
    "restart-design": ("run_id",),
    "revalidate-requirement": ("run_id",),
    "clarify-more": ("run_id",),
    "approve-checkpoint": ("run_id", "checkpoint_id"),
    "answer-clarification": ("run_id", "question_id", "answer", "answered_by"),
    "prepare-target-repo": ("init_codegraph",),
    "adopt-run-workspace": ("run_id",),
    "cleanup-run-workspace": ("run_id",),
}

_SETTINGS_OVERRIDE_ARGUMENTS: dict[str, str] = {
    "llm_mode": "SDLC_LLM_MODE",
    "design_repair_limit": "SDLC_DESIGN_REPAIR_LIMIT",
    "design_validation_trials": "SDLC_DESIGN_VALIDATION_TRIALS",
    "design_llm_call_budget": "SDLC_DESIGN_LLM_CALL_BUDGET",
    "designer_contract_version": "SDLC_DESIGNER_CONTRACT_VERSION",
    "uiux_skill_mode": "SDLC_UIUX_SKILL_MODE",
    "uiux_skill_root": "SDLC_UIUX_SKILL_ROOT",
    "uiux_skill_timeout_seconds": "SDLC_UIUX_SKILL_TIMEOUT_SECONDS",
    "vertical_trace_mode": "SDLC_VERTICAL_TRACE_MODE",
    "implementation_plan_repair_limit": "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT",
    "implementation_repair_limit": "SDLC_IMPLEMENTATION_REPAIR_LIMIT",
    "qa_repair_limit": "SDLC_QA_REPAIR_LIMIT",
    "sync_mode": "SDLC_SYNC_MODE",
    "target_repo": "SDLC_TARGET_REPO_PATH",
    "target_remote": "SDLC_TARGET_REPO_REMOTE",
}


def command_from_args(args: argparse.Namespace) -> ApplicationCommand:
    """Chuyển argparse Namespace thành command không phụ thuộc transport."""

    runtime_value = getattr(args, "source_runtime_root", None) or getattr(args, "runtime_root", None)
    runtime_root = Path(runtime_value) if runtime_value else None
    argument_names = _COMMAND_ARGUMENTS.get(str(args.command), ())
    arguments = {name: getattr(args, name) for name in argument_names}
    overrides: dict[str, str] = {}
    for argument_name, setting_name in _SETTINGS_OVERRIDE_ARGUMENTS.items():
        value = getattr(args, argument_name, None)
        if value is not None:
            overrides[setting_name] = str(value)
    return ApplicationCommand(
        name=str(args.command),
        arguments=arguments,
        settings_overrides=overrides,
        runtime_root=runtime_root,
    )


def _settings_from_args(args: argparse.Namespace):
    """Compatibility helper; settings ownership now belongs to ApplicationService."""

    service = ApplicationService(root=repo_root())
    return service.settings_for(command_from_args(args))


def run(args: argparse.Namespace) -> dict[str, object]:
    """CLI adapter delegates the structured command to the application service."""

    return ApplicationService(root=repo_root()).execute(command_from_args(args))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = run(args)
        exit_code = 0
    except HarnessError as exc:
        payload = exc.to_response()
        exit_code = 1
    except Exception as exc:  # pragma: no cover - lớp bảo vệ cuối cùng cho CLI.
        payload = {"ok": False, "error_code": "internal_error", "message": str(exc)}
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

