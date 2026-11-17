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
        if str(_dict_value(state.get("last_error")).get("error_code") or "") not in _RETRYABLE_ERROR_CODES:
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

    def _continue_browser_readiness(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Pin, then derive the exact readiness scope from typed artifacts (no LLM, no owner step)."""

        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        try:
            closure = _dict_value(state.get("runtime_closure"))
            if str(closure.get("status") or "") != "runtime_closed":
                raise HarnessError(
                    "browser_stages_require_runtime_closure",
                    f"Browser stages start only after the runtime closure is closed: {run_id}",
                    run_id=run_id,
                )
            inputs = self._browser_inputs(state=state, run_id=run_id)
            scenarios = set(_string_items(inputs["context"].get("scenario_ids")))
            blocked_scenarios = set(
                _string_items(_dict_value(_dict_value(state.get("quality_reports")).get("runtime_closure")).get("blocked_scenario_ids"))
            )
            if not scenarios - blocked_scenarios:
                raise HarnessError(
                    "browser_stages_no_runnable_scenario",
                    f"Every scenario is blocked by an unresolved runtime input: {sorted(blocked_scenarios)}",
                    run_id=run_id,
                )
            self._pin_browser_stages(state, project_profile_id=inputs["planning_policy"].project_profile_id, run_id=run_id)
            self._set_readiness(state, status="deriving")
            self.store.write_state(state)
            target_path: Path = inputs["target_path"]
            version = _next_closure_artifact_version(self.store, run_id, "browser_readiness_context")
            context = derive_readiness_context(
                run_id=run_id,
                version=version,
                closure_contract=inputs["contract"],
                closure_contract_ref=str(closure.get("approved_contract") or ""),
                closure_context=inputs["context"],
                planning_policy=inputs["planning_policy"],
                facts=inputs["facts"],
                files=list(inputs["files"]),
                module_facts=self._module_facts(inputs, run_id=run_id),
                read_text=lambda path: _read_text(target_path / path),
            )
            context_ref = _write_closure_json(self.store, run_id, f"browser_readiness_context_v{version}.json", context)
            latest_artifacts["browser_readiness_context"] = context_ref
            if context["status"] == "blocked":
                blocked = _dict_value(context.get("blocked"))
                raise HarnessError(str(blocked.get("error_code")), str(blocked.get("message")), run_id=run_id)
            if context["status"] == "not_required":
                self._set_readiness(state, status="not_required", context=context_ref)
                return self._browser_ready(
                    state=state, run_id=run_id, latest_artifacts=latest_artifacts, summary=str(context["reason"])
                )
            packet_version = _next_closure_artifact_version(self.store, run_id, "browser_readiness_work_packet")
            packet = build_readiness_work_packet(
                context=context,
                context_ref=context_ref,
                closure_contract=inputs["contract"],
                closure_contract_ref=str(closure.get("approved_contract") or ""),
                closure_context=inputs["context"],
                frozen_packet=inputs["frozen_packet"],
                version=packet_version,
            )
            packet_ref = _write_closure_json(
                self.store, run_id, f"browser_readiness_work_packet_v{packet_version}.json", packet
            )
            latest_artifacts["browser_readiness_work_packet"] = packet_ref
            write_scope = set(_string_items(context.get("write_scope")))
            # Everything else already changed in the worktree is approved earlier output: hash-bound (R1).
            frozen_dirty = [
                path
                for path in worktree_dirty_paths(target_path)
                if path not in write_scope and path != ".codegraph" and not path.startswith(".codegraph/")
            ]
            developer_required = bool(_string_items(context.get("static_importers")))
            self._set_readiness(
                state,
                status="ready" if developer_required else "written",
                context=context_ref,
                work_packet=packet_ref,
                unit_id=str(_dict_items(packet.get("execution_units"))[0].get("unit_id") or ""),
                developer_skipped=not developer_required,
                expected_dirty_paths=frozen_dirty,
                expected_dirty_hashes={path: _file_sha256(target_path / path) for path in frozen_dirty},
                artifact_version=0,
                repair_session=None,
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="deriving"
            )
        status = "browser_readiness_ready" if developer_required else "browser_readiness_written"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _READINESS_PHASE,
                "status": status,
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Browser readiness cho run {run_id}: scope {sorted(write_scope)}, "
                    f"{len(_string_items(context.get('demo_literals')))} demo literal. "
                    + (
                        "Developer sẽ chuyển demo module sau cờ build."
                        if developer_required
                        else "Không file production nào import tĩnh demo module; chạy kiểm R1–R5 ngay."
                    )
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _route_browser_qa_journey_patch(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        findings: list[dict[str, object]],
    ) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        patches = _int_value(pin.get("journey_patch_iterations"))
        if patches >= max(0, int(self.settings.browser_journey_patch_limit)):
            raise HarnessError(
                "browser_journey_patch_limit_exceeded",
                f"Browser journey patch limit reached ({patches}) with an open journey defect.",
                run_id=run_id,
            )
        failures, planning_gaps = _journey_patch_context(
            findings=findings,
            evidence_records=_manifest_text_evidence(inputs["manifest"], variant="qa"),
            views=_dict_items(inputs["plan"].get("views")),
        )
        if not any(_string_items(item.get("evidence_refs")) for item in planning_gaps):
            raise HarnessError(
                "browser_qa_review_contract_invalid",
                "A journey_defect blocker must cite browser evidence for a typed journey step.",
                run_id=run_id,
            )
        version = _next_closure_artifact_version(self.store, run_id, "browser_qa_journey_failures")
        failures_ref = _write_closure_json(
            self.store,
            run_id,
            f"browser_qa_journey_failures_v{version}.json",
            {
                "schema_version": "browser_qa_journey_failures_v1",
                "run_id": run_id,
                "plan": self._journey_pin(state).get("plan"),
                "manifest": inputs["manifest_ref"],
                "findings": findings,
                "failures": failures,
                "planning_gaps": planning_gaps,
            },
        )
        latest_artifacts["browser_qa_journey_failures"] = failures_ref
        self._set_journey(state, status="qa_patch_needed", failures=failures_ref)
        self._set_qa(state, status="journey_patch_needed")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _QA_PHASE,
                "status": "browser_qa_journey_patch_needed",
                "latest_artifacts": latest_artifacts,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Browser QA cần vá typed journey cho run {run_id}.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_browser_qa_journey_patch(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = _dict_value(state.get("browser_stages"))
        iteration = _int_value(pin.get("journey_patch_iterations")) + 1
        journey = self._journey_pin(state)
        attempt = max(1, _int_value(journey.get("attempt"))) + (1 if new_attempt else 0)
        self._set_journey(state, status="qa_patching", attempt=attempt)
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            planning = self._journey_planning_inputs(
                state=state, run_id=run_id, inputs=inputs, latest_artifacts=latest_artifacts
            )
            previous = _read_json_artifact(
                self._existing_artifact_path(str(journey.get("plan") or ""), run_id=run_id), run_id=run_id
            )
            failures = _read_json_artifact(
                self._existing_artifact_path(str(journey.get("failures") or ""), run_id=run_id), run_id=run_id
            )
            planner = self._journey_agent(
                run_id=run_id,
                execution_dir=f"browser_execution/step_review/qa_journey/attempt_{attempt}/patch_{iteration}",
            )
            outcome = self._journey_plan_loop(
                planner=planner,
                planning=planning,
                base_round=_journey_patch_round(
                    previous_plan=previous,
                    feedback=failures,
                    manifest=_read_json_artifact(
                        self._existing_artifact_path(str(failures.get("manifest") or ""), run_id=run_id),
                        run_id=run_id,
                    ),
                    variant="qa",
                ),
                run_id=run_id,
            )
            if not outcome["ok"]:
                raise HarnessError("browser_journey_plan_invalid", str(outcome["message"]), run_id=run_id)
            plan_ref = self._write_journey_plan(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                planning=planning,
                outcome=outcome,
                source=f"qa_patch_{iteration}",
            )
            plan = _read_json_artifact(self._existing_artifact_path(plan_ref, run_id=run_id), run_id=run_id)
            views = _dict_items(plan.get("views"))
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=inputs["packet"],
                views=[baseline_view(), *views],
                demo_literals=self._readiness_literals(state, run_id=run_id),
                stage=f"qa_journey_patch_{iteration}",
            )
            _require_suite_passed(suite, run_id=run_id)
            verification_version = _next_closure_artifact_version(self.store, run_id, "browser_qa_verification")
            verification_ref = _write_closure_json(
                self.store,
                run_id,
                f"browser_qa_verification_v{verification_version}.json",
                {
                    "schema_version": "browser_qa_verification_v1",
                    "run_id": run_id,
                    "source": "journey_patch",
                    "journey_plan": plan_ref,
                    "manifest": suite["manifest_ref"],
                    "classification": suite["classification"],
                    "verification": _verification_digest(suite["result"]),
                    "probe_failures": suite["probe_failures"],
                },
            )
            latest_artifacts.update(
                {
                    "browser_qa_evidence_manifest": suite["manifest_ref"],
                    "browser_qa_verification": verification_ref,
                }
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state,
                run_id=run_id,
                progress=exc.progress,
                latest_artifacts=latest_artifacts,
                resume_status="browser_qa_journey_patch_needed",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="qa_journey_patch",
                phase=_QA_PHASE,
            )
        pin = _dict_value(state.get("browser_stages"))
        state["browser_stages"] = {**pin, "journey_patch_iterations": iteration}
        self._set_journey(state, status="captured", plan=plan_ref)
        self._set_qa(
            state,
            status="verified",
            current_manifest=suite["manifest_ref"],
            verification=verification_ref,
            needs_regression=False,
        )
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _QA_PHASE,
                "status": "browser_qa_verified",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "browser_qa_verified"},
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Journey patch {iteration} đã capture lại; Browser QA sẽ chấm lại.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ Developer sessions

    def _continue_browser_readiness_act(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        readiness = self._readiness_pin(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            packet = self._readiness_packet(readiness, run_id=run_id)
            artifact_version = _int_value(readiness.get("artifact_version")) or _next_phase4_artifact_version(
                self.store, run_id
            )
            self._set_readiness(state, status="executing", artifact_version=artifact_version)
            self.store.write_state(state)
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=packet,
                target_path=inputs["target_path"],
                latest_artifacts=latest_artifacts,
                mode="browser_readiness",
                current_phase=_READINESS_PHASE,
                repair_iteration=0,
                session_iteration=1,
                artifact_version=artifact_version,
                manifest_key="browser_readiness_execution_manifest",
                journal_key="browser_readiness_execution_journal",
                expected_dirty_paths=_string_items(readiness.get("expected_dirty_paths")),
                session_scope="browser_readiness",
            )
            report_path = self._readiness_change_report(
                state=state, run_id=run_id, inputs=inputs, result=result, artifact_version=artifact_version
            )
        except DeveloperExecutionError as exc:
            return self._pause_browser_developer(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, mode="browser_readiness",
                iteration=0, artifact_version=_int_value(self._readiness_pin(state).get("artifact_version")),
                manifest_key="browser_readiness_execution_manifest", journal_key="browser_readiness_execution_journal",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="executing"
            )
        return self._readiness_written(
            state=state, run_id=run_id, latest_artifacts=latest_artifacts, report_path=report_path,
            summary=f"Developer đã viết readiness unit cho run {run_id}; chạy kiểm R1–R5.",
        )

    def _continue_browser_readiness_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        readiness = self._readiness_pin(state)
        iteration = _int_value(_dict_value(_dict_value(state.get("browser_stages")).get("repair_iterations")).get("readiness")) + 1
        session = _dict_value(readiness.get("repair_session"))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            packet = self._readiness_packet(readiness, run_id=run_id)
            feedback_ref = str(latest_artifacts.get("browser_readiness_repair_feedback") or "")
            if not feedback_ref:
                raise HarnessError(
                    "browser_readiness_repair_feedback_missing",
                    f"Browser readiness repair has no feedback artifact: {run_id}",
                    run_id=run_id,
                )
            feedback = _read_json_artifact(self._existing_artifact_path(feedback_ref, run_id=run_id), run_id=run_id)
            if session.get("feedback") != feedback_ref:
                allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
                session = {
                    "feedback": feedback_ref,
                    "artifact_version": _next_phase4_artifact_version(self.store, run_id),
                    # Recomputed here: the Harness-written specs of the last verification are now dirty too.
                    "expected_dirty_paths": [
                        path
                        for path in worktree_dirty_paths(inputs["target_path"])
                        if path not in allowed and path != ".codegraph" and not path.startswith(".codegraph/")
                    ],
                }
                self._set_readiness(state, status="repairing", repair_session=session)
                self.store.write_state(state)
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=packet,
                target_path=inputs["target_path"],
                latest_artifacts=latest_artifacts,
                mode="browser_readiness_repair",
                current_phase=_READINESS_PHASE,
                repair_iteration=iteration,
                session_iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="browser_readiness_repair_execution_manifest",
                journal_key="browser_readiness_repair_execution_journal",
                repair_feedback=feedback,
                repair_scope="browser_readiness",
                expected_dirty_paths=_string_items(session.get("expected_dirty_paths")),
            )
            report_path = self._readiness_change_report(
                state=state, run_id=run_id, inputs=inputs, result=result,
                artifact_version=_int_value(session.get("artifact_version")),
                repair={"repair_iteration": iteration, "repair_feedback": feedback_ref},
            )
        except DeveloperExecutionError as exc:
            return self._pause_browser_developer(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc,
                mode="browser_readiness_repair", iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="browser_readiness_repair_execution_manifest",
                journal_key="browser_readiness_repair_execution_journal",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="repairing"
            )
        pin = _dict_value(state.get("browser_stages"))
        state["browser_stages"] = {
            **pin,
            "repair_iterations": {**_dict_value(pin.get("repair_iterations")), "readiness": iteration},
        }
        self._set_readiness(state, repair_session=None)
        return self._readiness_written(
            state=state, run_id=run_id, latest_artifacts=latest_artifacts, report_path=report_path,
            summary=f"Browser readiness repair {iteration} xong cho run {run_id}; chạy lại kiểm R1–R5.",
        )

    def _readiness_packet(self, readiness: dict[str, object], *, run_id: str) -> dict[str, object]:
        packet_ref = str(readiness.get("work_packet") or "")
        if not packet_ref:
            raise HarnessError(
                "browser_readiness_packet_missing", f"Browser readiness has no work packet: {run_id}", run_id=run_id
            )
        return _read_json_artifact(self._existing_artifact_path(packet_ref, run_id=run_id), run_id=run_id)

    def _readiness_change_report(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        inputs: dict[str, object],
        result,
        artifact_version: int,
        repair: dict[str, object] | None = None,
    ) -> str:
        return write_phase4_code_change_report(
            store=self.store,
            run_id=run_id,
            report={
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": inputs["frozen_packet"].get("issue_key"),
                "target_repo": str(inputs["target_path"]),
                "branch": inputs["target_info"].current_branch,
                "summary": result.summary,
                "files": result.files,
                "execution_mode": "tool_loop",
                "browser_readiness_work_packet": self._readiness_pin(state).get("work_packet"),
                "developer_execution_manifest": result.manifest_path,
                "developer_execution_journal": result.journal_path,
                **(repair or {}),
            },
            version=artifact_version,
        )

    def _readiness_written(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        report_path: str,
        summary: str,
    ) -> dict[str, object]:
        latest_artifacts["code_change_report"] = report_path
        self._set_readiness(state, status="written")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _READINESS_PHASE,
                "status": "browser_readiness_written",
                "latest_artifacts": latest_artifacts,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": summary,
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

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

    def _continue_browser_readiness_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        readiness = self._set_readiness(state, status="verifying")
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            target_path: Path = inputs["target_path"]
            context = _read_json_artifact(
                self._existing_artifact_path(str(readiness.get("context") or ""), run_id=run_id), run_id=run_id
            )
            packet = self._readiness_packet(readiness, run_id=run_id)
            unit_id = str(readiness.get("unit_id") or "")
            # R1: everything outside the scope keeps its baseline. The Developer cannot write there, so
            # a drift is an outside change that no repair may undo: block for the owner.
            drift = readiness_scope_drift(
                baselines=_string_dict(readiness.get("expected_dirty_hashes")),
                current_dirty=worktree_dirty_paths(target_path),
                current_sha256=lambda path: _file_sha256(target_path / path),
                allowed={*_string_items(context.get("write_scope")), *_HARNESS_WRITTEN_PATHS},
            )
            if drift["drifted"] or drift["outside"]:
                raise HarnessError(
                    "browser_readiness_scope_conflict",
                    f"R1: files changed outside the readiness scope: drifted={drift['drifted']}, new={drift['outside']}.",
                    run_id=run_id,
                )
            r5 = static_import_problems(
                context=context,
                files=list(inputs["files"]),
                module_facts=self._module_facts(inputs, run_id=run_id),
                planning_policy=inputs["planning_policy"],
            )
            if r5:
                return self._route_browser_readiness_repair(
                    state=state, run_id=run_id, latest_artifacts=latest_artifacts, context=context, packet=packet,
                    unit_id=unit_id, source="R5", problems=r5,
                    summary=f"R5: {len(r5)} production file(s) still import a demo module statically.",
                )
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=packet,
                views=[baseline_view()],
                demo_literals=_string_items(context.get("demo_literals")),
                stage="readiness",
            )
            result, classification, policy = suite["result"], suite["classification"], suite["policy"]
            stages, probe_results, probe_failures = suite["stages"], suite["probe_results"], suite["probe_failures"]
            capture_results = suite["captures"]
            latest_artifacts["browser_readiness_evidence_manifest"] = suite["manifest_ref"]
            version = _next_closure_artifact_version(self.store, run_id, "browser_readiness_verification")
            r4 = (
                demo_render_problems(
                    capture_results=capture_results,
                    demo_literals=_string_items(context.get("demo_literals")),
                    view_id=BASELINE_VIEW_ID,
                )
                if classification["kind"] == "passed"
                else []
            )
            report = {
                "schema_version": "browser_readiness_verification_v1",
                "run_id": run_id,
                "policy_version": policy.policy_version,
                **result.to_dict(),
                "classification": classification,
                "build_scan": stages["frontend_build"].build_scan if "frontend_build" in stages else None,
                "probe_results": probe_results,
                "probe_failures": probe_failures,
                "r4_problems": r4,
                "evidence_manifest": latest_artifacts["browser_readiness_evidence_manifest"],
            }
            report_path = _write_closure_json(self.store, run_id, f"browser_readiness_verification_v{version}.json", report)
            latest_artifacts["browser_readiness_verification"] = report_path
            state["quality_reports"] = {
                **_dict_value(state.get("quality_reports")),
                "browser_readiness_verification": {
                    "ok": classification["kind"] == "passed" and not r4,
                    "status": result.status,
                    "classification": classification,
                    "executed_stage_ids": list(result.executed_stage_ids),
                    "build_scan": report["build_scan"],
                    "probe_results": probe_results,
                    "captures": len(capture_results),
                    "report": report_path,
                },
            }
            if classification["kind"] == "implementation":
                return self._route_browser_readiness_repair(
                    state=state, run_id=run_id, latest_artifacts=latest_artifacts, context=context, packet=packet,
                    unit_id=unit_id, source=str(classification.get("stage_id") or "verification"),
                    problems=_readiness_verification_problems(result, classification, probe_failures),
                    summary=result.reason,
                    extra={"verification": _verification_digest(result), "probe_failures": probe_failures},
                )
            if classification["kind"] != "passed":
                raise HarnessError(
                    "browser_readiness_environment_failed"
                    if classification["kind"] == "environment"
                    else "browser_readiness_verification_policy_failed",
                    f"{classification['stage_id'] or 'verification'}: {result.reason}",
                    run_id=run_id,
                )
            if r4:
                return self._route_browser_readiness_repair(
                    state=state, run_id=run_id, latest_artifacts=latest_artifacts, context=context, packet=packet,
                    unit_id=unit_id, source="R4", problems=r4,
                    summary="R4: the flagged page renders none of the demo literals.",
                )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="verifying"
            )
        self._set_readiness(state, status="passed", verification=report_path)
        return self._browser_ready(
            state=state,
            run_id=run_id,
            latest_artifacts=latest_artifacts,
            summary=(
                f"Browser readiness passed for run {run_id}: production build has no demo literal, "
                f"probes answer as approved, V-0 renders demo data at {len(BROWSER_VIEWPORTS)} viewport(s)."
            ),
        )

    def _route_browser_readiness_repair(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        context: dict[str, object],
        packet: dict[str, object],
        unit_id: str,
        source: str,
        problems: list[dict[str, object]],
        summary: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Every readiness problem goes to the single readiness unit; one counter (D18: limit 2)."""

        pin = _dict_value(state.get("browser_stages"))
        iterations = _int_value(_dict_value(pin.get("repair_iterations")).get("readiness"))
        if iterations >= max(0, int(self.settings.browser_readiness_repair_limit)):
            raise HarnessError(
                "browser_readiness_repair_limit_exceeded",
                f"Browser readiness repair limit reached ({iterations}) with an open {source} failure: {summary}",
                run_id=run_id,
            )
        version = _next_closure_artifact_version(self.store, run_id, "browser_readiness_repair_feedback")
        feedback = closure_repair_feedback(
            run_id=run_id,
            version=version,
            repair_iteration=iterations + 1,
            source=source,
            summary=summary,
            findings=readiness_findings(problems, unit_id=unit_id),
            plan_extensions=[],
            allowed_paths=_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")),
            extra=extra or {},
        )
        feedback.update(
            {
                "version": f"browser_readiness_repair_feedback_v{version}",
                "status": "browser_readiness_repair_needed",
                "required_action": (
                    "Repair every finding assigned to this unit from the current worktree with the smallest coherent "
                    "change. Demo modules load only behind the build-time flag and every other build keeps the "
                    "approved production values. Harness then reruns R1, R5 and the browser suite (R2-R4)."
                ),
            }
        )
        latest_artifacts["browser_readiness_repair_feedback"] = _write_closure_json(
            self.store, run_id, f"browser_readiness_repair_feedback_v{version}.json", feedback
        )
        self._set_readiness(state, status="repair_needed")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _READINESS_PHASE,
                "status": "browser_readiness_repair_needed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Browser readiness cần repair ({source}) cho run {run_id}: {summary}",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

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

    def _readiness_literals(self, state: dict[str, object], *, run_id: str) -> list[str]:
        """R3 stays on for every later suite: the demo literals the readiness context derived."""

        ref = str(self._readiness_pin(state).get("context") or "")
        if not ref:
            return []
        context = _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)
        return _string_items(context.get("demo_literals")) if context.get("status") == "required" else []

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

    def _journey_agent(self, *, run_id: str, execution_dir: str) -> BrowserJourneyPlanner:
        planner = self.journey_planner
        if not bool(self.browser_stage_step_review.get("enabled")):
            return planner
        return BrowserJourneyPlanner(
            ReviewableLlmClient(
                planner.llm_client,
                store=self.store,
                run_id=run_id,
                review=self.browser_stage_step_review,
                call_prefix="browser-journey-planner",
                execution_dir=execution_dir,
                error_prefix="browser_stage",
                label="Journey Planner",
            )
        )

    def _journey_planning_inputs(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        inputs: dict[str, object],
        latest_artifacts: dict[str, str],
    ) -> dict[str, object]:
        context = inputs["context"]
        blocked = set(
            _string_items(_dict_value(_dict_value(state.get("quality_reports")).get("runtime_closure")).get("blocked_scenario_ids"))
        )
        runnable = [item for item in _string_items(context.get("scenario_ids")) if item not in blocked]
        node_scenarios = _dict_value(context.get("design_node_scenarios"))
        frozen = inputs["frozen_packet"]
        catalog = obligation_catalog(frozen_packet=frozen, node_scenarios=node_scenarios, runnable_scenarios=set(runnable))
        owning_units = {str(item["unit_id"]) for item in catalog}
        planning_policy, facts = inputs["planning_policy"], inputs["facts"]
        source_paths = sorted(
            {
                path
                for unit in _dict_items(frozen.get("execution_units"))
                if str(unit.get("unit_id") or "") in owning_units
                for path in _string_items(unit.get("write_paths"))
                if planning_policy.component_for_path(path) == facts.browser_component_id
                and not planning_policy.is_test_path(path)
                and not planning_policy.is_stylesheet_path(path)
            }
        )
        ui_sources: list[dict[str, object]] = []
        total = 0
        for path in source_paths:
            content = _read_text(inputs["target_path"] / path)
            if content is None:
                continue
            budget = max(0, min(MAX_UI_SOURCE_FILE_CHARS, MAX_UI_SOURCE_TOTAL_CHARS - total))
            ui_sources.append({"path": path, "content": content[:budget], "content_truncated": len(content) > budget})
            total += min(len(content), budget)
        contract = inputs["contract"]
        model_ref = str(
            _dict_value(_dict_value(frozen.get("requirement_contract")).get("artifact_refs")).get("requirement_model")
            or ""
        )
        view = journey_planner_view(
            runnable_scenarios=runnable,
            scenarios=requirement_scenarios(
                frozen_packet=frozen,
                requirement_model=(
                    _read_json_artifact(self._existing_artifact_path(model_ref, run_id=run_id), run_id=run_id)
                    if model_ref
                    else {}
                ),
                runnable_scenarios=runnable,
            ),
            scenario_nodes=scenario_design_nodes(
                node_scenarios=node_scenarios, catalog=catalog, runnable_scenarios=set(runnable)
            ),
            catalog=catalog,
            contracts=experience_contracts(frozen, {str(item["design_node_id"]) for item in catalog}),
            baseline_aria=self._baseline_aria(state=state, run_id=run_id, inputs=inputs, latest_artifacts=latest_artifacts),
            ui_sources=ui_sources,
            demo_data={
                "decisions": [
                    {key: item.get(key) for key in ("id", "input_ids", "statement", "scope")}
                    for item in _dict_items(contract.get("demo_decisions"))
                ],
                "stand_ins": [
                    {key: item.get(key) for key in ("stand_in_id", "decision_ref", "env_key")}
                    for item in _dict_items(contract.get("verification_stand_ins"))
                ],
            },
        )
        return {"runnable": runnable, "node_scenarios": node_scenarios, "catalog": catalog, "view": view}

    def _baseline_aria(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        inputs: dict[str, object],
        latest_artifacts: dict[str, str],
    ) -> dict[str, str]:
        """ARIA of V-0 per viewport: from the readiness evidence, or one V-0 capture when readiness was skipped."""

        ref = latest_artifacts.get("browser_readiness_evidence_manifest") or latest_artifacts.get(
            "browser_journey_v0_evidence_manifest"
        )
        if ref:
            manifest = _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)
        else:
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=inputs["packet"],
                views=[baseline_view()],
                demo_literals=self._readiness_literals(state, run_id=run_id),
                stage="journey_v0",
            )
            _require_suite_passed(suite, run_id=run_id)
            latest_artifacts["browser_journey_v0_evidence_manifest"] = suite["manifest_ref"]
            manifest = suite["manifest"]
        aria: dict[str, str] = {}
        for record in _dict_items(manifest.get("records")):
            if record.get("view_id") == BASELINE_VIEW_ID:
                aria[str(record.get("viewport_id"))] = str(record.get("aria_snapshot") or "")
        return aria

    def _journey_plan_loop(
        self,
        *,
        planner: BrowserJourneyPlanner,
        planning: dict[str, object],
        base_round: dict[str, object] | None,
        run_id: str,
    ) -> dict[str, object]:
        """Planner call, gates J1-J7, bounded gate-repair rounds (same limit as closure planning)."""

        limit = max(0, int(self.settings.implementation_plan_repair_limit))
        rounds: list[dict[str, object]] = []
        previous_round = base_round
        previous_fingerprint = ""
        for round_number in range(1, limit + 2):
            proposal = planner.propose(planner_view=planning["view"], previous_round=previous_round, run_id=run_id)
            patch_scope = _dict_value(_dict_value(base_round).get("patch_scope"))
            output, issues = proposal["output"], []
            if patch_scope:
                accepted = _dict_value(_dict_value(base_round).get("previous_output"))
                affected_obligation_ids = set(_string_items(patch_scope.get("affected_obligation_ids")))
                output, issues = merge_journey_patch(
                    accepted, proposal["output"], affected_obligation_ids=affected_obligation_ids
                )
            elif "removed_view_keys" in output:
                issues.append({"gate": "J1", "message": "`removed_view_keys` is only valid in a patch round."})
            issues.extend(
                journey_plan_rule_issues(
                    output,
                    catalog=planning["catalog"],
                    node_scenarios=planning["node_scenarios"],
                    runnable_scenarios=set(planning["runnable"]),
                )
            )
            if patch_scope:
                issues.extend(
                    journey_patch_scope_issues(
                        output,
                        previous_output=accepted,
                        affected_view_keys=set(_string_items(patch_scope.get("affected_view_keys"))),
                        affected_obligation_ids=affected_obligation_ids,
                    )
                )
            rounds.append({"round": round_number, "planner_attempts": proposal["attempts"], "issues": issues})
            if not issues:
                return {"ok": True, "output": output, "rounds": rounds}
            fingerprint = hashlib.sha256(
                json.dumps([proposal["output"], issues], sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if fingerprint == previous_fingerprint:
                return {"ok": False, "rounds": rounds, "message": "Journey plan repair stalled: the same output and issues repeated."}
            previous_fingerprint = fingerprint
            previous_round = {
                **(base_round or {}),
                "gate_repair_instruction": (
                    _JOURNEY_PATCH_GATE_REPAIR_INSTRUCTION if patch_scope else _JOURNEY_GATE_REPAIR_INSTRUCTION
                ),
                **(
                    {"rejected_output": proposal["output"]}
                    if patch_scope
                    else {"previous_output": proposal["output"]}
                ),
                "blocking_issues": issues,
            }
        return {"ok": False, "rounds": rounds, "message": f"Journey plan still breaks J1-J7 after {limit} repair round(s)."}

    def _write_journey_plan(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        planning: dict[str, object],
        outcome: dict[str, object],
        source: str,
    ) -> str:
        version = _next_closure_artifact_version(self.store, run_id, "browser_journey_plan")
        plan = {
            "schema_version": JOURNEY_PLAN_SCHEMA_VERSION,
            "run_id": run_id,
            "version": version,
            "source": source,
            "attempt": _int_value(self._journey_pin(state).get("attempt")),
            "runnable_scenario_ids": planning["runnable"],
            "obligation_catalog": planning["catalog"],
            "output": outcome["output"],
            "views": journey_views(outcome["output"]),
            "not_reachable": _dict_items(outcome["output"].get("not_reachable")),
            "observations": _string_items(outcome["output"].get("observations")),
            "rounds": outcome["rounds"],
        }
        ref = _write_closure_json(self.store, run_id, f"browser_journey_plan_v{version}.json", plan)
        latest_artifacts["browser_journey_plan"] = ref
        return ref

    def _continue_browser_journey_planning(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        journey = self._journey_pin(state)
        attempt = max(1, _int_value(journey.get("attempt"))) + (1 if new_attempt else 0)
        self._set_journey(state, status="planning", attempt=attempt)
        state["browser_stages"] = {**_dict_value(state.get("browser_stages")), "status": "journey"}
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            planning = self._journey_planning_inputs(
                state=state, run_id=run_id, inputs=inputs, latest_artifacts=latest_artifacts
            )
            planner = self._journey_agent(
                run_id=run_id, execution_dir=f"browser_execution/step_review/journey/attempt_{attempt}/plan"
            )
            outcome = self._journey_plan_loop(planner=planner, planning=planning, base_round=None, run_id=run_id)
            if not outcome["ok"]:
                raise HarnessError("browser_journey_plan_invalid", str(outcome["message"]), run_id=run_id)
            plan_ref = self._write_journey_plan(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, planning=planning, outcome=outcome,
                source="plan",
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state, run_id=run_id, progress=exc.progress, latest_artifacts=latest_artifacts,
                resume_status="browser_ready",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="planning",
                phase=_JOURNEY_PHASE,
            )
        return self._journey_planned(state=state, run_id=run_id, latest_artifacts=latest_artifacts, plan_ref=plan_ref)

    def _continue_browser_journey_patch(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        """journey_defect: the Planner patches the failed steps from the error and ARIA at the failure."""

        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = _dict_value(state.get("browser_stages"))
        journey = self._journey_pin(state)
        iteration = _int_value(pin.get("journey_patch_iterations")) + 1
        attempt = max(1, _int_value(journey.get("attempt"))) + (1 if new_attempt else 0)
        self._set_journey(state, status="patching", attempt=attempt)
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            planning = self._journey_planning_inputs(
                state=state, run_id=run_id, inputs=inputs, latest_artifacts=latest_artifacts
            )
            previous = _read_json_artifact(
                self._existing_artifact_path(str(journey.get("plan") or ""), run_id=run_id), run_id=run_id
            )
            failures = _read_json_artifact(
                self._existing_artifact_path(str(journey.get("failures") or ""), run_id=run_id), run_id=run_id
            )
            planner = self._journey_agent(
                run_id=run_id,
                execution_dir=f"browser_execution/step_review/journey/attempt_{attempt}/patch_{iteration}",
            )
            outcome = self._journey_plan_loop(
                planner=planner,
                planning=planning,
                base_round=_journey_patch_round(
                    previous_plan=previous,
                    feedback=failures,
                    manifest=_read_json_artifact(
                        self._existing_artifact_path(str(failures.get("manifest") or ""), run_id=run_id),
                        run_id=run_id,
                    ),
                    variant="baseline",
                ),
                run_id=run_id,
            )
            if not outcome["ok"]:
                raise HarnessError("browser_journey_plan_invalid", str(outcome["message"]), run_id=run_id)
            plan_ref = self._write_journey_plan(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, planning=planning, outcome=outcome,
                source=f"patch_{iteration}",
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state, run_id=run_id, progress=exc.progress, latest_artifacts=latest_artifacts,
                resume_status="browser_journey_patch_needed",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="patching",
                phase=_JOURNEY_PHASE,
            )
        state["browser_stages"] = {**_dict_value(state.get("browser_stages")), "journey_patch_iterations": iteration}
        return self._journey_planned(state=state, run_id=run_id, latest_artifacts=latest_artifacts, plan_ref=plan_ref)

    def _journey_planned(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        plan_ref: str,
    ) -> dict[str, object]:
        plan = _read_json_artifact(self._existing_artifact_path(plan_ref, run_id=run_id), run_id=run_id)
        self._set_journey(state, status="planned", plan=plan_ref)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_journey_planned",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "pending_step_review": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Journey plan ({plan['source']}) cho run {run_id}: {len(_dict_items(plan.get('views')))} view, "
                    f"{len(_dict_items(plan.get('not_reachable')))} obligation not_reachable. Chạy baseline capture tiếp."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_browser_baseline_capture(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        journey = self._set_journey(state, status="capturing")
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            plan = _read_json_artifact(
                self._existing_artifact_path(str(journey.get("plan") or ""), run_id=run_id), run_id=run_id
            )
            views = _dict_items(plan.get("views"))
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=inputs["packet"],
                views=[baseline_view(), *views],
                demo_literals=self._readiness_literals(state, run_id=run_id),
                stage="baseline",
            )
            latest_artifacts["browser_baseline_evidence_manifest"] = suite["manifest_ref"]
            _require_suite_passed(suite, run_id=run_id)
            failures = journey_step_failures(suite["manifest"], views)
            version = _next_closure_artifact_version(self.store, run_id, "browser_journey_verification")
            verification_ref = _write_closure_json(
                self.store,
                run_id,
                f"browser_journey_verification_v{version}.json",
                {
                    "schema_version": "browser_journey_verification_v1",
                    "run_id": run_id,
                    "source": "baseline",
                    "manifest": suite["manifest_ref"],
                    "classification": suite["classification"],
                    "verification": _verification_digest(suite["result"]),
                    "probe_results": suite["probe_results"],
                    "probe_failures": suite["probe_failures"],
                },
            )
            latest_artifacts["browser_journey_verification"] = verification_ref
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc, stage="capturing",
                phase=_JOURNEY_PHASE,
            )
        manifest = suite["manifest"]
        self._set_journey(
            state,
            status="captured",
            baseline_manifest=suite["manifest_ref"],
            current_manifest=suite["manifest_ref"],
            verification=verification_ref,
            needs_regression=False,
        )
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_journey_captured",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "browser_stages": {**pin, "status": "journey_captured"},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "browser_baseline": {
                        "ok": False,
                        "status": "pending_semantic_review",
                        "views": len(views),
                        "captures": len(_dict_items(manifest.get("records"))),
                        "step_failed": _dict_items(_dict_value(manifest.get("summary")).get("step_failed")),
                        "observe_missed": _dict_items(_dict_value(manifest.get("summary")).get("observe_missed")),
                        "not_reachable": len(_dict_items(plan.get("not_reachable"))),
                        "journey_patch_iterations": _int_value(pin.get("journey_patch_iterations")),
                        "plan": journey.get("plan"),
                        "manifest": suite["manifest_ref"],
                        "verification": verification_ref,
                        "probe_results": suite["probe_results"],
                    },
                },
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Journey capture xong cho run {run_id}: {len(views)} view + V-0, "
                    f"{len(_dict_items(manifest.get('records')))} capture, "
                    f"{len(failures)} bước step_failed, "
                    f"{len(_dict_items(_dict_value(manifest.get('summary')).get('observe_missed')))} observe trượt. "
                    f"Chờ semantic execution review; ảnh: `{suite['manifest_ref']}`."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _journey_review_agent(self, *, run_id: str, attempt: int) -> BrowserQaValidator:
        validator = self.browser_qa_validator
        if not bool(self.browser_stage_step_review.get("enabled")):
            return validator
        return BrowserQaValidator(
            ReviewableLlmClient(
                validator.llm_client,
                store=self.store,
                run_id=run_id,
                review=self.browser_stage_step_review,
                call_prefix="browser-journey-validator",
                execution_dir=f"browser_execution/step_review/journey_validation/attempt_{attempt}",
                error_prefix="browser_stage",
                label="Browser Journey Execution Validator",
                carry_approved_output_notes=False,
            )
        )

    def _browser_journey_review_tasks(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        inputs = self._browser_inputs(state=state, run_id=run_id)
        planning = self._journey_planning_inputs(
            state=state,
            run_id=run_id,
            inputs=inputs,
            latest_artifacts=latest_artifacts,
        )
        plan_ref = str(self._journey_pin(state).get("plan") or "")
        plan = _read_json_artifact(self._existing_artifact_path(plan_ref, run_id=run_id), run_id=run_id)
        manifest_ref = str(
            self._journey_pin(state).get("current_manifest")
            or self._journey_pin(state).get("baseline_manifest")
            or ""
        )
        if not manifest_ref:
            raise HarnessError(
                "browser_journey_evidence_missing",
                "Journey execution review needs the current baseline evidence manifest.",
                run_id=run_id,
            )
        manifest = _read_json_artifact(
            self._existing_artifact_path(manifest_ref, run_id=run_id), run_id=run_id
        )
        all_evidence = _manifest_text_evidence(manifest, variant="baseline")
        views = _dict_items(plan.get("views"))
        catalog = _dict_items(planning.get("catalog"))
        tasks: list[dict[str, object]] = []
        for scenario_id in sorted(_string_items(planning.get("runnable"))):
            scenario_views = [item for item in views if str(item.get("scenario_id") or "") == scenario_id]
            view_ids = {str(item.get("view_id") or "") for item in scenario_views}
            obligations = [
                item for item in catalog if scenario_id in _string_items(item.get("scenario_ids"))
            ]
            scenario_obligation_ids = {str(item.get("obligation_id") or "") for item in obligations}
            not_reachable = [
                item
                for item in _dict_items(plan.get("not_reachable"))
                if str(item.get("obligation_id") or "") in scenario_obligation_ids
            ]
            evidence = [item for item in all_evidence if str(item.get("view_id") or "") in view_ids]
            scenario_manifest = {
                **manifest,
                "views": [
                    item for item in _dict_items(manifest.get("views")) if item.get("view_id") in view_ids
                ],
                "records": [
                    item
                    for item in _dict_items(manifest.get("records"))
                    if str(item.get("view_id") or "") in view_ids
                ],
            }
            selection = select_primary_images(
                [{"variant": "baseline", "manifest": scenario_manifest}],
                run_dir=self.store.run_dir(run_id),
                max_images=int(self.settings.browser_max_images_per_call),
                include_baseline=False,
            )
            images = _dict_items(selection.get("images"))
            candidate_units = _scenario_candidate_units(inputs["frozen_packet"], scenario_id)
            scenario = {
                "scenario_id": scenario_id,
                "views": scenario_views,
                "obligations": [_review_obligation(item) for item in obligations],
                "not_reachable": not_reachable,
                "candidate_units": candidate_units,
            }
            context = {
                "evidence": evidence,
                "journey_constraints": {
                    key: _dict_value(planning.get("view")).get(key)
                    for key in ("demo_data", "step_actions", "execution_facts")
                },
                "closure_contract": {
                    key: inputs["contract"].get(key)
                    for key in ("runtime_inputs", "demo_decisions", "verification_stand_ins")
                },
                "omitted_image_capture_keys": _string_items(selection.get("omitted_capture_keys")),
            }
            tasks.append(
                {
                    "scenario": scenario,
                    "context": context,
                    "evidence_refs": {
                        str(item.get("evidence_ref") or "")
                        for item in evidence
                        if item.get("evidence_ref")
                    },
                    "candidate_unit_ids": {
                        str(item.get("unit_id") or "")
                        for item in candidate_units
                        if item.get("unit_id")
                    },
                    "images": images,
                    "prompt": journey_execution_review_prompt(
                        scenario=scenario,
                        review_context=context,
                        images=images,
                    ),
                }
            )
        return {**inputs, "plan": plan, "manifest": manifest, "manifest_ref": manifest_ref}, tasks

    def _continue_browser_journey_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        journey = self._journey_pin(state)
        previous_attempt = _int_value(journey.get("review_attempt"))
        attempt = max(1, previous_attempt)
        if new_attempt or (previous_attempt and str(journey.get("status") or "") == "captured"):
            attempt += 1
        self._set_journey(state, status="reviewing", review_attempt=attempt)
        self.store.write_state(state)
        try:
            if self._journey_pin(state).get("needs_regression"):
                try:
                    regression = self._run_browser_journey_regression(
                        state=state,
                        run_id=run_id,
                        latest_artifacts=latest_artifacts,
                    )
                except LlmStepReviewRequired as exc:
                    return self._pause_for_code_validation_step_review(
                        state=state, run_id=run_id, progress=exc.progress
                    )
                if regression is not None:
                    return regression
            inputs, tasks = self._browser_journey_review_tasks(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts
            )
            cache = CodeValidationTaskCache(
                self.store.run_dir(run_id) / "browser_execution" / "browser_journey_task_cache_v1.json"
            )
            validator = self._journey_review_agent(run_id=run_id, attempt=attempt)
            reviews: list[dict[str, object]] = []
            cache_hits = 0
            for task in tasks:
                # Step review replays approved calls to preserve their persisted ordinal before the next scenario.
                cached = None if self.browser_stage_step_review.get("enabled") else cache.get(str(task["prompt"]))
                if cached is not None:
                    try:
                        review = parse_journey_execution_review(
                            json.dumps(cached, ensure_ascii=False),
                            scenario=task["scenario"],
                            evidence_refs=set(task["evidence_refs"]),
                            candidate_unit_ids=set(task["candidate_unit_ids"]),
                        )
                    except HarnessError:
                        cached = None
                if cached is None:
                    review = validator.review_journey_scenario(
                        scenario=task["scenario"],
                        review_context=task["context"],
                        evidence_refs=set(task["evidence_refs"]),
                        candidate_unit_ids=set(task["candidate_unit_ids"]),
                        images=task["images"],
                        run_id=run_id,
                    )
                    cache.put(str(task["prompt"]), review)
                else:
                    cache_hits += 1
                reviews.append(
                    {
                        "scenario_id": _dict_value(task["scenario"]).get("scenario_id"),
                        **review,
                        "cache_hit": cached is not None,
                    }
                )
            version = _next_closure_artifact_version(self.store, run_id, "browser_journey_reviews")
            reviews_ref = _write_closure_json(
                self.store,
                run_id,
                f"browser_journey_reviews_v{version}.json",
                {
                    "schema_version": "browser_journey_reviews_v1",
                    "run_id": run_id,
                    "manifest": inputs["manifest_ref"],
                    "attempt": attempt,
                    "cache_hits": cache_hits,
                    "reviews": reviews,
                },
            )
            latest_artifacts["browser_journey_reviews"] = reviews_ref
            self._set_journey(state, status="reviewed", reviews=reviews_ref)
            blockers = [
                finding
                for review in reviews
                for finding in _dict_items(review.get("blocking_findings"))
            ]
            # Code before journey: a journey patched around wrong behavior is patched again after the repair, and
            # the review that follows every repair reports the journey defects that are still open.
            implementation = [item for item in blockers if item.get("category") == "implementation"]
            if implementation:
                return self._route_browser_journey_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    problems=implementation,
                    source="browser_journey_review",
                )
            journey_defects = [item for item in blockers if item.get("category") == "journey_defect"]
            if journey_defects:
                return self._route_browser_journey_patch(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    findings=journey_defects,
                )
            return self._close_browser_journey_review(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                inputs=inputs,
                reviews=reviews,
                reviews_ref=reviews_ref,
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state,
                run_id=run_id,
                progress=exc.progress,
                latest_artifacts=latest_artifacts,
                resume_status="browser_journey_captured",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="journey_review",
                phase=_JOURNEY_PHASE,
            )

    def _route_browser_journey_patch(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        findings: list[dict[str, object]],
    ) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        patches = _int_value(pin.get("journey_patch_iterations"))
        if patches >= max(0, int(self.settings.browser_journey_patch_limit)):
            raise HarnessError(
                "browser_journey_patch_limit_exceeded",
                f"Browser journey patch limit reached ({patches}) with an open journey defect.",
                run_id=run_id,
            )
        failures, planning_gaps = _journey_patch_context(
            findings=findings,
            evidence_records=_manifest_text_evidence(inputs["manifest"], variant="baseline"),
            views=_dict_items(inputs["plan"].get("views")),
        )
        if not failures and not planning_gaps:
            raise HarnessError(
                "browser_journey_review_contract_invalid",
                "A journey_defect blocker must cite a failed typed step or a not_reachable planning gap.",
                run_id=run_id,
            )
        version = _next_closure_artifact_version(self.store, run_id, "browser_journey_step_failures")
        failures_ref = _write_closure_json(
            self.store,
            run_id,
            f"browser_journey_step_failures_v{version}.json",
            {
                "schema_version": "browser_journey_step_failures_v1",
                "run_id": run_id,
                "plan": self._journey_pin(state).get("plan"),
                "manifest": inputs["manifest_ref"],
                "findings": findings,
                "failures": failures,
                "planning_gaps": planning_gaps,
            },
        )
        latest_artifacts["browser_journey_step_failures"] = failures_ref
        self._set_journey(state, status="patch_needed", failures=failures_ref)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_journey_patch_needed",
                "latest_artifacts": latest_artifacts,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Journey execution review cần vá typed journey cho run {run_id}.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _close_browser_journey_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        reviews: list[dict[str, object]],
        reviews_ref: str,
    ) -> dict[str, object]:
        warnings = [item for review in reviews for item in _dict_items(review.get("warnings"))]
        observations = [item for review in reviews for item in _string_items(review.get("observations"))]
        self._set_journey(state, status="validated", reviews=reviews_ref, needs_regression=False)
        pin = _dict_value(state.get("browser_stages"))
        baseline = _dict_value(_dict_value(state.get("quality_reports")).get("browser_baseline"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_baseline_captured",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "browser_stages": {**pin, "status": "baseline_captured"},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "browser_baseline": {
                        **baseline,
                        "ok": True,
                        "status": "pass",
                        "semantic_review": reviews_ref,
                        "warning_count": len(warnings),
                        "observation_count": len(observations),
                        "manifest": inputs["manifest_ref"],
                    },
                },
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Journey execution gate pass cho run {run_id}: {len(reviews)} scenario; "
                    "baseline đủ để bắt đầu UI Polish."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ UI Polish contract (L5.4.4)

    def _ui_polish_agents(
        self,
        *,
        run_id: str,
        attempt: int,
    ) -> tuple[UiPolishPlanner, UiPolishValidator]:
        planner, validator = self.polish_planner, self.polish_validator
        if not bool(self.browser_stage_step_review.get("enabled")):
            return planner, validator
        base_dir = f"browser_execution/step_review/polish/attempt_{attempt}"

        def reviewable(client, role: str, label: str) -> ReviewableLlmClient:
            return ReviewableLlmClient(
                client,
                store=self.store,
                run_id=run_id,
                review=self.browser_stage_step_review,
                call_prefix=f"browser-polish-{role}",
                execution_dir=f"{base_dir}/{role}",
                error_prefix="browser_stage",
                label=label,
            )

        return (
            UiPolishPlanner(reviewable(planner.llm_client, "planner", "UI Polish Planner")),
            UiPolishValidator(reviewable(validator.llm_client, "validator", "UI Polish Validator")),
        )

    def _ui_polish_inputs(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
    ) -> dict[str, object]:
        inputs = self._browser_inputs(state=state, run_id=run_id)
        frozen = inputs["frozen_packet"]
        facts = inputs["facts"]
        planning_policy = inputs["planning_policy"]
        target_path: Path = inputs["target_path"]
        ui_units = [
            unit
            for unit in _dict_items(frozen.get("execution_units"))
            if str(unit.get("component_id") or "") == facts.browser_component_id
            and (
                "ui" in {str(value).lower() for value in _string_items(unit.get("surfaces"))}
                or any(
                    str(item.get("surface") or "").lower() == "ui"
                    for item in _dict_items(unit.get("surface_contracts"))
                )
            )
        ]
        if not ui_units:
            raise HarnessError(
                "ui_polish_authority_missing",
                "The frozen Plan has no UI unit in the browser component.",
                run_id=run_id,
            )
        all_visual_contracts = visual_contracts_by_node(frozen)
        design_node_ids = sorted(
            {
                str(node_id)
                for unit in ui_units
                for node_id in _string_items(unit.get("design_node_ids"))
                if str(node_id) in all_visual_contracts
            }
        )
        visual_contracts = {node_id: all_visual_contracts[node_id] for node_id in design_node_ids}
        if not visual_contracts:
            raise HarnessError(
                "ui_polish_authority_missing",
                "The browser UI units have no approved visual_contract.",
                run_id=run_id,
            )

        profile_path = self.store.run_dir(run_id) / "developer_execution" / "ui_visual_profiles_v1.json"
        profile_store = read_visual_profiles(profile_path)
        scopes = sorted({app_visual_scope_id(unit) for unit in ui_units})
        profiles = _dict_value(profile_store.get("profiles"))
        matches = [(scope, _dict_value(profiles.get(scope))) for scope in scopes if _dict_value(profiles.get(scope))]
        if len(matches) != 1:
            raise HarnessError(
                "ui_polish_visual_profile_missing",
                f"Expected one committed app visual profile for {scopes}; found {len(matches)}.",
                run_id=run_id,
            )
        app_scope_id, visual_profile = matches[0]
        profile_ref = {"app_scope_id": app_scope_id, "sha256": "sha256:" + _file_sha256(profile_path)}

        readiness_ref = str(self._readiness_pin(state).get("context") or "")
        readiness = (
            _read_json_artifact(self._existing_artifact_path(readiness_ref, run_id=run_id), run_id=run_id)
            if readiness_ref
            else {}
        )
        demo_paths = {
            str(item.get("path") or "") for item in _dict_items(readiness.get("demo_modules"))
        }
        candidate_paths = sorted(
            {
                path
                for unit in ui_units
                for path in _string_items(unit.get("write_paths"))
                if (target_path / path).is_file()
                and planning_policy.component_for_path(path) == facts.browser_component_id
                and not planning_policy.is_test_path(path)
                and path not in demo_paths
                and path not in _HARNESS_WRITTEN_PATHS
            }
        )
        if not candidate_paths:
            raise HarnessError(
                "ui_polish_scope_missing",
                "The frozen browser UI units expose no existing presentation path to polish.",
                run_id=run_id,
            )

        manifest_ref = latest_artifacts.get("browser_baseline_evidence_manifest")
        if not manifest_ref:
            raise HarnessError("ui_polish_baseline_missing", "UI Polish needs the baseline evidence manifest.", run_id=run_id)
        manifest = _read_json_artifact(self._existing_artifact_path(manifest_ref, run_id=run_id), run_id=run_id)
        selection = select_primary_images(
            [{"variant": "baseline", "manifest": manifest}],
            run_dir=self.store.run_dir(run_id),
            max_images=int(self.settings.browser_max_images_per_call),
        )
        images = _dict_items(selection.get("images"))
        evidence = _selected_evidence(manifest=manifest, images=images)
        evidence_refs = {str(item["evidence_ref"]) for item in evidence}
        authority_refs = set(_dict_value(visual_profile.get("tokens"))) | set(visual_contracts)
        guidance: dict[str, object] = {}
        # Hướng 1 (owner, 2026-11-16): a run that pinned the UI/UX skill consults it here instead of waiting for the
        # Developer to ask; guidance stays out of authority_refs (U4 accepts profile token keys and Design IDs only).
        if str(state.get(UIUX_MODE_STATE_KEY) or "off") == "enforce":
            provider = self.developer_tool_loop.uiux_provider
            if provider is None:
                raise HarnessError(
                    "ui_polish_guidance_unavailable",
                    "This run pins the UI/UX skill but SDLC_UIUX_SKILL_MODE is not enforce.",
                    run_id=run_id,
                )
            guidance = ui_polish_guidance(
                search=provider.search, visual_contracts=visual_contracts, visual_profile=visual_profile
            )
        context = {
            "visual_profile_ref": profile_ref,
            "visual_profile": visual_profile,
            "profile_token_keys": sorted(_dict_value(visual_profile.get("tokens"))),
            "design_node_ids": design_node_ids,
            "visual_contracts": visual_contracts,
            "candidate_paths": candidate_paths,
            "baseline_evidence": evidence,
            "omitted_capture_keys": _string_items(selection.get("omitted_capture_keys")),
            "path_policy": planning_policy.prompt_context(),
            **({"uiux_guidance": guidance} if guidance else {}),
        }
        return {
            "guidance": guidance,
            **inputs,
            "profile_path": profile_path,
            "profile_ref": profile_ref,
            "visual_profile": visual_profile,
            "visual_contracts": visual_contracts,
            "planner_context": context,
            "validator_context": context,
            "images": images,
            "evidence_refs": evidence_refs,
            "authority_refs": authority_refs,
            "forbidden_paths": sorted(demo_paths | _HARNESS_WRITTEN_PATHS),
            "manifest": manifest,
        }

    def _run_ui_polish_contract_loop(
        self,
        *,
        run_id: str,
        attempt: int,
        version: int,
        inputs: dict[str, object],
    ) -> dict[str, object]:
        planner, validator = self._ui_polish_agents(run_id=run_id, attempt=attempt)
        limit = max(0, int(self.settings.implementation_plan_repair_limit))
        rounds: list[dict[str, object]] = []
        previous_round: dict[str, object] | None = None
        validator_round: dict[str, object] | None = None
        previous_fingerprint = ""
        for round_number in range(1, limit + 2):
            proposal = planner.propose(
                planner_context=inputs["planner_context"],
                images=inputs["images"],
                previous_round=previous_round,
                run_id=run_id,
            )
            contract = assemble_ui_polish_contract(
                run_id=run_id,
                version=version,
                planner_output=proposal["output"],
                planning_policy=inputs["planning_policy"],
            )
            issues = polish_contract_rule_issues(
                contract=contract,
                expected_profile_ref=inputs["profile_ref"],
                visual_profile=inputs["visual_profile"],
                visual_contracts=inputs["visual_contracts"],
                frozen_packet=inputs["frozen_packet"],
                planning_policy=inputs["planning_policy"],
                target_path=inputs["target_path"],
                forbidden_paths=inputs["forbidden_paths"],
            )
            record: dict[str, object] = {
                "round": round_number,
                "planner_attempts": proposal["attempts"],
                "rule_review": {"ok": not issues, "issues": issues},
            }
            rounds.append(record)
            if not issues:
                review = validator.review_contract(
                    contract=contract,
                    validator_context=inputs["validator_context"],
                    evidence_refs=inputs["evidence_refs"],
                    authority_refs=inputs["authority_refs"],
                    images=inputs["images"],
                    previous_round=validator_round,
                    run_id=run_id,
                )
                record["validator"] = review
                if review["ok"]:
                    return {
                        "ok": True,
                        "contract": contract,
                        "review": {
                            "schema_version": "ui_polish_contract_review_v1",
                            "ok": True,
                            "rounds": rounds,
                            "warnings": review["warnings"],
                            "observations": review["observations"],
                            "summary": review["summary"],
                        },
                    }
                blockers = list(review["blocking_findings"])
                validator_round = {"previous_blocking_findings": blockers}
            else:
                blockers = issues
            fingerprint = hashlib.sha256(
                json.dumps([proposal["output"], blockers], sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            if fingerprint == previous_fingerprint:
                return {
                    "ok": False,
                    "review": {"schema_version": "ui_polish_contract_review_v1", "ok": False, "rounds": rounds},
                    "message": "UI Polish contract repair stalled: the same output and blockers repeated.",
                }
            previous_fingerprint = fingerprint
            previous_round = {"previous_output": proposal["output"], "blocking_issues": blockers}
        return {
            "ok": False,
            "review": {"schema_version": "ui_polish_contract_review_v1", "ok": False, "rounds": rounds},
            "message": f"UI Polish contract still has blockers after {limit} patch round(s).",
        }

    def _continue_ui_polish_planning(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        polish = self._polish_pin(state)
        attempt = max(1, _int_value(polish.get("attempt"))) + (1 if new_attempt else 0)
        version = _next_closure_artifact_version(self.store, run_id, "ui_polish_contract")
        self._set_polish(state, status="planning", attempt=attempt)
        state["browser_stages"] = {**_dict_value(state.get("browser_stages")), "status": "polish_planning"}
        self.store.write_state(state)
        try:
            inputs = self._ui_polish_inputs(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
            )
            outcome = self._run_ui_polish_contract_loop(
                run_id=run_id,
                attempt=attempt,
                version=version,
                inputs=inputs,
            )
            review_ref = _write_closure_json(
                self.store,
                run_id,
                f"ui_polish_contract_review_v{version}.json",
                outcome["review"],
            )
            latest_artifacts["ui_polish_contract_review"] = review_ref
            if inputs["guidance"]:
                latest_artifacts["ui_polish_guidance"] = _write_closure_json(
                    self.store, run_id, f"ui_polish_guidance_v{version}.json", inputs["guidance"]
                )
            if not outcome["ok"]:
                raise HarnessError("ui_polish_contract_invalid", str(outcome["message"]), run_id=run_id)
            contract_ref = _write_closure_json(
                self.store,
                run_id,
                f"ui_polish_contract_v{version}.json",
                outcome["contract"],
            )
            profile_path: Path = inputs["profile_path"]
            profile_artifact_ref = _artifact_reference(profile_path, root=self.settings.repo_root)
            latest_artifacts.update(
                {
                    "ui_polish_contract": contract_ref,
                    "ui_polish_visual_profile": profile_artifact_ref,
                }
            )
            checkpoint_ref = _write_closure_text(
                self.store,
                run_id,
                f"ui_polish_contract_checkpoint_v{version}.md",
                _ui_polish_checkpoint_markdown(
                    run_id=run_id,
                    version=version,
                    contract=outcome["contract"],
                    review=outcome["review"],
                    images=inputs["images"],
                    manifest_key="browser_baseline_evidence_manifest",
                    guidance=inputs["guidance"],
                ),
            )
            latest_artifacts["ui_polish_contract_checkpoint"] = checkpoint_ref
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state,
                run_id=run_id,
                progress=exc.progress,
                latest_artifacts=latest_artifacts,
                resume_status="browser_baseline_captured",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="polish_planning",
                phase=_POLISH_PHASE,
            )

        checkpoint_id = f"checkpoint_ui_polish_contract_{run_id}_v{version}"
        self._set_polish(state, status="waiting_review", contract=contract_ref, review=review_ref, version=version)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _POLISH_PHASE,
                "status": "waiting_ui_polish_contract_review",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": UI_POLISH_CHECKPOINT_TYPE,
                    "artifact_path": checkpoint_ref,
                    "contract_path": contract_ref,
                },
                "pending_step_review": None,
                "allowed_next_actions": ALLOWED_UI_POLISH_REVIEW_ACTIONS,
                "last_error": None,
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "ui_polish_contract": {
                        "ok": True,
                        "version": version,
                        "unit_count": len(_dict_items(outcome["contract"].get("polish_units"))),
                        "warning_count": len(_dict_items(outcome["review"].get("warnings"))),
                        "rounds": len(_dict_items(outcome["review"].get("rounds"))),
                        "contract": contract_ref,
                        "review": review_ref,
                    },
                },
                "summary_for_user": (
                    f"UI Polish contract v{version} cho run {run_id} đang chờ anh duyệt; "
                    f"{len(_dict_items(outcome['contract'].get('polish_units')))} unit, "
                    f"{len(_dict_items(outcome['review'].get('warnings')))} warning."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def approve_ui_polish_contract_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Approve the presentation contract; Developer execution belongs to L5.4.5."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("status") or "") != "waiting_ui_polish_contract_review":
                raise HarnessError("checkpoint_not_current", f"Run is not waiting for UI Polish review: {run_id}", run_id=run_id)
            checkpoint = _dict_value(state.get("pending_checkpoint"))
            current_id = str(checkpoint.get("checkpoint_id") or "")
            if checkpoint.get("type") != UI_POLISH_CHECKPOINT_TYPE or (checkpoint_id and checkpoint_id != current_id):
                raise HarnessError("checkpoint_not_current", "This is not the current UI Polish checkpoint.", run_id=run_id)
            contract_ref = str(checkpoint.get("contract_path") or "")
            contract = _read_json_artifact(self._existing_artifact_path(contract_ref, run_id=run_id), run_id=run_id)
            profile_path = self.store.run_dir(run_id) / "developer_execution" / "ui_visual_profiles_v1.json"
            expected = _dict_value(_dict_value(contract.get("authority")).get("visual_profile_ref"))
            current = {"app_scope_id": expected.get("app_scope_id"), "sha256": "sha256:" + _file_sha256(profile_path)}
            if current != expected:
                raise HarnessError(
                    "ui_polish_profile_changed",
                    "U5: the committed visual profile changed after the UI Polish contract was proposed.",
                    run_id=run_id,
                )
            pin = _dict_value(state.get("browser_stages"))
            polish = _dict_value(pin.get("polish"))
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": _POLISH_PHASE,
                    "status": "ui_polish_ready",
                    "pending_checkpoint": None,
                    "browser_stages": {
                        **pin,
                        "status": "ui_polish_ready",
                        "polish": {
                            **polish,
                            "status": "approved",
                            "approved_checkpoint_id": current_id,
                            "approved_contract": contract_ref,
                            "approved_at": utc_now_iso(),
                        },
                    },
                    "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                    "last_error": None,
                    "summary_for_user": f"Anh đã duyệt UI Polish contract cho run {run_id}.",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ UI Polish execution (L5.4.5)

    def _ui_polish_packet(self, state: dict[str, object], *, run_id: str) -> dict[str, object]:
        packet_ref = str(self._polish_pin(state).get("work_packet") or "")
        if not packet_ref:
            raise HarnessError("ui_polish_packet_missing", "UI Polish has no approved work packet.", run_id=run_id)
        return _read_json_artifact(self._existing_artifact_path(packet_ref, run_id=run_id), run_id=run_id)

    def _accepted_polish_contract_warnings(
        self, latest_artifacts: dict[str, str], *, run_id: str
    ) -> list[dict[str, object]]:
        review_ref = str(latest_artifacts.get("ui_polish_contract_review") or "")
        if not review_ref:
            return []
        review = _read_json_artifact(self._existing_artifact_path(review_ref, run_id=run_id), run_id=run_id)
        return _dict_items(review.get("warnings"))

    def _continue_ui_polish_act(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            polish = self._polish_pin(state)
            contract_ref = str(polish.get("approved_contract") or "")
            contract = _read_json_artifact(
                self._existing_artifact_path(contract_ref, run_id=run_id), run_id=run_id
            )
            packet_ref = str(polish.get("work_packet") or "")
            if not packet_ref:
                packet_version = _next_closure_artifact_version(self.store, run_id, "ui_polish_work_packet")
                packet = build_ui_polish_work_packet(
                    contract=contract,
                    contract_ref=contract_ref,
                    frozen_packet=inputs["frozen_packet"],
                    version=packet_version,
                    accepted_warnings=self._accepted_polish_contract_warnings(latest_artifacts, run_id=run_id),
                )
                packet_ref = _write_closure_json(
                    self.store, run_id, f"ui_polish_work_packet_v{packet_version}.json", packet
                )
                latest_artifacts["ui_polish_work_packet"] = packet_ref
                allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
                dirty = [
                    path
                    for path in worktree_dirty_paths(inputs["target_path"])
                    if path not in allowed and path != ".codegraph" and not path.startswith(".codegraph/")
                ]
                source_version = _next_closure_artifact_version(self.store, run_id, "ui_polish_source_baseline")
                source_ref = _write_closure_json(
                    self.store,
                    run_id,
                    f"ui_polish_source_baseline_v{source_version}.json",
                    _source_snapshot(inputs["target_path"], sorted(allowed)),
                )
                latest_artifacts["ui_polish_source_baseline"] = source_ref
                artifact_version = _next_phase4_artifact_version(self.store, run_id)
                polish = self._set_polish(
                    state,
                    status="ready",
                    work_packet=packet_ref,
                    source_baseline=source_ref,
                    expected_dirty_paths=dirty,
                    expected_dirty_hashes={path: _file_sha256(inputs["target_path"] / path) for path in dirty},
                    artifact_version=artifact_version,
                    repair_session=None,
                )
                self.store.write_state(state)
            else:
                packet = self._ui_polish_packet(state, run_id=run_id)
                artifact_version = _int_value(polish.get("artifact_version"))
            self._set_polish(state, status="executing")
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=packet,
                target_path=inputs["target_path"],
                latest_artifacts=latest_artifacts,
                mode="ui_polish",
                current_phase=_POLISH_PHASE,
                repair_iteration=0,
                session_iteration=1,
                artifact_version=artifact_version,
                manifest_key="ui_polish_execution_manifest",
                journal_key="ui_polish_execution_journal",
                expected_dirty_paths=_string_items(polish.get("expected_dirty_paths")),
                session_scope="ui_polish",
            )
            report_path = self._polish_change_report(
                state=state,
                run_id=run_id,
                inputs=inputs,
                result=result,
                artifact_version=artifact_version,
            )
        except DeveloperExecutionError as exc:
            return self._pause_browser_developer(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                mode="ui_polish",
                iteration=0,
                artifact_version=_int_value(self._polish_pin(state).get("artifact_version")),
                manifest_key="ui_polish_execution_manifest",
                journal_key="ui_polish_execution_journal",
                phase=_POLISH_PHASE,
                label="UI Polish",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc,
                stage="polish_executing", phase=_POLISH_PHASE,
            )
        return self._polish_written(
            state=state,
            run_id=run_id,
            latest_artifacts=latest_artifacts,
            report_path=report_path,
            summary=f"Developer đã hoàn thành UI Polish cho run {run_id}; chạy lại browser suite.",
        )

    def _continue_ui_polish_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = _dict_value(state.get("browser_stages"))
        iteration = _int_value(_dict_value(pin.get("repair_iterations")).get("polish")) + 1
        session = _dict_value(self._polish_pin(state).get("repair_session"))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            packet = self._ui_polish_packet(state, run_id=run_id)
            feedback_ref = str(latest_artifacts.get("ui_polish_repair_feedback") or "")
            if not feedback_ref:
                raise HarnessError("ui_polish_repair_feedback_missing", "UI Polish repair has no feedback.", run_id=run_id)
            feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_ref, run_id=run_id), run_id=run_id
            )
            if session.get("feedback") != feedback_ref:
                allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
                session = {
                    "feedback": feedback_ref,
                    "artifact_version": _next_phase4_artifact_version(self.store, run_id),
                    "expected_dirty_paths": [
                        path
                        for path in worktree_dirty_paths(inputs["target_path"])
                        if path not in allowed and path != ".codegraph" and not path.startswith(".codegraph/")
                    ],
                }
                self._set_polish(state, status="repairing", repair_session=session)
                self.store.write_state(state)
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=packet,
                target_path=inputs["target_path"],
                latest_artifacts=latest_artifacts,
                mode="ui_polish_repair",
                current_phase=_POLISH_PHASE,
                repair_iteration=iteration,
                session_iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="ui_polish_repair_execution_manifest",
                journal_key="ui_polish_repair_execution_journal",
                repair_feedback=feedback,
                repair_scope="ui_polish",
                expected_dirty_paths=_string_items(session.get("expected_dirty_paths")),
            )
            report_path = self._polish_change_report(
                state=state,
                run_id=run_id,
                inputs=inputs,
                result=result,
                artifact_version=_int_value(session.get("artifact_version")),
                repair={"repair_iteration": iteration, "repair_feedback": feedback_ref},
            )
        except DeveloperExecutionError as exc:
            return self._pause_browser_developer(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                mode="ui_polish_repair",
                iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="ui_polish_repair_execution_manifest",
                journal_key="ui_polish_repair_execution_journal",
                phase=_POLISH_PHASE,
                label="UI Polish repair",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc,
                stage="polish_repairing", phase=_POLISH_PHASE,
            )
        pin = _dict_value(state.get("browser_stages"))
        state["browser_stages"] = {
            **pin,
            "repair_iterations": {**_dict_value(pin.get("repair_iterations")), "polish": iteration},
        }
        self._set_polish(state, repair_session=None)
        return self._polish_written(
            state=state,
            run_id=run_id,
            latest_artifacts=latest_artifacts,
            report_path=report_path,
            summary=f"UI Polish repair {iteration} xong cho run {run_id}; chạy lại browser suite.",
        )

    def _polish_change_report(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        inputs: dict[str, object],
        result,
        artifact_version: int,
        repair: dict[str, object] | None = None,
    ) -> str:
        return write_phase4_code_change_report(
            store=self.store,
            run_id=run_id,
            report={
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": inputs["frozen_packet"].get("issue_key"),
                "target_repo": str(inputs["target_path"]),
                "branch": inputs["target_info"].current_branch,
                "summary": result.summary,
                "files": result.files,
                "execution_mode": "tool_loop",
                "ui_polish_work_packet": self._polish_pin(state).get("work_packet"),
                "developer_execution_manifest": result.manifest_path,
                "developer_execution_journal": result.journal_path,
                **(repair or {}),
            },
            version=artifact_version,
        )

    def _polish_written(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        report_path: str,
        summary: str,
    ) -> dict[str, object]:
        latest_artifacts["code_change_report"] = report_path
        self._set_polish(state, status="written")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _POLISH_PHASE,
                "status": "ui_polish_written",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": summary,
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_ui_polish_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        for key in ("ui_polish_regression", "ui_polish_result_review", "ui_polish_result_checkpoint"):
            latest_artifacts.pop(key, None)
        self._set_polish(state, status="verifying")
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            polish = self._polish_pin(state)
            packet = self._ui_polish_packet(state, run_id=run_id)
            contract = _read_json_artifact(
                self._existing_artifact_path(str(polish.get("approved_contract") or ""), run_id=run_id),
                run_id=run_id,
            )
            allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
            drift = readiness_scope_drift(
                baselines=_string_dict(polish.get("expected_dirty_hashes")),
                current_dirty=worktree_dirty_paths(inputs["target_path"]),
                current_sha256=lambda path: _file_sha256(inputs["target_path"] / path),
                allowed={*allowed, *_HARNESS_WRITTEN_PATHS},
            )
            if drift["drifted"] or drift["outside"]:
                raise HarnessError(
                    "ui_polish_scope_conflict",
                    f"Files changed outside the approved Polish scope: drifted={drift['drifted']}, new={drift['outside']}.",
                    run_id=run_id,
                )
            journey_ref = str(self._journey_pin(state).get("plan") or "")
            journey = _read_json_artifact(self._existing_artifact_path(journey_ref, run_id=run_id), run_id=run_id)
            views = _dict_items(journey.get("views"))
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=packet,
                views=[baseline_view(), *views],
                demo_literals=self._readiness_literals(state, run_id=run_id),
                stage="polish",
            )
            latest_artifacts["browser_polish_evidence_manifest"] = suite["manifest_ref"]
            baseline_ref = str(self._journey_pin(state).get("baseline_manifest") or "")
            baseline = _read_json_artifact(self._existing_artifact_path(baseline_ref, run_id=run_id), run_id=run_id)
            regressions = _baseline_capture_regressions(baseline, suite["manifest"])
            source_baseline = _read_json_artifact(
                self._existing_artifact_path(str(polish.get("source_baseline") or ""), run_id=run_id), run_id=run_id
            )
            source_diff = _source_diff(inputs["target_path"], source_baseline)
            diff_version = _next_closure_artifact_version(self.store, run_id, "ui_polish_source_diff")
            diff_ref = _write_closure_json(
                self.store, run_id, f"ui_polish_source_diff_v{diff_version}.json", source_diff
            )
            latest_artifacts["ui_polish_source_diff"] = diff_ref
            diffs = aria_diff(baseline, suite["manifest"])
            version = _next_closure_artifact_version(self.store, run_id, "ui_polish_verification")
            report = {
                "schema_version": "ui_polish_verification_v1",
                "run_id": run_id,
                "contract": polish.get("approved_contract"),
                "work_packet": polish.get("work_packet"),
                "classification": suite["classification"],
                "verification": _verification_digest(suite["result"]),
                "build_scan": suite["stages"]["frontend_build"].build_scan if "frontend_build" in suite["stages"] else None,
                "probe_results": suite["probe_results"],
                "probe_failures": suite["probe_failures"],
                "baseline_manifest": baseline_ref,
                "after_manifest": suite["manifest_ref"],
                "baseline_regressions": regressions,
                "aria_diff": diffs,
                "source_diff": diff_ref,
                "worktree_fingerprint": _worktree_fingerprint(inputs["target_path"]),
            }
            report_ref = _write_closure_json(
                self.store, run_id, f"ui_polish_verification_v{version}.json", report
            )
            latest_artifacts["ui_polish_verification"] = report_ref
            if suite["classification"]["kind"] == "implementation":
                return self._route_ui_polish_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    contract=contract,
                    packet=packet,
                    source=str(suite["classification"].get("stage_id") or "verification"),
                    problems=_polish_verification_problems(suite),
                    summary=suite["result"].reason,
                )
            if suite["classification"]["kind"] != "passed":
                raise HarnessError(
                    "ui_polish_environment_failed"
                    if suite["classification"]["kind"] == "environment"
                    else "ui_polish_verification_policy_failed",
                    f"{suite['classification'].get('stage_id') or 'verification'}: {suite['result'].reason}",
                    run_id=run_id,
                )
            if regressions:
                return self._route_ui_polish_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    contract=contract,
                    packet=packet,
                    source="baseline_regression",
                    problems=[
                        {
                            "source": "baseline_regression",
                            "paths": [],
                            "issue": f"Baseline-passing capture {item['view_id']}:{item['viewport_id']} now fails.",
                            "expected_change": "The approved journey capture passes again without reverting unrelated polish.",
                        }
                        for item in regressions
                    ],
                    summary=f"{len(regressions)} baseline-passing view/viewport capture(s) regressed.",
                )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc,
                stage="polish_verifying", phase=_POLISH_PHASE,
            )
        self._set_polish(
            state,
            status="verified",
            verification=report_ref,
            after_manifest=suite["manifest_ref"],
        )
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _POLISH_PHASE,
                "status": "ui_polish_verified",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "ui_polish_verified"},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "ui_polish_verification": {
                        "ok": True,
                        "report": report_ref,
                        "after_manifest": suite["manifest_ref"],
                        "aria_changed": sum(1 for item in diffs if item.get("status") != "unchanged"),
                        "repair_iterations": _int_value(_dict_value(pin.get("repair_iterations")).get("polish")),
                    },
                },
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"UI Polish verification pass cho run {run_id}; "
                    f"{sum(1 for item in diffs if item.get('status') != 'unchanged')} ARIA capture thay đổi, chờ semantic review."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _ui_polish_result_validator(self, *, run_id: str, attempt: int) -> UiPolishValidator:
        validator = self.polish_validator
        if not bool(self.browser_stage_step_review.get("enabled")):
            return validator
        return UiPolishValidator(
            ReviewableLlmClient(
                validator.llm_client,
                store=self.store,
                run_id=run_id,
                review=self.browser_stage_step_review,
                call_prefix="browser-polish-result-validator",
                execution_dir=f"browser_execution/step_review/polish_result/attempt_{attempt}",
                error_prefix="browser_stage",
                label="UI Polish Result Validator",
            )
        )

    def _continue_ui_polish_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        polish = self._polish_pin(state)
        previous_attempt = _int_value(polish.get("result_review_attempt"))
        attempt = max(1, previous_attempt)
        # A review after a fresh verification (a Polish repair round) is a new attempt: it must never replay the
        # previous round's approved exchanges, as the Journey review does after a fresh capture.
        if new_attempt or (previous_attempt and str(polish.get("status") or "") == "verified"):
            attempt += 1
        self._set_polish(state, status="reviewing", result_review_attempt=attempt)
        self.store.write_state(state)
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            packet = self._ui_polish_packet(state, run_id=run_id)
            contract = _read_json_artifact(
                self._existing_artifact_path(str(polish.get("approved_contract") or ""), run_id=run_id),
                run_id=run_id,
            )
            verification = _read_json_artifact(
                self._existing_artifact_path(str(polish.get("verification") or ""), run_id=run_id), run_id=run_id
            )
            changes = approved_closure_changes(
                contract=inputs["contract"], packet=inputs["packet"], pin=inputs["pin"]
            )
            readiness_ref = str(self._readiness_pin(state).get("context") or "")
            readiness = (
                _read_json_artifact(self._existing_artifact_path(readiness_ref, run_id=run_id), run_id=run_id)
                if readiness_ref else {}
            )
            polish_paths = _string_items(_dict_value(packet.get("output_contract")).get("allowed_paths"))
            changes = {
                **changes,
                "note": str(changes.get("note") or "")
                + " Browser Readiness and UI Polish were also owner-approved bounded changes.",
                "changed_paths": sorted(
                    {
                        *_string_items(changes.get("changed_paths")),
                        *_string_items(readiness.get("write_scope")),
                        *polish_paths,
                    }
                ),
                "browser_stage_changes": {
                    "readiness_context": readiness_ref,
                    "ui_polish_contract": polish.get("approved_contract"),
                    "ui_polish_paths": polish_paths,
                },
            }
            trace_result = self._validate_code_trace_snapshot(
                state=state,
                artifact_paths=latest_artifacts,
                run_id=run_id,
                target_path=inputs["target_path"],
                work_packet=self._browser_repair_work_packet(
                    inputs=inputs,
                    latest_artifacts=latest_artifacts,
                    run_id=run_id,
                ),
                verification_evidence=verification,
                approved_closure_changes=changes,
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_code_validation_step_review(state=state, run_id=run_id, progress=exc.progress)
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc,
                stage="polish_review", phase=_POLISH_PHASE,
            )

        try:
            regression_ref = str(latest_artifacts.get("ui_polish_regression") or "")
            if not regression_ref:
                version = _next_closure_artifact_version(self.store, run_id, "ui_polish_regression")
                regression_ref = _write_closure_json(
                    self.store, run_id, f"ui_polish_regression_v{version}.json", trace_result.to_dict()
                )
                latest_artifacts["ui_polish_regression"] = regression_ref
            if not trace_result.ok:
                if not _is_repairable_code_trace_failure(trace_result):
                    raise HarnessError(
                        _nonrepairable_code_trace_error_code(trace_result),
                        f"Code Validator regression after UI Polish did not pass: {trace_result.summary}",
                        run_id=run_id,
                    )
                return self._route_ui_polish_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    contract=contract,
                    packet=packet,
                    source="validator_code_trace",
                    problems=_regression_problems(trace_result),
                    summary=str(trace_result.summary),
                    extra={"missing_links": list(getattr(trace_result, "missing_links", []) or [])},
                )

            before_ref = str(self._journey_pin(state).get("baseline_manifest") or "")
            after_ref = str(polish.get("after_manifest") or "")
            before = _read_json_artifact(self._existing_artifact_path(before_ref, run_id=run_id), run_id=run_id)
            after = _read_json_artifact(self._existing_artifact_path(after_ref, run_id=run_id), run_id=run_id)
            selection = select_primary_images(
                [{"variant": "before", "manifest": before}, {"variant": "after", "manifest": after}],
                run_dir=self.store.run_dir(run_id),
                max_images=int(self.settings.browser_max_images_per_call),
            )
            images = _dict_items(selection.get("images"))
            before_text, after_text = _share_identical_values(
                _manifest_text_evidence(before, variant="before"),
                _manifest_text_evidence(after, variant="after"),
            )
            evidence_refs = {
                str(item.get("evidence_ref") or "") for item in [*before_text, *after_text]
                if item.get("evidence_ref")
            }
            visual_refs = {str(item.get("label") or "") for item in images if item.get("label")}
            profile_path = self.store.run_dir(run_id) / "developer_execution" / "ui_visual_profiles_v1.json"
            source_diff = _read_json_artifact(
                self._existing_artifact_path(str(latest_artifacts.get("ui_polish_source_diff") or ""), run_id=run_id),
                run_id=run_id,
            )
            contract_review_ref = str(latest_artifacts.get("ui_polish_contract_review") or "")
            accepted_contract_review = _read_json_artifact(
                self._existing_artifact_path(contract_review_ref, run_id=run_id), run_id=run_id
            )
            authority_refs = {
                ref
                for unit in _dict_items(contract.get("polish_units"))
                for ref in _string_items(unit.get("authority_refs"))
            }
            guidance_ref = str(latest_artifacts.get("ui_polish_guidance") or "")
            guidance = (
                _read_json_artifact(self._existing_artifact_path(guidance_ref, run_id=run_id), run_id=run_id)
                if guidance_ref
                else {}
            )
            result_review = self._ui_polish_result_validator(run_id=run_id, attempt=attempt).review_result(
                contract=contract,
                result_context={
                    "visual_profile": read_visual_profiles(profile_path),
                    "source_diff": source_diff,
                    "before_evidence": before_text,
                    "after_evidence": after_text,
                    "aria_diff": verification.get("aria_diff"),
                    "omitted_image_capture_keys": _string_items(selection.get("omitted_capture_keys")),
                    "accepted_contract_warnings": _dict_items(accepted_contract_review.get("warnings")),
                    "code_validator_regression": {"ok": True, "artifact": regression_ref},
                    **({"uiux_guidance": guidance} if guidance else {}),
                },
                evidence_refs=evidence_refs,
                visual_evidence_refs=visual_refs,
                authority_refs=authority_refs,
                images=images,
                run_id=run_id,
            )
            result_version = _next_closure_artifact_version(self.store, run_id, "ui_polish_result_review")
            result_ref = _write_closure_json(
                self.store, run_id, f"ui_polish_result_review_v{result_version}.json", result_review
            )
            latest_artifacts["ui_polish_result_review"] = result_ref
            if result_review["blocking_findings"]:
                return self._route_ui_polish_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    contract=contract,
                    packet=packet,
                    source="ui_polish_result_review",
                    problems=self._polish_result_repair_problems(
                        state, run_id=run_id, findings=result_review["blocking_findings"], after_manifest=after
                    ),
                    summary=f"UI Polish result review found {len(result_review['blocking_findings'])} blocker(s).",
                )
            checkpoint_version = _next_closure_artifact_version(self.store, run_id, "ui_polish_result_checkpoint", suffix=".md")
            checkpoint_ref = _write_closure_text(
                self.store,
                run_id,
                f"ui_polish_result_checkpoint_v{checkpoint_version}.md",
                _ui_polish_result_checkpoint_markdown(
                    run_id=run_id,
                    review=result_review,
                    aria_diffs=_dict_items(verification.get("aria_diff")),
                    images=images,
                ),
            )
            latest_artifacts["ui_polish_result_checkpoint"] = checkpoint_ref
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state,
                run_id=run_id,
                progress=exc.progress,
                latest_artifacts=latest_artifacts,
                resume_status="ui_polish_verified",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts, exc=exc,
                stage="polish_review", phase=_POLISH_PHASE,
            )

        checkpoint_id = f"checkpoint_ui_polish_result_{run_id}_v{checkpoint_version}"
        self._set_polish(state, status="waiting_result_review", result_review=result_ref)
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _POLISH_PHASE,
                "status": "waiting_ui_polish_review",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": UI_POLISH_RESULT_CHECKPOINT_TYPE,
                    "artifact_path": checkpoint_ref,
                    "review_path": result_ref,
                },
                "browser_stages": {**pin, "status": "waiting_ui_polish_review"},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "ui_polish": {
                        "ok": True,
                        "review": result_ref,
                        "regression": regression_ref,
                        "warning_count": len(_dict_items(result_review.get("warnings"))),
                        "repair_iterations": _int_value(_dict_value(pin.get("repair_iterations")).get("polish")),
                    },
                },
                "pending_step_review": None,
                "allowed_next_actions": ALLOWED_UI_POLISH_REVIEW_ACTIONS,
                "last_error": None,
                "summary_for_user": f"UI Polish cho run {run_id} đã pass review và đang chờ anh duyệt ảnh trước/sau.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _polish_result_repair_problems(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        findings: list[dict[str, object]],
        after_manifest: dict[str, object],
    ) -> list[dict[str, object]]:
        """Result-review blockers with the after-Polish captures they cite, as the journey repair path does."""

        plan_ref = str(self._journey_pin(state).get("plan") or "")
        plan = (
            _read_json_artifact(self._existing_artifact_path(plan_ref, run_id=run_id), run_id=run_id)
            if plan_ref
            else {}
        )
        return _findings_with_evidence(
            findings,
            manifest=after_manifest,
            views=_dict_items(plan.get("views")),
            variant="after",
            fields=_POLISH_FINDING_EVIDENCE_FIELDS,
        )

    def _route_ui_polish_repair(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        contract: dict[str, object],
        packet: dict[str, object],
        source: str,
        problems: list[dict[str, object]],
        summary: str,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        iterations = _int_value(_dict_value(pin.get("repair_iterations")).get("polish"))
        if iterations >= max(0, int(self.settings.browser_polish_repair_limit)):
            raise HarnessError(
                "ui_polish_repair_limit_exceeded",
                f"UI Polish repair limit reached ({iterations}) with an open {source} failure: {summary}",
                run_id=run_id,
            )
        findings = _polish_repair_findings(contract=contract, problems=problems, run_id=run_id)
        version = _next_closure_artifact_version(self.store, run_id, "ui_polish_repair_feedback")
        feedback = closure_repair_feedback(
            run_id=run_id,
            version=version,
            repair_iteration=iterations + 1,
            source=source,
            summary=summary,
            findings=findings,
            plan_extensions=[],
            allowed_paths=_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")),
            extra=extra or {},
        )
        feedback.update(
            {
                "version": f"ui_polish_repair_feedback_v{version}",
                "status": "ui_polish_repair_needed",
                "required_action": (
                    "Repair only the cited UI Polish units inside the approved contract. Preserve Requirement, Design, "
                    "journey behavior and every browser-stage result that already passes."
                ),
            }
        )
        feedback_ref = _write_closure_json(
            self.store, run_id, f"ui_polish_repair_feedback_v{version}.json", feedback
        )
        latest_artifacts["ui_polish_repair_feedback"] = feedback_ref
        self._set_polish(state, status="repair_needed")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _POLISH_PHASE,
                "status": "ui_polish_repair_needed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"UI Polish cần repair ({source}) cho run {run_id}: {summary}",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def approve_ui_polish_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("status") or "") != "waiting_ui_polish_review":
                raise HarnessError("checkpoint_not_current", f"Run is not waiting for UI Polish result approval: {run_id}", run_id=run_id)
            checkpoint = _dict_value(state.get("pending_checkpoint"))
            current_id = str(checkpoint.get("checkpoint_id") or "")
            if checkpoint.get("type") != UI_POLISH_RESULT_CHECKPOINT_TYPE or (checkpoint_id and checkpoint_id != current_id):
                raise HarnessError("checkpoint_not_current", "This is not the current UI Polish result checkpoint.", run_id=run_id)
            pin = _dict_value(state.get("browser_stages"))
            polish = _dict_value(pin.get("polish"))
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": _POLISH_PHASE,
                    "status": "ui_polished",
                    "pending_checkpoint": None,
                    "browser_stages": {
                        **pin,
                        "status": "ui_polished",
                        "polish": {
                            **polish,
                            "status": "approved_result",
                            "approved_result_checkpoint_id": current_id,
                            "approved_result_at": utc_now_iso(),
                        },
                        "qa": {**_dict_value(pin.get("qa")), "status": "ready"},
                    },
                    "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                    "last_error": None,
                    "summary_for_user": f"Anh đã duyệt kết quả UI Polish cho run {run_id}.",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

    # ------------------------------------------------------------------ Browser QA (L5.4.6)

    def _route_browser_journey_repair(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        problems: list[dict[str, object]],
        source: str,
    ) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        iterations = _int_value(_dict_value(pin.get("repair_iterations")).get("qa"))
        if iterations >= max(0, int(self.settings.browser_qa_repair_limit)):
            raise HarnessError(
                "browser_qa_repair_limit_exceeded",
                f"Shared browser behavior repair limit reached ({iterations}) with an open {source} blocker.",
                run_id=run_id,
            )
        units = {
            str(item.get("unit_id") or ""): item
            for item in _dict_items(inputs["frozen_packet"].get("execution_units"))
            if str(item.get("unit_id") or "")
        }
        cited = {
            unit_id
            for problem in problems
            for unit_id in _string_items(problem.get("unit_ids"))
            if unit_id in units
        }
        if not cited:
            cited = set(_string_items(self._journey_pin(state).get("repair_unit_ids"))) & set(units)
        if not cited:
            raise HarnessError(
                "browser_journey_repair_unit_missing",
                "Journey implementation blockers do not map to a frozen Plan unit.",
                run_id=run_id,
            )
        allowed_paths = sorted(
            {path for unit_id in cited for path in _string_items(units[unit_id].get("write_paths"))}
        )
        findings = [
            {
                **problem,
                "source": str(problem.get("source") or source),
                "unit_ids": sorted(set(_string_items(problem.get("unit_ids"))) & cited) or sorted(cited),
                "issue": str(
                    problem.get("issue") or problem.get("message") or "Browser journey implementation blocker."
                ),
                "expected_change": str(
                    problem.get("expected_change") or "Restore the approved observable browser outcome."
                ),
            }
            for problem in _findings_with_evidence(
                problems,
                manifest=inputs.get("manifest"),
                views=_dict_items(_dict_value(inputs.get("plan")).get("views")),
                variant="baseline",
            )
        ]
        version = _next_closure_artifact_version(self.store, run_id, "browser_journey_repair_feedback")
        feedback = closure_repair_feedback(
            run_id=run_id,
            version=version,
            repair_iteration=iterations + 1,
            source=source,
            summary=f"Journey execution review found {len(findings)} implementation blocker(s).",
            findings=findings,
            plan_extensions=[],
            allowed_paths=allowed_paths,
            extra={},
        )
        feedback.update(
            {
                "version": f"browser_journey_repair_feedback_v{version}",
                "status": "browser_journey_repair_needed",
                "required_action": (
                    "Repair only the cited frozen Plan units so the planned browser states become observable. "
                    "Preserve approved Requirement, Design, closure and every journey outcome that already passes."
                    + (_FINDING_EVIDENCE_NOTE if any(item.get("evidence") for item in findings) else "")
                ),
            }
        )
        feedback_ref = _write_closure_json(
            self.store, run_id, f"browser_journey_repair_feedback_v{version}.json", feedback
        )
        latest_artifacts["browser_journey_repair_feedback"] = feedback_ref
        journey = self._journey_pin(state)
        self._set_journey(
            state,
            status="repair_needed",
            repair_unit_ids=sorted(cited),
            repair_paths=sorted(set(_string_items(journey.get("repair_paths"))) | set(allowed_paths)),
            repair_session=None,
        )
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_journey_repair_needed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Journey execution cần Developer repair ({source}) cho run {run_id}.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_browser_journey_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = _dict_value(state.get("browser_stages"))
        iteration = _int_value(_dict_value(pin.get("repair_iterations")).get("qa")) + 1
        journey = self._journey_pin(state)
        session = _dict_value(journey.get("repair_session"))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            feedback_ref = str(latest_artifacts.get("browser_journey_repair_feedback") or "")
            if not feedback_ref:
                raise HarnessError(
                    "browser_journey_repair_feedback_missing",
                    "Journey execution repair has no feedback.",
                    run_id=run_id,
                )
            feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_ref, run_id=run_id), run_id=run_id
            )
            if session.get("feedback") != feedback_ref:
                session = {
                    "feedback": feedback_ref,
                    "artifact_version": _next_phase4_artifact_version(self.store, run_id),
                    "expected_dirty_paths": [
                        path
                        for path in worktree_dirty_paths(inputs["target_path"])
                        if path != ".codegraph" and not path.startswith(".codegraph/")
                    ],
                }
                self._set_journey(state, status="repairing", repair_session=session)
                self.store.write_state(state)
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=self._browser_repair_work_packet(
                    inputs=inputs,
                    latest_artifacts=latest_artifacts,
                    run_id=run_id,
                ),
                target_path=inputs["target_path"],
                latest_artifacts=latest_artifacts,
                mode="browser_journey_repair",
                current_phase=_JOURNEY_PHASE,
                repair_iteration=iteration,
                session_iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="browser_journey_repair_execution_manifest",
                journal_key="browser_journey_repair_execution_journal",
                repair_feedback=feedback,
                repair_scope="browser_journey",
                expected_dirty_paths=_string_items(session.get("expected_dirty_paths")),
            )
            report_ref = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report={
                    "run_id": run_id,
                    "source_ref": state.get("source_ref"),
                    "issue_key": inputs["frozen_packet"].get("issue_key"),
                    "target_repo": str(inputs["target_path"]),
                    "branch": inputs["target_info"].current_branch,
                    "summary": result.summary,
                    "files": result.files,
                    "execution_mode": "tool_loop",
                    "browser_journey_repair_feedback": feedback_ref,
                    "repair_iteration": iteration,
                    "developer_execution_manifest": result.manifest_path,
                    "developer_execution_journal": result.journal_path,
                },
                version=_int_value(session.get("artifact_version")),
            )
            latest_artifacts["code_change_report"] = report_ref
        except DeveloperExecutionError as exc:
            return self._pause_browser_developer(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                mode="browser_journey_repair",
                iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="browser_journey_repair_execution_manifest",
                journal_key="browser_journey_repair_execution_journal",
                phase=_JOURNEY_PHASE,
                label="Browser journey repair",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="journey_repairing",
                phase=_JOURNEY_PHASE,
            )
        pin = _dict_value(state.get("browser_stages"))
        state["browser_stages"] = {
            **pin,
            "repair_iterations": {**_dict_value(pin.get("repair_iterations")), "qa": iteration},
        }
        self._set_journey(state, status="repair_written", repair_session=None)
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_journey_repair_written",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "browser_journey_repair_written"},
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Journey implementation repair {iteration} xong; chạy lại suite và cùng journey."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_browser_journey_repair_verification(
        self, *, state: dict[str, object], run_id: str
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            plan = _read_json_artifact(
                self._existing_artifact_path(str(self._journey_pin(state).get("plan") or ""), run_id=run_id),
                run_id=run_id,
            )
            iteration = _int_value(
                _dict_value(_dict_value(state.get("browser_stages")).get("repair_iterations")).get("qa")
            )
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=inputs["frozen_packet"],
                views=[baseline_view(), *_dict_items(plan.get("views"))],
                demo_literals=self._readiness_literals(state, run_id=run_id),
                stage=f"journey_repair_{iteration}",
            )
            version = _next_closure_artifact_version(self.store, run_id, "browser_journey_verification")
            verification_ref = _write_closure_json(
                self.store,
                run_id,
                f"browser_journey_verification_v{version}.json",
                {
                    "schema_version": "browser_journey_verification_v1",
                    "run_id": run_id,
                    "source": "implementation_repair",
                    "repair_iteration": iteration,
                    # Final Verification F1 proves the published tree is this verified tree (L5.5.6).
                    "worktree_fingerprint": _worktree_fingerprint(inputs["target_path"]),
                    "manifest": suite["manifest_ref"],
                    "classification": suite["classification"],
                    "verification": _verification_digest(suite["result"]),
                    "probe_results": suite["probe_results"],
                    "probe_failures": suite["probe_failures"],
                },
            )
            latest_artifacts.update(
                {
                    "browser_baseline_evidence_manifest": suite["manifest_ref"],
                    "browser_journey_verification": verification_ref,
                }
            )
            if suite["classification"]["kind"] == "implementation":
                unit_ids = _string_items(self._journey_pin(state).get("repair_unit_ids"))
                return self._route_browser_journey_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    problems=[
                        {
                            **_suite_failure_problem(suite),
                            "unit_ids": unit_ids,
                            "expected_change": "Restore the approved full suite, probes and browser capture.",
                        }
                    ],
                    source="browser_journey_verification",
                )
            if suite["classification"]["kind"] != "passed":
                raise HarnessError(
                    "browser_baseline_environment_failed"
                    if suite["classification"]["kind"] == "environment"
                    else "browser_journey_verification_policy_failed",
                    f"{suite['classification'].get('stage_id') or 'verification'}: {suite['result'].reason}",
                    run_id=run_id,
                )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="journey_repair_verifying",
                phase=_JOURNEY_PHASE,
            )
        self._set_journey(
            state,
            status="captured",
            current_manifest=suite["manifest_ref"],
            baseline_manifest=suite["manifest_ref"],
            verification=verification_ref,
            needs_regression=True,
        )
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _JOURNEY_PHASE,
                "status": "browser_journey_captured",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "journey_captured"},
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": "Journey repair verification pass; chạy regression rồi semantic review lại.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _route_browser_qa_repair(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        problems: list[dict[str, object]],
        source: str,
    ) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        iterations = _int_value(_dict_value(pin.get("repair_iterations")).get("qa"))
        if iterations >= max(0, int(self.settings.browser_qa_repair_limit)):
            raise HarnessError(
                "browser_qa_repair_limit_exceeded",
                f"Browser QA repair limit reached ({iterations}) with an open {source} blocker.",
                run_id=run_id,
            )
        units = {
            str(item.get("unit_id") or ""): item
            for item in _dict_items(inputs["frozen_packet"].get("execution_units"))
            if str(item.get("unit_id") or "")
        }
        cited: set[str] = set()
        for problem in problems:
            problem_unit_ids = [
                unit_id for unit_id in _string_items(problem.get("unit_ids")) if unit_id in units
            ]
            problem_paths = _string_items(problem.get("paths"))
            for unit_id in problem_unit_ids:
                if unit_id not in units:
                    continue
                paths = problem_paths or _string_items(units[unit_id].get("write_paths"))
                selected = select_plan_unit_owners(
                    paths=paths,
                    cited_unit_ids=[unit_id],
                    plan_units=list(units.values()),
                )
                cited.update(_string_items(selected.get("unit_ids")))
            if not problem_unit_ids and problem_paths:
                selected = select_plan_unit_owners(
                    paths=problem_paths,
                    cited_unit_ids=[],
                    plan_units=list(units.values()),
                )
                cited.update(_string_items(selected.get("unit_ids")))
        if not cited:
            cited = set(_string_items(self._qa_pin(state).get("repair_unit_ids"))) & set(units)
        if not cited:
            raise HarnessError(
                "browser_qa_repair_unit_missing",
                "Browser QA implementation blockers do not map to a frozen Plan unit.",
                run_id=run_id,
            )
        allowed_paths = sorted(
            {path for unit_id in cited for path in _string_items(units[unit_id].get("write_paths"))}
        )
        findings = [
            {
                **problem,
                "source": str(problem.get("source") or source),
                "unit_ids": sorted(set(_string_items(problem.get("unit_ids"))) & cited) or sorted(cited),
                "issue": str(problem.get("issue") or problem.get("message") or "Browser QA implementation blocker."),
                "expected_change": str(problem.get("expected_change") or "Restore the approved browser outcome."),
            }
            for problem in _findings_with_evidence(
                problems,
                manifest=inputs.get("manifest"),
                views=_dict_items(_dict_value(inputs.get("plan")).get("views")),
                variant="qa",
            )
        ]
        version = _next_closure_artifact_version(self.store, run_id, "browser_qa_repair_feedback")
        feedback = closure_repair_feedback(
            run_id=run_id,
            version=version,
            repair_iteration=iterations + 1,
            source=source,
            summary=f"Browser QA found {len(findings)} implementation blocker(s).",
            findings=findings,
            plan_extensions=[],
            allowed_paths=allowed_paths,
            extra={},
        )
        feedback.update(
            {
                "version": f"browser_qa_repair_feedback_v{version}",
                "status": "browser_qa_repair_needed",
                "required_action": (
                    "Repair only the cited frozen Plan units. Preserve every approved Requirement, Design, closure, "
                    "journey and UI Polish outcome that already passes."
                    + (_FINDING_EVIDENCE_NOTE if any(item.get("evidence") for item in findings) else "")
                ),
            }
        )
        feedback_ref = _write_closure_json(
            self.store, run_id, f"browser_qa_repair_feedback_v{version}.json", feedback
        )
        latest_artifacts["browser_qa_repair_feedback"] = feedback_ref
        self._set_qa(
            state,
            status="repair_needed",
            repair_unit_ids=sorted(cited),
            repair_paths=sorted(set(_string_items(self._qa_pin(state).get("repair_paths"))) | set(allowed_paths)),
            repair_session=None,
        )
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _QA_PHASE,
                "status": "browser_qa_repair_needed",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Browser QA cần Developer repair ({source}) cho run {run_id}.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_browser_qa_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        pin = _dict_value(state.get("browser_stages"))
        iteration = _int_value(_dict_value(pin.get("repair_iterations")).get("qa")) + 1
        qa = self._qa_pin(state)
        session = _dict_value(qa.get("repair_session"))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            feedback_ref = str(latest_artifacts.get("browser_qa_repair_feedback") or "")
            if not feedback_ref:
                raise HarnessError("browser_qa_repair_feedback_missing", "Browser QA repair has no feedback.", run_id=run_id)
            feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_ref, run_id=run_id), run_id=run_id
            )
            if session.get("feedback") != feedback_ref:
                session = {
                    "feedback": feedback_ref,
                    "artifact_version": _next_phase4_artifact_version(self.store, run_id),
                    "expected_dirty_paths": [
                        path
                        for path in worktree_dirty_paths(inputs["target_path"])
                        if path != ".codegraph" and not path.startswith(".codegraph/")
                    ],
                }
                self._set_qa(state, status="repairing", repair_session=session)
                self.store.write_state(state)
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=inputs["frozen_packet"],
                target_path=inputs["target_path"],
                latest_artifacts=latest_artifacts,
                mode="browser_qa_repair",
                current_phase=_QA_PHASE,
                repair_iteration=iteration,
                session_iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="browser_qa_repair_execution_manifest",
                journal_key="browser_qa_repair_execution_journal",
                repair_feedback=feedback,
                repair_scope="browser_qa",
                expected_dirty_paths=_string_items(session.get("expected_dirty_paths")),
            )
            report_ref = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report={
                    "run_id": run_id,
                    "source_ref": state.get("source_ref"),
                    "issue_key": inputs["frozen_packet"].get("issue_key"),
                    "target_repo": str(inputs["target_path"]),
                    "branch": inputs["target_info"].current_branch,
                    "summary": result.summary,
                    "files": result.files,
                    "execution_mode": "tool_loop",
                    "browser_qa_repair_feedback": feedback_ref,
                    "repair_iteration": iteration,
                    "developer_execution_manifest": result.manifest_path,
                    "developer_execution_journal": result.journal_path,
                },
                version=_int_value(session.get("artifact_version")),
            )
            latest_artifacts["code_change_report"] = report_ref
        except DeveloperExecutionError as exc:
            return self._pause_browser_developer(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                mode="browser_qa_repair",
                iteration=iteration,
                artifact_version=_int_value(session.get("artifact_version")),
                manifest_key="browser_qa_repair_execution_manifest",
                journal_key="browser_qa_repair_execution_journal",
                phase=_QA_PHASE,
                label="Browser QA repair",
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="qa_repairing",
                phase=_QA_PHASE,
            )
        pin = _dict_value(state.get("browser_stages"))
        state["browser_stages"] = {
            **pin,
            "repair_iterations": {**_dict_value(pin.get("repair_iterations")), "qa": iteration},
        }
        self._set_qa(state, status="written", repair_session=None)
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _QA_PHASE,
                "status": "browser_qa_written",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "browser_qa_written"},
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Browser QA repair {iteration} xong; chạy lại suite và cùng journey.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_browser_qa_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        try:
            inputs = self._browser_inputs(state=state, run_id=run_id)
            plan = _read_json_artifact(
                self._existing_artifact_path(str(self._journey_pin(state).get("plan") or ""), run_id=run_id),
                run_id=run_id,
            )
            iteration = _int_value(_dict_value(_dict_value(state.get("browser_stages")).get("repair_iterations")).get("qa"))
            suite = self._run_browser_suite(
                run_id=run_id,
                inputs=inputs,
                packet=inputs["frozen_packet"],
                views=[baseline_view(), *_dict_items(plan.get("views"))],
                demo_literals=self._readiness_literals(state, run_id=run_id),
                stage=f"qa_repair_{iteration}",
            )
            version = _next_closure_artifact_version(self.store, run_id, "browser_qa_verification")
            verification_ref = _write_closure_json(
                self.store,
                run_id,
                f"browser_qa_verification_v{version}.json",
                {
                    "schema_version": "browser_qa_verification_v1",
                    "run_id": run_id,
                    "source": "implementation_repair",
                    "repair_iteration": iteration,
                    # Final Verification F1 proves the published tree is this verified tree (L5.5.6).
                    "worktree_fingerprint": _worktree_fingerprint(inputs["target_path"]),
                    "manifest": suite["manifest_ref"],
                    "classification": suite["classification"],
                    "verification": _verification_digest(suite["result"]),
                    "probe_results": suite["probe_results"],
                    "probe_failures": suite["probe_failures"],
                },
            )
            latest_artifacts.update(
                {
                    "browser_qa_evidence_manifest": suite["manifest_ref"],
                    "browser_qa_verification": verification_ref,
                }
            )
            if suite["classification"]["kind"] == "implementation":
                unit_ids = _string_items(self._qa_pin(state).get("repair_unit_ids"))
                return self._route_browser_qa_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    problems=[
                        {
                            **_suite_failure_problem(suite),
                            "unit_ids": unit_ids,
                            "expected_change": "Restore the approved full suite, probes and browser capture.",
                        }
                    ],
                    source="browser_qa_verification",
                )
            if suite["classification"]["kind"] != "passed":
                raise HarnessError(
                    "browser_qa_environment_failed"
                    if suite["classification"]["kind"] == "environment"
                    else "browser_qa_verification_policy_failed",
                    f"{suite['classification'].get('stage_id') or 'verification'}: {suite['result'].reason}",
                    run_id=run_id,
                )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="qa_verifying",
                phase=_QA_PHASE,
            )
        self._set_qa(
            state,
            status="verified",
            current_manifest=suite["manifest_ref"],
            verification=verification_ref,
            evidence_tree_hash=_worktree_fingerprint(inputs["target_path"]).get("sha256"),
            needs_regression=True,
        )
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _QA_PHASE,
                "status": "browser_qa_verified",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "browser_qa_verified"},
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": "Browser QA repair verification pass; chạy regression và chấm lại evidence đổi.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _browser_qa_agent(self, *, run_id: str, attempt: int) -> BrowserQaValidator:
        validator = self.browser_qa_validator
        if not bool(self.browser_stage_step_review.get("enabled")):
            return validator
        return BrowserQaValidator(
            ReviewableLlmClient(
                validator.llm_client,
                store=self.store,
                run_id=run_id,
                review=self.browser_stage_step_review,
                call_prefix="browser-qa-validator",
                execution_dir=f"browser_execution/step_review/qa/attempt_{attempt}",
                error_prefix="browser_stage",
                label="Browser QA Validator",
                carry_approved_output_notes=False,
            )
        )

    def _ensure_browser_qa_evidence(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
    ) -> dict[str, object] | None:
        qa = self._qa_pin(state)
        if qa.get("current_manifest") and qa.get("verification"):
            return None
        inputs = self._browser_inputs(state=state, run_id=run_id)
        polish = self._polish_pin(state)
        manifest_ref = str(polish.get("after_manifest") or "")
        verification_ref = str(polish.get("verification") or "")
        if not manifest_ref or not verification_ref:
            raise HarnessError(
                "browser_qa_evidence_missing",
                "Browser QA needs the approved post-Polish evidence and verification artifacts.",
                run_id=run_id,
            )
        verification = _read_json_artifact(
            self._existing_artifact_path(verification_ref, run_id=run_id), run_id=run_id
        )
        captured = _dict_value(verification.get("worktree_fingerprint"))
        current = _worktree_fingerprint(inputs["target_path"])
        policy = build_browser_stage_policy(project=get_project_profile(inputs["planning_policy"].project_profile_id))
        captured_stages = tuple(
            _string_items((_dict_value(verification.get("verification")) or verification).get("executed_stage_ids"))
        )
        if (
            captured.get("sha256")
            and captured.get("sha256") == current.get("sha256")
            and captured_stages == policy.stage_ids
        ):
            latest_artifacts["browser_qa_evidence_manifest"] = manifest_ref
            self._set_qa(
                state,
                current_manifest=manifest_ref,
                verification=verification_ref,
                evidence_source="ui_polish_reuse",
                evidence_tree_hash=current.get("sha256"),
                needs_regression=False,
            )
            self.store.write_state(state)
            return None

        plan = _read_json_artifact(
            self._existing_artifact_path(str(self._journey_pin(state).get("plan") or ""), run_id=run_id),
            run_id=run_id,
        )
        suite = self._run_browser_suite(
            run_id=run_id,
            inputs=inputs,
            packet=inputs["frozen_packet"],
            views=[baseline_view(), *_dict_items(plan.get("views"))],
            demo_literals=self._readiness_literals(state, run_id=run_id),
            stage="qa_initial",
        )
        if suite["classification"]["kind"] == "implementation":
            # No review has cited a unit yet: the owners of every changed path get the finding (mechanical).
            changed = set(worktree_dirty_paths(inputs["target_path"]))
            owned = sorted(
                path
                for unit in _dict_items(inputs["frozen_packet"].get("execution_units"))
                for path in _string_items(unit.get("write_paths"))
                if path in changed
            )
            return self._route_browser_qa_repair(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                inputs=inputs,
                problems=[
                    {
                        **_suite_failure_problem(suite),
                        "paths": sorted(set(owned)),
                        "expected_change": (
                            "Make this verification stage pass again by changing the cited units' code. Tests the "
                            "repository already had stay unchanged, and every approved outcome stays as it is."
                        ),
                    }
                ],
                source="browser_qa_initial_verification",
            )
        if suite["classification"]["kind"] != "passed":
            raise HarnessError(
                "browser_qa_environment_failed"
                if suite["classification"]["kind"] == "environment"
                else "browser_qa_verification_policy_failed",
                f"{suite['classification'].get('stage_id') or 'verification'}: {suite['result'].reason}",
                run_id=run_id,
            )
        version = _next_closure_artifact_version(self.store, run_id, "browser_qa_verification")
        verification_ref = _write_closure_json(
            self.store,
            run_id,
            f"browser_qa_verification_v{version}.json",
            {
                "schema_version": "browser_qa_verification_v1",
                "run_id": run_id,
                "source": "worktree_changed_after_ui_polish_capture",
                "previous_worktree_fingerprint": captured,
                "worktree_fingerprint": _worktree_fingerprint(inputs["target_path"]),
                "manifest": suite["manifest_ref"],
                "classification": suite["classification"],
                "verification": _verification_digest(suite["result"]),
                "probe_results": suite["probe_results"],
                "probe_failures": suite["probe_failures"],
            },
        )
        latest_artifacts.update(
            {
                "browser_qa_evidence_manifest": suite["manifest_ref"],
                "browser_qa_verification": verification_ref,
            }
        )
        current = _worktree_fingerprint(inputs["target_path"])
        self._set_qa(
            state,
            current_manifest=suite["manifest_ref"],
            verification=verification_ref,
            evidence_source="qa_initial_capture",
            evidence_tree_hash=current.get("sha256"),
            needs_regression=False,
        )
        self.store.write_state(state)
        return None

    def _browser_qa_tasks(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        inputs = self._browser_inputs(state=state, run_id=run_id)
        planning = self._journey_planning_inputs(
            state=state,
            run_id=run_id,
            inputs=inputs,
            latest_artifacts=latest_artifacts,
        )
        plan_ref = str(self._journey_pin(state).get("plan") or "")
        plan = _read_json_artifact(self._existing_artifact_path(plan_ref, run_id=run_id), run_id=run_id)
        qa = self._qa_pin(state)
        manifest_ref = str(qa.get("current_manifest") or self._polish_pin(state).get("after_manifest") or "")
        if not manifest_ref:
            raise HarnessError("browser_qa_evidence_missing", "Browser QA has no post-Polish evidence manifest.", run_id=run_id)
        manifest = _read_json_artifact(
            self._existing_artifact_path(manifest_ref, run_id=run_id), run_id=run_id
        )
        all_evidence = _manifest_text_evidence(manifest, variant="qa")
        views = _dict_items(plan.get("views"))
        catalog = _dict_items(planning.get("catalog"))
        tasks: list[dict[str, object]] = []
        for scenario_id in sorted(_string_items(planning.get("runnable"))):
            scenario_views = [item for item in views if str(item.get("scenario_id") or "") == scenario_id]
            view_ids = {str(item.get("view_id") or "") for item in scenario_views}
            obligations = [item for item in catalog if scenario_id in _string_items(item.get("scenario_ids"))]
            obligation_ids = {str(item.get("obligation_id") or "") for item in obligations}
            evidence = [item for item in all_evidence if str(item.get("view_id") or "") in view_ids]
            scenario_manifest = {
                **manifest,
                "views": [item for item in _dict_items(manifest.get("views")) if item.get("view_id") in view_ids],
                "records": [
                    item for item in _dict_items(manifest.get("records"))
                    if str(item.get("view_id") or "") in view_ids
                ],
            }
            selection = select_primary_images(
                [{"variant": "qa", "manifest": scenario_manifest}],
                run_dir=self.store.run_dir(run_id),
                max_images=int(self.settings.browser_max_images_per_call),
                include_baseline=False,
            )
            images = _dict_items(selection.get("images"))
            candidate_units = _scenario_candidate_units(inputs["frozen_packet"], scenario_id)
            candidate_unit_ids = {
                str(item.get("unit_id") or "") for item in candidate_units if item.get("unit_id")
            }
            scenario = {
                "scenario_id": scenario_id,
                "obligations": [
                    {**_review_obligation(item), "owner_unit_ids": [str(item.get("unit_id") or "")]}
                    for item in obligations
                ],
                "candidate_units": candidate_units,
            }
            context = {
                "views": scenario_views,
                "evidence": evidence,
                "not_reachable": [
                    item
                    for item in _dict_items(plan.get("not_reachable"))
                    if str(item.get("obligation_id") or "") in obligation_ids
                ],
                "closure_contract": {
                    key: inputs["contract"].get(key)
                    for key in ("runtime_inputs", "demo_decisions", "verification_stand_ins")
                },
                "omitted_image_capture_keys": _string_items(selection.get("omitted_capture_keys")),
            }
            ownership = {
                str(item.get("obligation_id") or ""): set(candidate_unit_ids)
                for item in obligations
            }
            tasks.append(
                {
                    "scenario": scenario,
                    "context": context,
                    "evidence_refs": {
                        str(item.get("evidence_ref") or "") for item in evidence if item.get("evidence_ref")
                    },
                    "ownership": ownership,
                    "images": images,
                    "prompt": browser_qa_review_prompt(
                        scenario=scenario,
                        review_context=context,
                        images=images,
                    ),
                }
            )
        return {**inputs, "plan": plan, "manifest": manifest, "manifest_ref": manifest_ref}, tasks

    def _continue_browser_qa_review(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        new_attempt: bool = False,
    ) -> dict[str, object]:
        latest_artifacts = dict(_string_dict(state.get("latest_artifacts")))
        qa = self._qa_pin(state)
        previous_attempt = _int_value(qa.get("attempt"))
        attempt = max(1, previous_attempt)
        if new_attempt or (previous_attempt and str(qa.get("status") or "") == "verified"):
            attempt += 1
        self._set_qa(state, status="reviewing", attempt=attempt)
        self.store.write_state(state)
        try:
            routed = self._ensure_browser_qa_evidence(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts
            )
            if routed is not None:
                return routed
            inputs, tasks = self._browser_qa_tasks(
                state=state, run_id=run_id, latest_artifacts=latest_artifacts
            )
            if self._qa_pin(state).get("needs_regression"):
                try:
                    regression = self._run_browser_qa_regression(
                        state=state,
                        run_id=run_id,
                        latest_artifacts=latest_artifacts,
                        inputs=inputs,
                    )
                except LlmStepReviewRequired as exc:
                    return self._pause_for_code_validation_step_review(
                        state=state, run_id=run_id, progress=exc.progress
                    )
                if regression is not None:
                    return regression
            cache = CodeValidationTaskCache(
                self.store.run_dir(run_id) / "browser_execution" / "browser_qa_task_cache_v1.json"
            )
            validator = self._browser_qa_agent(run_id=run_id, attempt=attempt)
            reviews: list[dict[str, object]] = []
            cache_hits = 0
            for task in tasks:
                # Step review replays approved calls to preserve their persisted ordinal before the next scenario.
                cached = None if self.browser_stage_step_review.get("enabled") else cache.get(str(task["prompt"]))
                if cached is not None:
                    try:
                        review = parse_browser_qa_review(
                            json.dumps(cached, ensure_ascii=False),
                            scenario=task["scenario"],
                            evidence_refs=set(task["evidence_refs"]),
                            ownership=task["ownership"],
                        )
                    except HarnessError:
                        cached = None
                if cached is None:
                    review = validator.review_scenario(
                        scenario=task["scenario"],
                        review_context=task["context"],
                        evidence_refs=set(task["evidence_refs"]),
                        ownership=task["ownership"],
                        images=task["images"],
                        run_id=run_id,
                    )
                    cache.put(str(task["prompt"]), review)
                else:
                    cache_hits += 1
                reviews.append(
                    {
                        "scenario_id": _dict_value(task["scenario"]).get("scenario_id"),
                        **review,
                        "cache_hit": cached is not None,
                    }
                )
            version = _next_closure_artifact_version(self.store, run_id, "browser_qa_reviews")
            reviews_ref = _write_closure_json(
                self.store,
                run_id,
                f"browser_qa_reviews_v{version}.json",
                {
                    "schema_version": "browser_qa_reviews_v1",
                    "run_id": run_id,
                    "manifest": inputs["manifest_ref"],
                    "attempt": attempt,
                    "cache_hits": cache_hits,
                    "reviews": reviews,
                },
            )
            latest_artifacts["browser_qa_reviews"] = reviews_ref
            self._set_qa(state, status="reviewed", reviews=reviews_ref, current_manifest=inputs["manifest_ref"])
            blockers = [
                finding
                for review in reviews
                for finding in _dict_items(review.get("blocking_findings"))
            ]
            # Same order as the journey gate: code repair first, the re-review reports journey defects still open.
            implementation = [item for item in blockers if item.get("category") == "implementation"]
            if implementation:
                return self._route_browser_qa_repair(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    problems=implementation,
                    source="browser_qa_review",
                )
            journey = [item for item in blockers if item.get("category") == "journey_defect"]
            if journey:
                return self._route_browser_qa_journey_patch(
                    state=state,
                    run_id=run_id,
                    latest_artifacts=latest_artifacts,
                    inputs=inputs,
                    findings=journey,
                )
            return self._close_browser_qa(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                inputs=inputs,
                reviews=reviews,
            )
        except LlmStepReviewRequired as exc:
            return self._pause_for_browser_step_review(
                state=state,
                run_id=run_id,
                progress=exc.progress,
                latest_artifacts=latest_artifacts,
                resume_status=str(state.get("status") or "ui_polished"),
            )
        except HarnessError as exc:
            return self._block_browser_stage(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                exc=exc,
                stage="qa_review",
                phase=_QA_PHASE,
            )

    def _run_browser_journey_regression(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
    ) -> dict[str, object] | None:
        inputs = self._browser_inputs(state=state, run_id=run_id)
        journey = self._journey_pin(state)
        verification_ref = str(journey.get("verification") or "")
        verification = _read_json_artifact(
            self._existing_artifact_path(verification_ref, run_id=run_id), run_id=run_id
        )
        changes = approved_closure_changes(
            contract=inputs["contract"], packet=inputs["packet"], pin=inputs["pin"]
        )
        readiness_ref = str(self._readiness_pin(state).get("context") or "")
        readiness = (
            _read_json_artifact(self._existing_artifact_path(readiness_ref, run_id=run_id), run_id=run_id)
            if readiness_ref
            else {}
        )
        changed_paths = sorted(
            {
                *_string_items(changes.get("changed_paths")),
                *_string_items(readiness.get("write_scope")),
                *_string_items(journey.get("repair_paths")),
            }
        )
        trace_result = self._validate_code_trace_snapshot(
            state=state,
            artifact_paths=latest_artifacts,
            run_id=run_id,
            target_path=inputs["target_path"],
            work_packet=inputs["frozen_packet"],
            verification_evidence=verification,
            approved_closure_changes={
                **changes,
                "note": str(changes.get("note") or "") + " Pre-Polish browser journey repairs are included.",
                "changed_paths": changed_paths,
                "browser_stage_changes": {
                    "readiness_context": readiness_ref,
                    "browser_journey_repair_paths": _string_items(journey.get("repair_paths")),
                },
            },
        )
        version = _next_closure_artifact_version(self.store, run_id, "browser_journey_regression")
        regression_ref = _write_closure_json(
            self.store, run_id, f"browser_journey_regression_v{version}.json", trace_result.to_dict()
        )
        latest_artifacts["browser_journey_regression"] = regression_ref
        self._set_journey(state, needs_regression=False, regression=regression_ref)
        if trace_result.ok:
            self.store.write_state(state)
            return None
        if not _is_repairable_code_trace_failure(trace_result):
            raise HarnessError(
                _nonrepairable_code_trace_error_code(trace_result),
                f"Code Validator regression after journey repair did not pass: {trace_result.summary}",
                run_id=run_id,
            )
        return self._route_browser_journey_repair(
            state=state,
            run_id=run_id,
            latest_artifacts=latest_artifacts,
            inputs=inputs,
            problems=_regression_problems(trace_result),
            source="validator_code_trace",
        )

    def _run_browser_qa_regression(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
    ) -> dict[str, object] | None:
        qa = self._qa_pin(state)
        verification_ref = str(qa.get("verification") or "")
        verification = _read_json_artifact(
            self._existing_artifact_path(verification_ref, run_id=run_id), run_id=run_id
        )
        changes = approved_closure_changes(
            contract=inputs["contract"], packet=inputs["packet"], pin=inputs["pin"]
        )
        readiness_ref = str(self._readiness_pin(state).get("context") or "")
        readiness = (
            _read_json_artifact(self._existing_artifact_path(readiness_ref, run_id=run_id), run_id=run_id)
            if readiness_ref else {}
        )
        polish_packet = self._ui_polish_packet(state, run_id=run_id)
        changed_paths = sorted(
            {
                *_string_items(changes.get("changed_paths")),
                *_string_items(readiness.get("write_scope")),
                *_string_items(self._journey_pin(state).get("repair_paths")),
                *_string_items(_dict_value(polish_packet.get("output_contract")).get("allowed_paths")),
                *_string_items(qa.get("repair_paths")),
            }
        )
        trace_result = self._validate_code_trace_snapshot(
            state=state,
            artifact_paths=latest_artifacts,
            run_id=run_id,
            target_path=inputs["target_path"],
            work_packet=inputs["frozen_packet"],
            verification_evidence=verification,
            approved_closure_changes={
                **changes,
                "note": str(changes.get("note") or "") + " Browser-stage approved changes are included.",
                "changed_paths": changed_paths,
                "browser_stage_changes": {
                    "readiness_context": readiness_ref,
                    "browser_journey_repair_paths": _string_items(self._journey_pin(state).get("repair_paths")),
                    "ui_polish_contract": self._polish_pin(state).get("approved_contract"),
                    "browser_qa_repair_paths": _string_items(qa.get("repair_paths")),
                },
            },
        )
        version = _next_closure_artifact_version(self.store, run_id, "browser_qa_regression")
        regression_ref = _write_closure_json(
            self.store, run_id, f"browser_qa_regression_v{version}.json", trace_result.to_dict()
        )
        latest_artifacts["browser_qa_regression"] = regression_ref
        self._set_qa(state, needs_regression=False, regression=regression_ref)
        if trace_result.ok:
            self.store.write_state(state)
            return None
        if not _is_repairable_code_trace_failure(trace_result):
            raise HarnessError(
                _nonrepairable_code_trace_error_code(trace_result),
                f"Code Validator regression after Browser QA repair did not pass: {trace_result.summary}",
                run_id=run_id,
            )
        return self._route_browser_qa_repair(
            state=state,
            run_id=run_id,
            latest_artifacts=latest_artifacts,
            inputs=inputs,
            problems=_regression_problems(trace_result),
            source="validator_code_trace",
        )

    def _close_browser_qa(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        inputs: dict[str, object],
        reviews: list[dict[str, object]],
    ) -> dict[str, object]:
        qa_version = _next_qa_artifact_version(self.store, run_id)
        units = {str(item.get("unit_id") or ""): item for item in _dict_items(inputs["frozen_packet"].get("execution_units"))}
        scenarios = []
        requirement_ids: set[str] = set()
        for review in reviews:
            unit_ids = {
                str(unit_id)
                for finding in _dict_items(review.get("findings"))
                for unit_id in _string_items(finding.get("unit_ids"))
            }
            if not unit_ids:
                reviewed_ids = set(_string_items(review.get("reviewed_obligation_ids")))
                unit_ids = {
                    str(item.get("unit_id") or "")
                    for item in obligation_catalog(
                        frozen_packet=inputs["frozen_packet"],
                        node_scenarios=_dict_value(inputs["context"].get("design_node_scenarios")),
                        runnable_scenarios={str(review.get("scenario_id") or "")},
                    )
                    if str(item.get("obligation_id") or "") in reviewed_ids
                }
            refs = sorted({ref for unit_id in unit_ids for ref in _string_items(_dict_value(units.get(unit_id)).get("requirement_refs"))})
            requirement_ids.update(refs)
            scenarios.append(
                {
                    "scenario_id": review.get("scenario_id"),
                    "name": review.get("scenario_id"),
                    "requirement_ids": refs,
                    "result": "pass",
                    "evidence": self._qa_pin(state).get("current_manifest"),
                    "risk": "",
                }
            )
        generated_ref = _write_closure_json(
            self.store,
            run_id,
            f"qa_generated_tests_v{qa_version}.json",
            {
                "version": "qa_generated_tests_v2",
                "mode": "browser_qa_v1",
                "summary": "Typed browser journey and Harness-rendered capture spec used as Browser QA acceptance tests.",
                "test_files": [{"path": BROWSER_CAPTURE_SPEC_PATH, "purpose": "Harness-rendered browser journey"}],
                "scenarios": scenarios,
                "risks": [],
                "requirement_contract": inputs["frozen_packet"].get("requirement_contract") or {
                    "version": "v2", "revision": 0
                },
                "generation_context": {"packet_mode": "browser_qa_scenario_v1"},
                "generation_profile": {"browser": {"framework": "playwright", "owned_by": "harness"}},
            },
        )
        source_execution_ref = str(
            self._qa_pin(state).get("verification") or latest_artifacts.get("ui_polish_verification") or ""
        )
        if not source_execution_ref:
            raise HarnessError("browser_qa_execution_missing", "Browser QA has no verification artifact.", run_id=run_id)
        source_execution = _read_json_artifact(
            self._existing_artifact_path(source_execution_ref, run_id=run_id), run_id=run_id
        )
        execution_ref = _write_closure_json(
            self.store,
            run_id,
            f"qa_test_execution_v{qa_version}.json",
            {
                "version": "qa_test_execution_v1",
                "mode": "browser_qa_v1",
                "run_id": run_id,
                "status": "pass",
                "source_verification": source_execution_ref,
                "evidence_manifest": self._qa_pin(state).get("current_manifest"),
                "verification": source_execution.get("verification") or source_execution,
                "selected_test_files": [BROWSER_CAPTURE_SPEC_PATH],
                "verified_test_files": [BROWSER_CAPTURE_SPEC_PATH],
            },
        )
        report_version = _next_closure_artifact_version(self.store, run_id, "browser_qa_report", suffix=".md")
        report_ref = _write_closure_text(
            self.store,
            run_id,
            f"browser_qa_report_v{report_version}.md",
            _browser_qa_report_markdown(
                run_id=run_id,
                reviews=reviews,
                manifest=self._qa_pin(state).get("current_manifest"),
                not_reachable=_dict_items(inputs["plan"].get("not_reachable")),
            ),
        )
        latest_artifacts.update(
            {
                "browser_qa_report": report_ref,
                "qa_generated_tests": generated_ref,
                "qa_test_execution": execution_ref,
                "qa_test_report": report_ref,
            }
        )
        pin = _dict_value(state.get("browser_stages"))
        self._set_qa(state, status="passed", report=report_ref)
        pin = _dict_value(state.get("browser_stages"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _QA_PHASE,
                "status": "qa_tested",
                "latest_artifacts": latest_artifacts,
                "browser_stages": {**pin, "status": "browser_qa_passed"},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "qa_acceptance": {
                        "ok": True,
                        "status": "pass",
                        "mode": "browser_qa_v1",
                        "requirement_contract_version": _dict_value(inputs["frozen_packet"].get("requirement_contract")).get("version", "v2"),
                        "requirement_revision": _dict_value(inputs["frozen_packet"].get("requirement_contract")).get("revision", 0),
                        "summary": "Browser QA passed every runnable scenario.",
                        "scenario_count": len(scenarios),
                        "risk_count": 0,
                        "requirement_ids": sorted(requirement_ids),
                        "requirements_covered": len(requirement_ids),
                        "selected_test_files": [BROWSER_CAPTURE_SPEC_PATH],
                        "verified_test_files": [BROWSER_CAPTURE_SPEC_PATH],
                    },
                },
                "pending_checkpoint": None,
                "pending_step_review": None,
                "allowed_next_actions": ALLOWED_QA_TESTED_ACTIONS,
                "last_error": None,
                "summary_for_user": f"Browser QA pass cho run {run_id}; run đã sẵn sàng cho publication.",
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

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

    def _browser_ready(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        summary: str,
    ) -> dict[str, object]:
        pin = _dict_value(state.get("browser_stages"))
        readiness = _dict_value(pin.get("readiness"))
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": _READINESS_PHASE,
                "status": "browser_ready",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "browser_stages": {**pin, "status": "browser_ready"},
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "browser_readiness": {
                        "ok": True,
                        "status": str(readiness.get("status") or ""),
                        "developer_skipped": bool(readiness.get("developer_skipped")),
                        "repair_iterations": _int_value(_dict_value(pin.get("repair_iterations")).get("readiness")),
                        "context": readiness.get("context"),
                        "verification": readiness.get("verification"),
                    },
                },
                "allowed_next_actions": ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
                "last_error": None,
                "summary_for_user": summary,
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

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
