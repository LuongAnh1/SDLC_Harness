"""Phase 4 implementation cycle."""

from __future__ import annotations

from pathlib import Path
import re

from ..agents.developer import (
    DeveloperExecutionError,
    DeveloperExecutionResult,
    build_developer_work_packet,
    refresh_developer_work_packet,
)
from ..artifacts.writers import (
    write_phase4_code_change_report,
    write_phase4_code_trace_report,
    write_phase4_developer_work_packet,
    write_phase4_repair_feedback,
    write_phase4_test_report,
)
from ..core.errors import HarnessError
from ..profiles import COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
from ..storage.runtime import utc_now_iso
from ..verification import VerificationResult
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import (
    ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
    ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS,
    ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS,
    ALLOWED_IMPLEMENTATION_TESTED_ACTIONS,
    ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
    ALLOWED_IMPLEMENTATION_WRITTEN_ACTIONS,
    REVALIDATABLE_IMPLEMENTATION_ERROR_CODES,
    _implementation_blocked_actions,
    _implementation_repair_feedback,
    _int_value,
    _is_repairable_code_trace_failure,
    _is_repairable_verification_failure,
    _next_phase4_artifact_version,
    _nonrepairable_code_trace_error_code,
    _planning_policy_for_state,
    _read_previous_repair_feedback,
    _required_phase4_artifact,
    _verification_attempt_count,
    _verification_policy_for_state,
    _verification_quality_report,
    _verification_repair_feedback,
)


