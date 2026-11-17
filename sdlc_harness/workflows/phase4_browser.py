"""Phase 4 browser stages (L5.4): Browser Readiness (L5.4.1B), journey and baseline (L5.4.2).

```text
runtime_closed
  -> pin state.browser_stages, derive the readiness scope (no LLM)
       -> browser_ready                       (no demo fixture in the browser bundle)
       -> browser_readiness_written           (no production file imports a demo module statically)
       -> browser_readiness_ready -> [Developer browser_readiness_v1] -> browser_readiness_written
  -> R1 scope, R5 static imports, browser suite (R2 probes, R3 production build scan), R4 demo rendered
       -> browser_ready
       -> browser_readiness_repair_needed -> [Developer repair] -> browser_readiness_written
browser_ready
  -> Journey Planner (1 call, J1-J7, gate-repair rounds)  -> browser_journey_planned
  -> baseline capture (V-0 + views, both viewports)      -> browser_baseline_captured
       step_failed views -> browser_journey_patch_needed -> Planner patch -> browser_journey_planned
```

No owner step: readiness is mechanical and the journey plan is gated mechanically; meaning is
judged later by Browser QA. Each transition is one continue_run, like the runtime closure.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from ..agents.developer import DeveloperExecutionError
from ..agents.developer.browser_stage_contract import (
    assemble_ui_polish_contract,
    browser_component_files,
    build_browser_repair_work_packet,
    build_readiness_work_packet,
    build_ui_polish_work_packet,
    demo_render_problems,
    derive_readiness_context,
    polish_contract_rule_issues,
    readiness_findings,
    readiness_scope_drift,
    static_import_problems,
    ui_polish_guidance,
    visual_contracts_by_node,
)
from ..agents.designer.uiux import UIUX_MODE_STATE_KEY
from ..agents.developer.polish_planner import POLISH_INCONCLUSIVE, UiPolishPlanner
from ..agents.developer.visual_profile import app_visual_scope_id, read_visual_profiles
from ..agents.developer.closure_contract import (
    approved_closure_changes,
    closure_repair_feedback,
    render_runtime_probe_spec,
    runtime_probe_failures,
    runtime_probe_run,
    select_plan_unit_owners,
)
from ..agents.developer.runtime_config import worktree_dirty_paths
from ..agents.tester.browser_plan import (
    BASELINE_VIEW_ID,
    BROWSER_VIEWPORTS,
    JOURNEY_PLAN_SCHEMA_VERSION,
    MAX_FAILURE_ARIA_CHARS,
    aria_diff,
    baseline_view,
    build_browser_evidence_manifest,
    experience_contracts,
    journey_patch_scope_issues,
    journey_plan_rule_issues,
    journey_planner_view,
    journey_step_failures,
    journey_views,
    merge_journey_patch,
    obligation_catalog,
    render_browser_capture_spec,
    requirement_scenarios,
    scenario_design_nodes,
    select_primary_images,
)
from ..agents.tester.journey_planner import JOURNEY_INCONCLUSIVE, BrowserJourneyPlanner
from ..agents.validator.browser_review import (
    BROWSER_QA_INCONCLUSIVE,
    JOURNEY_EXECUTION_INCONCLUSIVE,
    BrowserQaValidator,
    UiPolishValidator,
    browser_qa_review_prompt,
    journey_execution_review_prompt,
    parse_browser_qa_review,
    parse_journey_execution_review,
)
from ..agents.validator.code_review_tasks import CodeValidationTaskCache
from ..artifacts.writers import write_phase4_code_change_report
from ..core.errors import HarnessError
from ..llm.step_review import LlmStepReviewRequired, ReviewableLlmClient
from ..profiles import (
    BROWSER_STAGE_POLICY_VERSION,
    browser_stage_facts,
    build_browser_stage_policy,
    get_project_profile,
)
from ..storage.runtime import utc_now_iso
from ..verification import BROWSER_CAPTURE_SPEC_PATH, RUNTIME_PROBE_SPEC_PATH, BrowserCaptureRun
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import (
    ALLOWED_QA_TESTED_ACTIONS,
    _bounded_diagnostic,
    _dict_items,
    _int_value,
    _next_phase4_artifact_version,
    _next_qa_artifact_version,
    _string_items,
)
from .phase4 import _is_repairable_code_trace_failure, _nonrepairable_code_trace_error_code
from .phase4_closure import (
    _classify_closure_verification,
    _file_sha256,
    _next_closure_artifact_version,
    _regression_problems,
    _write_closure_json,
    _write_closure_text,
)

BROWSER_STAGES_CONTRACT_VERSION = "browser_stages_contract_v1"
ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_UI_POLISH_REVIEW_ACTIONS = ["approve_checkpoint", "get_status", "cancel_run"]
UI_POLISH_CHECKPOINT_TYPE = "ui_polish_contract_review"
UI_POLISH_RESULT_CHECKPOINT_TYPE = "ui_polish_result_review"
_READINESS_PHASE = "browser_readiness"
_JOURNEY_PHASE = "browser_journey"
_POLISH_PHASE = "ui_polish"
_QA_PHASE = "browser_qa"
_DEVELOPER_MODES = {
    "browser_readiness",
    "browser_readiness_repair",
    "ui_polish",
    "ui_polish_repair",
    "browser_qa_repair",
    "browser_journey_repair",
}
# Blocks a later continue_run retries: the machine, the AST tool or the provider output failed,
# not the code. An inconclusive planner output retries in a new step-review attempt.
_RETRYABLE_ERROR_CODES = {
    "browser_readiness_environment_failed",
    "browser_baseline_environment_failed",
    "ui_polish_environment_failed",
    "browser_qa_environment_failed",
    "typescript_ast_extractor_unavailable",
    "typescript_ast_extraction_failed",
    JOURNEY_INCONCLUSIVE,
    POLISH_INCONCLUSIVE,
    BROWSER_QA_INCONCLUSIVE,
    JOURNEY_EXECUTION_INCONCLUSIVE,
}
STEP_REVIEW_STATUS = "browser_stage_step_review_required"
MAX_UI_SOURCE_FILE_CHARS = 32000
MAX_UI_SOURCE_TOTAL_CHARS = 120000
_JOURNEY_GATE_REPAIR_INSTRUCTION = (
    "The last plan in `previous_output` broke the Harness gates listed in `blocking_issues`. Return the full "
    "corrected plan."
)
_JOURNEY_PATCH_GATE_REPAIR_INSTRUCTION = (
    "The last patch in `rejected_output` broke the Harness gates listed in `blocking_issues` once it was merged onto "
    "`previous_output`. Return the corrected patch in the same patch form. `previous_output` stays the accepted "
    "base; keep every view and obligation outside `patch_scope` unchanged."
)
_JOURNEY_PATCH_INSTRUCTION = (
    "Execution review of the accepted plan in `previous_output` found typed steps that failed and/or obligations "
    "whose journey must change. Return a patch as in `patch_output_schema`, not the full plan: `views` holds only "
    "the views you change or add (a view with an accepted `key` replaces that view, a new `key` adds one), "
    "`removed_view_keys` drops views, and `not_reachable` lists every obligation of "
    "`patch_scope.affected_obligation_ids` that stays unobservable. The Harness keeps everything you do not return "
    "exactly as accepted and checks the merged plan. "
    "`patch_scope` is the complete edit scope: everything outside it was already semantically reviewed and "
    "accepted, so change only views in `affected_view_keys` (or add views) and only the placement of obligations "
    "in `affected_obligation_ids`. "
    "`step_failures` are the captures with a real step error, with that error and the ARIA at that point: `kind` is "
    "`step_failed` when the action could not run and the view stopped there, or `observe_missed` when the action ran "
    "but its `observe` locator never became visible. "
    "`planning_gaps` carry each semantic issue and expected_change with the view keys and evidence it cites; treat "
    "them as the outcome to close, not as a mandatory recipe. Cited evidence that passed is context, not a failed "
    "step, yet a view whose steps all passed still needs a change when a gap shows that it does not expose the cited "
    "outcome deterministically at every viewport. "
    "`captured_states` is what the browser really rendered after every step of the reviewed run, grouped by "
    "identical ARIA snapshot. An evidence ref reads `<stage>:<view id>:<viewport>:s<step index>` and `view_ids` maps "
    "each view key to its view id. For every state it shows, `captured_states` is the authority for accessible names "
    "and visible text; use `ui_sources` only for states that were never captured. "
    "Attach `observe` to the triggering click or press for a transient state (the Harness holds network responses "
    "during that step, see `execution_facts`); use a later `wait_for` for a settled state. Only when no typed step "
    "can hold a requested transient state on screen at every viewport, keep it in `not_reachable` with the exact "
    "constraint and explain it in `observations` instead of inventing a fragile path. If a step is right and the "
    "app is wrong, do not return that view."
)
_FINDING_EVIDENCE_NOTE = (
    " A finding's `evidence` holds the browser captures it cites: the typed `steps` that led to each capture and "
    "what the browser really showed then (ARIA text, focus, layout, network)."
)
# Files the Harness itself writes into the worktree; never scope drift.
_HARNESS_WRITTEN_PATHS = {".env.example", RUNTIME_PROBE_SPEC_PATH, BROWSER_CAPTURE_SPEC_PATH}


def _scenario_candidate_units(packet: dict[str, object], scenario_id: str) -> list[dict[str, object]]:
    """Frozen Plan units serving a scenario, including their transitive dependency closure."""

    units = {
        str(item.get("unit_id") or ""): item
        for item in _dict_items(packet.get("execution_units"))
        if str(item.get("unit_id") or "")
    }
    selected = {
        unit_id
        for unit_id, item in units.items()
        if str(item.get("scenario_id") or "") == scenario_id
    }
    pending = list(selected)
    while pending:
        unit_id = pending.pop()
        for dependency in _string_items(_dict_value(units.get(unit_id)).get("depends_on")):
            if dependency in units and dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return [
        {
            "unit_id": unit_id,
            "component_id": units[unit_id].get("component_id"),
            "scenario_id": units[unit_id].get("scenario_id"),
            "objective": units[unit_id].get("objective") or units[unit_id].get("summary"),
            "write_paths": _string_items(units[unit_id].get("write_paths")),
        }
        for unit_id in sorted(selected)
    ]


def _journey_patch_context(
    *,
    findings: list[dict[str, object]],
    evidence_records: list[dict[str, object]],
    views: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    evidence = {
        str(item.get("evidence_ref") or ""): item
        for item in evidence_records
        if str(item.get("evidence_ref") or "")
    }
    views_by_id = {str(item.get("view_id") or ""): item for item in views}
    failures: list[dict[str, object]] = []
    planning_gaps: list[dict[str, object]] = []
    seen: set[tuple[str, str, int]] = set()
    for finding in findings:
        evidence_refs = _string_items(finding.get("evidence_refs"))
        # A Browser QA finding names no view: the views whose evidence it cites are the ones it is about.
        view_ids = _string_items(finding.get("view_ids")) or list(
            dict.fromkeys(str(evidence[ref].get("view_id") or "") for ref in evidence_refs if ref in evidence)
        )
        planning_gaps.append(
            {
                "view_keys": [
                    str(views_by_id[view_id].get("key") or "") for view_id in view_ids if view_id in views_by_id
                ],
                "view_ids": view_ids,
                "obligation_ids": _string_items(finding.get("obligation_ids")),
                "evidence_refs": evidence_refs,
                "issue": finding.get("issue"),
                "expected_change": finding.get("expected_change"),
            }
        )
        for ref in evidence_refs:
            record = _dict_value(evidence.get(ref))
            observe = _dict_value(record.get("observe"))
            error = str(record.get("error") or "").strip() or (
                str(observe.get("error") or "The observe locator never became visible.")
                if observe.get("status") == "missed"
                else ""
            )
            if not error:
                continue
            view = _dict_value(views_by_id.get(str(record.get("view_id") or "")))
            step = _int_value(record.get("step"))
            identity = (str(record.get("view_id") or ""), str(record.get("viewport_id") or ""), step)
            if identity in seen:
                continue
            seen.add(identity)
            steps = _dict_items(view.get("steps"))
            failures.append(
                {
                    "view_key": view.get("key"),
                    "view_id": record.get("view_id"),
                    "viewport_id": record.get("viewport_id"),
                    "step_index": step,
                    "step": steps[step] if 0 <= step < len(steps) else None,
                    # step_failed: the action could not run and the view stopped; observe_missed: it ran, unseen.
                    "kind": "step_failed" if record.get("status") == "step_failed" else "observe_missed",
                    "error": error,
                    "aria_at_failure": str(record.get("aria_snapshot") or ""),
                }
            )
    return failures, planning_gaps


_FINDING_EVIDENCE_FIELDS = ("status", "error", "observe", "focus", "layout", "network")
# A visual Polish blocker is about computed presentation, so its Developer also reads styles and contrast.
_POLISH_FINDING_EVIDENCE_FIELDS = (*_FINDING_EVIDENCE_FIELDS, "styles", "contrast")


def _suite_failure_problem(suite: dict[str, object]) -> dict[str, object]:
    """A failed verification stage as a repair problem, with the stage's own bounded output as evidence."""

    stage_id = str(suite["classification"].get("stage_id") or "")
    stage = suite["stages"].get(stage_id)
    problem: dict[str, object] = {"source": stage_id or "verification", "issue": suite["result"].reason}
    if stage is not None and (stage.stdout or stage.stderr):
        problem["stage_output"] = {
            "argv": list(stage.argv),
            "stdout": _bounded_diagnostic(stage.stdout),
            "stderr": _bounded_diagnostic(stage.stderr),
        }
    return problem


