"""Workflow Phase 2 V0: approved requirement -> design checkpoint.

Lát đầu Phase 2 chỉ khóa contract CLI/state/artifact/sync. Designer và Validator
ở đây là fake/deterministic để Niko có thể duyệt requirement rồi nhận checkpoint
design; các bước LLM Designer, parser PlantUML và traceability thật sẽ thay sau.
"""

from __future__ import annotations

from ..artifacts.writers import write_phase2_design_artifacts
from ..core.config import Settings
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_design_checkpoint

ALLOWED_DESIGN_ACTIONS = [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status",
]


class PhaseTwoWorkflow:
    """Điều phối lát Phase 2 V0 từ checkpoint requirement sang checkpoint design."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

    def approve_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Duyệt requirement checkpoint hiện tại và tạo design checkpoint V0."""

        state = self.store.read_state(run_id)
        self._require_requirement_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)

        artifact_paths = _string_dict(state.get("latest_artifacts"))
        requirement_path = artifact_paths.get("requirement_packet", "")
        if not requirement_path:
            raise HarnessError(
                "approved_requirement_not_found",
                f"Run does not have an approved requirement artifact: {run_id}",
                run_id=run_id,
            )
        requirement_abs_path = (self.store.repo_root / requirement_path).resolve()
        if not requirement_abs_path.exists():
            raise HarnessError(
                "approved_requirement_not_found",
                f"Requirement artifact does not exist: {requirement_path}",
                run_id=run_id,
            )

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_drafting",
                "status": "design_drafting",
                "approved_artifacts": {
                    "requirement_packet": requirement_path,
                },
                "allowed_next_actions": ["get_status", "cancel_run"],
                "summary_for_user": "Đã duyệt requirement checkpoint, đang tạo design checkpoint V0.",
            }
        )
        self.store.write_state(state)

        requirement_packet = requirement_abs_path.read_text(encoding="utf-8")
        design_paths = write_phase2_design_artifacts(
            store=self.store,
            run_id=run_id,
            requirement_packet=requirement_packet,
            version=1,
        )
        merged_artifacts = {**artifact_paths, **design_paths}
        last_sync = sync_design_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref", "")),
            artifact_paths=merged_artifacts,
        )

        checkpoint_id = f"checkpoint_design_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "design_review",
                "status": "waiting_design_approval",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "design_approval",
                    "artifact_ref": "design_artifact_v1",
                    "artifact_path": design_paths["design_artifact"],
                },
                "allowed_next_actions": ALLOWED_DESIGN_ACTIONS,
                "last_sync": last_sync,
                "quality_reports": {
                    **_dict_value(state.get("quality_reports")),
                    "design_traceability": {
                        "ok": True,
                        "mode": "fake_validator_v0",
                        "versions": ["v1"],
                        "summary": "Design V0 đã có class diagram, sequence diagram và missing links = 0.",
                        "report": design_paths["design_quality_report"],
                    },
                },
                "summary_for_user": (
                    f"Đã duyệt requirement và tạo design checkpoint V0 cho run {run_id}. "
                    f"Fake Validator V0 ghi missing links=0 và sync status={last_sync.get('status')}. "
                    "Run đang chờ anh review design trước khi sang Phase 3."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _require_requirement_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        """Chặn approve sai checkpoint hoặc sai trạng thái để workflow không nhảy cóc."""

        if str(state.get("status")) != "waiting_requirement_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for requirement approval: {run_id}",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "requirement_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not a requirement checkpoint: {run_id}",
                run_id=run_id,
            )
        current_checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if checkpoint_id and checkpoint_id != current_checkpoint_id:
            raise HarnessError(
                "checkpoint_not_current",
                f"Checkpoint is not current: {checkpoint_id}",
                run_id=run_id,
            )

    def _status_response(self, state: dict[str, object], *, created_new_run: bool) -> dict[str, object]:
        """Giữ response shape giống Phase 1 để Niko không phải biết nội bộ workflow."""

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


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
