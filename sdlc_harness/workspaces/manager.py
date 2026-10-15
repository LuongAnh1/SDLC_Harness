"""Tạo, lưu và xác minh Git worktree riêng cho từng SDLC run.

Manager sở hữu identity machine-local, thứ tự lock và các side effect Git của
workspace. Từ Slice 4.9A3, Phase 3 tạo workspace còn toàn bộ action Phase 4
resolve và khóa workspace này thay vì thao tác trên checkout canonical.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
from types import TracebackType
from typing import BinaryIO, Iterator

from ..core.config import Settings
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore, utc_now_iso
from .model import RunWorkspaceRecord, branch_name_for_run, run_suffix

WORKSPACE_ARTIFACT = "run_workspace_v1.json"


def _normalized_remote(value: str) -> str:
    return value.strip().replace("\\", "/").rstrip("/").removesuffix(".git").lower()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _is_same_or_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class WorkspaceFileLock:
    """Non-blocking advisory lock dùng handle OS, không dựa vào stale marker."""

    def __init__(self, path: Path, *, run_id: str | None = None):
        self.path = path
        self.run_id = run_id
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "WorkspaceFileLock":
        handle: BinaryIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            self._acquire(handle)
            self._handle = handle
            return self
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise HarnessError(
                "run_workspace_locked",
                f"Workspace lock is already held: {self.path.name}",
                run_id=self.run_id,
            ) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    @staticmethod
    def _acquire(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class RunWorkspaceManager:
    """Quản lý workspace identity và Git worktree theo `run_id`."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = RuntimeStore(repo_root=settings.repo_root, runtime_root=settings.runtime_root)
        self.locks_dir = settings.runtime_root / "locks"

    def artifact_path(self, run_id: str) -> Path:
        run_suffix(run_id)
        return self.store.artifact_path(run_id, WORKSPACE_ARTIFACT)

    def expected_worktree_path(self, run_id: str) -> Path:
        run_suffix(run_id)
        return (self.settings.target_worktree_root / run_id).resolve()

    def registry_lock(self) -> WorkspaceFileLock:
        return WorkspaceFileLock(self.locks_dir / "worktree-registry.lock")

    def run_lock(self, run_id: str) -> WorkspaceFileLock:
        run_suffix(run_id)
        return WorkspaceFileLock(self.locks_dir / f"workspace-{run_id}.lock", run_id=run_id)

    def create_for_run(self, run_id: str) -> RunWorkspaceRecord:
        """Tạo worktree mới từ remote base hoặc trả workspace hợp lệ đã có."""

        run_suffix(run_id)
        with self.registry_lock():
            with self.run_lock(run_id):
                state = self.store.read_state(run_id)
                if self.artifact_path(run_id).exists():
                    record = self._read_unlocked(run_id)
                    self._validate_record(record, state=state, verify_git=True)
                    self._reconcile_state(record, state=state)
                    return record
                return self._create_locked(run_id=run_id, state=state)

    def write(self, record: RunWorkspaceRecord) -> Path:
        """Ghi contract thủ công; production creation nên dùng `create_for_run`."""

        with self.run_lock(record.run_id):
            state = self.store.read_state(record.run_id)
            self._validate_record(record, state=state, verify_git=False)
            return self._write_record_locked(record)

    def read(self, run_id: str) -> RunWorkspaceRecord:
        return self._read_unlocked(run_id)

    def _read_unlocked(self, run_id: str) -> RunWorkspaceRecord:
        path = self.artifact_path(run_id)
        if not path.exists():
            raise HarnessError("run_workspace_missing", f"Run workspace not found: {run_id}", run_id=run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError(
                "run_workspace_invalid",
                f"Run workspace artifact is not valid JSON: {run_id}",
                run_id=run_id,
            ) from exc
        return RunWorkspaceRecord.from_dict(payload, expected_run_id=run_id)

    def resolve(self, run_id: str) -> RunWorkspaceRecord:
        """Xác minh runtime artifact và identity Git thật mà không giữ action lock."""

        state = self.store.read_state(run_id)
        record = self._read_unlocked(run_id)
        self._validate_record(record, state=state, verify_git=True)
        return record

    @contextmanager
    def locked_workspace(self, run_id: str) -> Iterator[RunWorkspaceRecord]:
        """Giữ per-run lock trong suốt action đọc/sửa code của caller."""

        with self.run_lock(run_id):
            state = self.store.read_state(run_id)
            record = self._read_unlocked(run_id)
            self._validate_record(record, state=state, verify_git=True)
            yield record

    def refresh_locked(
        self,
        record: RunWorkspaceRecord,
        *,
        pr_number: int | None = None,
        pr_url: str | None = None,
    ) -> RunWorkspaceRecord:
        """Cập nhật HEAD/PR sau mutation Git khi caller vẫn đang giữ run lock.

        Phương thức này cố ý không tự lấy lock để tránh nested lock. Caller phải gọi
        nó bên trong ``locked_workspace`` sau commit/push thành công hoặc một publish
        lỗi một phần đã làm HEAD di chuyển.
        """

        state = self.store.read_state(record.run_id)
        self._validate_record(record, state=state, verify_git=False)
        self._validate_git_identity(record, verify_recorded_head=False)
        head_sha = self._git(
            Path(record.worktree_path),
            "rev-parse",
            "HEAD",
            error_code="workspace_path_invalid",
            run_id=record.run_id,
        )
        self._git(
            Path(record.worktree_path),
            "merge-base",
            "--is-ancestor",
            record.base_sha,
            head_sha,
            error_code="workspace_branch_mismatch",
            run_id=record.run_id,
        )
        refreshed = replace(
            record,
            head_sha=head_sha,
            pr_number=record.pr_number if pr_number is None else pr_number,
            pr_url=record.pr_url if pr_url is None else pr_url,
            updated_at=utc_now_iso(),
        )
        self._write_record_locked(refreshed)
        self._reconcile_state(refreshed, state=state)
        return refreshed

    def _create_locked(self, *, run_id: str, state: dict[str, object]) -> RunWorkspaceRecord:
        canonical_repo, remote = self._validate_canonical_repo(run_id=run_id)
        worktree_path = self.expected_worktree_path(run_id)
        if _is_same_or_inside(worktree_path, canonical_repo):
            raise HarnessError(
                "workspace_path_invalid",
                "Run worktree must not be created inside the canonical target repo.",
                run_id=run_id,
            )
        if worktree_path.exists():
            raise HarnessError(
                "workspace_path_invalid",
                f"Workspace path already exists without an owning artifact: {worktree_path}",
                run_id=run_id,
            )

        base_ref = f"origin/{self.settings.target_base_branch}"
        self._git(
            canonical_repo,
            "fetch",
            "--prune",
            "origin",
            error_code="workspace_fetch_failed",
            run_id=run_id,
        )
        base_sha = self._git(
            canonical_repo,
            "rev-parse",
            "--verify",
            base_ref,
            error_code="workspace_fetch_failed",
            run_id=run_id,
        )
        branch = branch_name_for_run(
            prefix=self.settings.implementation_branch_prefix,
            source_ref=str(state.get("source_ref") or ""),
            issue_key=self._source_identity(state),
            run_id=run_id,
        )
        if self._ref_exists(canonical_repo, f"refs/heads/{branch}", run_id=run_id) or self._ref_exists(
            canonical_repo,
            f"refs/remotes/origin/{branch}",
            run_id=run_id,
        ):
            raise HarnessError(
                "workspace_branch_conflict",
                f"Workspace branch already exists without an owning artifact: {branch}",
                run_id=run_id,
            )

        try:
            self.settings.target_worktree_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError("workspace_path_invalid", str(exc), run_id=run_id) from exc

        try:
            self._git(
                canonical_repo,
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_path),
                base_sha,
                error_code="workspace_creation_failed",
                run_id=run_id,
            )
        except HarnessError as exc:
            self._raise_after_safe_rollback(
                exc=exc,
                canonical_repo=canonical_repo,
                worktree_path=worktree_path,
                branch=branch,
                base_sha=base_sha,
                run_id=run_id,
            )
            raise AssertionError("unreachable")

        try:
            now = utc_now_iso()
            head_sha = self._git(
                worktree_path,
                "rev-parse",
                "HEAD",
                error_code="workspace_creation_failed",
                run_id=run_id,
            )
            record = RunWorkspaceRecord(
                run_id=run_id,
                source_ref=str(state.get("source_ref") or ""),
                canonical_repo=str(canonical_repo),
                worktree_path=str(worktree_path),
                branch=branch,
                base_ref=base_ref,
                base_sha=base_sha,
                head_sha=head_sha,
                remote=remote,
                lifecycle="active",
                provisioning_mode="created_for_run",
                created_at=now,
                updated_at=now,
            )
            self._validate_record(record, state=state, verify_git=True)
            self._write_record_locked(record)
            self._reconcile_state(record, state=state)
        except HarnessError as exc:
            if self.artifact_path(run_id).exists():
                raise
            self._raise_after_safe_rollback(
                exc=exc,
                canonical_repo=canonical_repo,
                worktree_path=worktree_path,
                branch=branch,
                base_sha=base_sha,
                run_id=run_id,
            )
        return record

    def _raise_after_safe_rollback(
        self,
        *,
        exc: HarnessError,
        canonical_repo: Path,
        worktree_path: Path,
        branch: str,
        base_sha: str,
        run_id: str,
    ) -> None:
        rollback_error = self._rollback_unrecorded_workspace(
            canonical_repo=canonical_repo,
            worktree_path=worktree_path,
            branch=branch,
            base_sha=base_sha,
            run_id=run_id,
        )
        if rollback_error:
            raise HarnessError(
                "workspace_creation_partial",
                f"{exc.message}; safe rollback failed: {rollback_error}",
                run_id=run_id,
            ) from exc
        raise exc

    def _write_record_locked(self, record: RunWorkspaceRecord) -> Path:
        return self.store.write_json_artifact(record.run_id, WORKSPACE_ARTIFACT, record.to_dict())

    def _validate_record(
        self,
        record: RunWorkspaceRecord,
        *,
        state: dict[str, object],
        verify_git: bool,
    ) -> None:
        self._validate_static(record)
        if record.source_ref != str(state.get("source_ref") or ""):
            raise HarnessError(
                "run_workspace_mismatch",
                "Workspace source_ref does not match runtime state.",
                run_id=record.run_id,
            )
        if verify_git:
            self._validate_git(record)

    def _validate_static(self, record: RunWorkspaceRecord) -> None:
        run_id = record.run_id
        suffix = run_suffix(run_id)
        canonical_repo = Path(record.canonical_repo).resolve()
        worktree_path = Path(record.worktree_path).resolve()
        worktree_root = self.settings.target_worktree_root.resolve()
        if canonical_repo != self.settings.target_repo_path.resolve():
            raise HarnessError(
                "run_workspace_mismatch",
                "Workspace canonical repo does not match SDLC_TARGET_REPO_PATH.",
                run_id=run_id,
            )
        if not _is_inside(worktree_path, worktree_root) or worktree_path != self.expected_worktree_path(run_id):
            raise HarnessError(
                "workspace_path_invalid",
                "Workspace path is outside the configured root or does not match run_id.",
                run_id=run_id,
            )
        if record.provisioning_mode == "created_for_run" and not record.branch.endswith(f"-{suffix}"):
            raise HarnessError(
                "workspace_branch_mismatch",
                "Created workspace branch does not contain the owning run suffix.",
                run_id=run_id,
            )
        expected_base = f"origin/{self.settings.target_base_branch}"
        if record.base_ref != expected_base:
            raise HarnessError(
                "workspace_branch_mismatch",
                f"Workspace base_ref must be {expected_base}.",
                run_id=run_id,
            )
        if _normalized_remote(record.remote) != _normalized_remote(self.settings.target_repo_remote):
            raise HarnessError(
                "workspace_remote_mismatch",
                "Workspace remote does not match SDLC_TARGET_REPO_REMOTE.",
                run_id=run_id,
            )
        for field_name, sha in (("base_sha", record.base_sha), ("head_sha", record.head_sha)):
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
                raise HarnessError(
                    "run_workspace_invalid",
                    f"Workspace {field_name} is not a Git object id.",
                    run_id=run_id,
                )

    def _validate_git(self, record: RunWorkspaceRecord) -> None:
        self._validate_git_identity(record, verify_recorded_head=True)

    def _validate_git_identity(self, record: RunWorkspaceRecord, *, verify_recorded_head: bool) -> None:
        run_id = record.run_id
        if record.lifecycle != "active":
            raise HarnessError(
                "workspace_path_invalid",
                f"Workspace lifecycle is not active: {record.lifecycle}",
                run_id=run_id,
            )
        worktree_path = Path(record.worktree_path).resolve()
        if not worktree_path.is_dir():
            raise HarnessError(
                "workspace_path_invalid",
                f"Workspace directory does not exist: {worktree_path}",
                run_id=run_id,
            )
        registry = self._worktree_registry(Path(record.canonical_repo), run_id=run_id)
        entry = registry.get(worktree_path)
        if not entry:
            raise HarnessError(
                "workspace_path_invalid",
                "Workspace path is not registered by the canonical Git repository.",
                run_id=run_id,
            )
        expected_branch_ref = f"refs/heads/{record.branch}"
        if entry.get("branch") != expected_branch_ref:
            raise HarnessError(
                "workspace_branch_mismatch",
                f"Git worktree is not registered on branch {record.branch}.",
                run_id=run_id,
            )
        current_branch = self._git(
            worktree_path,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            error_code="workspace_path_invalid",
            run_id=run_id,
        )
        if current_branch != record.branch:
            raise HarnessError(
                "workspace_branch_mismatch",
                f"Workspace branch mismatch: expected {record.branch}, got {current_branch}.",
                run_id=run_id,
            )
        remote = self._git(
            worktree_path,
            "remote",
            "get-url",
            "origin",
            error_code="workspace_remote_mismatch",
            run_id=run_id,
        )
        if _normalized_remote(remote) != _normalized_remote(record.remote):
            raise HarnessError(
                "workspace_remote_mismatch",
                "Git worktree origin does not match workspace artifact.",
                run_id=run_id,
            )
        head_sha = self._git(
            worktree_path,
            "rev-parse",
            "HEAD",
            error_code="workspace_path_invalid",
            run_id=run_id,
        )
        if verify_recorded_head and (head_sha != record.head_sha or entry.get("HEAD") != record.head_sha):
            raise HarnessError(
                "workspace_branch_mismatch",
                "Workspace HEAD does not match the recorded head_sha.",
                run_id=run_id,
            )
        if verify_recorded_head:
            self._git(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                record.base_sha,
                record.head_sha,
                error_code="workspace_branch_mismatch",
                run_id=run_id,
            )

    def _validate_canonical_repo(self, *, run_id: str) -> tuple[Path, str]:
        target_path = self.settings.target_repo_path.resolve()
        if target_path == self.settings.repo_root.resolve():
            raise HarnessError("target_repo_is_harness", "Target repo points to SDLC_Harness itself.", run_id=run_id)
        if not target_path.is_dir():
            raise HarnessError("target_repo_not_found", f"Target repo does not exist: {target_path}", run_id=run_id)
        top_level = self._git(
            target_path,
            "rev-parse",
            "--show-toplevel",
            error_code="target_repo_not_git",
            run_id=run_id,
        )
        if Path(top_level).resolve() != target_path:
            raise HarnessError(
                "target_repo_not_git",
                f"Configured target path is not the canonical repository root: {target_path}",
                run_id=run_id,
            )
        remote = self._git(
            target_path,
            "remote",
            "get-url",
            "origin",
            error_code="workspace_remote_mismatch",
            run_id=run_id,
        )
        if _normalized_remote(remote) != _normalized_remote(self.settings.target_repo_remote):
            raise HarnessError(
                "workspace_remote_mismatch",
                f"Target repo remote mismatch: expected {self.settings.target_repo_remote}, got {remote}",
                run_id=run_id,
            )
        status = self._git(
            target_path,
            "status",
            "--porcelain",
            error_code="workspace_dirty",
            run_id=run_id,
        )
        if self._dirty_status_without_codegraph(status):
            raise HarnessError(
                "workspace_dirty",
                "Canonical target repo has uncommitted changes outside .codegraph/.",
                run_id=run_id,
            )
        return target_path, remote

    def _reconcile_state(self, record: RunWorkspaceRecord, *, state: dict[str, object]) -> None:
        artifact_ref = self.store.relative(self.artifact_path(record.run_id))
        latest_artifacts = dict(state.get("latest_artifacts") or {})
        workspace_summary = record.status_summary()
        if latest_artifacts.get("run_workspace") == artifact_ref and state.get("workspace") == workspace_summary:
            return
        latest_artifacts["run_workspace"] = artifact_ref
        state.update(
            {
                "updated_at": utc_now_iso(),
                "latest_artifacts": latest_artifacts,
                "workspace": workspace_summary,
            }
        )
        self.store.write_state(state)

    def _rollback_unrecorded_workspace(
        self,
        *,
        canonical_repo: Path,
        worktree_path: Path,
        branch: str,
        base_sha: str,
        run_id: str,
    ) -> str:
        """Rollback không force, chỉ khi workspace vẫn sạch và chưa có commit riêng."""

        try:
            registry = self._worktree_registry(canonical_repo, run_id=run_id)
            if worktree_path.resolve() in registry:
                status = self._git(
                    worktree_path,
                    "status",
                    "--porcelain",
                    error_code="workspace_creation_partial",
                    run_id=run_id,
                )
                head_sha = self._git(
                    worktree_path,
                    "rev-parse",
                    "HEAD",
                    error_code="workspace_creation_partial",
                    run_id=run_id,
                )
                if status.strip() or head_sha != base_sha:
                    return "new worktree is dirty or moved away from base_sha"
                self._git(
                    canonical_repo,
                    "worktree",
                    "remove",
                    str(worktree_path),
                    error_code="workspace_creation_partial",
                    run_id=run_id,
                )
            elif worktree_path.exists():
                return "unregistered workspace path remains after failed creation"
            if self._ref_exists(canonical_repo, f"refs/heads/{branch}", run_id=run_id):
                branch_sha = self._git(
                    canonical_repo,
                    "rev-parse",
                    branch,
                    error_code="workspace_creation_partial",
                    run_id=run_id,
                )
                if branch_sha != base_sha:
                    return "new branch no longer points at base_sha"
                self._git(
                    canonical_repo,
                    "branch",
                    "-d",
                    branch,
                    error_code="workspace_creation_partial",
                    run_id=run_id,
                )
            return ""
        except HarnessError as exc:
            return exc.message

    def _worktree_registry(self, canonical_repo: Path, *, run_id: str) -> dict[Path, dict[str, str]]:
        raw = self._git(
            canonical_repo,
            "worktree",
            "list",
            "--porcelain",
            error_code="workspace_path_invalid",
            run_id=run_id,
        )
        entries: dict[Path, dict[str, str]] = {}
        current: dict[str, str] = {}
        for line in [*raw.splitlines(), ""]:
            if not line.strip():
                path = current.get("worktree")
                if path:
                    entries[Path(path).resolve()] = dict(current)
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value.strip()
        return entries

    def _ref_exists(self, canonical_repo: Path, ref: str, *, run_id: str) -> bool:
        try:
            completed = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", ref],
                cwd=str(canonical_repo),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise HarnessError(
                "workspace_creation_failed",
                f"Cannot inspect Git ref {ref}: {exc}",
                run_id=run_id,
            ) from exc
        if completed.returncode not in {0, 1}:
            detail = completed.stderr.strip() or f"git show-ref exited {completed.returncode}"
            raise HarnessError(
                "workspace_creation_failed",
                f"Cannot inspect Git ref {ref}: {detail}",
                run_id=run_id,
            )
        return completed.returncode == 0

    def _git(
        self,
        cwd: Path,
        *args: str,
        error_code: str,
        run_id: str,
    ) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = str(getattr(exc, "stderr", "") or "").strip()
            detail = stderr or str(exc)
            raise HarnessError(
                error_code,
                f"Git command failed ({' '.join(args)}): {detail}",
                run_id=run_id,
            ) from exc
        return completed.stdout.strip()

    @staticmethod
    def _dirty_status_without_codegraph(status: str) -> str:
        lines: list[str] = []
        for line in status.splitlines():
            path = line[3:].strip() if len(line) > 3 else line.strip()
            if path == ".codegraph" or path.startswith(".codegraph/") or path.startswith(".codegraph\\"):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    @staticmethod
    def _source_identity(state: dict[str, object]) -> str:
        source_ref = str(state.get("source_ref") or "")
        if source_ref.startswith("jira_issue:"):
            return source_ref.split(":", 1)[1]
        dataset = str(state.get("dataset") or "").strip()
        use_case = str(state.get("use_case") or "").strip()
        if dataset and use_case:
            return f"{dataset}-{use_case}"
        if source_ref.startswith("dataset_use_case:"):
            parts = source_ref.split(":")
            if len(parts) >= 3:
                return f"{parts[-2]}-{parts[-1]}"
        return source_ref or "implementation"
