"""Run or resume Phase 1 with operator review around every LLM call."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.step_review import LlmStepReviewRequired
from sdlc_harness.workflows.phase1 import PhaseOneWorkflow


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run controlled Phase 1 and inspect each DeepSeek input/output."
    )
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--dataset")
    parser.add_argument("--use-case")
    parser.add_argument("--requested-by", default="operator")
    parser.add_argument(
        "--review-input",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pause before each provider call (default: enabled).",
    )
    parser.add_argument(
        "--review-output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pause after each provider response (default: enabled).",
    )
    parser.add_argument("--review-call-id")
    parser.add_argument("--review-decision", choices=("approve", "reject"))
    parser.add_argument("--review-note", default="")
    parser.add_argument("--answer-question-id", help="With --run-id: answer this clarification question.")
    parser.add_argument("--answer", default="", help="Answer text or option key for --answer-question-id.")
    parser.add_argument("--answered-by", default="owner")
    parser.add_argument(
        "--markdown-answers",
        help='With --run-id: one JSON object {"Q-1": "a", ...} answering the OpenCode Markdown questions; '
        "a question left out takes its recommended option.",
    )
    args = parser.parse_args()
    if args.answer_question_id and (not args.run_id or not str(args.answer).strip()):
        raise HarnessError(
            "phase1_step_review_args_invalid",
            "--answer-question-id needs --run-id and a non-empty --answer.",
        )

    if bool(args.run_id) == bool(args.dataset or args.use_case):
        raise HarnessError(
            "phase1_step_review_args_invalid",
            "Choose either --run-id or both --dataset and --use-case.",
        )
    if not args.run_id and (not args.dataset or not args.use_case):
        raise HarnessError(
            "phase1_step_review_args_invalid",
            "A new run requires both --dataset and --use-case.",
        )
    if not args.review_input and not args.review_output:
        raise HarnessError(
            "phase1_step_review_args_invalid",
            "Enable at least one of --review-input or --review-output.",
        )
    if bool(args.review_call_id) != bool(args.review_decision):
        raise HarnessError(
            "phase1_step_review_args_invalid",
            "--review-call-id and --review-decision must be supplied together.",
        )
    if args.review_decision == "reject" and not str(args.review_note).strip():
        raise HarnessError(
            "phase1_step_review_args_invalid",
            "A rejected exchange requires --review-note.",
        )

    base = load_settings(root=ROOT)
    if not base.llm_api_key:
        raise HarnessError(
            "phase1_step_review_missing_key",
            "Configured SDLC_LLM_API_KEY is required.",
        )
    profile_overrides: dict[str, str] = {}
    for profile_name, profile in base.phase1_llm_profiles.items():
        prefix = f"SDLC_PHASE1_{profile_name.upper()}"
        profile_overrides.update(
            {
                f"{prefix}_MODEL": profile.model,
                f"{prefix}_THINKING_EFFORT": profile.thinking_effort,
                f"{prefix}_MAX_TOKENS": str(profile.max_tokens),
                f"{prefix}_TIMEOUT_SECONDS": str(profile.timeout_seconds),
            }
        )
    settings = load_settings(
        root=ROOT,
        runtime_root=Path(args.runtime_root).resolve(),
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
            "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
            **profile_overrides,
        },
    )
    workflow = PhaseOneWorkflow(
        settings,
        requirement_step_review={
            "enabled": True,
            "review_input": bool(args.review_input),
            "review_output": bool(args.review_output),
            "call_id": str(args.review_call_id or ""),
            "decision": str(args.review_decision or ""),
            "note": str(args.review_note or ""),
        },
    )
    if args.markdown_answers:
        if not args.run_id:
            raise HarnessError("phase1_step_review_args_invalid", "--markdown-answers needs --run-id.")
        answers = json.loads(args.markdown_answers)
        if not isinstance(answers, dict):
            raise HarnessError("phase1_step_review_args_invalid", "--markdown-answers must be a JSON object.")
        result = workflow.answer_markdown_questions(
            run_id=str(args.run_id),
            answers={str(key): str(value) for key, value in answers.items()},
            answered_by=str(args.answered_by),
        )
    elif args.answer_question_id:
        try:
            result = workflow.answer_clarification(
                run_id=str(args.run_id),
                question_id=str(args.answer_question_id),
                answer=str(args.answer),
                answered_by=str(args.answered_by),
            )
        except LlmStepReviewRequired as exc:
            # Nothing is written before the update call is approved: rerun with the same answer to resume.
            print(json.dumps({"status": "clarification_update_paused", "pending_step_review": exc.progress}, indent=2))
            return 0
    else:
        result = (
            workflow.continue_requirement_run(run_id=str(args.run_id))
            if args.run_id
            else workflow.start_or_get_run(
                dataset=str(args.dataset),
                use_case=str(args.use_case),
                requested_by=str(args.requested_by),
            )
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if str(result.get("status") or "") in {
        "requirement_paused",
        "waiting_requirement_clarification",
        "waiting_requirement_approval",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
