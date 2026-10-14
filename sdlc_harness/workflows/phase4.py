"""Workflow Phase 4: implementation plan -> code/test/trace/QA repair.

Module này điều phối Phase 4 sau khi implementation plan đã được human duyệt:
đóng gói DeveloperWorkPacket, cho Developer.act ghi code vào target repo, chạy
Verification Runner, rồi bắt Validator kiểm trace requirement/design sang
code/test. Nếu Validator thấy gap nằm trong code/test, workflow ghi feedback cho
Developer.repair, refresh snapshot từ target repo thật, chạy lại runner +
Validator và chỉ pass khi trace đạt `implementation_traced`. Sau đó QA sinh
acceptance tests; nếu test fail do code/test, workflow có thể giao feedback lại
Developer.repair rồi chạy lại Runner -> Validator -> QA.

`revalidate_implementation` là đường vận hành riêng cho run từng bị block vì
repair limit: nó kiểm lại code hiện tại bằng logic Validator mới mà không tự
gọi Developer sửa thêm.
"""

from __future__ import annotations

from pathlib import Path

from ..agents.developer import (
    DeveloperActAgent,
    DeveloperActResult,
    build_developer_work_packet,
    refresh_developer_work_packet,
)
from ..agents.tester import QaGeneratedTestSuite, QaTestResult, QaTesterAgent, QaTesterLlmError
from ..agents.validator import CodeTraceValidator
from ..artifacts.writers import (
    write_phase4_code_change_report,
    write_phase4_code_trace_report,
    write_phase4_developer_raw_response,
    write_phase4_developer_work_packet,
    write_phase4_qa_execution_report,
    write_phase4_qa_generated_tests,
    write_phase4_qa_raw_response,
    write_phase4_qa_repair_feedback,
    write_phase4_qa_test_report,
    write_phase4_repair_feedback,
    write_phase4_test_report,
)
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..repo_context import CodeGraphContextProvider
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..verification import VerificationRunner
from .phase3 import (
    _dirty_status_without_codegraph,
    _dict_value,
    _git_output,
    _issue_key_from_state,
    _normalize_remote,
    _read_json_artifact,
    _string_dict,
    _target_source_files,
)

ALLOWED_IMPLEMENTATION_READY_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_WRITTEN_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_TESTED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_TRACED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_REPAIR_NEEDED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS = ["revalidate_implementation", "get_status", "cancel_run"]
ALLOWED_QA_REPAIR_NEEDED_ACTIONS = ["continue_run", "get_status", "cancel_run"]
ALLOWED_QA_TESTED_ACTIONS = ["get_status", "cancel_run"]

MAX_QA_REQUIREMENT_CHARS = 6000
MAX_QA_VERIFICATION_CHARS = 4000
MAX_QA_CODE_FILE_CHARS = 5000
MAX_QA_CODE_TOTAL_CHARS = 24000
MAX_QA_TRACE_SAMPLE_EDGES = 20


