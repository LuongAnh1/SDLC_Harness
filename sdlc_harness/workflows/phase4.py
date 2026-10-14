"""Workflow Phase 4: implementation plan -> code/test/trace.

Module này điều phối phần đầu của Phase 4 sau khi implementation plan đã được
human duyệt: đóng gói DeveloperWorkPacket, cho Developer.act ghi code vào target
repo, chạy Verification Runner, rồi bắt Validator kiểm trace requirement/design
sang code/test. Code trace hiện là gate hybrid: static evidence trước, sau đó
bắt buộc gọi LLM để review graph ngữ nghĩa trước khi cho workflow đi tiếp.
"""

from __future__ import annotations

from pathlib import Path

from ..agents.developer import DeveloperActAgent, DeveloperActResult, build_developer_work_packet
from ..agents.validator import CodeTraceValidator
from ..artifacts.writers import (
    write_phase4_code_change_report,
    write_phase4_code_trace_report,
    write_phase4_developer_work_packet,
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


class PhaseFourWorkflow:
    """Điều phối bước duyệt implementation plan trước khi Developer.act chạy."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.repo_context_provider = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")
        self.llm_client = LlmClient(settings)
        self.developer = DeveloperActAgent(self.llm_client)
        self.verification_runner = VerificationRunner()
        self.code_trace_validator = CodeTraceValidator(self.llm_client)

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
        if status == "implementation_repair_needed":
            raise HarnessError(
                "implementation_repair_not_implemented",
                f"Run needs Developer repair feedback handling before it can continue: {run_id}",
                run_id=run_id,
            )
        raise HarnessError(
            "run_not_continuable",
            f"Run is not ready for the next Phase 4 step: {run_id}",
            run_id=run_id,
        )

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
                    "summary_for_user": (
                        f"Validator code trace còn thiếu link có thể sửa ở code/test cho run {run_id}: "
                        f"{trace_result.summary} Đã ghi implementation_repair_feedback_v1.json để slice sau "
                        "giao Developer repair; workflow chưa tự sửa code ở slice 4.5."
                    ),
                }
            )
        else:
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
                        "implementation_traceability_failed",
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


def _implementation_repair_feedback(
    *,
    run_id: str,
    state: dict[str, object],
    work_packet: dict[str, object],
    trace_result: object,
    trace_path: str,
    target_repo: str,
    branch: str,
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
        "version": "implementation_repair_feedback_v1",
        "run_id": run_id,
        "source_ref": state.get("source_ref"),
        "issue_key": work_packet.get("issue_key"),
        "status": "implementation_repair_needed",
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
