"""Workflow Phase 3: approved design -> implementation plan checkpoint.

Phase 3 chỉ bắt đầu sau khi design checkpoint đã được duyệt. Workflow đọc
requirement/design/trace graph đã duyệt, khóa target repo guard, tạo
implementation plan có cấu trúc và chạy Validator.review_plan hybrid. Phase này chưa
sửa target repo, chưa tạo branch/commit/PR.
"""

from __future__ import annotations

# Smart Developer V1: Phase 3 yêu cầu target repo đã được CodeGraph index trước.
# Workflow chỉ đọc repo context để lập plan, chưa tạo branch/commit/PR.

import json
import re
import subprocess
from pathlib import Path

from ..agents.developer import DeveloperPlanAgent
from ..agents.validator import ImplementationPlanValidator
from ..artifacts.writers import write_phase3_implementation_plan, write_phase3_plan_review_report
from ..core.config import Settings
from ..core.errors import HarnessError
from ..llm.client import LlmClient
from ..repo_context import CodeGraphContextProvider
from ..storage.runtime import RuntimeStore, utc_now_iso
from ..sync import sync_implementation_plan_checkpoint

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
        llm_client = LlmClient(settings)
        self.developer = DeveloperPlanAgent(llm_client)
        self.validator = ImplementationPlanValidator(llm_client)
        self.repo_context_provider = CodeGraphContextProvider(fake_mode=settings.llm_mode == "fake")

    def approve_design_checkpoint(self, *, run_id: str, checkpoint_id: str | None = None) -> dict[str, object]:
        """Duyệt design checkpoint và tạo implementation plan checkpoint."""

        state = self.store.read_state(run_id)
        self._require_design_checkpoint(state, run_id=run_id, checkpoint_id=checkpoint_id)
        artifact_paths = _string_dict(state.get("latest_artifacts"))
        requirement_path = _required_artifact(artifact_paths, "requirement_packet", run_id=run_id)
        design_path = _required_artifact(artifact_paths, "design_artifact", run_id=run_id)
        trace_path = _required_artifact(artifact_paths, "requirement_design_trace", run_id=run_id)
        class_graph_path = _required_artifact(artifact_paths, "class_diagram_graph", run_id=run_id)
        sequence_graph_path = _required_artifact(artifact_paths, "sequence_diagram_graph", run_id=run_id)
        requirement_abs_path = self._existing_artifact_path(requirement_path, run_id=run_id)
        design_abs_path = self._existing_artifact_path(design_path, run_id=run_id)
        trace_abs_path = self._existing_artifact_path(trace_path, run_id=run_id)
        class_graph_abs_path = self._existing_artifact_path(class_graph_path, run_id=run_id)
        sequence_graph_abs_path = self._existing_artifact_path(sequence_graph_path, run_id=run_id)

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
                "pending_checkpoint": None,
                "last_error": None,
                "approved_artifacts": {
                    **_dict_value(state.get("approved_artifacts")),
                    "requirement_packet": requirement_path,
                    "design_artifact": design_path,
                    "requirement_design_trace": trace_path,
                    "class_diagram_graph": class_graph_path,
                    "sequence_diagram_graph": sequence_graph_path,
                    **_optional_artifacts(artifact_paths, "requirement_graph"),
                },
                "allowed_next_actions": ["get_status", "cancel_run"],
                "summary_for_user": "Đã duyệt design checkpoint, đang tạo implementation plan Phase 3.",
            }
        )
        self.store.write_state(state)

        requirement_packet = requirement_abs_path.read_text(encoding="utf-8")
        design_artifact = design_abs_path.read_text(encoding="utf-8")
        requirement_design_trace = _read_json_artifact(trace_abs_path, run_id=run_id)
        class_graph = _read_json_artifact(class_graph_abs_path, run_id=run_id)
        sequence_graph = _read_json_artifact(sequence_graph_abs_path, run_id=run_id)
        issue_key = _issue_key_from_state(state)
        planned_branch = _planned_branch(
            prefix=self.settings.implementation_branch_prefix,
            issue_key=issue_key,
        )
        repo_context = self.repo_context_provider.collect(
            target_path=target_info.path,
            target_files=target_info.source_files,
            class_graph=class_graph,
            sequence_graph=sequence_graph,
            issue_key=issue_key,
        ).to_dict()
        try:
            plan_draft = self.developer.plan(
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                issue_key=issue_key,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                target_repo_path=str(target_info.path),
                target_repo_remote=target_info.remote,
                target_base_branch=self.settings.target_base_branch,
                planned_branch=planned_branch,
                target_files=target_info.source_files,
                test_command="powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
                repo_context=repo_context,
            )
        except HarnessError as exc:
            self._mark_phase3_failed(state, run_id=run_id, error=exc)
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        review = None
        plan_paths: dict[str, str] = {}
        review_report_path = ""
        merged_artifacts = dict(artifact_paths)
        quality_reports = _dict_value(state.get("quality_reports"))
        versions: list[str] = []
        repair_limit = self.settings.implementation_plan_repair_limit
        for repair_iteration in range(repair_limit + 1):
            version = repair_iteration + 1
            if repair_iteration == 0:
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_plan_validating",
                        "status": "implementation_plan_validating",
                        "summary_for_user": (
                            "Developer đã sinh implementation plan, Validator đang kiểm rule + LLM "
                            "trước khi mở checkpoint."
                        ),
                    }
                )
            else:
                state.update(
                    {
                        "updated_at": utc_now_iso(),
                        "current_phase": "implementation_plan_repairing",
                        "status": "implementation_plan_repairing",
                        "summary_for_user": (
                            f"Validator còn issue, Developer đang repair implementation plan "
                            f"lần {repair_iteration}/{repair_limit}."
                        ),
                    }
                )
            self.store.write_state(state)

            review = self.validator.review(
                structured_plan=plan_draft.structured,
                requirement_packet=requirement_packet,
                design_artifact=design_artifact,
                requirement_design_trace=requirement_design_trace,
                class_graph=class_graph,
                sequence_graph=sequence_graph,
                repair_iterations=repair_iteration,
            )
            plan_paths = write_phase3_implementation_plan(
                store=self.store,
                run_id=run_id,
                markdown=plan_draft.markdown,
                structured_plan=plan_draft.structured,
                version=version,
            )
            review_report_path = write_phase3_plan_review_report(
                store=self.store,
                run_id=run_id,
                review_report=review.to_dict(),
                version=version,
            )
            versions.append(f"v{version}")
            merged_artifacts = {
                **artifact_paths,
                **plan_paths,
                "implementation_plan_review": review_report_path,
            }
            quality_reports = {
                **_dict_value(state.get("quality_reports")),
                "implementation_plan_review": {
                    **review.to_dict(),
                    "versions": versions,
                    "report": review_report_path,
                },
            }
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "latest_artifacts": merged_artifacts,
                    "quality_reports": quality_reports,
                }
            )
            self.store.write_state(state)
            if review.ok or repair_iteration >= repair_limit:
                break

            feedback = review.developer_feedback or "\n".join(review.issues)
            try:
                plan_draft = self.developer.repair_plan(
                    previous_draft=plan_draft,
                    review_report=review.to_dict(),
                    developer_feedback=feedback,
                    iteration=repair_iteration + 1,
                    run_id=run_id,
                    source_ref=str(state.get("source_ref") or ""),
                    issue_key=issue_key,
                    requirement_packet=requirement_packet,
                    design_artifact=design_artifact,
                    requirement_design_trace=requirement_design_trace,
                    class_graph=class_graph,
                    sequence_graph=sequence_graph,
                    target_repo_path=str(target_info.path),
                    target_repo_remote=target_info.remote,
                    target_base_branch=self.settings.target_base_branch,
                    planned_branch=planned_branch,
                    target_files=target_info.source_files,
                    test_command="powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
                    repo_context=repo_context,
                )
            except HarnessError as exc:
                self._mark_phase3_failed(state, run_id=run_id, error=exc)
                raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
        if review is None:
            raise HarnessError(
                "implementation_plan_review_failed",
                "Phase 3 did not produce an implementation plan review.",
                run_id=run_id,
            )
        if not review.ok:
            error = HarnessError(
                "implementation_plan_review_failed",
                (
                    f"Validator.review_plan found {len(review.issues)} issue(s) "
                    f"after {review.repair_iterations} repair iteration(s)."
                ),
                run_id=run_id,
            )
            state.update(
                {
                    "updated_at": utc_now_iso(),
                    "current_phase": "implementation_planning",
                    "status": "blocked",
                    "latest_artifacts": merged_artifacts,
                    "pending_checkpoint": None,
                    "allowed_next_actions": ["get_status", "request_change", "cancel_run"],
                    "last_error": error.to_response(),
                    "quality_reports": quality_reports,
                    "summary_for_user": (
                        "Phase 3 bị chặn vì implementation plan chưa qua Validator.review_plan hybrid. "
                        f"Report: `{review_report_path}`."
                    ),
                }
            )
            self.store.write_state(state)
            return self._status_response(state, created_new_run=False)

        last_sync = sync_implementation_plan_checkpoint(
            store=self.store,
            settings=self.settings,
            run_id=run_id,
            source_ref=str(state.get("source_ref") or ""),
            artifact_paths=merged_artifacts,
        )
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
                    "artifact_ref": _artifact_ref(plan_paths["implementation_plan"]),
                    "artifact_path": plan_paths["implementation_plan"],
                },
                "allowed_next_actions": ALLOWED_IMPLEMENTATION_PLAN_ACTIONS,
                "last_error": None,
                "last_sync": last_sync,
                "quality_reports": quality_reports,
                "summary_for_user": (
                    f"Đã duyệt design và tạo implementation plan cho run {run_id}. "
                    f"Validator.review_plan hybrid đã pass sau {review.repair_iterations} vòng repair, "
                    f"target repo `{target_info.path}` sạch, "
                    f"remote khớp, planned branch `{planned_branch}`, sync status={last_sync.get('status')}. "
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

    def _mark_phase3_failed(self, state: dict[str, object], *, run_id: str, error: HarnessError) -> None:
        """Ghi rõ lỗi Phase 3 để run không kẹt với checkpoint design cũ."""

        state.update(
            {
                "updated_at": utc_now_iso(),
                "current_phase": "implementation_planning",
                "status": "failed",
                "pending_checkpoint": None,
                "allowed_next_actions": ["get_status", "cancel_run"],
                "last_error": HarnessError(error.error_code, error.message, run_id=run_id).to_response(),
                "summary_for_user": f"Phase 3 lỗi khi tạo implementation plan: {error.message}",
            }
        )
        self.store.write_state(state)

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


def _dirty_status_without_codegraph(status: str) -> str:
    lines: list[str] = []
    for line in status.splitlines():
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path == ".codegraph" or path.startswith(".codegraph/") or path.startswith(".codegraph\\"):
            continue
        lines.append(line)
    return "\n".join(lines)


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


def _read_json_artifact(path: Path, *, run_id: str) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "approved_design_not_found",
            f"Approved JSON artifact is not valid JSON: {path.name}",
            run_id=run_id,
        ) from exc
    if not isinstance(parsed, dict):
        raise HarnessError(
            "approved_design_not_found",
            f"Approved JSON artifact must be an object: {path.name}",
            run_id=run_id,
        )
    return parsed


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
