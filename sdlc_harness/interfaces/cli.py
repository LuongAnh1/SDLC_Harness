"""CLI JSON cho Niko gọi SDLC Harness bằng subprocess.

Quy tắc quan trọng: stdout chỉ có JSON. Nếu cần debug, ghi stderr/log file sau;
không trộn text tự do vào stdout vì Niko sẽ parse trực tiếp response này. CLI
hiện bao phủ start/status/sync/approve/continue, workspace adoption/cleanup và
đường vận hành `revalidate-implementation` cho Phase 4 repair-limit runs. Các
lệnh có thể ghi tool cộng tác mặc định `--sync-mode mock`; real sync phải được
caller chọn rõ.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..core.config import load_settings, repo_root
from ..core.errors import HarnessError
from ..workflows.approval import CheckpointApprovalWorkflow
from ..workflows.phase1 import PhaseOneWorkflow
from ..workflows.phase4 import PhaseFourWorkflow
from ..workflows.target_repo import TargetRepoPreparationWorkflow
from ..workflows.workspace_adoption import WorkspaceAdoptionWorkflow
from ..workflows.workspace_cleanup import WorkspaceCleanupWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlc-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start-or-get-run")
    start.add_argument("--dataset", required=True)
    start.add_argument("--use-case", required=True)
    start.add_argument("--requested-by", required=True)
    start.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    start.add_argument("--requirement-contract-version", choices=["v1", "v2"], default=None)
    _add_sync_mode_argument(start)
    start.add_argument("--runtime-root", default=None)

    start_jira = subparsers.add_parser("start-from-jira")
    start_jira.add_argument("--issue-key", required=True)
    start_jira.add_argument("--requested-by", required=True)
    start_jira.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    start_jira.add_argument("--requirement-contract-version", choices=["v1", "v2"], default=None)
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
    _add_sync_mode_argument(continue_run)
    continue_run.add_argument("--runtime-root", default=None)

    revalidate = subparsers.add_parser("revalidate-implementation")
    revalidate.add_argument("--run-id", required=True)
    revalidate.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    revalidate.add_argument("--implementation-repair-limit", default=None)
    revalidate.add_argument("--runtime-root", default=None)

    approve = subparsers.add_parser("approve-checkpoint")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--checkpoint-id", default=None)
    approve.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    approve.add_argument("--design-repair-limit", default=None)
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


def _settings_from_args(args: argparse.Namespace):
    runtime_root = Path(args.runtime_root) if getattr(args, "runtime_root", None) else None
    overrides = {}
    if getattr(args, "llm_mode", None):
        overrides["SDLC_LLM_MODE"] = args.llm_mode
    if getattr(args, "requirement_contract_version", None):
        overrides["SDLC_REQUIREMENT_CONTRACT_VERSION"] = str(args.requirement_contract_version)
    if getattr(args, "design_repair_limit", None) is not None:
        overrides["SDLC_DESIGN_REPAIR_LIMIT"] = str(args.design_repair_limit)
    if getattr(args, "implementation_plan_repair_limit", None) is not None:
        overrides["SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT"] = str(args.implementation_plan_repair_limit)
    if getattr(args, "implementation_repair_limit", None) is not None:
        overrides["SDLC_IMPLEMENTATION_REPAIR_LIMIT"] = str(args.implementation_repair_limit)
    if getattr(args, "qa_repair_limit", None) is not None:
        overrides["SDLC_QA_REPAIR_LIMIT"] = str(args.qa_repair_limit)
    if getattr(args, "sync_mode", None):
        overrides["SDLC_SYNC_MODE"] = str(args.sync_mode)
    if getattr(args, "target_repo", None):
        overrides["SDLC_TARGET_REPO_PATH"] = str(args.target_repo)
    if getattr(args, "target_remote", None):
        overrides["SDLC_TARGET_REPO_REMOTE"] = str(args.target_remote)
    return load_settings(root=repo_root(), runtime_root=runtime_root, overrides=overrides)


def run(args: argparse.Namespace) -> dict[str, object]:
    settings = _settings_from_args(args)
    if args.command == "start-or-get-run":
        workflow = PhaseOneWorkflow(settings)
        return workflow.start_or_get_run(
            dataset=args.dataset,
            use_case=args.use_case,
            requested_by=args.requested_by,
        )
    if args.command == "start-from-jira":
        workflow = PhaseOneWorkflow(settings)
        return workflow.start_from_jira(
            issue_key=args.issue_key,
            requested_by=args.requested_by,
        )
    if args.command == "get-status":
        workflow = PhaseOneWorkflow(settings)
        return workflow.get_status(run_id=args.run_id)
    if args.command == "sync-run":
        phase_one = PhaseOneWorkflow(settings)
        state = phase_one.store.read_state(args.run_id)
        if str(state.get("status") or "").startswith("code_review"):
            return PhaseFourWorkflow(settings).sync_code_review(run_id=args.run_id)
        return phase_one.sync_run(run_id=args.run_id)
    if args.command == "continue-run":
        workflow = PhaseFourWorkflow(settings)
        return workflow.continue_run(run_id=args.run_id)
    if args.command == "revalidate-implementation":
        workflow = PhaseFourWorkflow(settings)
        return workflow.revalidate_implementation(run_id=args.run_id)
    if args.command == "approve-checkpoint":
        workflow = CheckpointApprovalWorkflow(settings)
        return workflow.approve_checkpoint(
            run_id=args.run_id,
            checkpoint_id=args.checkpoint_id,
        )
    if args.command == "answer-clarification":
        return PhaseOneWorkflow(settings).answer_clarification(
            run_id=args.run_id,
            question_id=args.question_id,
            answer=args.answer,
            answered_by=args.answered_by,
        )
    if args.command == "prepare-target-repo":
        workflow = TargetRepoPreparationWorkflow(settings)
        return workflow.prepare(init_codegraph=bool(args.init_codegraph))
    if args.command == "adopt-run-workspace":
        return WorkspaceAdoptionWorkflow(settings).adopt(run_id=args.run_id)
    if args.command == "cleanup-run-workspace":
        return WorkspaceCleanupWorkflow(settings).cleanup(run_id=args.run_id)
    raise HarnessError("unknown_command", f"Unknown command: {args.command}")


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