class PhaseFourImplementationCycle:
    """Own this Phase 4 transition cycle; composed by PhaseFourWorkflow."""

    def revalidate_implementation(self, *, run_id: str) -> dict[str, object]:
        """Re-run Verification and Validator for a blocked, pending-repair, or traced snapshot."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            last_error = _dict_value(state.get("last_error"))
            blocked_revalidation = (
                str(state.get("status")) == "blocked"
                and last_error.get("error_code") in REVALIDATABLE_IMPLEMENTATION_ERROR_CODES
            )
            repair_feedback_revalidation = str(state.get("status") or "") == "implementation_repair_needed"
            traced_revalidation = str(state.get("status") or "") == "implementation_traced"
            if not blocked_revalidation and not repair_feedback_revalidation and not traced_revalidation:
                raise HarnessError(
                    "implementation_revalidate_not_allowed",
                    (
                        "Implementation revalidate is only allowed for a revalidatable Phase 4 block, "
                        f"pending Validator repair feedback, or traced implementation: {run_id}"
                    ),
                    run_id=run_id,
                )
            return self._revalidate_current_implementation(state=state, run_id=run_id)

    def _continue_developer_act(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy hoặc resume Developer tool-loop cho execution session hiện tại."""

        return self._continue_developer_tool_loop_act(state=state, run_id=run_id)

    @staticmethod
    def _tool_loop_session_active(state: dict[str, object], *, mode: str) -> bool:
        execution = _dict_value(state.get("developer_execution"))
        return (
            execution.get("executor") == "tool_loop"
            and execution.get("mode") == mode
            and execution.get("status") in {"executing", "paused", "blocked"}
        )

    @staticmethod
    def _developer_execution_state(
        state: dict[str, object],
        *,
        executor: str,
        mode: str,
        repair_iteration: int,
        session_iteration: int | None = None,
        artifact_version: int,
        progress: dict[str, object] | None = None,
        status: str,
        unit_aware: bool | None = None,
    ) -> dict[str, object]:
        existing = _dict_value(state.get("developer_execution"))
        if unit_aware is None:
            unit_aware = bool(
                (progress or {}).get("total_units") is not None
                or existing.get("contract_version") == "developer_execution_units_v1"
            )
        same_session = (
            existing.get("executor") == executor
            and existing.get("mode") == mode
            and _int_value(existing.get("repair_iteration")) == repair_iteration
            and _int_value(existing.get("artifact_version")) == artifact_version
        )
        session_ordinal = (
            _int_value(existing.get("session_iteration"))
            if session_iteration is None
            else max(0, session_iteration)
        )
        return {
            **(existing if same_session else {}),
            "schema_version": (
                "developer_execution_state_v2" if unit_aware else "developer_execution_state_v1"
            ),
            **({"contract_version": "developer_execution_units_v1"} if unit_aware else {}),
            "executor": executor,
            "mode": mode,
            "session_id": (
                f"{mode}-v{session_ordinal or artifact_version}"
                if unit_aware
                else existing.get("session_id", "")
            ),
            "repair_iteration": repair_iteration,
            **({"session_iteration": session_ordinal} if session_ordinal else {}),
            "artifact_version": artifact_version,
            **(progress or {}),
            "status": status,
        }

    def _execute_developer_tool_loop(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
        target_path: Path,
        latest_artifacts: dict[str, str],
        mode: str,
        current_phase: str,
        repair_iteration: int,
        session_iteration: int | None = None,
        artifact_version: int,
        manifest_key: str,
        journal_key: str,
        repair_feedback: dict[str, object] | None = None,
        repair_scope: str = "implementation",
        expected_dirty_paths: list[str] | None = None,
    ) -> DeveloperExecutionResult:
        """Chạy một durable tool-loop session và phản chiếu tiến độ vào workflow state."""

        unit_aware = bool(work_packet.get("execution_units"))
        session_key = manifest_key.replace("_manifest", "_session")
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": current_phase,
                "status": "implementation_executing",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
                "last_error": None,
                "developer_execution": self._developer_execution_state(
                    state,
                    executor="tool_loop",
                    mode=mode,
                    repair_iteration=repair_iteration,
                    session_iteration=session_iteration,
                    artifact_version=artifact_version,
                    status="executing",
                    unit_aware=unit_aware,
                ),
                "summary_for_user": f"Developer tool-loop đang thực thi {mode} cho run {run_id}.",
            }
        )
        self.store.write_state(state)

        def persist_progress(progress: dict[str, object], manifest_path: str, journal_path: str) -> None:
            latest_artifacts[manifest_key] = manifest_path
            latest_artifacts[journal_key] = journal_path
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": latest_artifacts,
                    "developer_execution": self._developer_execution_state(
                        state,
                        executor="tool_loop",
                        mode=mode,
                        repair_iteration=repair_iteration,
                        session_iteration=session_iteration,
                        artifact_version=artifact_version,
                        progress=progress,
                        status="executing",
                        unit_aware=unit_aware,
                    ),
                }
            )
            self.store.write_state(state)

        try:
            result = self.developer_tool_loop.execute(
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_path,
                repair_feedback=repair_feedback,
                repair_iteration=repair_iteration,
                session_iteration=session_iteration,
                repair_scope=repair_scope,
                expected_dirty_paths=expected_dirty_paths,
                verification_policy=_verification_policy_for_state(state, run_id=run_id),
                on_progress=persist_progress,
            )
        except DeveloperExecutionError:
            raise
        except HarnessError as exc:
            session_ordinal = repair_iteration if session_iteration is None else session_iteration
            session_name = f"{repair_scope}_repair_v{session_ordinal}" if repair_feedback else "act_v1"
            session_dir = self.store.run_dir(run_id) / "developer_execution" / session_name
            manifest_file = session_dir / "manifest.json"
            journal_file = session_dir / "journal.json"
            session_file = session_dir / "session.json"
            manifest_path = self.store.relative(manifest_file) if manifest_file.exists() else ""
            journal_path = self.store.relative(journal_file) if journal_file.exists() else ""
            session_path = self.store.relative(session_file) if session_file.exists() else ""
            raise DeveloperExecutionError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                retriable=exc.error_code == "llm_transport_failed",
                manifest_path=manifest_path,
                journal_path=journal_path,
                session_path=session_path,
                progress={
                    "status": "paused" if exc.error_code == "llm_transport_failed" else "blocked"
                },
            ) from exc
        except Exception as exc:
            session_ordinal = repair_iteration if session_iteration is None else session_iteration
            session_name = f"{repair_scope}_repair_v{session_ordinal}" if repair_feedback else "act_v1"
            session_dir = self.store.run_dir(run_id) / "developer_execution" / session_name
            manifest_file = session_dir / "manifest.json"
            journal_file = session_dir / "journal.json"
            session_file = session_dir / "session.json"
            manifest_path = self.store.relative(manifest_file) if manifest_file.exists() else ""
            journal_path = self.store.relative(journal_file) if journal_file.exists() else ""
            session_path = self.store.relative(session_file) if session_file.exists() else ""
            raise DeveloperExecutionError(
                "developer_execution_internal_error",
                f"Unexpected Developer executor failure ({type(exc).__name__}): {exc}",
                run_id=run_id,
                retriable=True,
                manifest_path=manifest_path,
                journal_path=journal_path,
                session_path=session_path,
                progress={"status": "paused"},
            ) from exc

        latest_artifacts[manifest_key] = result.manifest_path
        latest_artifacts[journal_key] = result.journal_path
        if result.session_path:
            latest_artifacts[session_key] = result.session_path
        state["latest_artifacts"] = latest_artifacts
        state["developer_execution"] = self._developer_execution_state(
            state,
            executor="tool_loop",
            mode=mode,
            repair_iteration=repair_iteration,
            session_iteration=session_iteration,
            artifact_version=artifact_version,
            progress=result.progress,
            status="completed",
            unit_aware=unit_aware,
        )
        return result

    def _continue_developer_tool_loop_act(
        self,
        *,
        state: dict[str, object],
        run_id: str,
    ) -> dict[str, object]:
        """Chạy hoặc resume initial Developer tool-loop trên đúng run worktree."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(
                self._existing_artifact_path(packet_path, run_id=run_id),
                run_id=run_id,
            )
            resuming = self._tool_loop_session_active(state, mode="developer_act")
            if not resuming:
                work_packet, upgraded_path = self._upgrade_unit_aware_work_packet(
                    state=state,
                    run_id=run_id,
                    work_packet=work_packet,
                )
                if upgraded_path:
                    packet_path = upgraded_path
                    latest_artifacts["developer_work_packet"] = upgraded_path
                    state["latest_artifacts"] = latest_artifacts
                    state["developer_execution_contract_version"] = "developer_execution_units_v1"
                    state["updated_at"] = utc_now_iso()
                    self.store.write_state(state)
            target_info = self._workspace_target_info(
                run_id=run_id,
                work_packet=work_packet,
                allow_dirty=resuming,
            )
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_info.path,
                latest_artifacts=latest_artifacts,
                mode="developer_act",
                current_phase="implementation_writing",
                repair_iteration=0,
                artifact_version=1,
                manifest_key="developer_execution_manifest",
                journal_key="developer_execution_journal",
            )
            applied_files = result.files
            report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "summary": result.summary,
                "files": applied_files,
                "execution_mode": "tool_loop",
                "developer_execution_manifest": result.manifest_path,
                "developer_execution_journal": result.journal_path,
            }
            report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=report,
                version=1,
            )
        except DeveloperExecutionError as exc:
            if exc.manifest_path:
                latest_artifacts["developer_execution_manifest"] = exc.manifest_path
            if exc.journal_path:
                latest_artifacts["developer_execution_journal"] = exc.journal_path
            if exc.session_path:
                latest_artifacts["developer_execution_session"] = exc.session_path
            if exc.failure_path:
                latest_artifacts["developer_failure_classification"] = exc.failure_path
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS if paused else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "developer_execution": self._developer_execution_state(
                        state,
                        executor="tool_loop",
                        mode="developer_act",
                        repair_iteration=0,
                        artifact_version=1,
                        progress=exc.progress,
                        status="paused" if paused else "blocked",
                    ),
                    "summary_for_user": (
                        f"Developer tool-loop đã tạm dừng cho run {run_id}: {exc.message}"
                        if paused
                        else f"Developer tool-loop bị chặn cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chuẩn bị Developer tool-loop: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts["code_change_report"] = report_path
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_writing",
                "status": "implementation_written",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_WRITTEN_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Developer tool-loop đã cập nhật {len(applied_files)} file cho run {run_id} "
                    f"trên branch `{target_info.current_branch}`. Chưa chạy Verification Runner."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _upgrade_unit_aware_work_packet(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
    ) -> tuple[dict[str, object], str]:
        """Upgrade an approved Plan V4 packet before the first Developer mutation only."""

        if work_packet.get("execution_units"):
            return work_packet, ""
        artifacts = _string_dict(state.get("latest_artifacts"))
        if any("execution_manifest" in key or "execution_journal" in key for key in artifacts):
            return work_packet, ""
        approved_plan = _string_dict(work_packet.get("approved_plan"))
        structured_ref = approved_plan.get("implementation_plan_structured")
        if not structured_ref:
            return work_packet, ""
        structured_plan = _read_json_artifact(
            self._existing_artifact_path(structured_ref, run_id=run_id),
            run_id=run_id,
        )
        if structured_plan.get("version") != COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION:
            return work_packet, ""
        target_info = self._workspace_target_info(
            run_id=run_id,
            work_packet=work_packet,
            allow_dirty=False,
        )
        workspace = self.workspace_manager.resolve(run_id)
        packet = build_developer_work_packet(
            run_id=run_id,
            source_ref=str(work_packet.get("source_ref") or state.get("source_ref") or ""),
            issue_key=str(work_packet.get("issue_key") or ""),
            structured_plan=structured_plan,
            target_repo_path=target_info.path,
            run_workspace=workspace.to_dict(),
            approved_plan_refs=approved_plan,
            canonical_requirements=[
                dict(item)
                for item in work_packet.get("canonical_requirements", [])
                if isinstance(item, dict)
            ],
            requirement_contract=_dict_value(work_packet.get("requirement_contract")),
            requirement_coverage=[
                dict(item)
                for item in work_packet.get("requirement_coverage", [])
                if isinstance(item, dict)
            ],
            effective_design_contract=_dict_value(work_packet.get("effective_design_contract")),
            planning_policy=_planning_policy_for_state(state, run_id=run_id),
            verification_policy=_verification_policy_for_state(state, run_id=run_id),
        )
        version = _next_phase4_artifact_version(self.store, run_id)
        path = write_phase4_developer_work_packet(
            store=self.store,
            run_id=run_id,
            packet=packet,
            version=version,
        )
        return packet, path

    def _continue_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.3: Verification Runner compile/unit test target repo."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        try:
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            result = self.verification_runner.run(
                target_path=target_info.path,
                work_packet=work_packet,
                verification_policy=_verification_policy_for_state(state, run_id=run_id),
            )
            report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                **result.to_dict(),
            }
            report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=report,
                version=1,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chạy Verification Runner: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **artifact_paths,
            "test_report": report_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=result,
                report_path=report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=0,
            ),
        }
        if result.status == "passed":
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_tested",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TESTED_ACTIONS,
                    "last_error": None,
                    "summary_for_user": (
                        f"Verification Runner đã pass compile/unit test cho run {run_id} "
                        f"trên branch `{target_info.current_branch}`. Slice 4.3 chưa chạy code trace, "
                        "chưa commit và chưa mở PR."
                    ),
                }
            )
        else:
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=result,
                report_path=report_path,
                artifact_version=1,
                repair_iteration=0,
                current_phase="implementation_testing",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _apply_verification_failure_state(
        self,
        *,
        state: dict[str, object],
        run_id: str,
        work_packet: dict[str, object],
        target_repo: str,
        branch: str,
        result: VerificationResult,
        report_path: str,
        artifact_version: int,
        repair_iteration: int,
        current_phase: str,
        latest_artifacts: dict[str, str],
        quality_reports: dict[str, object],
    ) -> None:
        """Route actionable compile/test diagnostics to Developer.repair."""

        repairable = _is_repairable_verification_failure(result)
        quality_reports["verification"] = {
            **_dict_value(quality_reports.get("verification")),
            "repairable": repairable,
        }
        if repairable and repair_iteration < self.settings.implementation_repair_limit:
            prior_feedback_ref = str(latest_artifacts.get("implementation_repair_feedback") or "").strip()
            prior_feedback = _read_previous_repair_feedback(
                store=self.store,
                artifact_ref=prior_feedback_ref,
                run_id=run_id,
            )
            previous_lineage = _dict_value(prior_feedback.get("repair_lineage"))
            root_feedback_ref = str(previous_lineage.get("root_feedback_artifact") or "").strip()
            root_feedback = _read_previous_repair_feedback(
                store=self.store,
                artifact_ref=root_feedback_ref,
                run_id=run_id,
            ) if root_feedback_ref and root_feedback_ref != prior_feedback_ref else prior_feedback
            feedback = _verification_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                result=result,
                report_path=report_path,
                target_repo=target_repo,
                branch=branch,
                feedback_version=artifact_version,
                repair_iteration=repair_iteration,
                prior_feedback=prior_feedback,
                prior_feedback_ref=prior_feedback_ref,
                root_feedback=root_feedback,
                root_feedback_ref=root_feedback_ref,
            )
            feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": feedback_path,
            }
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_source": "verification",
                "feedback_artifact": feedback_path,
                "repair_iterations": repair_iteration,
                "summary": result.reason,
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
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": None,
                    "summary_for_user": (
                        f"Verification Runner chưa pass cho run {run_id}: {result.reason} "
                        f"Đã ghi feedback v{artifact_version} để Developer sửa code/test trong allowed_paths."
                    ),
                }
            )
            return

        if repairable:
            error_code = "implementation_repair_limit_exceeded"
            message = (
                f"Implementation repair limit reached after {repair_iteration} iteration(s); "
                f"latest verification result: {result.reason}"
            )
        elif result.failure_kind == "environment":
            error_code = "verification_environment_failed"
            message = result.reason
        else:
            error_code = "test_skipped" if result.status == "skipped" else "test_failed"
            message = result.reason
        quality_reports["implementation_repair"] = {
            "needed": repairable,
            "feedback_source": "verification",
            "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
            "repair_iterations": repair_iteration,
            "limit_exceeded": error_code == "implementation_repair_limit_exceeded",
            "summary": message,
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": current_phase,
                "status": "blocked",
                "latest_artifacts": latest_artifacts,
                "quality_reports": quality_reports,
                "pending_checkpoint": None,
                "allowed_next_actions": _implementation_blocked_actions(error_code),
                "implementation_repair_iterations": repair_iteration,
                "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                "summary_for_user": (
                    f"Verification Runner status={result.status} cho run {run_id}: {message}. "
                    "Workflow chưa được phép chạy Validator hoặc mở PR."
                ),
            }
        )

    def _continue_code_trace(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.4: Validator trace approved design sang code/unit test."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
            for key in (
                "design_artifact",
                "requirement_design_trace",
                "class_diagram_graph",
                "sequence_diagram_graph",
            ):
                _required_phase4_artifact(artifact_paths, key, run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            trace_result = self._validate_code_trace_snapshot(
                state=state,
                artifact_paths=artifact_paths,
                run_id=run_id,
                target_path=target_info.path,
                work_packet=work_packet,
            )
            trace_payload = {
                **trace_result.to_dict(),
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
                version=1,
            )
            ledger_path = self._write_implementation_validation_ledger(
                run_id=run_id,
                trace_result=trace_result,
                version=1,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chạy Validator code trace: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **artifact_paths,
            "requirement_design_code_trace": trace_path,
            "implementation_validation_ledger": ledger_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if trace_result.ok:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_traced",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
                    "last_error": None,
                    "summary_for_user": (
                        f"Validator code trace đã pass cho run {run_id}: {trace_result.summary} "
                        "Slice 4.4 chưa chạy QA/Tester Agent, chưa commit và chưa mở PR."
                    ),
                }
            )
        elif _is_repairable_code_trace_failure(trace_result):
            feedback = _implementation_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                trace_result=trace_result,
                trace_path=trace_path,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=1,
                repair_iteration=0,
            )
            feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=1,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": feedback_path,
            }
            quality_reports = {
                **quality_reports,
                "implementation_repair": {
                    "needed": True,
                    "feedback_artifact": feedback_path,
                    "missing_count": len(trace_result.missing_links),
                        "repair_iterations": 0,
                    "summary": trace_result.summary,
                },
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
                    "last_error": None,
                    "implementation_repair_iterations": 0,
                    "summary_for_user": (
                        f"Validator code trace còn thiếu link có thể sửa ở code/test cho run {run_id}: "
                        f"{trace_result.summary} Đã ghi implementation_repair_feedback_v1.json. "
                        "Lần continue-run tiếp theo sẽ giao Developer.repair xử lý feedback này."
                    ),
                }
            )
        else:
            error_code = _nonrepairable_code_trace_error_code(trace_result)
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(error_code),
                    "last_error": HarnessError(
                        error_code,
                        trace_result.summary,
                        run_id=run_id,
                    ).to_response(),
                    "summary_for_user": (
                        f"Validator code trace còn thiếu link cho run {run_id}: {trace_result.summary} "
                        "Workflow chưa được phép mở PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _revalidate_current_implementation(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Re-run Verification Runner and Validator without editing code."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        previous_iterations = _int_value(state.get("implementation_repair_iterations"))
        revalidated_from = str(_dict_value(state.get("last_error")).get("error_code") or "blocked")
        reopen_repair_cycle = (
            revalidated_from == "implementation_repair_limit_exceeded"
            and previous_iterations >= self.settings.implementation_repair_limit
        )
        artifact_version = _next_phase4_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
            for key in (
                "design_artifact",
                "requirement_design_trace",
                "class_diagram_graph",
                "sequence_diagram_graph",
            ):
                _required_phase4_artifact(artifact_paths, key, run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._workspace_target_info(run_id=run_id, work_packet=work_packet)
            planning_policy = _planning_policy_for_state(state, run_id=run_id)
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
                "repair_iteration": previous_iterations,
                "revalidated_from": revalidated_from,
                "developer_work_packet": refreshed_packet_path,
                **verification_result.to_dict(),
            }
            test_report_path = write_phase4_test_report(
                store=self.store,
                run_id=run_id,
                report=test_report,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "test_report": test_report_path,
            }
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi revalidate implementation: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "verification": _verification_quality_report(
                result=verification_result,
                report_path=test_report_path,
                attempt=_verification_attempt_count(self.store, run_id),
                repair_iteration=previous_iterations,
            ),
        }
        if verification_result.status != "passed":
            repair_iteration = previous_iterations
            if reopen_repair_cycle and _is_repairable_verification_failure(verification_result):
                repair_iteration = 0
                state["implementation_repair_cycle"] = _int_value(state.get("implementation_repair_cycle")) + 1
                state["implementation_repair_total_iterations"] = max(
                    _int_value(state.get("implementation_repair_total_iterations")),
                    previous_iterations,
                )
                quality_reports["verification"] = {
                    **_dict_value(quality_reports.get("verification")),
                    "repair_iteration": repair_iteration,
                    "repair_cycle_reopened": True,
                    "previous_repair_iterations": previous_iterations,
                }
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=verification_result,
                report_path=test_report_path,
                artifact_version=artifact_version,
                repair_iteration=repair_iteration,
                current_phase="implementation_repair",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
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
                "repair_iteration": previous_iterations,
                "revalidated_from": revalidated_from,
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
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi Validator revalidate implementation: {exc.message}",
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
        trace_repair_iterations = previous_iterations
        if reopen_repair_cycle and _is_repairable_code_trace_failure(trace_result):
            trace_repair_iterations = 0
            state["implementation_repair_cycle"] = _int_value(state.get("implementation_repair_cycle")) + 1
            state["implementation_repair_total_iterations"] = max(
                _int_value(state.get("implementation_repair_total_iterations")),
                previous_iterations,
            )
        if trace_result.ok:
            quality_reports["implementation_repair"] = {
                "needed": False,
                "repair_iterations": previous_iterations,
                "revalidated": True,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_traced",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"Revalidate implementation da pass test va Validator code trace cho run {run_id}: "
                        f"{trace_result.summary} Workflow chua chay QA/Tester Agent, chua commit va chua mo PR."
                    ),
                }
            )
        elif (
            _is_repairable_code_trace_failure(trace_result)
            and trace_repair_iterations < self.settings.implementation_repair_limit
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
                repair_iteration=trace_repair_iterations,
            )
            feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": feedback_path,
            }
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": feedback_path,
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": trace_repair_iterations,
                "revalidated": True,
                "repair_cycle_reopened": reopen_repair_cycle,
                "previous_repair_iterations": previous_iterations,
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
                    "implementation_repair_iterations": trace_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"Revalidate implementation cho run {run_id} da tao feedback moi v{artifact_version}: "
                        f"{trace_result.summary} Co the chay continue-run de Developer.repair tiep."
                    ),
                }
            )
        else:
            if _is_repairable_code_trace_failure(trace_result):
                error_code = "implementation_repair_limit_exceeded"
                message = (
                    f"Implementation repair limit is still reached after revalidation: "
                    f"iterations={trace_repair_iterations}, limit={self.settings.implementation_repair_limit}. "
                    f"{trace_result.summary}"
                )
                allowed_actions = ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS
            else:
                error_code = _nonrepairable_code_trace_error_code(trace_result)
                message = trace_result.summary
                allowed_actions = _implementation_blocked_actions(error_code)
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": trace_repair_iterations,
                "limit_exceeded": error_code == "implementation_repair_limit_exceeded",
                "revalidated": True,
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
                    "allowed_next_actions": allowed_actions,
                    "implementation_repair_iterations": trace_repair_iterations,
                    "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Revalidate implementation van chua pass cho run {run_id}: {message} "
                        "Workflow chua duoc phep mo PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_implementation_repair(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy một vòng Developer.repair rồi test và trace lại."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        resume_tool_loop = self._tool_loop_session_active(state, mode="implementation_repair")
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
            packet_path = self._bound_repair_work_packet_path(
                run_id=run_id,
                current_packet_path=packet_path,
                artifact_version=artifact_version,
                resume_tool_loop=resume_tool_loop,
            )
            feedback_path = _required_phase4_artifact(
                artifact_paths,
                "implementation_repair_feedback",
                run_id=run_id,
            )
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            repair_feedback = _read_json_artifact(
                self._existing_artifact_path(feedback_path, run_id=run_id),
                run_id=run_id,
            )
            previous_iterations = _int_value(state.get("implementation_repair_iterations"))
            previous_total_iterations = max(
                _int_value(state.get("implementation_repair_total_iterations")),
                previous_iterations,
            )
            bound_session_iteration = _int_value(bound_execution.get("session_iteration"))
            if resume_tool_loop:
                repair_iteration = max(
                    1,
                    _int_value(bound_execution.get("repair_iteration")),
                )
                session_iteration = max(1, bound_session_iteration)
                total_repair_iterations = max(previous_total_iterations, session_iteration)
            else:
                repair_iteration = previous_iterations + 1
                total_repair_iterations = previous_total_iterations + 1
                session_iteration = self._next_repair_session_iteration(
                    run_id=run_id,
                    repair_scope="implementation",
                    minimum=total_repair_iterations,
                )
                total_repair_iterations = max(total_repair_iterations, session_iteration)
            if repair_iteration > self.settings.implementation_repair_limit:
                raise HarnessError(
                    "implementation_repair_limit_exceeded",
                    (
                        f"Implementation repair limit exceeded for run {run_id}: "
                        f"limit={self.settings.implementation_repair_limit}."
                    ),
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
                **artifact_paths,
                "developer_work_packet": refreshed_packet_path,
            }
            state["implementation_repair_total_iterations"] = total_repair_iterations
            result = self._execute_developer_tool_loop(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_path=target_info.path,
                latest_artifacts=latest_artifacts,
                mode="implementation_repair",
                current_phase="implementation_repair",
                repair_iteration=repair_iteration,
                session_iteration=session_iteration,
                artifact_version=artifact_version,
                manifest_key="developer_repair_execution_manifest",
                journal_key="developer_repair_execution_journal",
                repair_feedback=repair_feedback,
                repair_scope="implementation",
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
                latest_artifacts["developer_repair_execution_manifest"] = exc.manifest_path
            if exc.journal_path:
                latest_artifacts["developer_repair_execution_journal"] = exc.journal_path
            if exc.session_path:
                latest_artifacts["developer_repair_execution_session"] = exc.session_path
            if exc.failure_path:
                latest_artifacts["developer_failure_classification"] = exc.failure_path
            paused = exc.retriable
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "implementation_paused" if paused else "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS if paused else ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "developer_execution": {
                        **self._developer_execution_state(
                            state,
                            executor="tool_loop",
                            mode="implementation_repair",
                            repair_iteration=locals().get("repair_iteration", 0),
                            artifact_version=artifact_version,
                            progress=exc.progress,
                            status="paused" if paused else "blocked",
                        )
                    },
                    "summary_for_user": (
                        f"Developer repair tool loop đã tạm dừng cho run {run_id}: {exc.message}"
                        if paused
                        else f"Developer repair tool loop bị chặn cho run {run_id}: {exc.message}"
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS
                        if exc.error_code in REVALIDATABLE_IMPLEMENTATION_ERROR_CODES
                        else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Developer.repair xử lý feedback: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)
        except Exception as exc:  # Last-resort state recovery for executor defects.
            error = HarnessError(
                "developer_execution_internal_error",
                f"Unexpected Developer executor failure ({type(exc).__name__}): {exc}",
                run_id=run_id,
            )
            recovery_state: dict[str, object] = {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_repair",
                "status": "implementation_paused",
                "latest_artifacts": latest_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
                "last_error": error.to_response(),
                "summary_for_user": f"Developer repair đã tạm dừng an toàn cho run {run_id} vì lỗi executor nội bộ.",
            }
            recovery_state["developer_execution"] = self._developer_execution_state(
                state,
                executor="tool_loop",
                mode="implementation_repair",
                repair_iteration=locals().get("repair_iteration", 0),
                artifact_version=artifact_version,
                status="paused",
            )
            state.update(recovery_state)
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
                repair_iteration=repair_iteration,
            ),
        }
        if verification_result.status != "passed":
            state["implementation_repair_total_iterations"] = total_repair_iterations
            self._apply_verification_failure_state(
                state=state,
                run_id=run_id,
                work_packet=work_packet,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                result=verification_result,
                report_path=test_report_path,
                artifact_version=artifact_version,
                repair_iteration=repair_iteration,
                current_phase="implementation_repair",
                latest_artifacts=latest_artifacts,
                quality_reports=quality_reports,
            )
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
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": _implementation_blocked_actions(exc.error_code),
                    "implementation_repair_iterations": repair_iteration,
                    "implementation_repair_total_iterations": total_repair_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Validator trace lại sau Developer.repair: {exc.message}",
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
        if trace_result.ok:
            quality_reports["implementation_repair"] = {
                "needed": False,
                "repair_iterations": repair_iteration,
                "summary": trace_result.summary,
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_traced",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ALLOWED_IMPLEMENTATION_TRACED_ACTIONS,
                    "implementation_repair_iterations": repair_iteration,
                    "implementation_repair_total_iterations": total_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"Developer.repair iteration {repair_iteration} đã pass test và Validator code trace "
                        f"cho run {run_id}: {trace_result.summary} Slice 4.6 chưa chạy QA/Tester Agent, "
                        "chưa commit và chưa mở PR."
                    ),
                }
            )
        elif _is_repairable_code_trace_failure(trace_result) and repair_iteration < self.settings.implementation_repair_limit:
            feedback = _implementation_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                trace_result=trace_result,
                trace_path=trace_path,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=artifact_version,
                repair_iteration=repair_iteration,
            )
            next_feedback_path = write_phase4_repair_feedback(
                store=self.store,
                run_id=run_id,
                feedback=feedback,
                version=artifact_version,
            )
            latest_artifacts = {
                **latest_artifacts,
                "implementation_repair_feedback": next_feedback_path,
            }
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": next_feedback_path,
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": repair_iteration,
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
                    "implementation_repair_iterations": repair_iteration,
                    "implementation_repair_total_iterations": total_repair_iterations,
                    "last_error": None,
                    "summary_for_user": (
                        f"Developer.repair iteration {repair_iteration} đã chạy xong nhưng Validator vẫn còn "
                        f"missing link có thể sửa: {trace_result.summary} Đã ghi feedback v{artifact_version} "
                        "để chạy tiếp một vòng repair khác."
                    ),
                }
            )
        else:
            if _is_repairable_code_trace_failure(trace_result):
                error_code = "implementation_repair_limit_exceeded"
                message = (
                    f"Implementation repair limit reached after {repair_iteration} iteration(s): "
                    f"{trace_result.summary}"
                )
            else:
                error_code = _nonrepairable_code_trace_error_code(trace_result)
                message = trace_result.summary
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": repair_iteration,
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
                    "implementation_repair_iterations": repair_iteration,
                    "implementation_repair_total_iterations": total_repair_iterations,
                    "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Validator code trace vẫn chưa pass sau Developer.repair iteration {repair_iteration}: "
                        f"{message} Workflow chưa được phép mở PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _bound_repair_work_packet_path(
        self,
        *,
        run_id: str,
        current_packet_path: str,
        artifact_version: int,
        resume_tool_loop: bool,
    ) -> str:
        """Resume bằng đúng packet version đã khóa cùng execution session."""

        if not resume_tool_loop:
            return current_packet_path
        bound_path = self.store.artifact_path(
            run_id,
            f"developer_work_packet_v{artifact_version}.json",
        )
        if not bound_path.is_file():
            raise HarnessError(
                "developer_input_drift",
                (
                    "The Developer execution session is missing its bound work packet: "
                    f"developer_work_packet_v{artifact_version}.json"
                ),
                run_id=run_id,
            )
        return self.store.relative(bound_path)

    def _next_repair_session_iteration(
        self,
        *,
        run_id: str,
        repair_scope: str,
        minimum: int,
    ) -> int:
        """Chọn namespace session chưa dùng, kể cả khi state legacy thiếu ordinal."""

        normalized_scope = re.sub(r"[^a-z0-9_]+", "_", repair_scope.strip().lower()).strip("_")
        normalized_scope = normalized_scope or "implementation"
        pattern = re.compile(rf"^{re.escape(normalized_scope)}_repair_v(?P<ordinal>\d+)$")
        execution_root = self.store.run_dir(run_id) / "developer_execution"
        highest = 0
        if execution_root.is_dir():
            for candidate in execution_root.iterdir():
                if not candidate.is_dir():
                    continue
                match = pattern.fullmatch(candidate.name)
                if match:
                    highest = max(highest, int(match.group("ordinal")))
        return max(1, minimum, highest + 1)

