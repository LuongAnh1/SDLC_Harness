"""Workflow Phase 1: use case -> formal use case -> requirement checkpoint.

Phase này học trực tiếp từ TraceDev ở bước Requirement Refiner, nhưng thêm state,
artifact, checkpoint và mock sync để Niko có thể kiểm soát từng bước.
"""

from __future__ import annotations

from ..artifacts.writers import (
    write_formal_check_report,
    write_formal_use_case,
    write_requirement_packet,
    write_trace_graph,
    write_use_case_packet,
)
from ..artifacts import (
    read_requirement_model_v2,
    read_source_snapshot_v2,
    write_requirement_v2_artifacts,
    write_requirement_v2_revision_artifacts,
    write_source_snapshot_v2,
)
from ..core.config import Settings
from ..core.errors import HarnessError
from ..sources.fixtures import UseCaseSource, load_use_case
from ..sources.jira import load_jira_issue_source
from ..llm.client import LlmClient
from ..agents.requirements import (
    RequirementClarificationUpdater,
    RequirementModelV2Refiner,
    RequirementRefiner,
    RequirementValidator,
    find_clarification_submission,
    select_current_requirement_question,
)
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint, sync_implementation_plan_checkpoint, sync_requirement_checkpoint

ALLOWED_REQUIREMENT_ACTIONS = [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status",
]

ALLOWED_CODE_REVIEW_READY_ACTIONS = ["approve_checkpoint", "get_status", "sync_run", "cancel_run"]
ALLOWED_REQUIREMENT_CLARIFICATION_ACTIONS = [
    "answer_clarification",
    "get_status",
    "sync_run",
    "cancel_run",
]


