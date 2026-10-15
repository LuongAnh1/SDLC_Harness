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
from ..core.config import Settings
from ..core.errors import HarnessError
from ..sources.fixtures import UseCaseSource, load_use_case
from ..sources.jira import load_jira_issue_source
from ..llm.client import LlmClient
from ..agents.requirements import RequirementRefiner, RequirementValidator
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint, sync_implementation_plan_checkpoint, sync_requirement_checkpoint

ALLOWED_REQUIREMENT_ACTIONS = [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status",
]


class PhaseOneWorkflow:
    """Điều phối Phase 1 từ fixture/Jira tới checkpoint requirement.

    Workflow này sở hữu source loading, refiner, validator, artifact runtime và
    sync Jira/Confluence. Niko chỉ gọi CLI JSON và đọc response đã tóm tắt.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.refiner = RequirementRefiner(LlmClient(settings))
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
        elif checkpoint_type == "requirement_approval" and "requirement_packet" in normalized_artifacts:
            last_sync = sync_requirement_checkpoint(
                store=self.store,
                settings=self.settings,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=normalized_artifacts,
            )
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
            "workspace": state.get("workspace"),
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
        }
