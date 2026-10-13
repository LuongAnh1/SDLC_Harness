"""Workflow Phase 4 slice 4.1: approve implementation plan -> work packet.

Phase 4 đầy đủ sẽ sinh code, chạy test, kiểm trace và mở Draft PR. Slice 4.1
chỉ khóa phần chuyển tiếp an toàn: implementation plan đã được human duyệt được
đóng gói thành DeveloperWorkPacket để bước Developer.act sau này dùng. Workflow
này chưa tạo branch, chưa sửa code và chưa sync GitHub.
"""

from __future__ import annotations

from pathlib import Path

from ..agents.developer import build_developer_work_packet
from ..artifacts.writers import write_phase4_developer_work_packet
from ..core.config import Settings
from ..core.errors import HarnessError
from ..repo_context import CodeGraphContextProvider
from ..storage.runtime import RuntimeStore, utc_now_iso
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

ALLOWED_IMPLEMENTATION_READY_ACTIONS = ["get_status", "cancel_run"]


class PhaseFourWorkflow:
    """Điều phối bước duyệt implementation plan trước khi Developer.act chạy."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.repo_context_provider = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")

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

    def _validate_target_repo(self) -> "_TargetRepoInfo":
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
        if dirty_without_codegraph.strip():
            raise HarnessError("target_repo_dirty", "Target repo has uncommitted changes outside .codegraph/.")
        return _TargetRepoInfo(
            path=target_path,
            remote=remote,
            current_branch=_git_output(target_path, "rev-parse", "--abbrev-ref", "HEAD"),
            source_files=_target_source_files(target_path),
        )

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
            "summary_for_user": state.get("summary_for_user"),
            "llm": state.get("llm"),
        }


class _TargetRepoInfo:
    def __init__(self, *, path: Path, remote: str, current_branch: str, source_files: list[str]):
        self.path = path
        self.remote = remote
        self.current_branch = current_branch
        self.source_files = source_files


def _required_phase4_artifact(artifact_paths: dict[str, str], key: str, *, run_id: str) -> str:
    value = artifact_paths.get(key, "")
    if not value:
        raise HarnessError(
            "approved_implementation_plan_not_found",
            f"Run does not have an approved Phase 4 input artifact `{key}`: {run_id}",
            run_id=run_id,
        )
    return value
