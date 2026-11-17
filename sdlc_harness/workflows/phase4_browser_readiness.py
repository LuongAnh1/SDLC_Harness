"""Browser readiness: the demo seam, its Developer unit, verification and repair (`runtime_closed` -> `browser_ready`).

Composed into `PhaseFourWorkflow`; shared browser-stage helpers stay in `phase4_browser`."""

from __future__ import annotations

from .phase4_browser import (  # noqa: F401 - names the moved methods use
    ALLOWED_BROWSER_STAGE_AUTOMATION_ACTIONS,
    BASELINE_VIEW_ID,
    BROWSER_VIEWPORTS,
    DeveloperExecutionError,
    HarnessError,
    Path,
    _HARNESS_WRITTEN_PATHS,
    _READINESS_PHASE,
    _dict_items,
    _dict_value,
    _file_sha256,
    _int_value,
    _next_closure_artifact_version,
    _next_phase4_artifact_version,
    _read_json_artifact,
    _read_text,
    _readiness_verification_problems,
    _string_dict,
    _string_items,
    _verification_digest,
    _write_closure_json,
    baseline_view,
    build_readiness_work_packet,
    closure_repair_feedback,
    demo_render_problems,
    derive_readiness_context,
    readiness_findings,
    readiness_scope_drift,
    static_import_problems,
    utc_now_iso,
    worktree_dirty_paths,
    write_phase4_code_change_report,
)


class PhaseFourBrowserReadinessCycle:
    """Browser readiness: the demo seam, its Developer unit, verification and repair (`runtime_closed` -> `browser_ready`)."""

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
                return self._verification_repair("browser_readiness_verification")(
                    state=state, run_id=run_id, latest_artifacts=latest_artifacts, context=context, packet=packet,
                    unit_id=unit_id, source=str(classification.get("stage_id") or "verification"),
                    problems=_readiness_verification_problems(result, classification, probe_failures),
                    summary=result.reason,
                    extra={"verification": _verification_digest(result), "probe_failures": probe_failures},
                )
            if classification["kind"] != "passed":
                raise self._verification_blocker(
                    "browser_readiness_verification", classification, result.reason, run_id=run_id
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
        if iterations >= self._repair_limit(state, "browser_readiness_repair"):
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

    def _readiness_literals(self, state: dict[str, object], *, run_id: str) -> list[str]:
        """R3 stays on for every later suite: the demo literals the readiness context derived."""

        ref = str(self._readiness_pin(state).get("context") or "")
        if not ref:
            return []
        context = _read_json_artifact(self._existing_artifact_path(ref, run_id=run_id), run_id=run_id)
        return _string_items(context.get("demo_literals")) if context.get("status") == "required" else []

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
