"""Phase 4 qa cycle."""

from __future__ import annotations

from ..agents.developer import (
    DeveloperExecutionError,
    refresh_developer_work_packet,
)
from ..agents.tester import QaTestResult
from ..artifacts.writers import (
    write_phase4_code_change_report,
    write_phase4_code_trace_report,
    write_phase4_developer_work_packet,
    write_phase4_qa_execution_report,
    write_phase4_qa_generated_tests,
    write_phase4_qa_raw_response,
    write_phase4_qa_repair_feedback,
    write_phase4_qa_test_report,
    write_phase4_repair_feedback,
    write_phase4_test_report,
)
from ..core.errors import HarnessError
from ..storage.runtime import utc_now_iso
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import (
    ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
    ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
    ALLOWED_QA_GENERATION_RETRY_ACTIONS,
    ALLOWED_QA_REPAIR_NEEDED_ACTIONS,
    ALLOWED_QA_TESTED_ACTIONS,
    RETRYABLE_QA_GENERATION_ERROR_CODES,
    _apply_qa_generated_tests,
    _dict_items,
    _implementation_blocked_actions,
    _implementation_repair_feedback,
    _int_value,
    _is_repairable_code_trace_failure,
    _next_phase4_artifact_version,
    _next_qa_artifact_version,
    _nonrepairable_code_trace_error_code,
    _planning_policy_for_state,
    _previous_qa_owned_paths,
    _qa_policy_for_state,
    _qa_repair_dirty_paths,
    _qa_repair_feedback,
    _qa_result_from_generated_execution,
    _qa_review_context,
    _required_phase4_artifact,
    _string_items,
    _verification_attempt_count,
    _verification_policy_for_state,
    _verification_quality_report,
)


