"""Run an isolated L3 Designer shadow against a frozen local TypeScript clone.

The script seeds an approved Requirement V2, runs Phase 2 with the real
configured DeepSeek client, and forces mock sync. It never calls Jira,
Confluence, GitHub publication, Phase 3, or Phase 4.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sdlc_harness.agents.designer import DESIGNER_CONTRACT_STATE_KEY, GRAPH_VERSION
from sdlc_harness.agents.developer import (
    COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
    IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY,
)
from sdlc_harness.agents.requirements import normalize_and_validate_requirement_model_v2
from sdlc_harness.agents.validator import (
    VERTICAL_TRACE_GRAPH_VERSION,
    VERTICAL_TRACE_MODE_STATE_KEY,
    VERTICAL_TRACE_STATE_KEY,
)
from sdlc_harness.artifacts import write_requirement_v2_revision_artifacts, write_source_snapshot_v2
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_profile_snapshot
from sdlc_harness.sources.fixtures import UseCaseSource, source_provenance_entry
from sdlc_harness.storage.runtime import RuntimeStore, utc_now_iso
from sdlc_harness.workflows.phase2 import PhaseTwoWorkflow
from sdlc_harness.workflows.repo_discovery import (
    REPO_DISCOVERY_CONTRACT_STATE_KEY,
    REPO_DISCOVERY_CONTRACT_VERSION,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated L3 DeepSeek shadow with mock sync.")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--worktree-root", required=True)
    parser.add_argument("--approved-requirement")
    parser.add_argument("--source-file")
    parser.add_argument("--revalidate-run-id")
    parser.add_argument("--review-run-id")
    parser.add_argument("--step-review", action="store_true")
    parser.add_argument("--review-call-id")
    parser.add_argument("--review-decision", choices=("approve", "reject"))
    parser.add_argument("--review-note", default="")
    parser.add_argument("--design-call-budget", type=int, default=28)
    parser.add_argument("--design-repair-limit", type=int, default=3)
    parser.add_argument("--vertical-trace-mode", choices=("shadow", "enforce"), default="shadow")
    args = parser.parse_args()

    if args.revalidate_run_id and args.review_run_id:
        raise HarnessError("l3_shadow_args_invalid", "Choose either --revalidate-run-id or --review-run-id.")
    if args.review_run_id and not args.step_review:
        raise HarnessError("l3_shadow_args_invalid", "--review-run-id requires --step-review.")
    if bool(args.review_call_id) != bool(args.review_decision):
        raise HarnessError(
            "l3_shadow_args_invalid",
            "--review-call-id and --review-decision must be supplied together.",
        )
    if args.review_decision and not args.review_run_id:
        raise HarnessError("l3_shadow_args_invalid", "A review decision requires --review-run-id.")
    if args.review_decision == "reject" and not str(args.review_note).strip():
        raise HarnessError("l3_shadow_args_invalid", "A rejected call requires --review-note.")

    runtime_root = Path(args.runtime_root).resolve()
    target_repo = Path(args.target_repo).resolve()
    worktree_root = Path(args.worktree_root).resolve()
    _require_isolated_path(runtime_root, label="runtime root")
    _require_isolated_path(worktree_root, label="worktree root")
    remote = _git(target_repo, "remote", "get-url", "origin")

    base = load_settings(root=ROOT)
    if not base.llm_api_key:
        raise HarnessError("l3_shadow_missing_key", "Configured SDLC_LLM_API_KEY is required.")
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
            "SDLC_DESIGNER_LOOP_ENABLED": "1",
            "SDLC_DESIGNER_CONTRACT_VERSION": GRAPH_VERSION,
            "SDLC_VERTICAL_TRACE_MODE": args.vertical_trace_mode,
            "SDLC_DESIGN_VALIDATION_TRIALS": "1",
            "SDLC_DESIGN_LLM_CALL_BUDGET": str(max(4, args.design_call_budget)),
            "SDLC_DESIGN_REPAIR_LIMIT": str(max(0, args.design_repair_limit)),
            "SDLC_TARGET_REPO_PATH": str(target_repo),
            "SDLC_TARGET_REPO_REMOTE": remote,
            "SDLC_TARGET_BASE_BRANCH": "main",
            "SDLC_TARGET_WORKTREE_ROOT": str(worktree_root),
            "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
        },
    )

    store = RuntimeStore(repo_root=ROOT, runtime_root=runtime_root)
    run_id = str(
        args.review_run_id
        or args.revalidate_run_id
        or f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    )
    if not args.revalidate_run_id and not args.review_run_id:
        _seed_approved_requirement(
            store=store,
            settings=settings,
            run_id=run_id,
            approved_requirement=Path(args.approved_requirement).resolve() if args.approved_requirement else None,
            source_file=Path(args.source_file).resolve() if args.source_file else None,
            vertical_trace_mode=args.vertical_trace_mode,
        )
    elif args.review_run_id and args.step_review:
        _recover_missed_invalid_step_review(store=store, run_id=run_id)

    started = perf_counter()
    workflow = PhaseTwoWorkflow(settings, designer_step_review={
        "enabled": bool(args.step_review),
        "call_id": str(args.review_call_id or ""),
        "decision": str(args.review_decision or ""),
        "note": str(args.review_note or ""),
    })
    result = (
        workflow.continue_design(run_id=run_id)
        if args.review_run_id
        else (
            workflow.revalidate_design(run_id=run_id)
            if args.revalidate_run_id
            else workflow.approve_checkpoint(run_id=run_id, checkpoint_id=f"checkpoint_requirement_{run_id}")
        )
    )
    elapsed = round(perf_counter() - started, 3)
    state = store.read_state(run_id)
    report = _shadow_report(
        store=store,
        state=state,
        result=result,
        llm_summary=workflow.llm.summary(),
        elapsed_seconds=elapsed,
    )
    report_name = (
        f"l3_shadow_step_{str(args.review_call_id or 'pending')}.json"
        if args.review_run_id
        else ("l3_shadow_revalidate_report.json" if args.revalidate_run_id else "l3_shadow_report.json")
    )
    report_path = store.run_dir(run_id) / report_name
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": store.relative(report_path)}, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "waiting_design_approval" else 2


def _recover_missed_invalid_step_review(*, store: RuntimeStore, run_id: str) -> bool:
    """Restore the latest auto-retried invalid JSON as a controlled pending review."""

    state = store.read_state(run_id)
    last_error = state.get("last_error") if isinstance(state.get("last_error"), dict) else {}
    if (
        str(state.get("status") or "") != "design_blocked"
        or str(last_error.get("error_code") or "") != "designer_invalid_action_exhausted"
    ):
        return False
    session_files = sorted(store.run_dir(run_id).glob("designer_execution/*/designer_loop_session_v1.json"))
    if not session_files:
        raise HarnessError("l3_shadow_recovery_failed", "Blocked Designer run has no persisted session.", run_id=run_id)
    session_file = session_files[-1]
    session = json.loads(session_file.read_text(encoding="utf-8"))
    if isinstance(session.get("pending_step_review"), dict):
        return False

    exchange_path: Path | None = None
    exchange: dict[str, object] = {}
    for relative in reversed([str(item) for item in session.get("exchange_paths", [])]):
        candidate = ROOT / relative
        if not candidate.exists():
            continue
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        rejection = payload.get("rejection") if isinstance(payload.get("rejection"), dict) else {}
        if str(rejection.get("code") or "") == "invalid_json" and not payload.get("operator_review"):
            exchange_path = candidate
            exchange = payload
            break
    if exchange_path is None:
        raise HarnessError(
            "l3_shadow_recovery_failed",
            "Blocked Designer run has no recoverable invalid-JSON exchange.",
            run_id=run_id,
        )

    call_id = str(exchange.get("call_id") or "")
    try:
        call_ordinal = int(call_id.rsplit("-", 1)[-1])
    except ValueError as exc:
        raise HarnessError("l3_shadow_recovery_failed", "Recoverable exchange has an invalid call id.", run_id=run_id) from exc
    cursor = session.get("cursor") if isinstance(session.get("cursor"), dict) else {}
    if int(cursor.get("next_action_index") or 0) != call_ordinal + 1:
        raise HarnessError(
            "l3_shadow_recovery_failed",
            "Latest invalid exchange does not match the persisted Designer cursor.",
            run_id=run_id,
        )
    budgets = session.get("budgets") if isinstance(session.get("budgets"), dict) else {}
    action_budget = budgets.get("action") if isinstance(budgets.get("action"), dict) else {}
    invalid_budget = budgets.get("invalid_action") if isinstance(budgets.get("invalid_action"), dict) else {}
    if int(action_budget.get("used") or 0) < 1 or int(invalid_budget.get("used") or 0) < 1:
        raise HarnessError("l3_shadow_recovery_failed", "Designer budgets cannot be rolled back safely.", run_id=run_id)
    observations = session.get("observations") if isinstance(session.get("observations"), list) else []
    last_observation = observations[-1] if observations and isinstance(observations[-1], dict) else {}
    if str(last_observation.get("tool_id") or "") != "llm" or str(last_observation.get("status") or "") != "rejected":
        raise HarnessError(
            "l3_shadow_recovery_failed",
            "Latest Designer observation is not the invalid output being recovered.",
            run_id=run_id,
        )

    action_budget["used"] = int(action_budget.get("used") or 0) - 1
    invalid_budget["used"] = int(invalid_budget.get("used") or 0) - 1
    budgets["action"] = action_budget
    budgets["invalid_action"] = invalid_budget
    session["budgets"] = budgets
    target_key = str(session.get("active_repair_target_id") or "") or "__construction__"
    by_target = session.get("invalid_actions_by_target") if isinstance(session.get("invalid_actions_by_target"), dict) else {}
    by_target[target_key] = max(0, int(by_target.get(target_key) or 0) - 1)
    session["invalid_actions_by_target"] = by_target
    session["observations"] = observations[:-1]
    cursor["pending_call"] = None
    cursor["next_action_index"] = call_ordinal
    session["cursor"] = cursor
    session["pending_step_review"] = {
        "call_id": call_id,
        "exchange_file": exchange_path.name,
        "exchange_path": store.relative(exchange_path),
        "action": {},
        "parse_error": {"code": "invalid_json", "message": "Designer returned invalid JSON."},
        "recovered_from_auto_retry": True,
        "created_at": utc_now_iso(),
    }
    session["status"] = "review_required"
    session["last_error"] = None
    session["last_invalid_action"] = ""
    session["updated_at"] = utc_now_iso()
    session_file.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    exchange["status"] = "review_required"
    exchange["recovery"] = {"recovered_from_auto_retry": True, "timestamp": utc_now_iso()}
    exchange_path.write_text(json.dumps(exchange, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state.update(
        {
            "status": "design_paused",
            "current_phase": "design_investigating",
            "pending_checkpoint": None,
            "allowed_next_actions": ["continue_run", "get_status", "cancel_run"],
            "last_error": None,
            "summary_for_user": f"Designer call {call_id} requires operator review.",
            "updated_at": utc_now_iso(),
        }
    )
    store.write_state(state)
    return True


def _seed_approved_requirement(
    *,
    store: RuntimeStore,
    settings: object,
    run_id: str,
    approved_requirement: Path | None,
    source_file: Path | None,
    vertical_trace_mode: str,
) -> None:
    if (approved_requirement is None) != (source_file is None):
        raise HarnessError(
            "l3_shadow_source_args_invalid",
            "--approved-requirement and --source-file must be supplied together.",
        )
    source_path = source_file or ROOT / "fixtures" / "etour" / "UC2.txt"
    source_text = source_path.read_text(encoding="utf-8").strip()
    payload = deepcopy(
        json.loads(approved_requirement.read_text(encoding="utf-8"))
        if approved_requirement
        else _resolved_uc2_payload()
    )
    prior_source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    source_ref = str(prior_source.get("source_ref") or f"dataset_use_case:eTour:{source_path.stem}:l3-shadow")
    parts = source_ref.split(":")
    source = UseCaseSource(
        dataset=parts[1] if len(parts) > 2 else "eTour",
        use_case=parts[2] if len(parts) > 2 else source_path.stem,
        source_ref=source_ref,
        source_path=source_path,
        text=source_text,
    )
    snapshot_ref = write_source_snapshot_v2(store=store, run_id=run_id, source=source)
    source_entry = source_provenance_entry(source=source, artifact_ref=snapshot_ref)
    payload.update(
        {
            "schema_version": "requirement_model_v2",
            "revision": 1,
            "run_id": run_id,
            "source": {
                "source_id": source_entry["id"],
                "source_ref": source.source_ref,
                "artifact_ref": snapshot_ref,
                "fingerprint": source_entry["fingerprint"],
            },
            "provenance_ledger": {
                "schema_version": "requirement_provenance_v1",
                "sources": [source_entry],
            },
            "decisions": list(payload.get("decisions") or []) if approved_requirement else [],
            "validation": {"status": "invalid", "errors": []},
        }
    )
    validation = normalize_and_validate_requirement_model_v2(
        payload,
        {str(source_entry["id"]): source_text},
    )
    if not validation.report.ok:
        issue_summary = ", ".join(
            f"{issue.code}@{issue.path}" for issue in validation.report.issues
        )
        raise HarnessError(
            "l3_shadow_requirement_invalid",
            f"Seeded Requirement V2 did not validate: {issue_summary}",
        )
    refs = write_requirement_v2_revision_artifacts(
        store=store,
        run_id=run_id,
        model=validation.model,
        validation_report=validation.report,
        audit={
            "mode": "controlled_l3_shadow",
            "source": str(approved_requirement or "tests/fixtures/requirement_v2/uc2.json"),
        },
    )
    refs["requirement_source_snapshot"] = snapshot_ref
    now = utc_now_iso()
    state = {
        "run_id": run_id,
        "source_ref": source.source_ref,
        "created_at": now,
        "updated_at": now,
        "current_phase": "requirement_review",
        "status": "waiting_requirement_approval",
        "requirement_contract_version": "v2",
        REPO_DISCOVERY_CONTRACT_STATE_KEY: REPO_DISCOVERY_CONTRACT_VERSION,
        DESIGNER_CONTRACT_STATE_KEY: GRAPH_VERSION,
        IMPLEMENTATION_PLAN_CONTRACT_STATE_KEY: COMPONENT_AWARE_IMPLEMENTATION_PLAN_VERSION,
        VERTICAL_TRACE_STATE_KEY: VERTICAL_TRACE_GRAPH_VERSION,
        VERTICAL_TRACE_MODE_STATE_KEY: vertical_trace_mode,
        "profile_snapshot": build_profile_snapshot(
            project_profile_id=str(getattr(settings, "project_profile_id")),
            target_remote=str(getattr(settings, "target_repo_remote")),
            target_base_branch=str(getattr(settings, "target_base_branch")),
        ).to_dict(),
        "latest_artifacts": refs,
        "approved_artifacts": {},
        "pending_checkpoint": {
            "checkpoint_id": f"checkpoint_requirement_{run_id}",
            "type": "requirement_approval",
            "artifact_ref": "requirement_model_v2_r1",
            "artifact_path": refs["requirement_model"],
        },
        "allowed_next_actions": ["approve_checkpoint", "get_status", "cancel_run"],
        "last_sync": None,
        "last_error": None,
        "quality_reports": {"requirement_model_v2": {"final_report": validation.report.to_dict()}},
        "llm": {"mode": "deepseek", "model": str(getattr(settings, "llm_model"))},
        "summary_for_user": f"Controlled L3 shadow seeded with approved {source.use_case} Requirement V2.",
    }
    store.write_state(state)


def _resolved_uc2_payload() -> dict[str, object]:
    payload = deepcopy(
        json.loads((ROOT / "tests" / "fixtures" / "requirement_v2" / "uc2.json").read_text(encoding="utf-8"))
    )
    payload["open_questions"] = []
    for item in payload["flows"]["alternatives"] + payload["flows"]["exceptions"]:
        item["open_question_refs"] = []
        item["steps"] = [f"Execute the approved {item['id']} branch behavior."]
        item["outcome"] = f"The {item['id']} branch reaches an observable terminal state."
    payload["business_rules"][0]["open_question_refs"] = []
    resolved_acceptance = {
        "AC-1": ("The system rejects a duplicate cultural heritage record.", ["E-13"]),
        "AC-3": ("The operator can cancel the insertion operation.", ["E-10"]),
        "AC-4": ("Invalid or insufficient data activates the Errored use case.", ["E-7"]),
        "AC-5": ("A connection interruption is handled as an insertion exception.", ["E-11"]),
    }
    for item in payload["acceptance_criteria"]:
        item["open_question_refs"] = []
        if item["id"] in resolved_acceptance:
            item["text"], item["evidence_refs"] = resolved_acceptance[item["id"]]
            item["status"] = "explicit"
    for scenario in payload["scenario_contract"]["scenarios"]:
        scenario["open_question_refs"] = []
        for surface in scenario["surfaces"].values():
            surface["open_question_refs"] = []
    return payload


def _shadow_report(
    *,
    store: RuntimeStore,
    state: dict[str, object],
    result: dict[str, object],
    llm_summary: dict[str, object],
    elapsed_seconds: float,
) -> dict[str, object]:
    artifacts = state.get("latest_artifacts") if isinstance(state.get("latest_artifacts"), dict) else {}
    graph: dict[str, object] = {}
    graph_ref = str(artifacts.get("vertical_design_graph") or "")
    if graph_ref:
        graph_path = Path(graph_ref)
        if not graph_path.is_absolute():
            graph_path = ROOT / graph_path
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    journals = sorted(store.run_dir(str(state["run_id"])).glob("designer_execution/*/designer_loop_journal_v1.jsonl"))
    session_files = sorted(
        store.run_dir(str(state["run_id"])).glob("designer_execution/*/designer_loop_session_v1.json")
    )
    latest_session = json.loads(session_files[-1].read_text(encoding="utf-8")) if session_files else {}
    session_graph = latest_session.get("graph")
    if isinstance(session_graph, dict):
        graph = session_graph
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    pending_step_review = (
        latest_session.get("pending_step_review")
        if isinstance(latest_session.get("pending_step_review"), dict)
        else None
    )
    tool_calls = Counter()
    for journal in journals:
        for line in journal.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            tool_calls[str(entry.get("tool_id") or "unknown")] += 1
    evidence_nodes = sum(1 for node in nodes if node.get("evidence_refs"))
    return {
        "ok": result.get("status") == "waiting_design_approval",
        "run_id": state.get("run_id"),
        "status": result.get("status"),
        "sync_mode": "mock",
        "elapsed_seconds": elapsed_seconds,
        "llm_calls": llm_summary,
        "tool_calls": dict(sorted(tool_calls.items())),
        "graph": {
            "revision": graph.get("revision", 0),
            "nodes": len(nodes),
            "edges": len([item for item in graph.get("edges", []) if isinstance(item, dict)]),
            "partitions": len([item for item in graph.get("partitions", []) if isinstance(item, dict)]),
            "evidence_coverage": round(evidence_nodes / len(nodes), 4) if nodes else 0.0,
            "dispositions": dict(sorted(Counter(str(node.get("disposition") or "unknown") for node in nodes).items())),
        },
        "quality": state.get("quality_reports", {}).get("design_validation", {})
        if isinstance(state.get("quality_reports"), dict)
        else {},
        "vertical_trace": state.get("quality_reports", {}).get("vertical_trace", {})
        if isinstance(state.get("quality_reports"), dict)
        else {},
        "last_error": state.get("last_error"),
        "step_review": {
            "required": pending_step_review is not None,
            "call_id": str((pending_step_review or {}).get("call_id") or ""),
            "exchange_path": str((pending_step_review or {}).get("exchange_path") or ""),
        },
        "published_artifacts": sorted(
            key
            for key in artifacts
            if key
            in {
                "vertical_design_graph",
                "design_component_view",
                "design_surface_view",
                "design_projection_manifest",
                "design_finding_ledger",
                "design_artifact",
                "class_diagram_graph",
                "sequence_diagram_graph",
                "requirement_design_trace",
                "vertical_trace_graph",
                "vertical_trace_validation",
            }
        ),
    }


def _require_isolated_path(path: Path, *, label: str) -> None:
    if path == ROOT or ROOT in path.parents:
        return
    raise HarnessError("l3_shadow_path_unsafe", f"{label} must be inside the SDLC_Harness repository.")


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
