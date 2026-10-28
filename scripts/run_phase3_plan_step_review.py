"""Run or resume Phase 3 with an operator checkpoint after every LLM response."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.workflows.phase3 import PhaseThreeWorkflow


def main() -> int:
    parser = argparse.ArgumentParser(description="Run controlled Phase 3 plan review with DeepSeek.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--worktree-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--checkpoint-id")
    parser.add_argument("--review-call-id")
    parser.add_argument("--review-decision", choices=("approve", "reject"))
    parser.add_argument("--review-note", default="")
    parser.add_argument("--repair-limit", type=int, default=3)
    args = parser.parse_args()
    if bool(args.review_call_id) != bool(args.review_decision):
        raise HarnessError(
            "phase3_step_review_args_invalid",
            "--review-call-id and --review-decision must be supplied together.",
        )
    if args.review_decision == "reject" and not str(args.review_note).strip():
        raise HarnessError(
            "phase3_step_review_args_invalid",
            "A rejected exchange requires --review-note.",
        )

    runtime_root = Path(args.runtime_root).resolve()
    target_repo = Path(args.target_repo).resolve()
    worktree_root = Path(args.worktree_root).resolve()
    base = load_settings(root=ROOT)
    if not base.llm_api_key:
        raise HarnessError("phase3_step_review_missing_key", "Configured SDLC_LLM_API_KEY is required.")
    settings = load_settings(
        root=ROOT,
        runtime_root=runtime_root,
        overrides={
            "SDLC_LLM_MODE": "deepseek",
            "SDLC_LLM_API_KEY": base.llm_api_key,
            "SDLC_LLM_BASE_URL": base.llm_base_url,
            "SDLC_LLM_MODEL": base.llm_model,
            "SDLC_LLM_THINKING_MODE": base.llm_thinking_mode,
            "SDLC_LLM_TIMEOUT_SECONDS": str(base.llm_timeout_seconds),
            "SDLC_LLM_STRUCTURED_MAX_TOKENS": str(base.llm_structured_max_tokens),
            "SDLC_LLM_CONTEXT_WINDOW_TOKENS": str(base.llm_context_window_tokens),
            "SDLC_SYNC_MODE": "mock",
            "SDLC_IMPLEMENTATION_PLAN_REPAIR_LIMIT": str(max(0, args.repair_limit)),
            "SDLC_TARGET_REPO_PATH": str(target_repo),
            "SDLC_TARGET_REPO_REMOTE": _git(target_repo, "remote", "get-url", "origin"),
            "SDLC_TARGET_BASE_BRANCH": "main",
            "SDLC_TARGET_WORKTREE_ROOT": str(worktree_root),
            "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
        },
    )
    workflow = PhaseThreeWorkflow(
        settings,
        plan_step_review={
            "enabled": True,
            "call_id": str(args.review_call_id or ""),
            "decision": str(args.review_decision or ""),
            "note": str(args.review_note or ""),
        },
    )
    result = (
        workflow.continue_implementation_plan_step_review(run_id=args.run_id)
        if args.review_call_id
        else workflow.approve_design_checkpoint(
            run_id=args.run_id,
            checkpoint_id=args.checkpoint_id,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if str(result.get("status") or "") in {
        "implementation_plan_step_review_required",
        "waiting_implementation_plan_review",
    } else 2


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise HarnessError("phase3_step_review_git_failed", process.stderr.strip() or "git failed")
    return process.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
