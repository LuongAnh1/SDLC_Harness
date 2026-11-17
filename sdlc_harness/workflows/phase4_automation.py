"""Unattended mode (U1): run a Phase 4 run from one owner checkpoint to the next without per-step commands.

`run_until_checkpoint` repeats the ordinary `continue_run` step while the graph says the next step is machine work
(`graph.AUTOMATED_KINDS`) and stops where the owner is needed or nothing can move: an owner checkpoint, a per-call
step review, a block (never retried here, failures stay fail-closed), the end of the run, a status with no
continue route, a status outside Phase 4, a step that left the run state unchanged, or the run's LLM call ceiling
(the sum of per-component caps, U1.2; the owner raises it with `adjust_budget(llm_calls)`).
"""

from __future__ import annotations

import json

from ..core.errors import HarnessError
from ..storage.runtime import utc_now_iso
from .graph import AUTOMATED_KINDS, LLM_CALL_BUDGET, PHASE_FOUR, continue_handler, node_for
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import _dict_items, _int_value, _string_items

# Keys a step changes without moving the run: the timestamp and unattended mode's own call counter.
_VOLATILE_STATE_KEYS = ("updated_at", "automation")


class PhaseFourAutomation:
    """Owner actions `run_until_checkpoint` (each step is one `continue_run`) and `set_step_review`."""

    def run_until_checkpoint(self, *, run_id: str, max_steps: int | None = None) -> dict[str, object]:
        if max_steps is not None and max_steps < 1:
            raise HarnessError("automation_max_steps_invalid", "max_steps must be at least 1.", run_id=run_id)
        path: list[str] = []
        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            review = _dict_value(_dict_value(state.get("automation")).get("step_review"))
            # Per-call review configured on is never dropped silently: the owner turns it off per run (audited).
            stop = "step_review_enabled" if "enabled" not in review and self._per_call_review_enabled() else ""
            if review.get("enabled") is False:
                self._disable_per_call_review()
            while not stop:
                status = str(state.get("status") or "")
                stop = self._automation_stop(state)
                if stop:
                    break
                if max_steps is not None and len(path) >= max_steps:
                    stop = "step_limit"
                    break
                budget = self._llm_call_budget(state, run_id=run_id)
                if budget["used"] >= budget["ceiling"]:
                    stop = "llm_call_ceiling"
                    break
                before = _progress_view(state)
                calls_before = self.llm_client.provider_calls
                try:
                    self._continue_run_locked(run_id=run_id)
                finally:
                    # Count the step's provider calls even when it raised: they were spent.
                    self._record_unattended_calls(run_id, self.llm_client.provider_calls - calls_before)
                path.append(status)
                state = self.store.read_state(run_id)
                if _progress_view(state) == before:
                    stop = "no_progress"
                    break
        response = self._status_response(state, created_new_run=False)
        return {
            **response,
            "automation": {
                "mode": "run_until_checkpoint",
                "stop_reason": stop,
                "steps": len(path),
                "path": [*path, str(state.get("status") or "")],
                "llm_calls": self._llm_call_budget(state, run_id=run_id),
            },
        }

    # ------------------------------------------------------------------ per-call review (U1.3)

    def set_step_review(self, *, run_id: str, enabled: bool, reason: str) -> dict[str, object]:
        """Owner action: per-call review on or off for this run's unattended mode, recorded in `state.recoveries`."""

        if not str(reason or "").strip():
            raise HarnessError("step_review_reason_required", "A review change needs the owner's reason.", run_id=run_id)
        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            self._require_frozen_llm_mode(state, run_id=run_id)
            changed_at = utc_now_iso()
            automation = _dict_value(state.get("automation"))
            previous = _dict_value(automation.get("step_review")).get("enabled")
            state["automation"] = {
                **automation,
                "step_review": {"enabled": bool(enabled), "reason": reason, "set_at": changed_at},
            }
            state.setdefault("recoveries", []).append(
                {
                    "code": "set_step_review",
                    "previous_enabled": previous,
                    "enabled": bool(enabled),
                    "reason": reason,
                    "recovered_at": changed_at,
                }
            )
            state["updated_at"] = changed_at
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

    def _per_call_review_enabled(self) -> bool:
        reviews = (
            self.code_validation_step_review,
            self.qa_step_review,
            self.runtime_closure_step_review,
            self.browser_stage_step_review,
            self.developer_tool_loop.step_review,
        )
        return any(bool(_dict_value(review).get("enabled")) for review in reviews)

    def _disable_per_call_review(self) -> None:
        self.code_validation_step_review = {"enabled": False}
        self.qa_step_review = {"enabled": False}
        self.runtime_closure_step_review = {"enabled": False}
        self.browser_stage_step_review = {"enabled": False}
        self.developer_tool_loop.step_review = {}

    # ------------------------------------------------------------------ LLM call ceiling (U1.2, owner option A)

    def _llm_call_budget(self, state: dict[str, object], *, run_id: str) -> dict[str, object]:
        """Calls used against the run's ceiling: the owner's limit, else the sum of the per-component caps."""

        components = self._llm_call_components(state, run_id=run_id)
        computed = sum(components.values())
        override = _dict_value(_dict_value(state.get("repair_budgets")).get(LLM_CALL_BUDGET))
        return {
            # shortcut: counts the calls made in unattended mode only (manual steps are watched by the owner);
            # count every Phase 4 call here if unattended runs ever start mid-lane after long manual work.
            "used": _int_value(_dict_value(state.get("automation")).get("llm_calls_used")),
            "ceiling": max(0, _int_value(override.get("limit"))) if "limit" in override else computed,
            "computed": computed,
            "components": components,
        }

    def _record_unattended_calls(self, run_id: str, calls: int) -> None:
        if calls <= 0:
            return
        state = self.store.read_state(run_id)
        automation = _dict_value(state.get("automation"))
        state["automation"] = {**automation, "llm_calls_used": _int_value(automation.get("llm_calls_used")) + calls}
        self.store.write_state(state)

    def _llm_call_components(self, state: dict[str, object], *, run_id: str) -> dict[str, int]:
        """Each Phase 4 LLM component's cap: its count in the approved artifacts x the calls the Harness allows it.

        Every factor is a limit the Harness already enforces (actions per Developer item and their retries, repair
        and patch budgets, planner gate rounds, the Code Validator call budget), so a healthy run never reaches the
        sum; it only stops loops outside those limits. Components whose artifact does not exist yet count once it
        does, before their first call.
        """

        settings = self.settings
        latest = _string_dict(state.get("latest_artifacts"))

        def artifact(key: str) -> dict[str, object]:
            ref = latest.get(key)
            return _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id) if ref else {}

        retries = 1 + max(0, int(getattr(settings, "developer_max_action_retries", 2)))
        actions = int(getattr(settings, "developer_max_actions_per_item", 24)) + int(
            getattr(settings, "developer_verification_repair_action_reserve", 6)
        )

        def session(units: list[dict[str, object]], *, default_stages: int = 2) -> int:
            # per item: actions + timeout reserve (2 per stage + 2) + visual profile reserve (3), each with retries
            return sum(
                (actions + 2 * len(_string_items(unit.get("required_stage_ids")) or [""] * default_stages) + 2 + 3)
                * retries
                for unit in units
            )

        implementation_units = _dict_items(artifact("developer_work_packet").get("execution_units"))
        scenarios = max(1, len({str(unit.get("scenario_id") or "") for unit in implementation_units} - {""}))
        implementation_repairs = max(0, int(settings.implementation_repair_limit))
        plan_rounds = 1 + max(0, int(settings.implementation_plan_repair_limit))
        validation = max(1, int(settings.implementation_validation_call_budget))
        closure_repairs = self._repair_limit(state, "runtime_closure_repair")
        browser_repairs = self._repair_limit(state, "browser_qa_repair")
        patches = self._repair_limit(state, "browser_journey_patch")
        polish_repairs = self._repair_limit(state, "browser_polish_repair")
        review_rounds = 2 + patches + browser_repairs  # first review, one per patch and repair, one regression
        # The Code Validator checks the code again after every lane's code repair, not only implementation repairs.
        code_changes = 1 + implementation_repairs + closure_repairs + browser_repairs + polish_repairs
        components = {
            "developer_implementation": session(implementation_units) * (1 + implementation_repairs),
            "code_validation": validation * code_changes,
            "qa_generation": 2 * (1 + max(0, int(settings.qa_repair_limit))),
        }
        if self._runtime_closure_enforced(state):
            closure_units = _dict_items(artifact("runtime_closure_contract").get("closure_units"))
            components.update(
                {
                    # planner and validator, each with one contract retry per gate round
                    "closure_planning": 2 * 2 * plan_rounds,
                    "developer_closure": session(closure_units) * (1 + closure_repairs),
                    "closure_code_review": validation * (1 + closure_repairs),
                }
            )
        if self._browser_stages_enforced(state):
            polish_units = _dict_items(artifact("ui_polish_contract").get("polish_units"))
            readiness = self._repair_limit(state, "browser_readiness_repair")
            components.update(
                {
                    "developer_readiness": session([{}]) * (1 + readiness),
                    "journey_planning": 2 * plan_rounds * (1 + patches),
                    "journey_review": 2 * scenarios * review_rounds,
                    "polish_planning": 2 * 2 * plan_rounds,
                    "developer_polish": session(polish_units) * (1 + polish_repairs),
                    "polish_result_review": 2 * (1 + polish_repairs),
                    "developer_browser_repair": session(implementation_units) * browser_repairs,
                    "browser_qa_review": 2 * scenarios * review_rounds,
                }
            )
        return components

    def _automation_stop(self, state: dict[str, object]) -> str:
        """Why unattended mode cannot take the next step from this state, or "" when it can."""

        node = node_for(str(state.get("status") or ""))
        if node is None:
            return "unknown_status"
        if node.kind == "human":
            return "owner_checkpoint"
        if node.kind not in AUTOMATED_KINDS:
            return node.kind  # step_review, blocked, running or terminal
        if node.workflow != PHASE_FOUR:
            return "outside_phase4"
        if not continue_handler(self, state):
            return "not_continuable"
        return ""


def _progress_view(state: dict[str, object]) -> str:
    return json.dumps(
        {key: value for key, value in state.items() if key not in _VOLATILE_STATE_KEYS},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
