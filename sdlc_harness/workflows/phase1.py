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
from ..sources.fixtures import load_use_case
from ..llm.client import LlmClient
from ..agents.requirements import RequirementRefiner, RequirementValidator
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync.mock import mock_sync_requirement_checkpoint

ALLOWED_REQUIREMENT_ACTIONS = [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status",
]


class PhaseOneWorkflow:
    """Use case độc lập đầu tiên: eTour UC1 -> formal use case + requirement packet."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.refiner = RequirementRefiner(LlmClient(settings))
        self.validator = RequirementValidator()

    def start_or_get_run(self, *, dataset: str, use_case: str, requested_by: str) -> dict[str, object]:
        """Tạo run mới hoặc trả active run hiện tại cho cùng source_ref."""

        source = load_use_case(dataset=dataset, use_case=use_case, repo_root=self.settings.repo_root)
        active_state = self.store.find_active_run(source.source_ref)
        if active_state:
            return self._status_response(active_state, created_new_run=False)

        run_id = self.store.new_run_id()
        now = utc_now_iso()
        state: dict[str, object] = {
            "ok": True,
            "run_id": run_id,
            "source_ref": source.source_ref,
            "dataset": dataset,
            "use_case": use_case,
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
        requirement_text = self.refiner.refine(source, formal_use_case=formal_refinement.final_use_case)
        validation_report = self.validator.require_valid(requirement_text)
        requirement_packet_path = write_requirement_packet(
            store=self.store,
            run_id=run_id,
            requirement_packet=requirement_text,
            validation_report=validation_report,
        )
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
        last_sync = mock_sync_requirement_checkpoint(
            store=self.store,
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
                    "requirement_packet": validation_report.to_dict(),
                },
                "summary_for_user": (
                    f"Đã chuẩn hóa {dataset} {use_case} thành formal use case và requirement packet v1 "
                    "bằng Requirement Refiner, validator đã pass, và đã đồng bộ mock Jira/Confluence. "
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
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
        }