class PhaseFourQaCycle:
    """Own this Phase 4 transition cycle; composed by PhaseFourWorkflow."""

    def _continue_qa_test(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run QA from an implementation that already passed code trace."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        qa_version = _next_qa_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            qa_run = self._run_qa_generated_tests(
                state=state,
                artifact_paths=artifact_paths,
                latest_artifacts=latest_artifacts,
                work_packet=work_packet,
                target_info=target_info,
                qa_version=qa_version,
            )
        except HarnessError as exc:
            return self._block_qa_error(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                qa_version=qa_version,
                exc=exc,
                summary_prefix="QA/Tester Agent bi chan khi sinh/chay acceptance tests",
            )

        return self._finalize_qa_outcome(
            state=state,
            run_id=run_id,
            work_packet=work_packet,
            target_info=target_info,
            qa_run=qa_run,
            qa_version=qa_version,
            qa_repair_iterations=_int_value(state.get("qa_repair_iterations")),
            repaired_from=None,
        )

    def _continue_qa_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run one large QA repair loop: Developer.repair -> runner -> Validator -> QA."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        previous_qa_iterations = _int_value(state.get("qa_repair_iterations"))
        repair_iteration = previous_qa_iterations + 1
        resume_tool_loop = self._tool_loop_session_active(state, mode="qa_repair")
        bound_execution = _dict_value(state.get("developer_execution"))
        artifact_version = (
            _int_value(bound_execution.get("artifact_version"))
            if resume_tool_loop
            else _next_phase4_artifact_version(self.store, run_id)
        )
        if artifact_version <= 0:
            artifact_version = _next_phase4_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            feedback_path = _required_phase4_artifact(artifact_paths, "qa_repair_feedback", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            qa_feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_path, run_id=run_id),
                run_id=run_id,
            )
            if repair_iteration > self.settings.qa_repair_limit:
                raise HarnessError(
                    "qa_repair_limit_exceeded",
                    f"QA repair limit exceeded for run {run_id}: limit={self.settings.qa_repair_limit}.",
                    run_id=run_id,
                )
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            planning_policy = _planning_policy_for_state(state, run_id=run_id)
            if resume_tool_loop:
                refreshed_packet_path = packet_path
            else:
                work_packet = refresh_developer_work_packet(
                    packet=work_packet,
                    target_repo_path=target_info.path,
                    version=artifact_version,
                    run_id=run_id,
                    planning_policy=planning_policy,
                )
                refreshed_packet_path = write_phase4_developer_work_packet(
                    store=self.store,
                    run_id=run_id,
                    packet=work_packet,
                    version=artifact_version,
                )
            latest_artifacts = {
                **latest_artifacts,
                "developer_work_packet": refreshed_packet_path,
            }
            expected_dirty_paths = sorted(
                _qa_repair_dirty_paths(
                    store=self.store,
                    artifact_paths=artifact_paths,
                    qa_policy=_qa_policy_for_state(state, run_id=run_id),
                    run_id=run_id,
                )
            )
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_info.path,
                latest_artifacts=latest_artifacts,
                mode="qa_repair",
                current_phase="qa_repair",
                repair_iteration=repair_iteration,
                artifact_version=artifact_version,
                manifest_key="developer_qa_repair_execution_manifest",
                journal_key="developer_qa_repair_execution_journal",
                repair_feedback=qa_feedback,
                repair_scope="qa",
                expected_dirty_paths=expected_dirty_paths,
            )
            applied_files = result.files
            developer_summary = result.summary
            change_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "summary": developer_summary,
                "files": applied_files,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                "repair_source": "qa_acceptance",
                "execution_mode": "tool_loop",
                "developer_execution_manifest": result.manifest_path,
                "developer_execution_journal": result.journal_path,
            }
            change_report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=change_report,
                version=artifact_version,
            )
            verification_result = self.verification_runner.run(
                target_path=target_info.path,
                work_packet=work_packet,
                verification_policy=_verification_policy_for_state(state, run_id=run_id),
            )
            test_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                **verification_result.to_dict(),
            }
            test_report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=test_report,
                version=artifact_version,
            )
        except DeveloperExecutionError as exc:
            if exc.manifest_path:
                latest_artifacts["developer_qa_repair_execution_manifest"] = exc.manifest_path
            if exc.journal_path:
                latest_artifacts["developer_qa_repair_execution_journal"] = exc.journal_path
            if exc.session_path:
                latest_artifacts["developer_qa_repair_execution_session"] = exc.session_path
            if exc.failure_path:
                latest_artifacts["developer_failure_classification"] = exc.failure_path
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS if paused else ["get_status", "cancel_run"],
                    "qa_repair_iterations": previous_qa_iterations,
                    "last_error": exc.to_response(),
                    "developer_execution": {
                        **self._developer_execution_state(
                            state,
                            executor="tool_loop",
                            mode="qa_repair",
                            repair_iteration=repair_iteration,
                            artifact_version=artifact_version,
                            progress=exc.progress,
                            status="paused" if paused else "blocked",
                        )
                    },
                    "summary_for_user": (
                        f"Developer QA repair đã tạm dừng cho run {run_id}: {exc.message}"
                        if paused
                        else f"Developer QA repair bị chặn cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": previous_qa_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi Developer.repair xu ly QA feedback: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "code_change_report": change_report_path,
            "test_report": test_report_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=verification_result,
                report_path=test_report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=_int_value(state.get("implementation_repair_iterations")),
            ),
        }
        if verification_result.status != "passed":
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=verification_result,
                report_path=test_report_path,
                artifact_version=artifact_version,
                repair_iteration=_int_value(state.get("implementation_repair_iterations")),
                current_phase="qa_repair",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
            state["qa_repair_iterations"] = repair_iteration
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self._validate_code_trace_snapshot(
                state=state,
                artifact_paths=latest_artifacts,
                run_id=run_id,
                target_path=target_info.path,
                work_packet=work_packet,
                verification_evidence=_dict_value(quality_reports.get("verification")),
            )
            trace_payload = {
                **trace_result.to_dict(),
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": repair_iteration,
                "repaired_from": feedback_path,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
                version=artifact_version,
            )
            ledger_path = self._write_implementation_validation_ledger(
                run_id=run_id,
                trace_result=trace_result,
                version=artifact_version,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": repair_iteration,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi Validator trace lai sau QA repair: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "requirement_design_code_trace": trace_path,
            "implementation_validation_ledger": ledger_path,
        }
        quality_reports = {
            **quality_reports,
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if not trace_result.ok:
            implementation_iterations = _int_value(state.get("implementation_repair_iterations"))
            if (
                _is_repairable_code_trace_failure(trace_result)
                and implementation_iterations < self.settings.implementation_repair_limit
            ):
                feedback = _implementation_repair_feedback(
                    run_id=run_id,
                    state=state,
                    work_packet=work_packet,
                    trace_result=trace_result,
                    trace_path=trace_path,
                    target_repo=str(target_info.path),
                    branch=target_info.current_branch,
                    feedback_version=artifact_version,
                    repair_iteration=implementation_iterations,
                )
                implementation_feedback_path = write_phase4_repair_feedback(
                    store=self.store,
                    run_id=run_id,
                    feedback=feedback,
                    version=artifact_version,
                )
                latest_artifacts["implementation_repair_feedback"] = implementation_feedback_path
                quality_reports["implementation_repair"] = {
                    "needed": True,
                    "feedback_artifact": implementation_feedback_path,
                    "missing_count": len(trace_result.missing_links),
                    "repair_iterations": implementation_iterations,
                    "summary": trace_result.summary,
                }
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_repair",
                        "status": "implementation_repair_needed",
                        "latest_artifacts": latest_artifacts,
                        "quality_reports": quality_reports,
                        "pending_checkpoint": None,
                        "allowed_next_actions": ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
                        "implementation_repair_iterations": implementation_iterations,
                        "qa_repair_iterations": repair_iteration,
                        "last_error": None,
                        "summary_for_user": (
                            f"Validator code trace phát hiện missing link còn sửa được sau QA repair "
                            f"iteration {repair_iteration}: {trace_result.summary} Đã ghi feedback "
                            "để Developer chạy implementation repair trước khi QA tiếp tục."
                        ),
                    }
                )
                self.store.write_state(state)
                return self._status_response(state, created_new_run=False)

            if _is_repairable_code_trace_failure(trace_result):
                error_code = "implementation_repair_limit_exceeded"
                message = (
                    "Implementation repair limit reached after QA repair: "
                    f"iterations={implementation_iterations}, "
                    f"limit={self.settings.implementation_repair_limit}. {trace_result.summary}"
                )
            else:
                error_code = _nonrepairable_code_trace_error_code(trace_result)
                message = trace_result.summary
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": implementation_iterations,
                "limit_exceeded": error_code == "implementation_repair_limit_exceeded",
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(error_code),
                    "implementation_repair_iterations": implementation_iterations,
                    "qa_repair_iterations": repair_iteration,
                    "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Validator code trace không pass sau QA repair iteration {repair_iteration}: "
                        f"{message} Workflow chưa được phép chạy QA lại."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        qa_version = _next_qa_artifact_version(self.store, run_id)
        try:
            qa_run = self._run_qa_generated_tests(
                state=state,
                artifact_paths=latest_artifacts,
                latest_artifacts=latest_artifacts,
                work_packet=work_packet,
                target_info=target_info,
                qa_version=qa_version,
                repaired_from=feedback_path,
            )
        except HarnessError as exc:
            return self._block_qa_error(
                state=state,
                run_id=run_id,
                latest_artifacts=latest_artifacts,
                qa_version=qa_version,
                exc=exc,
                summary_prefix="QA/Tester Agent bi chan khi chay lai sau Developer.repair",
                qa_repair_iterations=repair_iteration,
            )

        return self._finalize_qa_outcome(
            state=state,
            run_id=run_id,
            work_packet=work_packet,
            target_info=target_info,
            qa_run=qa_run,
            qa_version=qa_version,
            qa_repair_iterations=repair_iteration,
            repaired_from=feedback_path,
            quality_reports_override=quality_reports,
        )

    def _run_qa_generated_tests(
        self,
        *,
        state: dict[str, object],
        artifact_paths: dict[str, str],
        latest_artifacts: dict[str, str],
        work_packet: dict[str, object],
        target_info: object,
        qa_version: int,
        repaired_from: str | None = None,
    ) -> dict[str, object]:
        """Sinh/apply QA tests, chạy baseline và từng QA class bắt buộc, rồi ghi artifacts."""

        run_id = str(state.get("run_id") or "")
        qa_policy = _qa_policy_for_state(state, run_id=run_id)

        context = _qa_review_context(
            store=self.store,
            state=state,
            artifact_paths=artifact_paths,
            run_id=run_id,
            target_repo=str(target_info.path),
            branch=target_info.current_branch,
            work_packet=work_packet,
            planning_policy=_planning_policy_for_state(state, run_id=run_id),
        )
        if repaired_from:
            context["repaired_from"] = repaired_from
        if qa_policy is not None:
            context["qa_generation_profile"] = qa_policy.prompt_context()
        generated_tests = self.qa_tester.generate_tests(
            context=context,
            qa_policy=qa_policy,
        )
        generated_payload = {
            **generated_tests.to_dict(),
            "run_id": state.get("run_id"),
            "source_ref": state.get("source_ref"),
            "issue_key": work_packet.get("issue_key"),
            "target_repo": str(target_info.path),
            "branch": target_info.current_branch,
            "repaired_from": repaired_from,
        }
        generated_path = write_phase4_qa_generated_tests(
            store=self.store,
            run_id=run_id,
            generated=generated_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_generated_tests": generated_path,
        }
        prior_qa_paths = _previous_qa_owned_paths(
            store=self.store,
            artifact_paths=artifact_paths,
            qa_policy=qa_policy,
            run_id=run_id,
        )
        applied_tests = _apply_qa_generated_tests(
            generated=generated_tests,
            target_path=target_info.path,
            run_id=run_id,
            qa_policy=qa_policy,
            prior_qa_paths=prior_qa_paths,
        )
        execution = self.verification_runner.run(
            target_path=target_info.path,
            work_packet=work_packet,
            required_test_files=[str(item.get("path") or "") for item in applied_tests],
            verification_policy=_verification_policy_for_state(
                state,
                run_id=str(state.get("run_id") or ""),
            ),
            qa_policy=qa_policy,
        )
        execution_payload = {
            "run_id": state.get("run_id"),
            "source_ref": state.get("source_ref"),
            "issue_key": work_packet.get("issue_key"),
            "target_repo": str(target_info.path),
            "branch": target_info.current_branch,
            "qa_generated_tests": generated_path,
            "applied_tests": applied_tests,
            "repaired_from": repaired_from,
            **execution.to_dict(),
        }
        execution_path = write_phase4_qa_execution_report(
            store=self.store,
            run_id=run_id,
            report=execution_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_test_execution": execution_path,
        }
        result = _qa_result_from_generated_execution(
            generated=generated_tests,
            execution=execution,
            execution_path=execution_path,
            generated_path=generated_path,
            applied_tests=applied_tests,
            qa_policy=qa_policy,
        )
        report_payload = {
            **result.to_dict(),
            "run_id": state.get("run_id"),
            "source_ref": state.get("source_ref"),
            "issue_key": work_packet.get("issue_key"),
            "target_repo": str(target_info.path),
            "branch": target_info.current_branch,
            "qa_generated_tests": generated_path,
            "qa_test_execution": execution_path,
            "execution_status": execution.status,
            "generation_profile": generated_tests.generation_profile or {},
            "selected_test_files": list(execution.selected_test_files),
            "verified_test_files": list(execution.verified_test_files),
            "applied_tests": applied_tests,
            "repaired_from": repaired_from,
            "requirement_contract": work_packet.get("requirement_contract"),
            "requirement_ids": sorted(
                {
                    requirement_id
                    for scenario in generated_tests.scenarios
                    for requirement_id in _string_items(scenario.get("requirement_ids"))
                }
            ),
        }
        report_path = write_phase4_qa_test_report(
            store=self.store,
            run_id=run_id,
            report=report_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_test_report": report_path,
        }
        return {
            "latest_artifacts": latest_artifacts,
            "result": result,
            "applied_tests": applied_tests,
            "generated_path": generated_path,
            "execution_path": execution_path,
            "report_path": report_path,
        }

    def _finalize_qa_outcome(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
        target_info: object,
        qa_run: dict[str, object],
        qa_version: int,
        qa_repair_iterations: int,
        repaired_from: str | None,
        quality_reports_override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Move the run to qa_tested, qa_repair_needed, or blocked after QA execution."""

        latest_artifacts = _string_dict(qa_run.get("latest_artifacts"))
        result = qa_run["result"]
        if not isinstance(result, QaTestResult):
            raise HarnessError("qa_test_failed", "QA result object is invalid.", run_id=run_id)
        applied_tests = _dict_items(qa_run.get("applied_tests"))
        quality_reports = {
            **(quality_reports_override or _dict_value(state.get("quality_reports"))),
            "qa_acceptance": result.to_quality_report(),
        }
        if result.ok:
            if qa_repair_iterations:
                quality_reports["qa_repair"] = {
                    "needed": False,
                    "repair_iterations": qa_repair_iterations,
                    "repaired_from": repaired_from,
                    "summary": result.summary,
                }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_testing",
                    "status": "qa_tested",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_QA_TESTED_ACTIONS,
                    "qa_repair_iterations": qa_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"QA/Tester Agent da sinh {len(applied_tests)} file acceptance test va Verification Runner "
                        f"da pass cho run {run_id}: {result.summary} Chua commit va chua mo PR."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if result.status == "fail" and qa_repair_iterations < self.settings.qa_repair_limit:
            feedback = _qa_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                qa_result=result,
                qa_run=qa_run,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=qa_version,
                repair_iteration=qa_repair_iterations,
            )
            feedback_path = write_phase4_qa_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=qa_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "qa_repair_feedback": feedback_path,
            }
            quality_reports["qa_repair"] = {
                "needed": True,
                "feedback_artifact": feedback_path,
                "repair_iterations": qa_repair_iterations,
                "summary": result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "qa_repair_needed",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_QA_REPAIR_NEEDED_ACTIONS,
                    "qa_repair_iterations": qa_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"QA/Tester Agent fail cho run {run_id}: {result.summary} "
                        f"Da ghi qa_repair_feedback_v{qa_version}.json; continue-run se giao Developer.repair."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if result.status == "skipped":
            error_code = "qa_test_skipped"
        elif result.status == "fail":
            error_code = "qa_repair_limit_exceeded"
            quality_reports["qa_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("qa_repair_feedback"),
                "repair_iterations": qa_repair_iterations,
                "limit_exceeded": True,
                "summary": result.summary,
            }
        else:
            error_code = "qa_test_failed"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "qa_testing",
                "status": "blocked",
                "latest_artifacts": latest_artifacts,
                "quality_reports": quality_reports,
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "qa_repair_iterations": qa_repair_iterations,
                "last_error": HarnessError(error_code, result.summary, run_id=run_id).to_response(),
                "summary_for_user": (
                    f"QA/Tester Agent status={result.status} cho run {run_id}: {result.summary} "
                    "Workflow chua duoc phep mo PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _block_qa_error(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        latest_artifacts: dict[str, str],
        qa_version: int,
        exc: HarnessError,
        summary_prefix: str,
        qa_repair_iterations: int | None = None,
    ) -> dict[str, object]:
        """Block a QA step and persist raw LLM output when available."""

        raw_response = getattr(exc, "raw_response", "")
        if raw_response:
            latest_artifacts = {
                **latest_artifacts,
                "qa_tester_raw_response": write_phase4_qa_raw_response(
                    store=self.store,
                    run_id=run_id,
                    raw_response=raw_response,
                    version=qa_version,
                ),
            }
        update = {
            "updated_at": utc_now_iso(),
            "current_phase": "qa_testing",
            "status": "blocked",
            "latest_artifacts": latest_artifacts,
            "pending_checkpoint": None,
            "allowed_next_actions": (
                ALLOWED_QA_GENERATION_RETRY_ACTIONS
                if exc.error_code in RETRYABLE_QA_GENERATION_ERROR_CODES
                else ["get_status", "cancel_run"]
            ),
            "last_error": exc.to_response(),
            "summary_for_user": f"{summary_prefix}: {exc.message}",
        }
        if qa_repair_iterations is not None:
            update["qa_repair_iterations"] = qa_repair_iterations
        state.update(update)
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