def _findings_with_evidence(
    findings: list[dict[str, object]],
    *,
    manifest: object,
    views: list[dict[str, object]],
    variant: str,
    fields: tuple[str, ...] = _FINDING_EVIDENCE_FIELDS,
) -> list[dict[str, object]]:
    """Give each finding the captures it cites, so the Developer reads what the Validator read, not a paraphrase."""

    records = {
        str(item["evidence_ref"]): item
        for item in (_manifest_text_evidence(manifest, variant=variant) if isinstance(manifest, dict) else [])
    }
    steps = {str(view.get("view_id") or ""): _dict_items(view.get("steps")) for view in views}
    first_ref: dict[str, str] = {}
    enriched: list[dict[str, object]] = []
    for finding in findings:
        evidence: list[dict[str, object]] = []
        for ref in _string_items(finding.get("evidence_refs")):
            record = records.get(ref)
            if record is None:
                continue
            aria = str(record.get("aria_snapshot") or "")
            shown = first_ref.setdefault(aria, ref)
            evidence.append(
                {
                    "evidence_ref": ref,
                    # The typed journey up to this capture: how the browser reached the state below.
                    "steps": steps.get(str(record.get("view_id") or ""), [])[: _int_value(record.get("step")) + 1],
                    **{key: record[key] for key in fields if record.get(key)},
                    **(
                        {
                            "aria_snapshot": aria[:MAX_FAILURE_ARIA_CHARS],
                            "aria_truncated": len(aria) > MAX_FAILURE_ARIA_CHARS or bool(record.get("aria_truncated")),
                        }
                        if shown == ref
                        else {"aria_snapshot_same_as": shown}
                    ),
                }
            )
        enriched.append({**finding, "evidence": evidence} if evidence else finding)
    return enriched