class PhaseFourWorkflow:
    """Điều phối các lát Phase 4 từ plan đã duyệt tới code trace pass."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.repo_context_provider = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")
        self.llm_client = LlmClient(settings)
        self.developer = DeveloperActAgent(self.llm_client)
        self.verification_runner = VerificationRunner()
        self.code_trace_validator = CodeTraceValidator(self.llm_client)
        self.qa_tester = QaTesterAgent(self.llm_client)

    def approve_implementation_plan_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, object]:
        """Duyệt implementation plan và sinh DeveloperWorkPacket V1."""

        state = self.store.read_state(run_id)
        self._require_implementation_plan_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        implementation_plan_path = _required_phase4_artifact(
            artifact_paths,
            "implementation_plan",
            run_id=run_id,
        )
        structured_plan_path = _required_phase4_artifact(
            artifact_paths,
            "implementation_plan_structured",
            run_id=run_id,
        )
        review_path = _required_phase4_artifact(
            artifact_paths,
            "implementation_plan_review",
            run_id=run_id,
        )
        # Các artifact design/trace vẫn được kiểm tra ở đây để đảm bảo packet
        # không bị tách khỏi graph đã được Validator dùng khi lập plan.
        for key in (
            "design_artifact",
            "requirement_design_trace",
            "class_diagram_graph",
            "sequence_diagram_graph",
        ):
            _required_phase4_artifact(artifact_paths, key, run_id=run_id)

        structured_plan = _read_json_artifact(
            self._existing_artifact_path(structured_plan_path, run_id=run_id),
            run_id=run_id,
        )
        review = _read_json_artifact(self._existing_artifact_path(review_path, run_id=run_id), run_id=run_id)
        if review.get("ok") is not True:
            raise HarnessError(
                "implementation_plan_review_not_passed",
                "Implementation plan review is not marked ok; Phase 4 cannot start.",
                run_id=run_id,
            )

        try:
            target_info = self._validate_target_repo()
            packet = build_developer_work_packet(
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                issue_key=_issue_key_from_state(state),
                structured_plan=structured_plan,
                target_repo_path=target_info.path,
                approved_plan_refs={
                    "implementation_plan": implementation_plan_path,
                    "implementation_plan_structured": structured_plan_path,
                    "implementation_plan_review": review_path,
                },
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_preparing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chuẩn bị DeveloperWorkPacket: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        packet_path = write_phase4_developer_work_packet(
            store=self.store,
            run_id=run_id,
            packet=packet,
            version=1,
        )
        latest_artifacts = {
            **artifact_paths,
            "developer_work_packet": packet_path,
        }
        approved_artifacts = {
            **_dict_value(state.get("approved_artifacts")),
            "implementation_plan": implementation_plan_path,
            "implementation_plan_structured": structured_plan_path,
            "implementation_plan_review": review_path,
        }
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_preparing",
                "status": "implementation_ready",
                "latest_artifacts": latest_artifacts,
                "approved_artifacts": approved_artifacts,
                "pending_checkpoint": None,
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_READY_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Đã duyệt implementation plan cho run {run_id} và chuẩn bị DeveloperWorkPacket V1. "
                    f"Target repo `{target_info.path}` sạch, packet có {len(packet['proposed_files'])} file mục tiêu. "
                    "Slice này chưa tạo branch, chưa sửa code và chưa mở PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def continue_run(self, *, run_id: str) -> dict[str, object]:
        """Chạy bước Phase 4 tiếp theo theo state hiện tại của run."""

        state = self.store.read_state(run_id)
        status = str(state.get("status"))
        if status == "implementation_ready":
            return self._continue_developer_act(state=state, run_id=run_id)
        if status == "implementation_written":
            return self._continue_verification(state=state, run_id=run_id)
        if status == "implementation_tested":
            return self._continue_code_trace(state=state, run_id=run_id)
        if status == "implementation_traced":
            return self._continue_qa_test(state=state, run_id=run_id)
        if status == "implementation_repair_needed":
            return self._continue_implementation_repair(state=state, run_id=run_id)
        if status == "qa_repair_needed":
            return self._continue_qa_repair(state=state, run_id=run_id)
        raise HarnessError(
            "run_not_continuable",
            f"Run is not ready for the next Phase 4 step: {run_id}",
            run_id=run_id,
        )

    def revalidate_implementation(self, *, run_id: str) -> dict[str, object]:
        """Re-run Verification Runner and Validator for repair-limit blocked runs."""

        state = self.store.read_state(run_id)
        last_error = _dict_value(state.get("last_error"))
        if str(state.get("status")) != "blocked" or last_error.get("error_code") != "implementation_repair_limit_exceeded":
            raise HarnessError(
                "implementation_revalidate_not_allowed",
                (
                    "Implementation revalidate is only allowed when a Phase 4 run is blocked by "
                    f"implementation_repair_limit_exceeded: {run_id}"
                ),
                run_id=run_id,
            )
        return self._revalidate_current_implementation(state=state, run_id=run_id)

    def _continue_developer_act(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.2: Developer.act sinh và apply code vào target repo."""

        try:
            artifact_paths = _string_dict(state.get("latest_artifacts"))
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._validate_target_repo()
            branch = self._checkout_implementation_branch(target_path=target_info.path, work_packet=work_packet)
            result = self.developer.act(work_packet=work_packet)
            applied_files = _apply_developer_changes(
                result=result,
                work_packet=work_packet,
                target_path=target_info.path,
                run_id=run_id,
            )
            report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": branch,
                "summary": result.summary,
                "files": applied_files,
            }
            report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=report,
                version=1,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_writing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Developer.act sinh/apply code: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **artifact_paths,
            "code_change_report": report_path,
        }
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
                    f"Developer.act đã apply {len(applied_files)} file cho run {run_id} trên branch `{branch}`. "
                    "Slice 4.2 chưa chạy test, chưa commit và chưa mở PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _continue_verification(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Chạy Slice 4.3: Verification Runner compile/unit test target repo."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        try:
            _required_phase4_artifact(artifact_paths, "code_change_report", run_id=run_id)
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._validate_target_repo(allow_dirty=True)
            result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
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
        if result.status == "passed":
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "implementation_tested",
                    "latest_artifacts": latest_artifacts,
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
            error_code = "test_skipped" if result.status == "skipped" else "test_failed"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": HarnessError(error_code, result.reason, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Verification Runner status={result.status} cho run {run_id}: {result.reason}. "
                        "Workflow không báo pass khi compile/unit test chưa pass thật."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

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
            target_info = self._validate_target_repo(allow_dirty=True)
            trace_result = self.code_trace_validator.validate(
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
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_testing",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi chạy Validator code trace: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **artifact_paths,
            "requirement_design_code_trace": trace_path,
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
                    "allowed_next_actions": ["get_status", "cancel_run"],
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

    def _continue_qa_test(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Run QA from an implementation that already passed code trace."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        qa_version = _next_qa_artifact_version(self.store, run_id)
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
            work_packet = _read_json_artifact(self._existing_artifact_path(packet_path, run_id=run_id), run_id=run_id)
            target_info = self._validate_target_repo(allow_dirty=True)
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
            artifact_version = _next_phase4_artifact_version(self.store, run_id)
            target_info = self._validate_target_repo(allow_dirty=True)
            work_packet = refresh_developer_work_packet(
                packet=work_packet,
                target_repo_path=target_info.path,
                version=artifact_version,
                run_id=run_id,
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
            result = self.developer.repair(
                work_packet=work_packet,
                repair_feedback=qa_feedback,
                repair_iteration=repair_iteration,
            )
            if result.raw_response:
                raw_response_path = write_phase4_developer_raw_response(
                    store=self.store,
                    run_id=run_id,
                    action="qa_repair",
                    raw_response=result.raw_response,
                    version=artifact_version,
                )
                latest_artifacts = {
                    **latest_artifacts,
                    "developer_repair_raw_response": raw_response_path,
                }
            applied_files = _apply_developer_changes(
                result=result,
                work_packet=work_packet,
                target_path=target_info.path,
                run_id=run_id,
            )
            change_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "summary": result.summary,
                "files": applied_files,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                "developer_raw_response": latest_artifacts.get("developer_repair_raw_response"),
                "repair_source": "qa_acceptance",
            }
            change_report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=change_report,
                version=artifact_version,
            )
            verification_result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
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
        if verification_result.status != "passed":
            error_code = "test_skipped" if verification_result.status == "skipped" else "test_failed"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": repair_iteration,
                    "last_error": HarnessError(error_code, verification_result.reason, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Developer.repair theo QA feedback iteration {repair_iteration} da apply code/test "
                        f"nhung Verification Runner status={verification_result.status}: {verification_result.reason}."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self.code_trace_validator.validate(
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
                "repair_iteration": repair_iteration,
                "repaired_from": feedback_path,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
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
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "implementation_traceability": trace_result.to_quality_report(),
        }
        if not trace_result.ok:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "qa_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "quality_reports": quality_reports,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "qa_repair_iterations": repair_iteration,
                    "last_error": HarnessError(
                        _nonrepairable_code_trace_error_code(trace_result),
                        trace_result.summary,
                        run_id=run_id,
                    ).to_response(),
                    "summary_for_user": (
                        f"Validator code trace khong pass sau QA repair iteration {repair_iteration}: "
                        f"{trace_result.summary} Workflow chua duoc phep chay QA lai."
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

        context = _qa_review_context(
            store=self.store,
            state=state,
            artifact_paths=artifact_paths,
            run_id=str(state.get("run_id") or ""),
            target_repo=str(target_info.path),
            branch=target_info.current_branch,
            work_packet=work_packet,
        )
        if repaired_from:
            context["repaired_from"] = repaired_from
        generated_tests = self.qa_tester.generate_tests(context=context)
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
            run_id=str(state.get("run_id") or ""),
            generated=generated_payload,
            version=qa_version,
        )
        latest_artifacts = {
            **latest_artifacts,
            "qa_generated_tests": generated_path,
        }
        applied_tests = _apply_qa_generated_tests(
            generated=generated_tests,
            target_path=target_info.path,
            run_id=str(state.get("run_id") or ""),
        )
        execution = self.verification_runner.run(
            target_path=target_info.path,
            work_packet=work_packet,
            required_test_files=[str(item.get("path") or "") for item in applied_tests],
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
            run_id=str(state.get("run_id") or ""),
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
            "applied_tests": applied_tests,
            "repaired_from": repaired_from,
        }
        report_path = write_phase4_qa_test_report(
            store=self.store,
            run_id=str(state.get("run_id") or ""),
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
        if isinstance(exc, QaTesterLlmError) and raw_response:
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
            "allowed_next_actions": ["get_status", "cancel_run"],
            "last_error": exc.to_response(),
            "summary_for_user": f"{summary_prefix}: {exc.message}",
        }
        if qa_repair_iterations is not None:
            update["qa_repair_iterations"] = qa_repair_iterations
        state.update(update)
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _revalidate_current_implementation(self, *, state: dict[str, object], run_id: str) -> dict[str, object]:
        """Re-run Verification Runner and Validator without editing code."""

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        latest_artifacts = dict(artifact_paths)
        previous_iterations = _int_value(state.get("implementation_repair_iterations"))
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
            target_info = self._validate_target_repo(allow_dirty=True)
            work_packet = refresh_developer_work_packet(
                packet=work_packet,
                target_repo_path=target_info.path,
                version=artifact_version,
                run_id=run_id,
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
            verification_result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
            test_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "repair_iteration": previous_iterations,
                "revalidated_from": "implementation_repair_limit_exceeded",
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
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bi chan khi revalidate implementation: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        if verification_result.status != "passed":
            error_code = "test_skipped" if verification_result.status == "skipped" else "test_failed"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "implementation_repair_iterations": previous_iterations,
                    "last_error": HarnessError(error_code, verification_result.reason, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Revalidate implementation cho run {run_id} khong pass Verification Runner "
                        f"status={verification_result.status}: {verification_result.reason}."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self.code_trace_validator.validate(
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
                "repair_iteration": previous_iterations,
                "revalidated_from": "implementation_repair_limit_exceeded",
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
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
                    "allowed_next_actions": ["get_status", "cancel_run"],
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
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
            "implementation_traceability": trace_result.to_quality_report(),
        }
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
        elif _is_repairable_code_trace_failure(trace_result) and previous_iterations < self.settings.implementation_repair_limit:
            feedback = _implementation_repair_feedback(
                run_id=run_id,
                state=state,
                work_packet=work_packet,
                trace_result=trace_result,
                trace_path=trace_path,
                target_repo=str(target_info.path),
                branch=target_info.current_branch,
                feedback_version=artifact_version,
                repair_iteration=previous_iterations,
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
                "repair_iterations": previous_iterations,
                "revalidated": True,
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
                    "implementation_repair_iterations": previous_iterations,
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
                    f"iterations={previous_iterations}, limit={self.settings.implementation_repair_limit}. "
                    f"{trace_result.summary}"
                )
                allowed_actions = ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS
            else:
                error_code = _nonrepairable_code_trace_error_code(trace_result)
                message = trace_result.summary
                allowed_actions = ["get_status", "cancel_run"]
            quality_reports["implementation_repair"] = {
                "needed": True,
                "feedback_artifact": latest_artifacts.get("implementation_repair_feedback"),
                "missing_count": len(trace_result.missing_links),
                "repair_iterations": previous_iterations,
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
                    "implementation_repair_iterations": previous_iterations,
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
        try:
            packet_path = _required_phase4_artifact(artifact_paths, "developer_work_packet", run_id=run_id)
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
            repair_iteration = previous_iterations + 1
            if repair_iteration > self.settings.implementation_repair_limit:
                raise HarnessError(
                    "implementation_repair_limit_exceeded",
                    (
                        f"Implementation repair limit exceeded for run {run_id}: "
                        f"limit={self.settings.implementation_repair_limit}."
                    ),
                    run_id=run_id,
                )
            artifact_version = _next_phase4_artifact_version(self.store, run_id)
            target_info = self._validate_target_repo(allow_dirty=True)
            work_packet = refresh_developer_work_packet(
                packet=work_packet,
                target_repo_path=target_info.path,
                version=artifact_version,
                run_id=run_id,
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
            result = self.developer.repair(
                work_packet=work_packet,
                repair_feedback=repair_feedback,
                repair_iteration=repair_iteration,
            )
            if result.raw_response:
                raw_response_path = write_phase4_developer_raw_response(
                    store=self.store,
                    run_id=run_id,
                    action="repair",
                    raw_response=result.raw_response,
                    version=artifact_version,
                )
                latest_artifacts = {
                    **latest_artifacts,
                    "developer_repair_raw_response": raw_response_path,
                }
            applied_files = _apply_developer_changes(
                result=result,
                work_packet=work_packet,
                target_path=target_info.path,
                run_id=run_id,
            )
            change_report = {
                "run_id": run_id,
                "source_ref": state.get("source_ref"),
                "issue_key": work_packet.get("issue_key"),
                "target_repo": str(target_info.path),
                "branch": target_info.current_branch,
                "summary": result.summary,
                "files": applied_files,
                "repair_iteration": repair_iteration,
                "repair_feedback": feedback_path,
                "developer_work_packet": refreshed_packet_path,
                "developer_raw_response": latest_artifacts.get("developer_repair_raw_response"),
            }
            change_report_path = write_phase4_code_change_report(
                store=self.store,
                run_id=run_id,
                report=change_report,
                version=artifact_version,
            )
            verification_result = self.verification_runner.run(target_path=target_info.path, work_packet=work_packet)
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
                        if exc.error_code == "implementation_repair_limit_exceeded"
                        else ["get_status", "cancel_run"]
                    ),
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Developer.repair xử lý feedback: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "code_change_report": change_report_path,
            "test_report": test_report_path,
        }
        if verification_result.status != "passed":
            error_code = "test_skipped" if verification_result.status == "skipped" else "test_failed"
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_repair",
                    "status": "blocked",
                    "latest_artifacts": latest_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": (
                        ALLOWED_IMPLEMENTATION_REPAIR_BLOCKED_ACTIONS
                        if error_code == "implementation_repair_limit_exceeded"
                        else ["get_status", "cancel_run"]
                    ),
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": HarnessError(error_code, verification_result.reason, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Developer.repair iteration {repair_iteration} đã apply code/test nhưng Verification Runner "
                        f"status={verification_result.status}: {verification_result.reason}. Workflow chưa được phép mở PR."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        try:
            trace_result = self.code_trace_validator.validate(
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
                "repair_iteration": repair_iteration,
                "repaired_from": feedback_path,
            }
            trace_path = write_phase4_code_trace_report(
                store=self.store,
                run_id=run_id,
                report=trace_payload,
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
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 4 bị chặn khi Validator trace lại sau Developer.repair: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        latest_artifacts = {
            **latest_artifacts,
            "requirement_design_code_trace": trace_path,
        }
        quality_reports = {
            **_dict_value(state.get("quality_reports")),
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
                    "allowed_next_actions": ["get_status", "cancel_run"],
                    "implementation_repair_iterations": repair_iteration,
                    "last_error": HarnessError(error_code, message, run_id=run_id).to_response(),
                    "summary_for_user": (
                        f"Validator code trace vẫn chưa pass sau Developer.repair iteration {repair_iteration}: "
                        f"{message} Workflow chưa được phép mở PR."
                    ),
                }
            )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _require_implementation_plan_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        if str(state.get("status")) != "waiting_implementation_plan_review":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for implementation plan review: {run_id}",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "implementation_plan_review":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not an implementation plan checkpoint: {run_id}",
                run_id=run_id,
            )
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError(
                "checkpoint_not_current",
                f"Checkpoint is not current: {checkpoint_id}",
                run_id=run_id,
            )

    def _existing_artifact_path(self, relative_path: str, *, run_id: str) -> Path:
        path = (self.store.repo_root / relative_path).resolve()
        if not path.exists():
            raise HarnessError(
                "approved_implementation_plan_not_found",
                f"Approved Phase 4 input artifact does not exist: {relative_path}",
                run_id=run_id,
            )
        return path

    def _validate_target_repo(self, *, allow_dirty: bool = False) -> "_TargetRepoInfo":
        target_path = self.settings.target_repo_path.resolve()
        repo_root = self.settings.repo_root.resolve()
        if target_path == repo_root:
            raise HarnessError("target_repo_is_harness", "Target repo points to SDLC_Harness itself.")
        if not target_path.exists() or not target_path.is_dir():
            raise HarnessError("target_repo_not_found", f"Target repo does not exist: {target_path}")
        if not (target_path / ".git").exists():
            raise HarnessError("target_repo_not_git", f"Target repo is not a git repository: {target_path}")
        self.repo_context_provider.require_index(target_path)

        remote = _git_output(target_path, "remote", "get-url", "origin")
        if _normalize_remote(remote) != _normalize_remote(self.settings.target_repo_remote):
            raise HarnessError(
                "target_repo_remote_mismatch",
                f"Target repo remote mismatch: expected {self.settings.target_repo_remote}, got {remote}",
            )

        dirty = _git_output(target_path, "status", "--porcelain")
        dirty_without_codegraph = _dirty_status_without_codegraph(dirty)
        if dirty_without_codegraph.strip() and not allow_dirty:
            raise HarnessError("target_repo_dirty", "Target repo has uncommitted changes outside .codegraph/.")
        return _TargetRepoInfo(
            path=target_path,
            remote=remote,
            current_branch=_git_output(target_path, "rev-parse", "--abbrev-ref", "HEAD"),
            source_files=_target_source_files(target_path),
        )

    def _checkout_implementation_branch(self, *, target_path: Path, work_packet: dict[str, object]) -> str:
        repository = _dict_value(work_packet.get("target_repository"))
        branch = str(repository.get("planned_branch") or "").strip()
        if not branch:
            raise HarnessError("developer_act_branch_missing", "DeveloperWorkPacket has no planned branch.")
        current_branch = _git_output(target_path, "rev-parse", "--abbrev-ref", "HEAD")
        if current_branch == branch:
            return branch
        if _branch_exists(target_path, branch):
            _git_output(target_path, "checkout", branch)
            return branch
        base_branch = str(repository.get("base_branch") or self.settings.target_base_branch or "main").strip()
        if current_branch != base_branch:
            _git_output(target_path, "checkout", base_branch)
        _git_output(target_path, "checkout", "-b", branch)
        return branch

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        return {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": state.get("latest_artifacts", {}),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": state.get("allowed_next_actions", []),
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "implementation_repair_iterations": state.get("implementation_repair_iterations", 0),
            "qa_repair_iterations": state.get("qa_repair_iterations", 0),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
        }


class _TargetRepoInfo:
    def __init__(self, *, path: Path, remote: str, current_branch: str, source_files: list[str]):
        self.path = path
        self.remote = remote
        self.current_branch = current_branch
        self.source_files = source_files


def _qa_review_context(
    *,
    store: RuntimeStore,
    state: dict[str, object],
    artifact_paths: dict[str, str],
    run_id: str,
    target_repo: str,
    branch: str,
    work_packet: dict[str, object],
) -> dict[str, object]:
    """Đóng gói context QA ngắn, có metadata truncation để tránh prompt quá dài."""

    requirement_path = _required_phase4_artifact(artifact_paths, "requirement_packet", run_id=run_id)
    verification_path = _required_phase4_artifact(artifact_paths, "test_report", run_id=run_id)
    trace_path = _required_phase4_artifact(artifact_paths, "requirement_design_code_trace", run_id=run_id)
    design_path = _required_phase4_artifact(artifact_paths, "design_artifact", run_id=run_id)
    implementation_plan_path = _required_phase4_artifact(artifact_paths, "implementation_plan", run_id=run_id)
    requirement = _bounded_artifact_section(
        store=store,
        relative_path=requirement_path,
        label="Accepted requirement/use-case packet",
        limit=MAX_QA_REQUIREMENT_CHARS,
    )
    verification = _bounded_artifact_section(
        store=store,
        relative_path=verification_path,
        label="Verification Runner report",
        limit=MAX_QA_VERIFICATION_CHARS,
    )
    trace_report = _read_json_artifact(store.repo_root / trace_path, run_id=run_id)
    code_under_test = _qa_code_under_test(
        target_repo=Path(target_repo),
        work_packet=work_packet,
        run_id=run_id,
    )
    traceability = _qa_traceability_summary(trace_report=trace_report, trace_path=trace_path)
    chars_sent = (
        int(requirement.get("chars_sent") or 0)
        + int(verification.get("chars_sent") or 0)
        + int(code_under_test.get("chars_sent") or 0)
    )
    chars_total = (
        int(requirement.get("chars_total") or 0)
        + int(verification.get("chars_total") or 0)
        + int(code_under_test.get("chars_total") or 0)
    )
    return {
        "task": "qa_acceptance_review_v1",
        "packet_mode": "trace_dev_style_compact_qa_packet_v1",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "target_repo": target_repo,
        "branch": branch,
        "work_packet_summary": {
            "implementation_steps": work_packet.get("implementation_steps"),
            "proposed_files": work_packet.get("proposed_files"),
            "test_intents": work_packet.get("test_intents"),
        },
        "accepted_requirement": requirement,
        "code_under_test": code_under_test,
        "verification": verification,
        "traceability": traceability,
        "artifact_references": {
            "design_artifact": design_path,
            "implementation_plan": implementation_plan_path,
            "requirement_design_code_trace": trace_path,
        },
        "context_budget": {
            "packet_mode": "trace_dev_style_compact_qa_packet_v1",
            "section_count": 3,
            "code_file_count": len(_dict_items(code_under_test.get("files"))),
            "chars_sent": chars_sent,
            "chars_total": chars_total,
            "any_truncated": (
                bool(requirement.get("content_truncated"))
                or bool(verification.get("content_truncated"))
                or bool(code_under_test.get("any_truncated"))
            ),
            "requirement_limit_chars": MAX_QA_REQUIREMENT_CHARS,
            "verification_limit_chars": MAX_QA_VERIFICATION_CHARS,
            "code_file_limit_chars": MAX_QA_CODE_FILE_CHARS,
            "code_total_limit_chars": MAX_QA_CODE_TOTAL_CHARS,
            "omitted_full_artifacts": ["design_artifact", "implementation_plan", "requirement_design_code_trace"],
        },
        "guardrails": [
            "QA/Tester generates acceptance/system tests from current code and test evidence.",
            "Full design, implementation plan, and trace report are references only; use summaries unless a future slice fetches them.",
            "QA/Tester must not request requirement/design edits in this slice.",
            "QA-generated test fail/skipped blocks the run; Developer QA repair loop is a later slice.",
        ],
    }


def _bounded_artifact_section(
    *,
    store: RuntimeStore,
    relative_path: str,
    label: str,
    limit: int,
) -> dict[str, object]:
    path = (store.repo_root / relative_path).resolve()
    if not path.exists():
        raise HarnessError(
            "approved_implementation_plan_not_found",
            f"QA input artifact does not exist: {relative_path}",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    excerpt = text[:limit]
    return {
        "label": label,
        "path": relative_path,
        "content": excerpt,
        "content_truncated": len(excerpt) < len(text),
        "chars_total": len(text),
        "chars_sent": len(excerpt),
    }


def _qa_code_under_test(
    *,
    target_repo: Path,
    work_packet: dict[str, object],
    run_id: str,
) -> dict[str, object]:
    """Lay source/test code hien tai tu target repo, khong dung snapshot cu trong packet."""

    files: list[dict[str, object]] = []
    chars_sent = 0
    chars_total = 0
    for rel_path in _qa_code_paths(work_packet=work_packet, run_id=run_id):
        target_file = (target_repo / rel_path).resolve()
        try:
            target_file.relative_to(target_repo.resolve())
        except ValueError as exc:
            raise HarnessError(
                "developer_act_unsafe_path",
                f"QA code path escapes target repo: {rel_path}",
                run_id=run_id,
            ) from exc
        if not target_file.exists():
            files.append(
                {
                    "path": rel_path,
                    "role": _qa_code_role(rel_path),
                    "exists": False,
                    "content": "",
                    "content_truncated": False,
                    "chars_total": 0,
                    "chars_sent": 0,
                }
            )
            continue
        text = target_file.read_text(encoding="utf-8", errors="replace")
        chars_total += len(text)
        remaining_total = max(MAX_QA_CODE_TOTAL_CHARS - chars_sent, 0)
        excerpt_limit = min(MAX_QA_CODE_FILE_CHARS, remaining_total)
        excerpt = text[:excerpt_limit]
        chars_sent += len(excerpt)
        files.append(
            {
                "path": rel_path,
                "role": _qa_code_role(rel_path),
                "exists": True,
                "content": excerpt,
                "content_truncated": len(excerpt) < len(text),
                "chars_total": len(text),
                "chars_sent": len(excerpt),
            }
        )
    return {
        "source": "target_repo_allowed_paths",
        "files": files,
        "file_count": len(files),
        "chars_sent": chars_sent,
        "chars_total": chars_total,
        "any_truncated": any(bool(item.get("content_truncated")) for item in files),
    }


def _qa_code_paths(*, work_packet: dict[str, object], run_id: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for rel_path in _allowed_paths(work_packet):
        if rel_path not in seen:
            paths.append(rel_path)
            seen.add(rel_path)
    if paths:
        return paths
    for item in _dict_items(work_packet.get("proposed_files")) + _dict_items(work_packet.get("current_files")):
        rel_path = _normalize_code_path(str(item.get("path") or ""), run_id=run_id)
        if rel_path not in seen:
            paths.append(rel_path)
            seen.add(rel_path)
    return paths


def _qa_code_role(rel_path: str) -> str:
    return "test" if rel_path.startswith("src/test/java/") else "production"


def _qa_traceability_summary(*, trace_report: dict[str, object], trace_path: str) -> dict[str, object]:
    edges = _dict_items(trace_report.get("graph_edges"))
    missing_links = _dict_items(trace_report.get("missing_links"))
    risks = _string_items(trace_report.get("risks"))
    llm_review = _dict_value(trace_report.get("llm_review"))
    review_context = _dict_value(llm_review.get("review_context"))
    return {
        "path": trace_path,
        "ok": bool(trace_report.get("ok")),
        "confidence": trace_report.get("confidence"),
        "summary": trace_report.get("summary"),
        "graph_edge_count": len(edges),
        "sample_graph_edges": edges[:MAX_QA_TRACE_SAMPLE_EDGES],
        "missing_link_count": len(missing_links),
        "sample_missing_links": missing_links[:MAX_QA_TRACE_SAMPLE_EDGES],
        "risks": risks[:MAX_QA_TRACE_SAMPLE_EDGES],
        "review_context": {
            "code_snippet_count": review_context.get("code_snippet_count"),
            "code_snippets_total_chars": review_context.get("code_snippets_total_chars"),
            "code_snippets_truncated": review_context.get("code_snippets_truncated"),
        },
    }


def _apply_qa_generated_tests(
    *,
    generated: QaGeneratedTestSuite,
    target_path: Path,
    run_id: str,
) -> list[dict[str, object]]:
    """Apply only QA-generated test files under src/test/java/."""

    applied: list[dict[str, object]] = []
    target_root = target_path.resolve()
    for item in generated.test_files:
        rel_path = _normalize_qa_test_path(str(item.get("path") or ""), run_id=run_id)
        target_file = (target_root / rel_path).resolve()
        try:
            target_file.relative_to(target_root)
        except ValueError as exc:
            raise HarnessError(
                "qa_test_unsafe_path",
                f"QA-generated test path escapes target repo: {rel_path}",
                run_id=run_id,
            ) from exc
        content = str(item.get("content") or "").strip()
        if not content:
            raise HarnessError(
                "qa_test_empty_content",
                f"QA-generated test file has empty content: {rel_path}",
                run_id=run_id,
            )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content.rstrip() + "\n", encoding="utf-8")
        applied.append(
            {
                "path": rel_path,
                "operation": "create_or_update",
                "scenarios": _string_items(item.get("scenarios")),
            }
        )
    if not applied:
        raise HarnessError("qa_test_files_missing", "QA did not generate any test file.", run_id=run_id)
    return applied


def _normalize_qa_test_path(raw_path: str, *, run_id: str) -> str:
    rel_path = _normalize_code_path(raw_path, run_id=run_id)
    if not rel_path.startswith("src/test/java/") or not rel_path.endswith(".java"):
        raise HarnessError(
            "qa_test_out_of_scope",
            f"QA-generated tests can only write Java tests under src/test/java/: {raw_path}",
            run_id=run_id,
        )
    return rel_path


def _qa_result_from_generated_execution(
    *,
    generated: QaGeneratedTestSuite,
    execution: object,
    execution_path: str,
    generated_path: str,
    applied_tests: list[dict[str, object]],
) -> QaTestResult:
    status = _qa_status_from_verification(str(getattr(execution, "status", "")))
    reason = str(getattr(execution, "reason", "") or "")
    scenarios: list[dict[str, object]] = []
    for item in generated.scenarios:
        scenarios.append(
            {
                "name": str(item.get("name") or ""),
                "result": status,
                "evidence": reason or str(item.get("expected_result") or ""),
                "risk": str(item.get("risk") or ""),
            }
        )
    if not scenarios:
        scenarios = [
            {
                "name": "QA-generated acceptance tests",
                "result": status,
                "evidence": reason,
                "risk": "",
            }
        ]
    if status == "pass":
        summary = f"QA-generated acceptance tests passed: {reason}"
    elif status == "skipped":
        summary = f"QA-generated acceptance tests were skipped: {reason}"
    else:
        summary = f"QA-generated acceptance tests failed: {reason}"
    evidence = [
        f"Generated tests artifact: {generated_path}",
        f"QA execution artifact: {execution_path}",
        f"Applied QA test files: {len(applied_tests)}",
        f"Verification Runner status: {getattr(execution, 'status', '')}",
    ]
    risks = list(generated.risks)
    if status != "pass":
        risks.append("QA-generated acceptance test execution did not pass.")
    review_context = {
        **(generated.generation_context or {}),
        "generated_test_file_count": len(generated.test_files),
        "applied_test_file_count": len(applied_tests),
        "execution_status": getattr(execution, "status", ""),
    }
    return QaTestResult(
        status=status,
        summary=summary,
        scenarios=scenarios,
        evidence=evidence,
        risks=risks,
        raw_response=generated.raw_response,
        review_context=review_context,
    )


def _qa_status_from_verification(status: str) -> str:
    if status == "passed":
        return "pass"
    if status == "skipped":
        return "skipped"
    return "fail"

def _qa_repair_feedback(
    *,
    run_id: str,
    state: dict[str, object],
    work_packet: dict[str, object],
    qa_result: QaTestResult,
    qa_run: dict[str, object],
    target_repo: str,
    branch: str,
    feedback_version: int,
    repair_iteration: int,
) -> dict[str, object]:
    """Build machine-readable QA feedback for Developer.repair."""

    output_contract = _dict_value(work_packet.get("output_contract"))
    failing_scenarios = [
        item for item in qa_result.scenarios if str(item.get("result") or "").strip().lower() != "pass"
    ]
    if not failing_scenarios:
        failing_scenarios = list(qa_result.scenarios)
    return {
        "version": f"qa_repair_feedback_v{feedback_version}",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "status": "qa_repair_needed",
        "repair_iteration": repair_iteration,
        "repair_scope": "code_or_test",
        "severity": "repair_required",
        "summary": qa_result.summary,
        "required_action": (
            "Developer must repair code/test behavior within allowed_paths so the QA-generated "
            "acceptance tests pass. Requirement, approved design, implementation plan, branch, "
            "and tool-sync records must not be edited in this slice."
        ),
        "target_repo": target_repo,
        "branch": branch,
        "qa_artifacts": {
            "qa_generated_tests": qa_run.get("generated_path"),
            "qa_test_execution": qa_run.get("execution_path"),
            "qa_test_report": qa_run.get("report_path"),
        },
        "failing_scenarios": failing_scenarios,
        "evidence": qa_result.evidence,
        "risks": qa_result.risks,
        "applied_tests": qa_run.get("applied_tests", []),
        "developer_constraints": {
            "allowed_paths": output_contract.get("allowed_paths", []),
            "changed_files_only": output_contract.get("changed_files_only"),
            "format": output_contract.get("format"),
        },
    }

def _required_phase4_artifact(artifact_paths: dict[str, str], key: str, *, run_id: str) -> str:
    value = artifact_paths.get(key, "")
    if not value:
        raise HarnessError(
            "approved_implementation_plan_not_found",
            f"Run does not have an approved Phase 4 input artifact `{key}`: {run_id}",
            run_id=run_id,
        )
    return value


def _branch_exists(target_path: Path, branch: str) -> bool:
    try:
        _git_output(target_path, "rev-parse", "--verify", branch)
    except HarnessError:
        return False
    return True


def _apply_developer_changes(
    *,
    result: DeveloperActResult,
    work_packet: dict[str, object],
    target_path: Path,
    run_id: str,
) -> list[dict[str, object]]:
    allowed_paths = set(_allowed_paths(work_packet))
    if not allowed_paths:
        raise HarnessError("developer_act_allowed_paths_missing", "DeveloperWorkPacket has no allowed paths.")
    applied: list[dict[str, object]] = []
    for item in result.files:
        rel_path = _normalize_code_path(str(item.get("path") or ""), run_id=run_id)
        if rel_path not in allowed_paths:
            raise HarnessError(
                "developer_act_out_of_scope",
                f"Developer.act tried to modify a file outside allowed_paths: {rel_path}",
                run_id=run_id,
            )
        target_file = (target_path / rel_path).resolve()
        try:
            target_file.relative_to(target_path.resolve())
        except ValueError as exc:
            raise HarnessError(
                "developer_act_unsafe_path",
                f"Developer.act path escapes target repo: {rel_path}",
                run_id=run_id,
            ) from exc
        content = str(item.get("content") or "")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content.rstrip() + "\n", encoding="utf-8")
        applied.append(
            {
                "path": rel_path,
                "operation": str(item.get("operation") or ""),
                "reason": str(item.get("reason") or ""),
                "covered_design_elements": _string_items(item.get("covered_design_elements")),
            }
        )
    if not applied:
        raise HarnessError("developer_act_failed", "Developer.act did not apply any file.")
    return applied


def _allowed_paths(work_packet: dict[str, object]) -> list[str]:
    output_contract = _dict_value(work_packet.get("output_contract"))
    values = output_contract.get("allowed_paths")
    if not isinstance(values, list):
        return []
    return [_normalize_code_path(str(value), run_id="") for value in values if str(value).strip()]


def _normalize_code_path(raw_path: str, *, run_id: str) -> str:
    path = raw_path.strip().replace("\\", "/").lstrip("/")
    if (
        not path
        or path.startswith("../")
        or "/../" in path
        or path == ".."
        or not (path.startswith("src/main/java/") or path.startswith("src/test/java/"))
    ):
        raise HarnessError(
            "developer_act_unsafe_path",
            f"Unsafe or unsupported Developer.act path: {raw_path}",
            run_id=run_id or None,
        )
    return path


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _next_phase4_artifact_version(store: RuntimeStore, run_id: str) -> int:
    """Chon version Phase 4 ke tiep ma khong ghi de artifact cu."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    prefixes = (
        "developer_work_packet_",
        "code_change_report_",
        "test_report_",
        "requirement_design_code_trace_",
        "implementation_repair_feedback_",
        "developer_repair_raw_response_",
    )
    max_version = 0
    if not artifact_dir.exists():
        return 1
    for path in artifact_dir.iterdir():
        name = path.name
        for prefix in prefixes:
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix) :]
            if not tail.startswith("v"):
                continue
            digits = []
            for char in tail[1:]:
                if not char.isdigit():
                    break
                digits.append(char)
            if digits:
                max_version = max(max_version, int("".join(digits)))
    return max_version + 1


def _next_qa_artifact_version(store: RuntimeStore, run_id: str) -> int:
    """Chọn version QA report riêng để lần đầu luôn là v1."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    prefixes = (
        "qa_generated_tests_",
        "qa_test_execution_",
        "qa_test_report_",
        "qa_tester_raw_response_",
        "qa_repair_feedback_",
    )
    max_version = 0
    if not artifact_dir.exists():
        return 1
    for path in artifact_dir.iterdir():
        name = path.name
        for prefix in prefixes:
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix) :]
            if not tail.startswith("v"):
                continue
            digits = []
            for char in tail[1:]:
                if not char.isdigit():
                    break
                digits.append(char)
            if digits:
                max_version = max(max_version, int("".join(digits)))
    return max_version + 1


