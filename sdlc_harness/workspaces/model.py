"""Mô hình dữ liệu và quy tắc đặt tên cho run workspace.

Module này chỉ xử lý identity thuần; nó không gọi Git, tạo worktree hoặc thay đổi
checkout. Nhờ vậy quy tắc branch/path được khóa bằng unit test và dùng chung cho
Phase 3/4 mà không phụ thuộc trạng thái checkout của canonical repo.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from ..core.errors import HarnessError

WORKSPACE_VERSION = "run_workspace_v1"
WORKSPACE_MODE = "git_worktree_v1"
WORKSPACE_LIFECYCLES = {"active", "cleanup_pending", "cleaned"}
WORKSPACE_PROVISIONING_MODES = {"created_for_run", "adopted_legacy"}
_RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}_([0-9a-fA-F]{8})$")


def run_suffix(run_id: str) -> str:
    """Lấy suffix ổn định từ run id chuẩn, đồng thời chặn path traversal."""

    match = _RUN_ID_PATTERN.fullmatch(run_id)
    if not match:
        raise HarnessError("run_workspace_invalid", f"Invalid workspace run_id: {run_id}", run_id=run_id)
    return match.group(1).lower()


def source_slug_for_workspace(*, source_ref: str, issue_key: str = "") -> str:
    """Chuẩn hóa Jira issue/source thành một đoạn Git branch dễ đọc."""

    source = issue_key.strip() or source_ref.rsplit(":", 1)[-1].strip()
    source = source.replace("đ", "d").replace("Đ", "D")
    ascii_source = unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_source.lower()).strip("-")
    return (slug or "implementation")[:48].rstrip("-") or "implementation"


def branch_name_for_run(*, prefix: str, source_ref: str, run_id: str, issue_key: str = "") -> str:
    """Sinh branch duy nhất theo source và run, không phụ thuộc trạng thái Git."""

    normalized_prefix = prefix.strip().strip("/")
    if (
        not normalized_prefix
        or ".." in normalized_prefix
        or "//" in normalized_prefix
        or "@{" in normalized_prefix
        or not re.fullmatch(r"[a-zA-Z0-9._/-]+", normalized_prefix)
    ):
        raise HarnessError("run_workspace_invalid", f"Invalid implementation branch prefix: {prefix}")
    slug = source_slug_for_workspace(source_ref=source_ref, issue_key=issue_key)
    return f"{normalized_prefix}/{slug}-{run_suffix(run_id)}"


@dataclass(frozen=True)
class RunWorkspaceRecord:
    """Bản ghi machine-local làm nguồn sự thật cho workspace của một run."""

    run_id: str
    source_ref: str
    canonical_repo: str
    worktree_path: str
    branch: str
    base_ref: str
    base_sha: str
    head_sha: str
    remote: str
    lifecycle: str
    provisioning_mode: str
    created_at: str
    updated_at: str
    pr_number: int | None = None
    pr_url: str = ""
    version: str = WORKSPACE_VERSION
    workspace_mode: str = WORKSPACE_MODE

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "source_ref": self.source_ref,
            "workspace_mode": self.workspace_mode,
            "canonical_repo": self.canonical_repo,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "remote": self.remote,
            "provisioning_mode": self.provisioning_mode,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "lifecycle": self.lifecycle,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def status_summary(self) -> dict[str, object]:
        """Trả identity gọn cho state/status, không làm lộ đường dẫn local."""

        return {
            "workspace_mode": self.workspace_mode,
            "provisioning_mode": self.provisioning_mode,
            "lifecycle": self.lifecycle,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "remote": self.remote,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
        }

    @classmethod
    def from_dict(cls, payload: object, *, expected_run_id: str | None = None) -> "RunWorkspaceRecord":
        """Parse schema V1 nghiêm ngặt để lỗi identity luôn fail closed."""

        if not isinstance(payload, dict):
            raise HarnessError("run_workspace_invalid", "Run workspace artifact must be a JSON object.")
        run_id = str(payload.get("run_id") or "")
        if expected_run_id and run_id != expected_run_id:
            raise HarnessError(
                "run_workspace_mismatch",
                f"Workspace artifact belongs to {run_id or '<missing>'}, expected {expected_run_id}.",
                run_id=expected_run_id,
            )
        if payload.get("version") != WORKSPACE_VERSION or payload.get("workspace_mode") != WORKSPACE_MODE:
            raise HarnessError("run_workspace_invalid", "Unsupported run workspace version or mode.", run_id=run_id or None)

        required = (
            "run_id",
            "source_ref",
            "canonical_repo",
            "worktree_path",
            "branch",
            "base_ref",
            "base_sha",
            "head_sha",
            "remote",
            "provisioning_mode",
            "lifecycle",
            "created_at",
            "updated_at",
        )
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise HarnessError(
                "run_workspace_invalid",
                "Run workspace artifact is missing: " + ", ".join(missing),
                run_id=run_id or None,
            )
        run_suffix(run_id)
        lifecycle = str(payload["lifecycle"])
        if lifecycle not in WORKSPACE_LIFECYCLES:
            raise HarnessError("run_workspace_invalid", f"Invalid workspace lifecycle: {lifecycle}", run_id=run_id)
        provisioning_mode = str(payload["provisioning_mode"])
        if provisioning_mode not in WORKSPACE_PROVISIONING_MODES:
            raise HarnessError(
                "run_workspace_invalid",
                f"Invalid workspace provisioning mode: {provisioning_mode}",
                run_id=run_id,
            )
        pr_number_raw = payload.get("pr_number")
        if pr_number_raw is not None and (type(pr_number_raw) is not int or pr_number_raw <= 0):
            raise HarnessError("run_workspace_invalid", "pr_number must be a positive integer or null.", run_id=run_id)
        return cls(
            run_id=run_id,
            source_ref=str(payload["source_ref"]),
            canonical_repo=str(payload["canonical_repo"]),
            worktree_path=str(payload["worktree_path"]),
            branch=str(payload["branch"]),
            base_ref=str(payload["base_ref"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            remote=str(payload["remote"]),
            lifecycle=lifecycle,
            provisioning_mode=provisioning_mode,
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            pr_number=pr_number_raw,
            pr_url=str(payload.get("pr_url") or ""),
        )
