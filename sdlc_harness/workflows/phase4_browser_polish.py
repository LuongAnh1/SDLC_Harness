"""UI Polish: contract planning and its owner checkpoint, the Developer edits, verification, result review.

Composed into `PhaseFourWorkflow`; shared browser-stage helpers stay in `phase4_browser`."""

from __future__ import annotations

from .phase4_browser import (  # noqa: F401 - names the moved methods use
    ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
    ALLOWED_UI_POLISH_REVIEW_ACTIONS,
    DeveloperExecutionError,
    HarnessError,
    LlmStepReviewRequired,
    Path,
    ReviewableLlmClient,
    UI_POLISH_CHECKPOINT_TYPE,
    UI_POLISH_RESULT_CHECKPOINT_TYPE,
    UiPolishPlanner,
    UiPolishValidator,
    _HARNESS_WRITTEN_PATHS,
    _POLISH_FINDING_EVIDENCE_FIELDS,
    _POLISH_PHASE,
    _artifact_reference,
    _baseline_capture_regressions,
    _dict_items,
    _dict_value,
    _file_sha256,
    _findings_with_evidence,
    _int_value,
    _is_repairable_code_trace_failure,
    _manifest_text_evidence,
    _next_closure_artifact_version,
    _next_phase4_artifact_version,
    _nonrepairable_code_trace_error_code,
    _polish_repair_findings,
    _polish_verification_problems,
    _read_json_artifact,
    _regression_problems,
    _selected_evidence,
    _share_identical_values,
    _source_diff,
    _source_snapshot,
    _string_dict,
    _string_items,
    _ui_polish_checkpoint_markdown,
    _ui_polish_result_checkpoint_markdown,
    _verification_digest,
    _worktree_fingerprint,
    _write_closure_json,
    _write_closure_text,
    app_visual_scope_id,
    approved_closure_changes,
    aria_diff,
    assemble_ui_polish_contract,
    baseline_view,
    build_ui_polish_work_packet,
    closure_repair_feedback,
    hashlib,
    json,
    polish_contract_rule_issues,
    read_visual_profiles,
    readiness_scope_drift,
    select_primary_images,
    ui_polish_guidance,
    utc_now_iso,
    visual_contracts_by_node,
    worktree_dirty_paths,
    write_phase4_code_change_report,
)
from ..core import run_state


class PhaseFourUiPolishCycle:
    """UI Polish: contract planning and its owner checkpoint, the Developer edits, verification, result review."""

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
        # Hướng 1 (owner, 2026-11-16): UI Polish consults the UI/UX skill here instead of waiting for the
        # Developer to ask; guidance stays out of authority_refs (U4 accepts profile token keys and Design IDs only).
        provider = self.developer_tool_loop.uiux_provider
        if provider is None:
            raise HarnessError(
                "ui_polish_guidance_unavailable",
                "UI Polish needs the UI/UX skill but the Developer has no guidance provider.",
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
        state[run_state.BROWSER_STAGES] = {**run_state.browser_stages(state), "status": "polish_planning"}
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
            pin = run_state.browser_stages(state)
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
        pin = run_state.browser_stages(state)
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
        pin = run_state.browser_stages(state)
        state[run_state.BROWSER_STAGES] = {
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
                return self._verification_repair("ui_polish_verification")(
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
                raise self._verification_blocker(
                    "ui_polish_verification", suite["classification"], suite["result"].reason, run_id=run_id
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
        pin = run_state.browser_stages(state)
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
        pin = run_state.browser_stages(state)
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
        pin = run_state.browser_stages(state)
        iterations = _int_value(_dict_value(pin.get("repair_iterations")).get("polish"))
        if iterations >= self._repair_limit(state, "browser_polish_repair"):
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
            pin = run_state.browser_stages(state)
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