def _journey_patch_round(
    *,
    previous_plan: dict[str, object],
    feedback: dict[str, object],
    manifest: dict[str, object],
    variant: str,
) -> dict[str, object]:
    """Patch-round input: the accepted plan, the review feedback and what the reviewed run really rendered."""

    previous_output = _dict_value(previous_plan.get("output"))
    captured: dict[str, list[str]] = {}
    for record in _manifest_text_evidence(manifest, variant=variant):
        captured.setdefault(str(record.get("aria_snapshot") or ""), []).append(str(record["evidence_ref"]))
    step_failures = _dict_items(feedback.get("failures"))
    planning_gaps = _dict_items(feedback.get("planning_gaps"))
    findings = _dict_items(feedback.get("findings"))
    affected_obligation_ids = {
        obligation_id
        for item in [*planning_gaps, *findings]
        for obligation_id in _string_items(item.get("obligation_ids"))
    }
    affected_view_keys = {
        str(item.get("view_key") or "") for item in step_failures if str(item.get("view_key") or "")
    }
    affected_view_keys.update(key for item in planning_gaps for key in _string_items(item.get("view_keys")))
    affected_view_keys.update(
        str(view.get("key") or "")
        for view in _dict_items(previous_output.get("views"))
        if affected_obligation_ids.intersection(_string_items(view.get("obligation_refs")))
    )
    return {
        "instruction": _JOURNEY_PATCH_INSTRUCTION,
        "previous_output": previous_output,
        "view_ids": {
            str(view.get("key") or ""): str(view.get("view_id") or "")
            for view in _dict_items(previous_plan.get("views"))
        },
        "step_failures": step_failures,
        "planning_gaps": planning_gaps,
        "captured_states": [
            {"aria_snapshot": aria, "evidence_refs": refs} for aria, refs in captured.items()
        ],
        "patch_scope": {
            "affected_view_keys": sorted(affected_view_keys),
            "affected_obligation_ids": sorted(affected_obligation_ids),
        },
    }