class PhaseOneWorkflow:
    """Điều phối Phase 1 từ fixture/Jira tới checkpoint requirement.

    Workflow này sở hữu source loading, refiner, validator, artifact runtime và
    sync Jira/Confluence. Niko chỉ gọi CLI JSON và đọc response đã tóm tắt.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.llm_client = LlmClient(settings)
        self.refiner = RequirementRefiner(self.llm_client)
        self.refiner_v2 = RequirementModelV2Refiner(self.llm_client)
        self.clarification_updater = RequirementClarificationUpdater(self.llm_client)
        self.validator = RequirementValidator()

    def start_or_get_run(self, *, dataset: str, use_case: str, requested_by: str) -> dict[str, object]:
        """Tạo run mới hoặc trả active run hiện tại cho cùng source_ref."""

        source = load_use_case(dataset=dataset, use_case=use_case, repo_root=self.settings.repo_root)
        return self._start_from_source(source=source, requested_by=requested_by)

    def start_from_jira(self, *, issue_key: str, requested_by: str) -> dict[str, object]:
        """Tạo run từ Jira issue hoặc trả active run hiện tại cho cùng issue."""

        source = load_jira_issue_source(settings=self.settings, issue_key=issue_key)
        return self._start_from_source(source=source, requested_by=requested_by)

    def _start_from_source(self, *, source: UseCaseSource, requested_by: str) -> dict[str, object]:
        """Chạy Phase 1 từ source đã được load, bất kể source là fixture hay Jira."""

        active_state = self.store.find_active_run(source.source_ref)
        if active_state:
            return self._status_response(active_state, created_new_run=False)

        run_id = self.store.new_run_id()
        now = utc_now_iso()
        state: dict[str, object] = {
            "ok": True,
            "run_id": run_id,
            "source_ref": source.source_ref,
            "dataset": source.dataset,
            "use_case": source.use_case,
            "requested_by": requested_by,
            "requirement_contract_version": self.settings.requirement_contract_version,
            "created_at": now,
            "updated_at": now,
            "current_phase": "requirement_drafting",
            "status": "requirement_drafting",
            "latest_artifacts": {},
            "pending_checkpoint": None,
            "allowed_next_actions": ["get_status", "cancel_run"],
            "last_sync": None,
            "last_error": None,
            "llm": {
                "mode": self.settings.llm_mode,
                "model": self.settings.llm_model,
            },
            "summary_for_user": "Đang chuẩn hóa use case thành formal use case và requirement packet.",
        }
        self.store.write_state(state)

        if self.settings.requirement_contract_version == "v2":
            return self._start_v2(source=source, state=state)

        try:
            use_case_packet_path = write_use_case_packet(store=self.store, run_id=run_id, source=source)
            formal_refinement = self.refiner.refine_formal_use_case(source)
            formal_use_case_path = write_formal_use_case(
                store=self.store,
                run_id=run_id,
                formal_use_case=formal_refinement.final_use_case,
            )
            formal_check_report_path = write_formal_check_report(
                store=self.store,
                run_id=run_id,
                refinement_result=formal_refinement,
            )
            requirement_refinement = self.refiner.refine_requirement_packet(
                source,
                formal_use_case=formal_refinement.final_use_case,
            )
            requirement_text = requirement_refinement.final_packet
            validation_report = requirement_refinement.final_report
            requirement_packet_path = write_requirement_packet(
                store=self.store,
                run_id=run_id,
                requirement_packet=requirement_text,
                validation_report=validation_report,
            )
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "status": "failed",
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 1 lỗi khi chuẩn hóa requirement: {exc.message}",
                }
            )
            self.store.write_state(state)
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        artifact_paths = {
            "use_case_packet": use_case_packet_path,
            "formal_use_case": formal_use_case_path,
            "formal_check_report": formal_check_report_path,
            "requirement_packet": requirement_packet_path,
        }
        trace_graph_path = write_trace_graph(
            store=self.store,
            run_id=run_id,
            source=source,
            artifact_paths={**artifact_paths, "trace_graph": "pending"},
            validation_report=validation_report,
            formal_refinement=formal_refinement,
            llm_mode=self.settings.llm_mode,
            llm_model=self.settings.llm_model,
        )
        artifact_paths["trace_graph"] = trace_graph_path
        last_sync = sync_requirement_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=source.source_ref,
            artifact_paths=artifact_paths,
        )

        checkpoint_id = f"checkpoint_requirement_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "waiting_requirement_approval",
                "status": "waiting_requirement_approval",
                "latest_artifacts": artifact_paths,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "requirement_approval",
                    "artifact_ref": "requirement_packet_v1",
                    "artifact_path": requirement_packet_path,
                },
                "allowed_next_actions": ALLOWED_REQUIREMENT_ACTIONS,
                "last_sync": last_sync,
                "quality_reports": {
                    "formal_use_case": formal_refinement.final_report.to_dict(),
                    "requirement_packet": requirement_refinement.to_report_dict(),
                },
                "summary_for_user": (
                    f"Đã chuẩn hóa {source.dataset} {source.use_case} thành formal use case và requirement packet v1 "
                    f"bằng Requirement Refiner, validator đã pass, và sync status={last_sync.get('status')}. "
                    "Run đang chờ anh duyệt requirement trước khi chuyển sang design."
                ),
            }
        )
        self.store.write_state(state)
        self.store.register_active_run(source.source_ref, run_id)
        return self._status_response(state, created_new_run=True)

    def _start_v2(self, *, source: UseCaseSource, state: dict[str, object]) -> dict[str, object]:
        """Create the canonical V2 model and stop at clarification or approval."""

        run_id = str(state["run_id"])
        try:
            use_case_packet_path = write_use_case_packet(store=self.store, run_id=run_id, source=source)
            source_snapshot_path = write_source_snapshot_v2(store=self.store, run_id=run_id, source=source)
            refinement = self.refiner_v2.refine(
                source,
                run_id=run_id,
                source_artifact_ref=source_snapshot_path,
            )
            artifact_paths = {
                "use_case_packet": use_case_packet_path,
                **write_requirement_v2_artifacts(
                    store=self.store,
                    run_id=run_id,
                    refinement_result=refinement,
                ),
            }
            selection = select_current_requirement_question(refinement.model)
            self._apply_v2_checkpoint_state(
                state=state,
                model=refinement.model,
                artifact_paths=artifact_paths,
                current_question=selection.current_question.to_dict() if selection.current_question else None,
                remaining_blocking_count=selection.remaining_blocking_count,
                quality_report=refinement.to_report_dict(),
                latest_decision=None,
            )
            self.store.write_state(state)
            last_sync = self._sync_requirement_state(state)
            state["last_sync"] = last_sync
            state["updated_at"] = utc_now_iso()
            state["summary_for_user"] = self._v2_summary(state, last_sync)
            self.store.write_state(state)
            self.store.register_active_run(source.source_ref, run_id)
            return self._status_response(state, created_new_run=True)
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "status": "failed",
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 1 V2 lỗi khi chuẩn hóa requirement: {exc.message}",
                }
            )
            self.store.write_state(state)
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc

    def answer_clarification(
        self,
        *,
        run_id: str,
        question_id: str,
        answer: str,
        answered_by: str,
    ) -> dict[str, object]:
        """Apply one answer to the current blocking Requirement V2 question."""

        with self.store.run_lock(run_id):
            state = self.store.read_state(run_id)
            if str(state.get("requirement_contract_version") or "v1") != "v2":
                raise HarnessError(
                    "requirement_clarification_not_supported",
                    "Clarification answers require Requirement Contract V2.",
                    run_id=run_id,
                )
            artifacts = self._normalized_artifacts(state)
            model_ref = artifacts.get("requirement_model")
            if not model_ref:
                raise HarnessError("requirement_model_v2_missing", "Canonical Requirement V2 artifact is missing.", run_id=run_id)
            model = read_requirement_model_v2(store=self.store, run_id=run_id, artifact_ref=model_ref)
            duplicate = find_clarification_submission(model, question_id, answer)
            if duplicate is not None:
                return self._status_response(state, created_new_run=False)
            if state.get("status") != "waiting_requirement_clarification":
                raise HarnessError(
                    "requirement_clarification_not_ready",
                    f"Run is not waiting for requirement clarification: {run_id}",
                    run_id=run_id,
                )
            checkpoint = state.get("pending_checkpoint")
            current_question = checkpoint.get("current_question") if isinstance(checkpoint, dict) else None
            if (
                not isinstance(current_question, dict)
                or current_question.get("id") != question_id
            ):
                raise HarnessError(
                    "requirement_question_not_current",
                    f"Question is not current: {question_id}",
                    run_id=run_id,
                )
            source_meta = model.source
            source_text = read_source_snapshot_v2(
                store=self.store,
                run_id=run_id,
                artifact_ref=str(source_meta.get("artifact_ref") or ""),
                expected_source_ref=str(source_meta.get("source_ref") or ""),
                expected_fingerprint=str(source_meta.get("fingerprint") or ""),
            )
            result = self.clarification_updater.apply(
                source_text=source_text,
                model=model,
                question_id=question_id,
                answer=answer,
                answered_by=answered_by,
            )
            revised_artifacts = write_requirement_v2_revision_artifacts(
                store=self.store,
                run_id=run_id,
                model=result.model,
                validation_report=result.final_report,
                audit=result.to_report_dict(),
            )
            artifacts.update(revised_artifacts)
            selection = select_current_requirement_question(result.model)
            self._apply_v2_checkpoint_state(
                state=state,
                model=result.model,
                artifact_paths=artifacts,
                current_question=selection.current_question.to_dict() if selection.current_question else None,
                remaining_blocking_count=selection.remaining_blocking_count,
                quality_report=result.to_report_dict(),
                latest_decision=result.decision,
            )
            self.store.write_state(state)
            last_sync = self._sync_requirement_state(state)
            state["last_sync"] = last_sync
            state["updated_at"] = utc_now_iso()
            state["summary_for_user"] = self._v2_summary(state, last_sync)
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

    def _apply_v2_checkpoint_state(
        self,
        *,
        state: dict[str, object],
        model,
        artifact_paths: dict[str, str],
        current_question: dict[str, object] | None,
        remaining_blocking_count: int,
        quality_report: dict[str, object],
        latest_decision: dict[str, object] | None,
    ) -> None:
        run_id = str(state["run_id"])
        if current_question is not None:
            status = "waiting_requirement_clarification"
            current_phase = "requirement_clarification"
            checkpoint = {
                "checkpoint_id": f"checkpoint_requirement_clarification_{run_id}",
                "type": "requirement_clarification",
                "artifact_ref": f"requirement_model_v2_r{model.revision}",
                "artifact_path": artifact_paths["requirement_model"],
                "current_question": current_question,
                "remaining_blocking_count": remaining_blocking_count,
            }
            allowed = ALLOWED_REQUIREMENT_CLARIFICATION_ACTIONS
        else:
            status = "waiting_requirement_approval"
            current_phase = status
            checkpoint = {
                "checkpoint_id": f"checkpoint_requirement_{run_id}",
                "type": "requirement_approval",
                "artifact_ref": f"requirement_model_v2_r{model.revision}",
                "artifact_path": artifact_paths["requirement_model"],
                "review_projection_ref": artifact_paths["requirement_packet"],
            }
            allowed = ALLOWED_REQUIREMENT_ACTIONS
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": current_phase,
                "status": status,
                "latest_artifacts": artifact_paths,
                "pending_checkpoint": checkpoint,
                "allowed_next_actions": list(allowed),
                "last_error": None,
                "quality_reports": {"requirement_model_v2": quality_report},
                "latest_requirement_decision": latest_decision,
            }
        )

    def _normalized_artifacts(self, state: dict[str, object]) -> dict[str, str]:
        value = state.get("latest_artifacts")
        if not isinstance(value, dict):
            raise HarnessError("sync_not_ready", "Run artifacts are not ready.", run_id=str(state.get("run_id") or ""))
        return {str(key): str(item) for key, item in value.items()}

    def _requirement_sync_context(self, state: dict[str, object]) -> dict[str, object]:
        checkpoint = state.get("pending_checkpoint")
        context: dict[str, object] = {
            "requirement_contract_version": str(state.get("requirement_contract_version") or "v1"),
            "status": str(state.get("status") or ""),
        }
        if isinstance(checkpoint, dict):
            context["checkpoint_type"] = str(checkpoint.get("type") or "")
            context["current_question"] = checkpoint.get("current_question")
            context["remaining_blocking_count"] = checkpoint.get("remaining_blocking_count", 0)
        decision = state.get("latest_requirement_decision")
        if isinstance(decision, dict):
            context["decision_summary"] = {
                "id": decision.get("id"),
                "question_id": decision.get("question_id"),
                "status": decision.get("status"),
                "answered_by": decision.get("answered_by"),
            }
        return context

    def _sync_requirement_state(self, state: dict[str, object]) -> dict[str, object]:
        return sync_requirement_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=str(state["run_id"]),
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=self._normalized_artifacts(state),
            checkpoint_context=self._requirement_sync_context(state),
        )

    @staticmethod
    def _v2_summary(state: dict[str, object], last_sync: dict[str, object]) -> str:
        checkpoint = state.get("pending_checkpoint")
        if state.get("status") == "waiting_requirement_clarification" and isinstance(checkpoint, dict):
            question = checkpoint.get("current_question")
            question_text = question.get("text") if isinstance(question, dict) else ""
            return (
                f"Requirement V2 cần anh làm rõ: {question_text} "
                f"Sync status={last_sync.get('status')}."
            )
        return (
            "Requirement V2 đã hết câu hỏi blocking và đang chờ duyệt. "
            f"Sync status={last_sync.get('status')}."
        )

    def get_status(self, *, run_id: str) -> dict[str, object]:
        """Đọc state hiện tại của run để Niko phản hồi người dùng."""

        state = self.store.read_state(run_id)
        return self._status_response(state, created_new_run=False)

    def sync_run(self, *, run_id: str) -> dict[str, object]:
        """Sync lại checkpoint hiện tại của run lên tool theo config hiện tại."""

        state = self.store.read_state(run_id)
        artifact_paths = state.get("latest_artifacts")
        if not isinstance(artifact_paths, dict):
            from ..core.errors import HarnessError

            raise HarnessError("sync_not_ready", f"Run is not ready for sync: {run_id}", run_id=run_id)
        source_ref = str(state.get("source_ref", ""))
        normalized_artifacts = {str(key): str(value) for key, value in artifact_paths.items()}
        checkpoint = state.get("pending_checkpoint")
        checkpoint_type = checkpoint.get("type") if isinstance(checkpoint, dict) else ""
        if checkpoint_type == "implementation_plan_review" and "implementation_plan" in normalized_artifacts:
            last_sync = sync_implementation_plan_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
            )
            waiting_text = "Run vẫn đang chờ anh review implementation plan trước khi sang Phase 4."
        elif checkpoint_type == "design_approval" and "design_artifact" in normalized_artifacts:
            last_sync = sync_design_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
            )
            waiting_text = "Run vẫn đang chờ anh review design trước khi sang Phase 3."
        elif checkpoint_type in {"requirement_approval", "requirement_clarification"} and "requirement_packet" in normalized_artifacts:
            last_sync = sync_requirement_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
                checkpoint_context=self._requirement_sync_context(state),
            )
            if checkpoint_type == "requirement_clarification":
                waiting_text = "Run vẫn đang chờ anh trả lời câu hỏi làm rõ requirement hiện tại."
            else:
                waiting_text = "Run vẫn đang chờ anh duyệt requirement trước khi chuyển sang design."
        else:
            from ..core.errors import HarnessError

            raise HarnessError("sync_not_ready", f"Run is not ready for sync: {run_id}", run_id=run_id)
        state.update(
            {
                "updated_at": utc_now_iso(),
                "last_sync": last_sync,
                "summary_for_user": (
                    f"Đã chạy sync cho run {run_id}; sync status={last_sync.get('status')}. "
                    f"{waiting_text}"
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        """Chuẩn hóa JSON response cho cả start và get-status."""

        response = {
            "ok": True,
            "created_new_run": created_new_run,
            "source_ref": state.get("source_ref"),
            "run_id": state.get("run_id"),
            "requirement_contract_version": state.get("requirement_contract_version", "v1"),
            "current_phase": state.get("current_phase"),
            "status": state.get("status"),
            "latest_artifacts": state.get("latest_artifacts", {}),
            "pending_checkpoint": state.get("pending_checkpoint"),
            "allowed_next_actions": state.get("allowed_next_actions", []),
            "last_sync": state.get("last_sync"),
            "last_error": state.get("last_error"),
            "quality_reports": state.get("quality_reports", {}),
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
        }
        checkpoint = response.get("pending_checkpoint")
        if (
            response.get("status") == "code_review_ready"
            and isinstance(checkpoint, dict)
            and checkpoint.get("type") == "code_review"
        ):
            run_id = str(response.get("run_id") or "")
            pull_request_url = str(checkpoint.get("pull_request_url") or "").strip()
            if pull_request_url:
                draft_pr_summary = f"Draft PR: {pull_request_url}. "
            else:
                draft_pr_summary = "Draft PR đã được tạo. "
            response["allowed_next_actions"] = list(ALLOWED_CODE_REVIEW_READY_ACTIONS)
            response["summary_for_user"] = (
                f"Checkpoint Code Review đã sẵn sàng cho run {run_id}. "
                f"{draft_pr_summary}"
                "Anh có thể approve checkpoint để chuyển Draft PR thành Ready for review."
            )
        return response
