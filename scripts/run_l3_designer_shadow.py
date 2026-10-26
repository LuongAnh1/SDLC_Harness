"""Run an isolated L3 Designer shadow against a frozen local TypeScript clone.

The script seeds an approved UC2 Requirement V2 fixture, runs Phase 2 with the
real configured DeepSeek client, and forces mock sync. It never calls Jira,
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
from sdlc_harness.agents.requirements import normalize_and_validate_requirement_model_v2
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
    parser.add_argument("--design-call-budget", type=int, default=28)
    parser.add_argument("--design-repair-limit", type=int, default=3)
    args = parser.parse_args()

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
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    _seed_approved_uc2(store=store, settings=settings, run_id=run_id)

    started = perf_counter()
    workflow = PhaseTwoWorkflow(settings)
    result = workflow.approve_checkpoint(run_id=run_id, checkpoint_id=f"checkpoint_requirement_{run_id}")
    elapsed = round(perf_counter() - started, 3)
    state = store.read_state(run_id)
    report = _shadow_report(
        store=store,
        state=state,
        result=result,
        llm_summary=workflow.progressive.llm.summary(),
        elapsed_seconds=elapsed,
    )
    report_path = store.run_dir(run_id) / "l3_shadow_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report": store.relative(report_path)}, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "waiting_design_approval" else 2


def _seed_approved_uc2(*, store: RuntimeStore, settings: object, run_id: str) -> None:
    source_path = ROOT / "fixtures" / "etour" / "UC2.txt"
    source_text = source_path.read_text(encoding="utf-8").strip()
    source = UseCaseSource(
        dataset="eTour",
        use_case="UC2",
        source_ref="dataset_use_case:eTour:UC2:l3-shadow",
        source_path=source_path,
        text=source_text,
    )
    snapshot_ref = write_source_snapshot_v2(store=store, run_id=run_id, source=source)
    source_entry = source_provenance_entry(source=source, artifact_ref=snapshot_ref)
    payload = _resolved_uc2_payload()
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
            "decisions": [],
            "validation": {"status": "invalid", "errors": []},
        }
    )
    validation = normalize_and_validate_requirement_model_v2(
        payload,
        {str(source_entry["id"]): source_text},
    )
    if not validation.report.ok:
        raise HarnessError("l3_shadow_requirement_invalid", "Seeded UC2 Requirement V2 did not validate.")
    refs = write_requirement_v2_revision_artifacts(
        store=store,
        run_id=run_id,
        model=validation.model,
        validation_report=validation.report,
        audit={"mode": "controlled_l3_shadow", "source": "tests/fixtures/requirement_v2/uc2.json"},
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
        "summary_for_user": "Controlled L3 shadow seeded with an approved UC2 Requirement V2 fixture.",
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
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    journals = sorted(store.run_dir(str(state["run_id"])).glob("designer_execution/*/designer_loop_journal_v1.jsonl"))
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
        "last_error": state.get("last_error"),
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