class PhaseFourBrowserStagesCycle:
    """Own the browser-stage transitions after ``runtime_closed``; composed by PhaseFourWorkflow."""

    # ------------------------------------------------------------------ routing

    def _browser_stages_enforced(self, state: dict[str, object]) -> bool:
        pinned_mode = str(_dict_value(state.get("browser_stages")).get("mode") or "")
        if pinned_mode:
            return pinned_mode == "enforce"
        return self.settings.browser_stages_mode == "enforce"

    def _browser_execution_active(self, state: dict[str, object]) -> bool:
        return str(_dict_value(state.get("developer_execution")).get("mode") or "") in _DEVELOPER_MODES

    def _browser_retry_stage(self, state: dict[str, object]) -> str:
        if str(state.get("status") or "") != "blocked" or str(state.get("current_phase") or "") not in {
            _READINESS_PHASE,
            _JOURNEY_PHASE,
            _POLISH_PHASE,
            _QA_PHASE,
        }:
            return ""
        error_code = str(_dict_value(state.get("last_error")).get("error_code") or "")
        # A spent repair budget the owner raised with `adjust_budget` reruns the step that hit it.
        if error_code not in _RETRYABLE_ERROR_CODES and not self._spent_budget_has_room(state):
            return ""
        return str(_dict_value(state.get("browser_stages")).get("blocked_from") or "deriving")

    def _resume_browser_stage(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        stage = self._browser_retry_stage(state)
        # A new attempt never replays the inconclusive exchange of the previous one.
        new_attempt = str(_dict_value(state.get("last_error")).get("error_code") or "") in {
            JOURNEY_INCONCLUSIVE,
            JOURNEY_EXECUTION_INCONCLUSIVE,
            POLISH_INCONCLUSIVE,
            BROWSER_QA_INCONCLUSIVE,
        }
        if stage == "verifying":
            return self._continue_browser_readiness_verification(state=state, run_id=run_id)
        if stage == "planning":
            return self._continue_browser_journey_planning(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "capturing":
            return self._continue_browser_baseline_capture(state=state, run_id=run_id)
        if stage == "journey_review":
            return self._continue_browser_journey_review(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "journey_repair_verifying":
            return self._continue_browser_journey_repair_verification(state=state, run_id=run_id)
        if stage == "patching":
            return self._continue_browser_journey_patch(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "polish_planning":
            return self._continue_ui_polish_planning(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "polish_verifying":
            return self._continue_ui_polish_verification(state=state, run_id=run_id)
        if stage == "polish_review":
            return self._continue_ui_polish_review(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "qa_review":
            return self._continue_browser_qa_review(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "qa_journey_patch":
            return self._continue_browser_qa_journey_patch(state=state, run_id=run_id, new_attempt=new_attempt)
        if stage == "qa_verifying":
            return self._continue_browser_qa_verification(state=state, run_id=run_id)
        return self._continue_browser_readiness(state=state, run_id=run_id)

    def _continue_browser_developer(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        mode = str(_dict_value(state.get("developer_execution")).get("mode") or "")
        if mode == "browser_journey_repair":
            return self._continue_browser_journey_repair(state=state, run_id=run_id)
        if mode == "browser_qa_repair":
            return self._continue_browser_qa_repair(state=state, run_id=run_id)
        if mode == "ui_polish_repair":
            return self._continue_ui_polish_repair(state=state, run_id=run_id)
        if mode == "ui_polish":
            return self._continue_ui_polish_act(state=state, run_id=run_id)
        if mode == "browser_readiness_repair":
            return self._continue_browser_readiness_repair(state=state, run_id=run_id)
        return self._continue_browser_readiness_act(state=state, run_id=run_id)

    # ------------------------------------------------------------------ pin and scope

    def _readiness_pin(self, state: dict[str, object]) -> dict[str, object]:
        return _dict_value(_dict_value(state.get("browser_stages")).get("readiness"))

    def _set_readiness(self, state: dict[str, object], **fields: object) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        readiness = {**_dict_value(pin.get("readiness")), **fields}
        state["browser_stages"] = {**pin, "readiness": readiness}
        return readiness

    def _pin_browser_stages(self, state: dict[str, object], *, project_profile_id: str, run_id: str) -> None:
        """Pin once per run; a legacy profile_snapshot is never rewritten (D10)."""

        if _dict_value(state.get("browser_stages")).get("mode"):
            return
        facts = browser_stage_facts(project_profile_id, run_id=run_id)
        state["browser_stages"] = {
            "mode": "enforce",
            "contract_version": BROWSER_STAGES_CONTRACT_VERSION,
            "verification_policy_version": BROWSER_STAGE_POLICY_VERSION,
            "viewports": [dict(item) for item in BROWSER_VIEWPORTS],
            "demo_flag_env_key": facts.demo_flag_env_key,
            "status": "readiness",
            "pinned_at": utc_now_iso(),
            "repair_iterations": {"readiness": 0, "polish": 0, "qa": 0},
            "journey_patch_iterations": 0,
            "readiness": {},
            "qa": {},
        }

    def _browser_inputs(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        inputs = self._closure_run_inputs(state=state, run_id=run_id)
        planning_policy = inputs["planning_policy"]
        facts = browser_stage_facts(planning_policy.project_profile_id, run_id=run_id)
        target_path: Path = inputs["target_info"].path
        roots = dict(planning_policy.component_roots).get(facts.browser_component_id, ())
        paths = [
            item.relative_to(target_path).as_posix()
            for root in roots
            if (target_path / root).is_dir()
            for item in (target_path / root).rglob("*")
            if item.is_file() and "node_modules" not in item.parts
        ]
        return {
            **inputs,
            "facts": facts,
            "target_path": target_path,
            "files": browser_component_files(paths, planning_policy=planning_policy, facts=facts),
        }

    def _browser_repair_work_packet(
        self,
        *,
        inputs: dict[str, object],
        latest_artifacts: dict[str, str],
        run_id: str,
    ) -> dict[str, object]:
        probe_reports = []
        for key in (
            "runtime_closure_verification",
            "browser_readiness_verification",
            "browser_journey_verification",
            "browser_qa_verification",
        ):
            ref = str(latest_artifacts.get(key) or "")
            if not ref:
                continue
            report = _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)
            if _dict_items(report.get("probe_results")):
                probe_reports.append({"source_ref": ref, "probe_results": report["probe_results"]})
        browser_evidence_reports = []
        evidence_ref = str(latest_artifacts.get("browser_baseline_evidence_manifest") or "")
        if evidence_ref:
            manifest = _read_json_artifact(
                self._existing_artifact_path(evidence_ref, run_id=run_id), run_id=run_id
            )
            browser_evidence_reports.append(
                {"source_ref": evidence_ref, "variant": "baseline", "records": _dict_items(manifest.get("records"))}
            )
        return build_browser_repair_work_packet(
            frozen_packet=inputs["frozen_packet"],
            closure_contract=inputs["contract"],
            closure_contract_ref=str(_dict_value(inputs.get("pin")).get("approved_contract") or ""),
            closure_context=inputs["context"],
            probe_reports=probe_reports,
            browser_evidence_reports=browser_evidence_reports,
        )

    def _module_facts(self, inputs: dict[str, object], *, run_id: str) -> dict[str, dict[str, object]]:
        return self.module_facts_extractor.extract_module_facts(
            target_path=inputs["target_path"], source_paths=list(inputs["files"]), run_id=run_id
        )

    # ------------------------------------------------------------------ Developer sessions

    def _pause_browser_developer(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        exc: DeveloperExecutionError,
        mode: str,
        iteration: int,
        artifact_version: int,
        manifest_key: str,
        journal_key: str,
        phase: str = _READINESS_PHASE,
        label: str = "Browser readiness",
    ) -> dict[str, object]:
        session_key = manifest_key.replace("_manifest", "_session")
        for key, value in (
            (manifest_key, exc.manifest_path),
            (journal_key, exc.journal_path),
            (session_key, exc.session_path),
            ("developer_failure_classification", exc.failure_path),
        ):
            if value:
                latest_artifacts[key] = value
        paused = exc.retriable
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": phase,
                "status": "implementation_paused" if paused else "blocked",
                "latest_artifacts": latest_artifacts,
                "allowed_next_actions": (
                    ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS if paused else ["get_status", "cancel_run"]
                ),
                "last_error": exc.to_response(),
                "developer_execution": self._developer_execution_state(
                    state,
                    executor="tool_loop",
                    mode=mode,
                    repair_iteration=iteration,
                    session_iteration=max(1, iteration),
                    artifact_version=artifact_version,
                    progress=exc.progress,
                    status="paused" if paused else "blocked",
                ),
                "summary_for_user": f"{label} Developer {'tạm dừng' if paused else 'bị chặn'} cho run {run_id}: {exc.message}",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ verification (R1-R5)

    # ------------------------------------------------------------------ shared browser suite

    def _run_browser_suite(
        self,
        *,
        run_id: str,
        inputs: dict[str, object],
        packet: dict[str, object],
        views: list[dict[str, object]],
        demo_literals: list[str],
        stage: str,
    ) -> dict[str, object]:
        """Render both Harness specs, run the browser stage policy and write the evidence manifest."""

        target_path: Path = inputs["target_path"]
        contract = inputs["contract"]
        stand_ins = _dict_items(contract.get("verification_stand_ins"))
        for relative, content in (
            (RUNTIME_PROBE_SPEC_PATH, render_runtime_probe_spec(contract)),
            (BROWSER_CAPTURE_SPEC_PATH, render_browser_capture_spec(views, stand_ins=stand_ins)),
        ):
            spec_file = target_path / relative
            spec_file.parent.mkdir(parents=True, exist_ok=True)
            spec_file.write_text(content, encoding="utf-8")
        policy = build_browser_stage_policy(project=get_project_profile(inputs["planning_policy"].project_profile_id))
        prefix = f"browser_{stage}_evidence_manifest"
        version = _next_closure_artifact_version(self.store, run_id, prefix)
        facts = inputs["facts"]
        evidence_dir = self.store.run_dir(run_id) / "browser_execution" / stage / f"v{version}" / "evidence"
        result = self.verification_runner.run(
            target_path=target_path,
            work_packet={**packet, "verification_stage_ids": list(policy.stage_ids)},
            verification_policy=policy,
            runtime_probe=runtime_probe_run(contract),
            browser_capture=BrowserCaptureRun(
                view_ids=tuple(str(view["view_id"]) for view in views),
                viewport_ids=tuple(str(item["viewport_id"]) for item in BROWSER_VIEWPORTS),
                stand_in_env_keys=tuple(str(item.get("env_key") or "") for item in stand_ins),
                demo_flag_env_key=facts.demo_flag_env_key,
                forbidden_build_literals=tuple(demo_literals),
                build_output_dir=facts.build_output_dir,
                evidence_dir=evidence_dir,
            ),
        )
        stages = {item.stage_id: item for item in result.stage_results}
        probe_results = [dict(item) for item in (stages["runtime_probe"].probe_results if "runtime_probe" in stages else ())]
        captures = list(stages["browser_capture"].capture_results) if "browser_capture" in stages else []
        manifest = build_browser_evidence_manifest(
            views=views,
            capture_results=captures,
            evidence_dir=evidence_dir.relative_to(self.store.run_dir(run_id)).as_posix(),
        )
        return {
            "result": result,
            "policy": policy,
            "classification": _classify_closure_verification(result),
            "stages": stages,
            "probe_results": probe_results,
            "probe_failures": runtime_probe_failures(contract, probe_results),
            "captures": captures,
            "manifest": manifest,
            "manifest_ref": _write_closure_json(self.store, run_id, f"{prefix}_v{version}.json", manifest),
        }

    # ------------------------------------------------------------------ journey plan (L5.4.2)

    def _journey_pin(self, state: dict[str, object]) -> dict[str, object]:
        return _dict_value(_dict_value(state.get("browser_stages")).get("journey"))

    def _set_journey(self, state: dict[str, object], **fields: object) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        journey = {**_dict_value(pin.get("journey")), **fields}
        state["browser_stages"] = {**pin, "journey": journey}
        return journey

    def _polish_pin(self, state: dict[str, object]) -> dict[str, object]:
        return _dict_value(_dict_value(state.get("browser_stages")).get("polish"))

    def _set_polish(self, state: dict[str, object], **fields: object) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        polish = {**_dict_value(pin.get("polish")), **fields}
        state["browser_stages"] = {**pin, "polish": polish}
        return polish

    def _qa_pin(self, state: dict[str, object]) -> dict[str, object]:
        return _dict_value(_dict_value(state.get("browser_stages")).get("qa"))

    def _set_qa(self, state: dict[str, object], **fields: object) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        qa = {**_dict_value(pin.get("qa")), **fields}
        state["browser_stages"] = {**pin, "qa": qa}
        return qa

    # ------------------------------------------------------------------ UI Polish contract (L5.4.4)

    # ------------------------------------------------------------------ UI Polish execution (L5.4.5)

    # ------------------------------------------------------------------ Browser QA (L5.4.6)

    def _browser_publication_paths(self, *, state: dict[str, object], run_id: str) -> list[str]:
        pin = _dict_value(state.get("browser_stages"))
        if str(pin.get("status") or "") != "browser_qa_passed":
            return []
        readiness_ref = str(_dict_value(pin.get("readiness")).get("context") or "")
        readiness = (
            _read_json_artifact(self._existing_artifact_path(readiness_ref, run_id=run_id), run_id=run_id)
            if readiness_ref else {}
        )
        polish_ref = str(_dict_value(pin.get("polish")).get("work_packet") or "")
        polish = (
            _read_json_artifact(self._existing_artifact_path(polish_ref, run_id=run_id), run_id=run_id)
            if polish_ref else {}
        )
        return sorted(
            {
                *_string_items(readiness.get("write_scope")),
                *_string_items(_dict_value(pin.get("journey")).get("repair_paths")),
                *_string_items(_dict_value(polish.get("output_contract")).get("allowed_paths")),
                *_string_items(_dict_value(pin.get("qa")).get("repair_paths")),
                BROWSER_CAPTURE_SPEC_PATH,
            }
            - {""}
        )

    # ------------------------------------------------------------------ step review

    def _pause_for_browser_step_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        progress: dict[str, object],
        latest_artifacts: dict[str, str],
        resume_status: str,
    ) -> dict[str, object]:
        if resume_status in {"ui_polished", "browser_qa_verified", "browser_qa_journey_patch_needed"}:
            phase, label = _QA_PHASE, "Browser QA"
        elif resume_status in {"browser_baseline_captured", "ui_polish_verified"}:
            phase, label = _POLISH_PHASE, "UI Polish"
        elif resume_status == "browser_journey_captured":
            phase, label = _JOURNEY_PHASE, "Journey Execution Validator"
        else:
            phase, label = _JOURNEY_PHASE, "Journey Planner"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": phase,
                "status": STEP_REVIEW_STATUS,
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "pending_step_review": progress,
                "browser_stage_step_review": dict(self.browser_stage_step_review),
                "browser_stage_resume": {"status": resume_status},
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"{label} đang chờ duyệt {progress.get('review_stage')} {progress.get('call_id')} cho run {run_id}."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def continue_browser_stage_step_review(self, *, run_id: str) -> dict[str, object]:
        """Replay approved browser-stage exchanges with the operator decision for the pending one."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("status") or "") != STEP_REVIEW_STATUS:
                raise HarnessError(
                    "browser_stage_step_review_not_pending",
                    f"Run has no pending browser-stage step review: {run_id}",
                    run_id=run_id,
                )
            self._require_frozen_llm_mode(state, run_id=run_id)
            decision = {
                key: self.browser_stage_step_review[key]
                for key in ("call_id", "decision", "note")
                if self.browser_stage_step_review.get(key) is not None
            }
            self.browser_stage_step_review = {
                **_dict_value(state.get("browser_stage_step_review")),
                **decision,
                "enabled": True,
            }
            resume_status = str(_dict_value(state.get("browser_stage_resume")).get("status") or "")
            state.update({"pending_step_review": None, "browser_stage_resume": None, "status": resume_status})
            if resume_status == "browser_journey_patch_needed":
                return self._dispatch("_continue_browser_journey_patch", state=state, run_id=run_id)
            if resume_status == "browser_journey_captured":
                return self._dispatch("_continue_browser_journey_review", state=state, run_id=run_id)
            if resume_status == "browser_baseline_captured":
                return self._dispatch("_continue_ui_polish_planning", state=state, run_id=run_id)
            if resume_status == "ui_polish_verified":
                return self._dispatch("_continue_ui_polish_review", state=state, run_id=run_id)
            if resume_status == "browser_qa_journey_patch_needed":
                return self._dispatch("_continue_browser_qa_journey_patch", state=state, run_id=run_id)
            if resume_status in {"ui_polished", "browser_qa_verified"}:
                return self._dispatch("_continue_browser_qa_review", state=state, run_id=run_id)
            return self._dispatch("_continue_browser_journey_planning", state=state, run_id=run_id)

    # ------------------------------------------------------------------ terminal states

    def _block_browser_stage(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        exc: HarnessError,
        stage: str,
        phase: str = _READINESS_PHASE,
    ) -> dict[str, object]:
        retryable = exc.error_code in _RETRYABLE_ERROR_CODES
        pin = _dict_value(state.get("browser_stages"))
        if pin.get("mode"):
            state["browser_stages"] = {**pin, "blocked_from": stage}
            if phase == _JOURNEY_PHASE:
                self._set_journey(state, status="blocked")
            elif phase == _POLISH_PHASE:
                self._set_polish(state, status="blocked")
            elif phase == _QA_PHASE:
                self._set_qa(state, status="blocked")
            else:
                self._set_readiness(state, status="blocked")
        raw = str(getattr(exc, "raw_response", "") or "")
        if raw:
            latest_artifacts["browser_stage_raw_response"] = _write_closure_text(
                self.store, run_id, f"browser_stage_raw_response_{stage}.txt", raw
            )
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": phase,
                "status": "blocked",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "pending_step_review": None,
                "allowed_next_actions": (
                    ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS if retryable else ["get_status", "cancel_run"]
                ),
                "last_error": exc.to_response(),
                "summary_for_user": f"Browser stage ({phase}) bị chặn cho run {run_id}: {exc.message}",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)


def _source_snapshot(target_path: Path, paths: list[str]) -> dict[str, object]:
    files = []
    for path in paths:
        content = _read_text(target_path / path)
        files.append(
            {
                "path": path,
                "exists": content is not None,
                "sha256": _file_sha256(target_path / path) if content is not None else None,
                "content": content,
            }
        )
    return {"schema_version": "ui_polish_source_baseline_v1", "files": files}


def _source_diff(target_path: Path, baseline: dict[str, object]) -> dict[str, object]:
    files = []
    for item in _dict_items(baseline.get("files")):
        path = str(item.get("path") or "")
        before = str(item.get("content") or "")
        current = _read_text(target_path / path)
        after = current or ""
        lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"before/{path}",
                tofile=f"after/{path}",
                lineterm="",
            )
        )
        files.append(
            {
                "path": path,
                "before_sha256": item.get("sha256"),
                "after_sha256": _file_sha256(target_path / path) if current is not None else None,
                "status": "changed" if lines else "unchanged",
                "diff": lines,
            }
        )
    return {"schema_version": "ui_polish_source_diff_v1", "files": files}


def _baseline_capture_regressions(
    baseline: dict[str, object],
    after: dict[str, object],
) -> list[dict[str, object]]:
    def failures(manifest: dict[str, object]) -> set[tuple[str, str]]:
        # A transient state that was observed before Polish and is missed after it is a regression too.
        summary = _dict_value(manifest.get("summary"))
        return {
            (str(item.get("view_id") or ""), str(item.get("viewport_id") or ""))
            for key in ("step_failed", "observe_missed")
            for item in _dict_items(summary.get(key))
        }

    old, new = failures(baseline), failures(after)
    return [
        {"view_id": view_id, "viewport_id": viewport_id}
        for view_id, viewport_id in sorted(new - old)
    ]


def _polish_verification_problems(suite: dict[str, object]) -> list[dict[str, object]]:
    probe_failures = _dict_items(suite.get("probe_failures"))
    if probe_failures:
        return [
            {
                "source": "runtime_probe",
                "paths": [],
                "issue": (
                    f"Probe {item.get('probe_id')} expected status {item.get('expected_status')} "
                    f"but got {item.get('actual_status')} after UI Polish."
                ),
                "expected_change": "The approved runtime probe passes again without widening the Polish scope.",
            }
            for item in probe_failures
        ]
    classification = _dict_value(suite.get("classification"))
    stage_id = str(classification.get("stage_id") or "verification")
    return [
        {
            "source": stage_id,
            "paths": [],
            "issue": f"UI Polish verification stage `{stage_id}` failed: {classification.get('reason')}",
            "expected_change": f"Stage `{stage_id}` passes again while preserving the approved visual change.",
        }
    ]


def _polish_repair_findings(
    *,
    contract: dict[str, object],
    problems: list[dict[str, object]],
    run_id: str,
) -> list[dict[str, object]]:
    units = _dict_items(contract.get("polish_units"))
    known = {str(item.get("unit_id") or "") for item in units if item.get("unit_id")}
    by_path: dict[str, set[str]] = {}
    for unit in units:
        unit_id = str(unit.get("unit_id") or "")
        for path in [*_string_items(unit.get("write_paths")), *_string_items(unit.get("new_paths"))]:
            by_path.setdefault(path, set()).add(unit_id)
    findings = []
    for problem in problems:
        cited = set(_string_items(problem.get("unit_ids"))) & known
        paths = _string_items(problem.get("paths"))
        mapped = {unit_id for path in paths for unit_id in by_path.get(path, set())}
        unit_ids = sorted(cited or mapped or (known if not paths else set()))
        if not unit_ids:
            raise HarnessError(
                "ui_polish_repair_scope_conflict",
                f"A {problem.get('source') or 'review'} finding cannot be mapped to an approved Polish path: {paths}.",
                run_id=run_id,
            )
        findings.append(
            {
                **problem,
                "source": str(problem.get("source") or "ui_polish_review"),
                "unit_ids": unit_ids,
                "paths": paths,
                "issue": str(problem.get("issue") or problem.get("message") or "UI Polish regression."),
                "expected_change": str(problem.get("expected_change") or "Restore the approved outcome."),
            }
        )
    return findings


def _review_obligation(item: dict[str, object]) -> dict[str, object]:
    """What a browser Validator reads of one catalog obligation: its trigger and the approved expected outcome."""

    return {key: item[key] for key in ("obligation_id", "summary", "contract", "design_node_id") if key in item}


def _manifest_text_evidence(manifest: dict[str, object], *, variant: str) -> list[dict[str, object]]:
    return [
        {
            "evidence_ref": (
                f"{variant}:{item.get('view_id')}:{item.get('viewport_id')}:s{int(item.get('step') or 0):02d}"
            ),
            **{
                key: item.get(key)
                for key in (
                    "view_id", "viewport_id", "step", "status", "error", "aria_snapshot",
                    "aria_truncated", "focus", "layout", "styles", "network", "network_truncated", "contrast",
                )
            },
            **({"observe": item["observe"]} if isinstance(item.get("observe"), dict) else {}),
        }
        for item in _dict_items(manifest.get("records"))
    ]


_SHARED_EVIDENCE_FIELDS = ("aria_snapshot", "styles", "layout", "focus", "contrast", "network")


def _share_identical_values(*record_lists: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    """Send each distinct heavy field value once; `same_as` names the earlier record that shows an identical one."""

    shown: dict[tuple[str, str], str] = {}
    shared: list[list[dict[str, object]]] = []
    for records in record_lists:
        rows: list[dict[str, object]] = []
        for record in records:
            ref = str(record.get("evidence_ref") or "")
            row = dict(record)
            same_as: dict[str, str] = {}
            for field in _SHARED_EVIDENCE_FIELDS:
                value = row.get(field)
                if not value:  # null or empty values are shorter than a reference
                    continue
                first = shown.setdefault((field, json.dumps(value, ensure_ascii=False, sort_keys=True)), ref)
                if first != ref:
                    del row[field]
                    same_as[field] = first
            rows.append({**row, "same_as": same_as} if same_as else row)
        shared.append(rows)
    return shared


def _worktree_fingerprint(target_path: Path) -> dict[str, object]:
    files = [
        {
            "path": path,
            "exists": (target_path / path).is_file(),
            "sha256": _file_sha256(target_path / path),
        }
        for path in sorted(
            path
            for path in worktree_dirty_paths(target_path)
            if path != ".codegraph" and not path.startswith(".codegraph/")
        )
    ]
    digest = hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "browser_worktree_fingerprint_v1",
        "sha256": f"sha256:{digest}",
        "files": files,
    }


def _browser_qa_report_markdown(
    *,
    run_id: str,
    reviews: list[dict[str, object]],
    manifest: object,
    not_reachable: list[dict[str, object]],
) -> str:
    lines = [
        "# Browser QA report",
        "",
        f"- Run: `{run_id}`",
        f"- Evidence manifest: `{manifest}`",
        "- Result: `pass`",
        "",
        "| Scenario | Obligation | Verdict | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    observations: list[str] = []
    for review in reviews:
        scenario_id = str(review.get("scenario_id") or "")
        for verdict in _dict_items(review.get("verdicts")):
            lines.append(
                "| "
                + " | ".join(
                    (
                        scenario_id.replace("|", "\\|"),
                        str(verdict.get("obligation_id") or "").replace("|", "\\|"),
                        str(verdict.get("verdict") or "").replace("|", "\\|"),
                        ", ".join(_string_items(verdict.get("evidence_refs"))).replace("|", "\\|"),
                    )
                )
                + " |"
            )
        observations.extend(_string_items(review.get("observations")))
        observations.extend(
            str(item.get("issue") or "")
            for item in _dict_items(review.get("warnings"))
            if str(item.get("issue") or "")
        )
    lines.extend(["", "## Observations", ""])
    lines.extend([f"- {item}" for item in observations] or ["- none"])
    lines.extend(["", "## Not reachable / not observable", ""])
    not_observable = [
        str(item.get("obligation_id") or "")
        for review in reviews
        for item in _dict_items(review.get("verdicts"))
        if item.get("verdict") == "not_observable"
    ]
    unreachable_rows = [
        f"`{item.get('obligation_id')}` — {item.get('reason')}"
        for item in not_reachable
    ]
    lines.extend([f"- {item}" for item in [*unreachable_rows, *[f'`{value}` — not observable' for value in not_observable]]] or ["- none"])
    return "\n".join(lines) + "\n"


def _ui_polish_result_checkpoint_markdown(
    *,
    run_id: str,
    review: dict[str, object],
    aria_diffs: list[dict[str, object]],
    images: list[dict[str, object]],
) -> str:
    lines = [
        "# UI Polish result review",
        "",
        "Approve this checkpoint to accept the reviewed UI Polish result.",
        "",
        "## Before / after images",
        "",
    ]
    for item in images:
        label = str(item.get("label") or "")
        variant, _, capture_key = label.partition(":")
        manifest_key = "browser_baseline_evidence_manifest" if variant == "before" else "browser_polish_evidence_manifest"
        url = (
            f"/api/v1/runs/{quote(run_id, safe='')}/browser-evidence/"
            f"{quote(manifest_key, safe='')}/{quote(capture_key, safe='')}"
        )
        lines.append(f"- [{label}]({url})")
    changed = [item for item in aria_diffs if item.get("status") != "unchanged"]
    lines.extend(
        [
            "",
            "## Semantic review",
            "",
            str(review.get("summary") or ""),
            "",
            f"Warnings: {len(_dict_items(review.get('warnings')))}",
            "",
            "## ARIA diff",
            "",
            f"Changed captures: {len(changed)} / {len(aria_diffs)}",
            "",
            "```json",
            json.dumps(changed, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _selected_evidence(
    *,
    manifest: dict[str, object],
    images: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_file = {
        str(_dict_value(item.get("screenshot")).get("file") or ""): item
        for item in _dict_items(manifest.get("records"))
    }
    evidence: list[dict[str, object]] = []
    for image in images:
        filename = Path(str(image.get("path") or "")).name
        record = by_file.get(filename)
        if record is None:
            continue
        evidence.append(
            {
                "evidence_ref": str(image.get("label") or ""),
                "view_id": record.get("view_id"),
                "viewport_id": record.get("viewport_id"),
                "step": record.get("step"),
                "status": record.get("status"),
                "error": record.get("error"),
                "aria_snapshot": record.get("aria_snapshot"),
                "aria_truncated": bool(record.get("aria_truncated")),
                "focus": record.get("focus"),
                "layout": record.get("layout"),
                "styles": record.get("styles"),
                "network": record.get("network"),
                "network_truncated": bool(record.get("network_truncated")),
                "contrast": record.get("contrast"),
            }
        )
    return evidence


def _artifact_reference(path: Path, *, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _ui_polish_checkpoint_markdown(
    *,
    run_id: str,
    version: int,
    contract: dict[str, object],
    review: dict[str, object],
    images: list[dict[str, object]],
    manifest_key: str,
    guidance: dict[str, object] | None = None,
) -> str:
    lines = [
        f"# UI Polish contract v{version}",
        "",
        "Approve this checkpoint to make the existing app visual profile and the bounded polish units authoritative.",
        "Requirement and Design remain frozen; approval does not write UI code yet.",
        "",
        "## Visual profile",
        "",
        f"[Open the committed app visual profile](/api/v1/runs/{quote(run_id, safe='')}/artifacts/ui_polish_visual_profile)",
        "",
        "## Baseline images",
        "",
    ]
    for item in images:
        label = str(item.get("label") or "")
        capture_key = label.split(":", 1)[1] if ":" in label else label
        url = (
            f"/api/v1/runs/{quote(run_id, safe='')}/browser-evidence/"
            f"{quote(manifest_key, safe='')}/{quote(capture_key, safe='')}"
        )
        lines.append(f"- [{label}]({url})")
    if guidance:
        lines.extend(["", "## UI/UX guidance consulted (advice, not authority)", ""])
        lines.extend(
            f"- [{', '.join(_string_items(row.get('topics')))}] {row.get('issue')}: do {row.get('do')}; "
            f"don't {row.get('dont')}"
            for row in _dict_items(guidance.get("rows"))
        )
    lines.extend(
        [
            "",
            "## Validator review",
            "",
            str(review.get("summary") or "No summary."),
            "",
            f"Warnings: {len(_dict_items(review.get('warnings')))}",
            "",
            *[
                f"- {item.get('issue')} Expected: {item.get('expected_change')}"
                for item in _dict_items(review.get("warnings"))
            ],
            *( [""] if _dict_items(review.get("warnings")) else [] ),
            "## Contract",
            "",
            "```json",
            json.dumps(contract, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _require_suite_passed(suite: dict[str, object], *, run_id: str) -> None:
    """Journey suites change no code: a machine fault retries, anything else is for the owner."""

    classification = _dict_value(suite.get("classification"))
    if classification.get("kind") == "passed":
        return
    raise HarnessError(
        "browser_baseline_environment_failed"
        if classification.get("kind") == "environment"
        else "browser_baseline_capture_failed",
        f"{classification.get('stage_id') or 'verification'}: {suite['result'].reason}",
        run_id=run_id,
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    except OSError:
        return None


def _readiness_verification_problems(result, classification: dict[str, object], probe_failures) -> list[dict[str, object]]:
    problems = [
        {
            "source": "runtime_probe",
            "paths": [],
            "issue": (
                f"Probe {failure['probe_id']} ({failure['method']} {failure['path']}) expected status "
                f"{failure['expected_status']} but got {failure['actual_status']} after the readiness change."
            ),
            "expected_change": f"Probe {failure['probe_id']} answers as approved on the production build again.",
        }
        for failure in probe_failures
    ]
    if problems:
        return problems
    stage_id = str(classification.get("stage_id") or "verification")
    return [
        {
            "source": stage_id,
            "paths": [],
            "issue": f"Readiness verification stage `{stage_id}` failed: {classification.get('reason')}",
            "expected_change": (
                f"Stage `{stage_id}` passes: the production build holds no demo literal and the flagged build "
                "still boots and renders."
                if stage_id in {"frontend_build", "browser_capture"}
                else f"Stage `{stage_id}` passes again after the readiness change."
            ),
        }
    ]


def _verification_digest(result) -> dict[str, object]:
    return {
        "status": result.status,
        "reason": result.reason,
        "executed_stage_ids": list(result.executed_stage_ids),
        "stage_results": [
            {
                **{key: value for key, value in item.to_dict().items() if key not in {"stdout", "stderr", "capture_results"}},
                "stdout": _bounded_diagnostic(item.stdout),
                "stderr": _bounded_diagnostic(item.stderr),
            }
            for item in result.stage_results
        ],
    }
