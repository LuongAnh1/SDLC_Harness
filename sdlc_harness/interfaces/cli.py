"""CLI JSON cho Niko gọi SDLC Harness bằng subprocess.

Quy tắc quan trọng: stdout chỉ có JSON. Nếu cần debug, ghi stderr/log file sau;
không trộn text tự do vào stdout vì Niko sẽ parse trực tiếp response này.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlc-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start-or-get-run")
    start.add_argument("--dataset", required=True)
    start.add_argument("--use-case", required=True)
    start.add_argument("--requested-by", required=True)
    start.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    start.add_argument("--runtime-root", default=None)

    start_jira = subparsers.add_parser("start-from-jira")
    start_jira.add_argument("--issue-key", required=True)
    start_jira.add_argument("--requested-by", required=True)
    start_jira.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    start_jira.add_argument("--runtime-root", default=None)

    status = subparsers.add_parser("get-status")
    status.add_argument("--run-id", required=True)
    status.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    status.add_argument("--runtime-root", default=None)

    sync = subparsers.add_parser("sync-run")
    sync.add_argument("--run-id", required=True)
    sync.add_argument("--runtime-root", default=None)

    approve = subparsers.add_parser("approve-checkpoint")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--checkpoint-id", default=None)
    approve.add_argument("--llm-mode", choices=["fake", "deepseek", "openai_compatible"], default=None)
    approve.add_argument("--design-repair-limit", default=None)
    approve.add_argument("--runtime-root", default=None)
    return parser


def _settings_from_args(args: argparse.Namespace):
    runtime_root = Path(args.runtime_root) if getattr(args, "runtime_root", None) else None
    overrides = {}
    if getattr(args, "llm_mode", None):
        overrides["SDLC_LLM_MODE"] = args.llm_mode
    if getattr(args, "design_repair_limit", None) is not None:
        overrides["SDLC_DESIGN_REPAIR_LIMIT"] = str(args.design_repair_limit)
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
        workflow = PhaseOneWorkflow(settings)
        return workflow.sync_run(run_id=args.run_id)
    if args.command == "approve-checkpoint":
        workflow = CheckpointApprovalWorkflow(settings)
        return workflow.approve_checkpoint(
            run_id=args.run_id,
            checkpoint_id=args.checkpoint_id,
        )
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

