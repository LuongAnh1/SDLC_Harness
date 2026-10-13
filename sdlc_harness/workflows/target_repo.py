"""Preflight/setup cho target repo mà Phase 3 sẽ đọc bằng CodeGraph.

Lệnh này được tách khỏi `approve-checkpoint` để CodeGraph init là thao tác có
chủ ý của người dùng, không âm thầm làm dirty target repo trong lúc duyệt
checkpoint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..core.config import Settings
from ..core.errors import HarnessError
from ..repo_context import CodeGraphContextProvider


class TargetRepoPreparationWorkflow:
    """Kiểm tra và chuẩn bị target repo cho Smart Developer."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.provider = CodeGraphContextProvider()

    def prepare(self, *, init_codegraph: bool = False) -> dict[str, object]:
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

        dirty_without_codegraph = _dirty_status_without_codegraph(_git_output(target_path, "status", "--porcelain"))
        if dirty_without_codegraph.strip():
            raise HarnessError(
                "target_repo_dirty",
                "Target repo has uncommitted changes outside .codegraph/.",
            )

        codegraph_report = self.provider.prepare_index(target_path) if init_codegraph else {
            "codegraph_exists_before": (target_path / ".codegraph").exists(),
            "codegraph_exists_after": (target_path / ".codegraph").exists(),
            "codegraph_initialized": False,
            "smoke_ok": (target_path / ".codegraph").exists(),
            "smoke_excerpt": "",
        }
        return {
            "ok": True,
            "target_repo": str(target_path),
            "remote": remote,
            "base_branch": self.settings.target_base_branch,
            "current_branch": _git_output(target_path, "rev-parse", "--abbrev-ref", "HEAD"),
            "dirty_without_codegraph": bool(dirty_without_codegraph.strip()),
            "codegraph": codegraph_report,
        }


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


def _dirty_status_without_codegraph(status: str) -> str:
    lines: list[str] = []
    for line in status.splitlines():
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path == ".codegraph" or path.startswith(".codegraph/") or path.startswith(".codegraph\\"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _normalize_remote(remote: str) -> str:
    return remote.strip().rstrip("/").removesuffix(".git").lower()