def _is_repairable_code_trace_failure(trace_result: object) -> bool:
    """Chỉ chuyển sang repair-needed khi thiếu link nằm ở code/test, không phải input graph."""

    missing_links = getattr(trace_result, "missing_links", [])
    if not isinstance(missing_links, list) or not missing_links:
        return False
    repairable_types = {"missing_source_file", "missing_test_file", "llm_semantic_gap"}
    for item in missing_links:
        if not isinstance(item, dict):
            return False
        link_type = str(item.get("type") or "").strip()
        if link_type not in repairable_types:
            return False
    return True


def _nonrepairable_code_trace_error_code(trace_result: object) -> str:
    missing_links = getattr(trace_result, "missing_links", [])
    if not isinstance(missing_links, list):
        return "implementation_traceability_failed"
    for item in missing_links:
        if not isinstance(item, dict):
            continue
        link_type = str(item.get("type") or "").strip()
        if link_type in {"trace_mapping_issue", "evidence_incomplete"}:
            return "validator_evidence_or_mapping_issue"
    return "implementation_traceability_failed"


def _implementation_repair_feedback(
    *,
    run_id: str,
    state: dict[str, object],
    work_packet: dict[str, object],
    trace_result: object,
    trace_path: str,
    target_repo: str,
    branch: str,
    feedback_version: int,
    repair_iteration: int,
) -> dict[str, object]:
    """Đóng gói feedback Validator theo schema ổn định cho Developer repair slice sau."""

    llm_review = getattr(trace_result, "llm_review", {})
    if not isinstance(llm_review, dict):
        llm_review = {}
    missing_links = getattr(trace_result, "missing_links", [])
    if not isinstance(missing_links, list):
        missing_links = []
    output_contract = _dict_value(work_packet.get("output_contract"))
    return {
        "version": f"implementation_repair_feedback_v{feedback_version}",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "status": "implementation_repair_needed",
        "repair_iteration": repair_iteration,
        "repair_scope": "code_or_test",
        "severity": "repair_required",
        "summary": getattr(trace_result, "summary", ""),
        "required_action": (
            "Developer must repair code/test within allowed_paths, then rerun Verification Runner "
            "and Validator code trace. Requirement and approved design must not be edited in this slice."
        ),
        "target_repo": target_repo,
        "branch": branch,
        "trace_artifact": trace_path,
        "missing_links": missing_links,
        "llm_review": {
            "ok": llm_review.get("ok"),
            "confidence": llm_review.get("confidence"),
            "summary": llm_review.get("summary"),
            "risks": llm_review.get("risks", []),
        },
        "developer_constraints": {
            "allowed_paths": output_contract.get("allowed_paths", []),
            "changed_files_only": output_contract.get("changed_files_only"),
            "format": output_contract.get("format"),
        },
    }




