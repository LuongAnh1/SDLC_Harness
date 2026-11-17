"""Browser journeys: planning, baseline capture, semantic review, journey patches and code repair.

Composed into `PhaseFourWorkflow`; shared browser-stage helpers stay in `phase4_browser`."""

from __future__ import annotations

from .phase4_browser import (  # noqa: F401 - names the moved methods use
    ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
    BASELINE_VIEW_ID,
    BrowserJourneyPlanner,
    BrowserQaValidator,
    CodeValidationTaskCache,
    DeveloperExecutionError,
    HarnessError,
    JOURNEY_PLAN_SCHEMA_VERSION,
    LlmStepReviewRequired,
    MAX_UI_SOURCE_FILE_CHARS,
    MAX_UI_SOURCE_TOTAL_CHARS,
    ReviewableLlmClient,
    _FINDING_EVIDENCE_NOTE,
    _JOURNEY_GATE_REPAIR_INSTRUCTION,
    _JOURNEY_PATCH_GATE_REPAIR_INSTRUCTION,
    _JOURNEY_PHASE,
    _dict_items,
    _dict_value,
    _findings_with_evidence,
    _int_value,
    _is_repairable_code_trace_failure,
    _journey_patch_context,
    _journey_patch_round,
    _manifest_text_evidence,
    _next_closure_artifact_version,
    _next_phase4_artifact_version,
    _nonrepairable_code_trace_error_code,
    _read_json_artifact,
    _read_text,
    _regression_problems,
    _require_suite_passed,
    _review_obligation,
    _scenario_candidate_units,
    _string_dict,
    _string_items,
    _suite_failure_problem,
    _verification_digest,
    _worktree_fingerprint,
    _write_closure_json,
    approved_closure_changes,
    baseline_view,
    closure_repair_feedback,
    experience_contracts,
    hashlib,
    journey_execution_review_prompt,
    journey_patch_scope_issues,
    journey_plan_rule_issues,
    journey_planner_view,
    journey_step_failures,
    journey_views,
    json,
    merge_journey_patch,
    obligation_catalog,
    parse_journey_execution_review,
    requirement_scenarios,
    scenario_design_nodes,
    select_primary_images,
    utc_now_iso,
    worktree_dirty_paths,
    write_phase4_code_change_report,
)


class PhaseFourBrowserJourneyCycle:
    """Browser journeys: planning, baseline capture, semantic review, journey patches and code repair."""

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
            routed = self._route_review_blockers(
                "browser_journey_review",
                blockers,
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                inputs=inputs,
            )
            if routed is not None:
                return routed
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
        problems: list[dict[str, object]],
        source: str,
    ) -> dict[str, object]:
        findings = problems
        pin = _dict_value(state.get("browser_stages"))
        patches = _int_value(pin.get("journey_patch_iterations"))
        if patches >= self._repair_limit(state, "browser_journey_patch"):
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
                "source": source,
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
        if iterations >= self._repair_limit(state, "browser_qa_repair"):
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
                return self._verification_repair("browser_journey_verification")(
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
                raise self._verification_blocker(
                    "browser_journey_verification", suite["classification"], suite["result"].reason, run_id=run_id
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
