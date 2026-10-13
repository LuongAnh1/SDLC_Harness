"""Workflow Phase 3 Slice 1: approved design -> implementation plan checkpoint.

Phase 3 chỉ bắt đầu sau khi design checkpoint đã được duyệt. Lát đầu này chưa
sửa target repo, chưa tạo branch/commit/PR; nó khóa target repo guard và sinh
implementation plan để Developer slice kế tiếp có input ổn định.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..artifacts.writers import write_phase3_implementation_plan
from ..core.config import Settings
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore, utc_now_iso

ALLOWED_IMPLEMENTATION_PLAN_ACTIONS = [
    "approve_checkpoint",
    "request_change",
    "cancel_run",
    "get_status",
]


class PhaseThreeWorkflow:
    """Điều phối Phase 3 từ checkpoint design sang implementation plan."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)

    def approve_design_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Duyệt design checkpoint và tạo implementation plan checkpoint."""

        state = self.store.read_state(run_id)
        self._require_design_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        requirement_path = _required_artifact(artifact_paths, "requirement_packet", run_id=run_id)
        design_path = _required_artifact(artifact_paths, "design_artifact", run_id=run_id)
        requirement_abs_path = self._existing_artifact_path(requirement_path, run_id=run_id)
        design_abs_path = self._existing_artifact_path(design_path, run_id=run_id)

        try:
            target_info = self._validate_target_repo()
        except HarnessError as exc:
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_planning",
                    "status": "blocked",
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "request_change", "cancel_run"],
                    "last_error": exc.to_response(),
                    "summary_for_user": f"Phase 3 bị chặn khi kiểm tra target repo: {exc.message}",
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "implementation_planning",
                "last_error": None,
                "approved_artifacts": {
                    **_dict_value(state.get("approved_artifacts")),
                    "requirement_packet": requirement_path,
                    "design_artifact": design_path,
                    **_optional_artifacts(
                        artifact_paths,
                        "requirement_design_trace",
                        "requirement_graph",
                        "class_diagram_graph",
                        "sequence_diagram_graph",
                    ),
                },
                "allowed_next_actions": ["get_status", "cancel_run"],
                "summary_for_user": "Đã duyệt design checkpoint, đang tạo implementation plan Phase 3.",
            }
        )
        self.store.write_state(state)

        requirement_packet = requirement_abs_path.read_text(encoding="utf-8")
        design_artifact = design_abs_path.read_text(encoding="utf-8")
        issue_key = _issue_key_from_state(state)
        planned_branch = _planned_branch(
            prefix=self.settings.implementation_branch_prefix,
            issue_key=issue_key,
        )
        implementation_plan_path = write_phase3_implementation_plan(
            store=self.store,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            issue_key=issue_key,
            requirement_packet=requirement_packet,
            design_artifact=design_artifact,
            target_repo_path=str(target_info.path),
            target_repo_remote=target_info.remote,
            target_base_branch=self.settings.target_base_branch,
            planned_branch=planned_branch,
            target_files=target_info.source_files,
            test_command="powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
        )
        merged_artifacts = {**artifact_paths, "implementation_plan": implementation_plan_path}
        checkpoint_id = f"checkpoint_implementation_plan_{run_id}"
        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "waiting_implementation_plan_review",
                "latest_artifacts": merged_artifacts,
                "pending_checkpoint": {
                    "checkpoint_id": checkpoint_id,
                    "type": "implementation_plan_review",
                    "artifact_ref": _artifact_ref(implementation_plan_path),
                    "artifact_path": implementation_plan_path,
                },
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_PLAN_ACTIONS,
                "last_error": None,
                "summary_for_user": (
                    f"Đã duyệt design và tạo implementation plan cho run {run_id}. "
                    f"Target repo `{target_info.path}` sạch, remote khớp, planned branch `{planned_branch}`. "
                    "Run đang chờ review implementation plan trước khi slice tiếp theo tạo code/PR."
                ),
            }
        )
        self.store.write_state(state)
        return self._status_response(state, created_new_run=False)

    def _require_design_checkpoint(
        self,
        state: dict[str, object],
        *,
        run_id: str,
        checkpoint_id: str | None,
    ) -> None:
        if str(state.get("status")) != "waiting_design_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Run is not waiting for design approval: {run_id}",
                run_id=run_id,
            )
        checkpoint = _dict_value(state.get("pending_checkpoint"))
        if not checkpoint:
            raise HarnessError(
                "no_pending_checkpoint",
                f"Run does not have a pending checkpoint: {run_id}",
                run_id=run_id,
            )
        if checkpoint.get("type") != "design_approval":
            raise HarnessError(
                "checkpoint_not_current",
                f"Pending checkpoint is not a design checkpoint: {run_id}",
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
                "approved_design_not_found",
                f"Approved artifact does not exist: {relative_path}",
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

        remote = _git_output(target_path, "remote", "get-url", "origin")
        if _normalize_remote(remote) != _normalize_remote(self.settings.target_repo_remote):
            raise HarnessError(
                "target_repo_remote_mismatch",
                f"Target repo remote mismatch: expected {self.settings.target_repo_remote}, got {remote}",
            )

        dirty = _git_output(target_path, "status", "--porcelain")
        if dirty.strip():
            raise HarnessError("target_repo_dirty", "Target repo has uncommitted changes.")

        current_branch = _git_output(target_path, "rev-parse", "--abbrev-ref", "HEAD")
        source_files = _target_source_files(target_path)
        return _TargetRepoInfo(
            path=target_path,
            remote=remote,
            current_branch=current_branch,
            source_files=source_files,
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


def _git_output(cwd: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HarnessError("target_repo_git_failed", f"Git command failed in target repo: {' '.join(args)}") from exc
    return completed.stdout.strip()


def _target_source_files(target_path: Path) -> list[str]:
    files: list[str] = []
    for base in ("src/main/java", "src/test/java"):
        root = target_path / base
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.java")):
            files.append(str(path.relative_to(target_path)).replace("\\", "/"))
    return files


def _required_artifact(artifact_paths: dict[str, str], key: str, *, run_id: str) -> str:
    value = artifact_paths.get(key, "")
    if not value:
        raise HarnessError(
            "approved_design_not_found",
            f"Run does not have an approved artifact `{key}`: {run_id}",
            run_id=run_id,
        )
    return value


def _optional_artifacts(artifact_paths: dict[str, str], *keys: str) -> dict[str, str]:
    return {key: artifact_paths[key] for key in keys if artifact_paths.get(key)}


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _issue_key_from_state(state: dict[str, object]) -> str:
    source_ref = str(state.get("source_ref") or "")
    if source_ref.startswith("jira_issue:"):
        return source_ref.split(":", 1)[1]
    dataset = str(state.get("dataset") or "")
    use_case = str(state.get("use_case") or "")
    if dataset and use_case:
        return f"{dataset}-{use_case}"
    return use_case or "SDLC"


def _planned_branch(*, prefix: str, issue_key: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", issue_key.lower()).strip("-") or "implementation"
    return f"{prefix}/{slug}-implementation"


def _normalize_remote(remote: str) -> str:
    return remote.strip().rstrip("/").removesuffix(".git").lower()


def _artifact_ref(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]
