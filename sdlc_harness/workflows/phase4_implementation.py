"""Phase 4 implementation cycle."""

from __future__ import annotations

from pathlib import Path
import re

from ..agents.developer import (
    DeveloperExecutionError,
    DeveloperExecutionResult,
    build_developer_work_packet,
    canonicalize_work_unit_dependencies,
    component_plan_projections,
    merge_work_unit_updates,
    refresh_developer_execution_context,
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
from ..profiles import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
)
from ..storage.runtime import utc_now_iso
from ..verification import VerificationResult
from .phase3 import _dict_value, _read_json_artifact, _string_dict
from .phase4 import (
    ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS,
    ALLOWED_IMPLEMENTATION_READY_ACTIONS,
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


_EXECUTION_UNIT_CONTRACT_FIELDS = {
    "boundary_contracts",
    "context_paths",
    "contract_anchor_paths",
    "migration_impact",
    "planned_code_targets",
    "runtime_bindings",
    "write_paths",
}


def _unit_target_paths(units: list[dict[str, object]]) -> dict[str, set[str]]:
    return {
        str(unit.get("unit_id") or ""): {
            str(target.get("path") or "")
            for target in unit.get("planned_code_targets", [])
            if isinstance(target, dict) and str(target.get("path") or "")
        }
        for unit in units
    }


def _packet_unit_target_paths(packet: dict[str, object]) -> dict[str, set[str]]:
    return {
        str(unit.get("unit_id") or ""): {
            str(target.get("path") or "").replace("\\", "/")
            for target in unit.get("planned_code_targets", [])
            if isinstance(target, dict) and str(target.get("path") or "")
        }
        for unit in packet.get("execution_units", [])
        if isinstance(unit, dict)
    }


def _unit_path_changes(
    before: dict[str, set[str]],
    after: dict[str, set[str]],
) -> list[dict[str, object]]:
    return [
        {
            "unit_id": unit_id,
            "added_paths": sorted(after.get(unit_id, set()) - before.get(unit_id, set())),
            "removed_paths": sorted(before.get(unit_id, set()) - after.get(unit_id, set())),
        }
        for unit_id in sorted(set(before) | set(after))
        if before.get(unit_id, set()) != after.get(unit_id, set())
    ]


def _has_complete_execution_unit_contract(work_packet: dict[str, object]) -> bool:
    if work_packet.get("schema_version") != UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION:
        return True
    raw_units = work_packet.get("execution_units")
    if not isinstance(raw_units, list):
        return False
    units = [item for item in raw_units if isinstance(item, dict)]
    return bool(units) and all(_EXECUTION_UNIT_CONTRACT_FIELDS <= set(unit) for unit in units)


class PhaseFourImplementationCycle:
    """Own this Phase 4 transition cycle; composed by PhaseFourWorkflow."""

    def recover_developer_scope(self, *, run_id: str) -> dict[str, object]:
        """Apply a deterministic scope amendment, preserving an active session when safe."""

        with self.workspace_manager.locked_workspace(run_id):
            state = self.store.read_state(run_id)
            execution = _dict_value(state.get("developer_execution"))
            if (
                str(state.get("current_phase") or "") != "implementation_writing"
                or str(state.get("status") or "") not in {"implementation_paused", "blocked"}
                or execution.get("executor") != "tool_loop"
                or execution.get("mode") != "developer_act"
            ):
                raise HarnessError(
                    "developer_scope_recovery_not_allowed",
                    f"Run is not paused in the initial Developer act session: {run_id}",
                    run_id=run_id,
                )

            artifacts = _string_dict(state.get("latest_artifacts"))
            plan_path = _required_phase4_artifact(
                artifacts,
                "implementation_plan_structured",
                run_id=run_id,
            )
            packet_path = _required_phase4_artifact(
                artifacts,
                "developer_work_packet",
                run_id=run_id,
            )
            design_graph_path = _required_phase4_artifact(
                artifacts,
                "vertical_design_graph",
                run_id=run_id,
            )
            requirement_graph_path = _required_phase4_artifact(
                artifacts,
                "requirement_graph",
                run_id=run_id,
            )
            structured_plan = _read_json_artifact(
                self._existing_artifact_path(plan_path, run_id=run_id),
                run_id=run_id,
            )
            previous_packet = _read_json_artifact(
                self._existing_artifact_path(packet_path, run_id=run_id),
                run_id=run_id,
            )
            design_graph = _read_json_artifact(
                self._existing_artifact_path(design_graph_path, run_id=run_id),
                run_id=run_id,
            )
            requirement_graph = _read_json_artifact(
                self._existing_artifact_path(requirement_graph_path, run_id=run_id),
                run_id=run_id,
            )
            planning_policy = _planning_policy_for_state(state, run_id=run_id)
            previous_units = [
                dict(item)
                for item in structured_plan.get("work_units", [])
                if isinstance(item, dict)
            ]
            amended_units = merge_work_unit_updates(
                units=previous_units,
                updates=previous_units,
                planning_policy=planning_policy,
            )
            amended_units = canonicalize_work_unit_dependencies(amended_units)

            previous_paths = _packet_unit_target_paths(previous_packet) or _unit_target_paths(previous_units)
            amended_paths = _unit_target_paths(amended_units)
            changed_paths = sorted(
                {
                    path
                    for unit_id in set(previous_paths) | set(amended_paths)
                    for path in previous_paths.get(unit_id, set()) ^ amended_paths.get(unit_id, set())
                }
            )
            allowed_scope_amendments = {"packages/db/src/index.ts"}
            if not changed_paths:
                raise HarnessError(
                    "developer_scope_recovery_not_needed",
                    "The approved Developer scope already matches the current deterministic policy.",
                    run_id=run_id,
                )
            if set(changed_paths) - allowed_scope_amendments:
                raise HarnessError(
                    "developer_scope_recovery_unsafe",
                    f"Scope recovery attempted unsupported path changes: {changed_paths}",
                    run_id=run_id,
                )

            projections = component_plan_projections(
                units=amended_units,
                design_graph=design_graph,
                requirement_graph=requirement_graph,
                legacy_mappings=[
                    dict(item)
                    for item in structured_plan.get("design_mappings", [])
                    if isinstance(item, dict)
                ],
                planning_policy=planning_policy,
            )
            amended_plan = {
                **structured_plan,
                "work_units": amended_units,
                **projections,
            }
            packet_version = _next_phase4_artifact_version(self.store, run_id)
            pending_review = _dict_value(execution.get("pending_step_review"))
            active_revision = max(1, _int_value(state.get("developer_act_revision")))
            preserve_session = bool(_int_value(execution.get("completed_items"))) and all(
                artifacts.get(key)
                for key in (
                    "developer_execution_manifest",
                    "developer_execution_journal",
                    "developer_execution_session",
                )
            )
            revision = active_revision if preserve_session else max(2, active_revision + 1)
            amendment_file = self.store.write_json_artifact(
                run_id,
                f"developer_scope_amendment_v{packet_version}.json",
                {
                    "schema_version": "developer_scope_amendment_v1",
                    "run_id": run_id,
                    "revision": revision,
                    "amendment_version": packet_version,
                    "work_packet_version": packet_version,
                    "reason": "Apply deterministic package-entrypoint closure without discarding worktree changes.",
                    "source_plan": plan_path,
                    "source_work_packet": packet_path,
                    "changed_paths": changed_paths,
                    "unit_path_changes": _unit_path_changes(previous_paths, amended_paths),
                    "superseded_step_review_call_id": str(pending_review.get("call_id") or ""),
                    "created_at": utc_now_iso(),
                },
            )
            amendment_path = self.store.relative(amendment_file)
            workspace = self.workspace_manager.resolve(run_id)
            target_info = self._workspace_target_info(run_id=run_id, allow_dirty=True)
            packet = build_developer_work_packet(
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                issue_key=str(previous_packet.get("issue_key") or ""),
                structured_plan=amended_plan,
                target_repo_path=target_info.path,
                run_workspace=workspace.to_dict(),
                approved_plan_refs={
                    **_dict_value(previous_packet.get("approved_plan")),
                    "developer_scope_amendment": amendment_path,
                },
                canonical_requirements=[
                    dict(item)
                    for item in previous_packet.get("canonical_requirements", [])
                    if isinstance(item, dict)
                ],
                requirement_contract=_dict_value(previous_packet.get("requirement_contract")),
                requirement_coverage=[
                    dict(item)
                    for item in previous_packet.get("requirement_coverage", [])
                    if isinstance(item, dict)
                ],
                effective_design_contract=_dict_value(
                    previous_packet.get("effective_design_contract")
                ),
                planning_policy=planning_policy,
                verification_policy=_verification_policy_for_state(state, run_id=run_id),
            )
            revised_packet_path = write_phase4_developer_work_packet(
                store=self.store,
                run_id=run_id,
                packet=packet,
                version=packet_version,
            )

            latest_artifacts = {
                **artifacts,
                "developer_scope_amendment": amendment_path,
                "developer_work_packet": revised_packet_path,
            }
            history = [
                dict(item)
                for item in state.get("developer_execution_history", [])
                if isinstance(item, dict)
            ]
            if preserve_session:
                completed_ids = [
                    value
                    for value in execution.get("completed_unit_ids", [])
                    if isinstance(value, str)
                ]
                changed_unit_ids = {
                    str(change.get("unit_id") or "")
                    for change in _unit_path_changes(previous_paths, amended_paths)
                    if "packages/db/src/index.ts" in change.get("added_paths", [])
                }
                reopened_ids = [value for value in completed_ids if value in changed_unit_ids]
                remaining_completed_ids = [value for value in completed_ids if value not in changed_unit_ids]
                ordered_unit_ids = [
                    str(unit.get("unit_id") or "") for unit in amended_units
                ]
                pending_ids = [value for value in ordered_unit_ids if value not in remaining_completed_ids]
                preserved_execution = {
                    **execution,
                    "status": "paused",
                    "completed_items": len(remaining_completed_ids),
                    "remaining_items": len(ordered_unit_ids) - len(remaining_completed_ids),
                    "completed_units": len(remaining_completed_ids),
                    "completed_unit_ids": remaining_completed_ids,
                    "pending_unit_ids": pending_ids,
                    "current_item": reopened_ids[0] if reopened_ids else pending_ids[0],
                    "current_unit_id": reopened_ids[0] if reopened_ids else pending_ids[0],
                    "scope_reopened_unit_ids": reopened_ids,
                }
                preserved_execution.pop("pending_step_review", None)
            else:
                for key in (
                    "developer_execution_manifest",
                    "developer_execution_journal",
                    "developer_execution_session",
                    "developer_failure_classification",
                ):
                    latest_artifacts.pop(key, None)
                history.append(
                    {
                        "revision": revision - 1,
                        "execution": execution,
                        "manifest": artifacts.get("developer_execution_manifest", ""),
                        "journal": artifacts.get("developer_execution_journal", ""),
                        "session": artifacts.get("developer_execution_session", ""),
                        "superseded_at": utc_now_iso(),
                    }
                )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing" if preserve_session else "implementation_preparing",
                    "status": "implementation_paused" if preserve_session else "implementation_ready",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ALLOWED_IMPLEMENTATION_EXECUTION_ACTIONS
                        if preserve_session
                        else ALLOWED_IMPLEMENTATION_READY_ACTIONS
                    ),
                    "last_error": None,
                    "developer_act_revision": revision,
                    "developer_execution_history": history,
                    "developer_execution": preserved_execution if preserve_session else {
                        "schema_version": "developer_execution_state_v2",
                        "contract_version": "developer_execution_units_v1",
                        "executor": "tool_loop",
                        "mode": "developer_act",
                        "session_id": f"developer_act-v{revision}",
                        "repair_iteration": 0,
                        "session_iteration": revision,
                        "artifact_version": revision,
                        "status": "pending",
                    },
                    "summary_for_user": (
                        f"Developer scope recovery kept act_v{revision} for run {run_id}; "
                        f"reopened only: {', '.join(reopened_ids)}."
                        if preserve_session
                        else f"Developer scope recovery V{revision} prepared for run {run_id}; "
                        f"preserved the worktree and amended: {', '.join(changed_paths)}."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

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
            session_name = (
                f"{repair_scope}_repair_v{session_ordinal}"
                if repair_feedback
                else f"act_v{max(1, session_ordinal)}"
            )
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
            session_name = (
                f"{repair_scope}_repair_v{session_ordinal}"
                if repair_feedback
                else f"act_v{max(1, session_ordinal)}"
            )
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
        act_revision = max(1, _int_value(state.get("developer_act_revision")))
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(
                self._existing_artifact_path(packet_path, run_id=run_id),
                run_id=run_id,
            )
            resuming = self._tool_loop_session_active(state, mode="developer_act")
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
            if not _has_complete_execution_unit_contract(work_packet):
                raise HarnessError(
                    "developer_execution_unit_contract_incomplete",
                    (
                        "Developer work packet omits approved execution-unit contracts. "
                        "Rebuild the packet before starting or resuming Developer execution."
                    ),
                    run_id=run_id,
                )
            target_info = self._workspace_target_info(
                run_id=run_id,
                work_packet=work_packet,
                allow_dirty=resuming or act_revision > 1,
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
                session_iteration=act_revision,
                artifact_version=act_revision,
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
                version=act_revision,
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
                        session_iteration=act_revision,
                        artifact_version=act_revision,
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

        if work_packet.get("execution_units") and _has_complete_execution_unit_contract(work_packet):
            return work_packet, ""
        artifacts = _string_dict(state.get("latest_artifacts"))
        has_execution_session = any(
            "execution_manifest" in key or "execution_journal" in key for key in artifacts
        )
        units = [
            item for item in work_packet.get("execution_units", [])
            if isinstance(item, dict)
        ]
        context_only_upgrade = bool(units) and all(
            {
                "boundary_contracts",
                "migration_impact",
                "planned_code_targets",
                "runtime_bindings",
                "write_paths",
            } <= set(unit)
            for unit in units
        )
        if has_execution_session and not context_only_upgrade:
            return work_packet, ""
        if has_execution_session:
            version = _next_phase4_artifact_version(self.store, run_id)
            packet = {
                **refresh_developer_execution_context(packet=work_packet, run_id=run_id),
                "version": f"developer_work_packet_v{version}",
                "mode": "approved_execution_context_upgrade_v1",
                "refreshed_at": utc_now_iso(),
                "refreshed_from": str(work_packet.get("version") or ""),
            }
            path = write_phase4_developer_work_packet(
                store=self.store,
                run_id=run_id,
                packet=packet,
                version=version,
            )
            return packet, path
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
            allow_dirty=context_only_upgrade,
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

