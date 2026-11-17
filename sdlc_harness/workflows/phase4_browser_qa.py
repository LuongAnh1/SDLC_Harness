"""Final Browser QA: evidence capture, semantic review, QA journey patches and code repair (`ui_polished` -> `qa_tested`).

Composed into `PhaseFourWorkflow`; shared browser-stage helpers stay in `phase4_browser`."""

from __future__ import annotations

from .phase4_browser import (  # noqa: F401 - names the moved methods use
    ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
    ALLOWED_QA_TESTED_ACTIONS,
    BROWSER_CAPTURE_SPEC_PATH,
    BrowserQaValidator,
    CodeValidationTaskCache,
    DeveloperExecutionError,
    HarnessError,
    LlmStepReviewRequired,
    ReviewableLlmClient,
    _FINDING_EVIDENCE_NOTE,
    _QA_PHASE,
    _browser_qa_report_markdown,
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
    _next_qa_artifact_version,
    _nonrepairable_code_trace_error_code,
    _read_json_artifact,
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
    _write_closure_text,
    approved_closure_changes,
    baseline_view,
    browser_qa_review_prompt,
    build_browser_stage_policy,
    closure_repair_feedback,
    get_project_profile,
    json,
    obligation_catalog,
    parse_browser_qa_review,
    select_plan_unit_owners,
    select_primary_images,
    utc_now_iso,
    worktree_dirty_paths,
    write_phase4_code_change_report,
)


class PhaseFourBrowserQaCycle:
    """Final Browser QA: evidence capture, semantic review, QA journey patches and code repair (`ui_polished` -> `qa_tested`)."""

    def _route_browser_qa_journey_patch(
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
                "source": source,
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
        if iterations >= self._repair_limit(state, "browser_qa_repair"):
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
                return self._verification_repair("browser_qa_verification")(
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
                raise self._verification_blocker(
                    "browser_qa_verification", suite["classification"], suite["result"].reason, run_id=run_id
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
            return self._verification_repair("browser_qa_initial_verification")(
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
            raise self._verification_blocker(
                "browser_qa_initial_verification", suite["classification"], suite["result"].reason, run_id=run_id
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
            routed = self._route_review_blockers(
                "browser_qa_review",
                blockers,
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                inputs=inputs,
            )
            if routed is not None:
                return routed
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
