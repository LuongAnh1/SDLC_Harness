"""Đọc PR identity và xuất code đã qua QA lên GitHub dưới dạng Draft PR.

Module sở hữu adapter GitHub của code plane: reader chỉ đọc identity của PR; còn
publisher kiểm manifest file, tạo commit, push branch và create/update Draft PR.
Workflow vẫn quyết định khi nào gọi adapter và cách chuyển state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from ..core.errors import HarnessError
from ..storage.runtime import utc_now_iso

GhRunner = Callable[[list[str], Path], str]


@dataclass(frozen=True)
class GitHubCodeReviewResult:
    """Kết quả máy đọc được của một lần publish code review."""

    payload: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return dict(self.payload)


class GitHubPullRequestReader:
    """Đọc identity bất biến của pull request mà không thay đổi GitHub."""

    def __init__(self, gh_runner: GhRunner | None = None):
        self._gh_runner = gh_runner or _run_gh

    def read(
        self,
        *,
        target_path: Path,
        repository: str,
        pr_number: int,
        run_id: str,
    ) -> dict[str, object]:
        """Trả identity branch/base/commit cần để nhận nuôi một run cũ."""

        try:
            return _json_object(
                self._gh_runner(
                    [
                        "pr",
                        "view",
                        str(pr_number),
                        "--repo",
                        repository,
                        "--json",
                        "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid",
                    ],
                    target_path,
                ),
                run_id=run_id,
            )
        except HarnessError as exc:
            raise HarnessError(
                "workspace_adoption_failed",
                f"Cannot read GitHub pull request identity: {exc.message}",
                run_id=run_id,
            ) from exc


class GitHubPublishError(HarnessError):
    """Lỗi publication kèm progress để retry không lặp commit/PR."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        run_id: str,
        partial_result: dict[str, object] | None = None,
    ):
        super().__init__(error_code, message, run_id=run_id)
        self.partial_result = dict(partial_result or {})


class GitHubCodeReviewPublisher:
    """Commit/push target branch và upsert một Draft PR bằng `gh` CLI."""

    def __init__(self, gh_runner: GhRunner | None = None):
        self._gh_runner = gh_runner or _run_gh

    def publish(
        self,
        *,
        target_path: Path,
        expected_remote: str,
        repository: str,
        branch: str,
        base_branch: str,
        issue_key: str,
        run_id: str,
        publication_paths: list[str],
        pr_title: str,
        pr_body_path: Path,
    ) -> GitHubCodeReviewResult:
        """Publish đúng file trong manifest và giữ đủ evidence cho retry."""

        progress: dict[str, object] = {
            "version": "github_code_review_v1",
            "phase": "code_review_publish",
            "status": "publishing",
            "repository": repository,
            "remote": expected_remote,
            "base_branch": base_branch,
            "branch": branch,
            "issue_key": issue_key,
            "run_id": run_id,
            "publication_paths": sorted(set(publication_paths)),
            "commit": {},
            "push": {},
            "pull_request": {},
            "updated_at": utc_now_iso(),
        }
        try:
            self._validate_repo(target_path, expected_remote=expected_remote, branch=branch, run_id=run_id)
            changed_paths = _changed_paths(target_path)
            allowed = set(progress["publication_paths"])
            unexpected = sorted(path for path in changed_paths if path not in allowed)
            if unexpected:
                raise GitHubPublishError(
                    "github_publish_unexpected_changes",
                    "Target repo has changed files outside the publication manifest: " + ", ".join(unexpected),
                    run_id=run_id,
                    partial_result=progress,
                )

            if changed_paths:
                staged_paths = sorted(changed_paths)
                _git(target_path, "add", "--", *staged_paths, run_id=run_id)
                _git(
                    target_path,
                    "commit",
                    "-m",
                    f"{issue_key} Implement approved use case",
                    "-m",
                    f"SDLC-Run: {run_id}",
                    run_id=run_id,
                )
                commit_operation = "created"
            else:
                staged_paths = []
                commit_operation = "reused"

            commit_sha = _find_run_commit(target_path, run_id=run_id)
            if not commit_sha:
                raise GitHubPublishError(
                    "github_publish_commit_missing",
                    f"No target changes or existing commit were found for run {run_id}.",
                    run_id=run_id,
                    partial_result=progress,
                )
            progress["commit"] = {
                "operation": commit_operation,
                "sha": commit_sha,
                "url": f"https://github.com/{repository}/commit/{commit_sha}",
                "staged_paths": staged_paths,
            }
            progress["updated_at"] = utc_now_iso()

            _git(target_path, "push", "--set-upstream", "origin", branch, run_id=run_id)
            progress["push"] = {"status": "pushed", "branch": branch}
            progress["updated_at"] = utc_now_iso()

            pull_request = self._upsert_draft_pr(
                target_path=target_path,
                repository=repository,
                branch=branch,
                base_branch=base_branch,
                title=pr_title,
                body_path=pr_body_path,
                run_id=run_id,
            )
            progress["pull_request"] = pull_request
            progress["status"] = "published"
            progress["updated_at"] = utc_now_iso()
            return GitHubCodeReviewResult(progress)
        except GitHubPublishError as exc:
            if exc.partial_result:
                raise
            raise GitHubPublishError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                partial_result=progress,
            ) from exc
        except HarnessError as exc:
            raise GitHubPublishError(
                exc.error_code,
                exc.message,
                run_id=run_id,
                partial_result=progress,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - đổi lỗi CLI thành contract ổn định.
            raise GitHubPublishError(
                "github_publish_failed",
                _trim_error(str(exc)),
                run_id=run_id,
                partial_result=progress,
            ) from exc

    def _validate_repo(self, target_path: Path, *, expected_remote: str, branch: str, run_id: str) -> None:
        current_branch = _git(target_path, "rev-parse", "--abbrev-ref", "HEAD", run_id=run_id)
        if current_branch != branch:
            raise GitHubPublishError(
                "github_publish_branch_mismatch",
                f"Expected target branch {branch}, got {current_branch}.",
                run_id=run_id,
            )
        actual_remote = _git(target_path, "remote", "get-url", "origin", run_id=run_id)
        if _normalize_remote(actual_remote) != _normalize_remote(expected_remote):
            raise GitHubPublishError(
                "github_publish_remote_mismatch",
                f"Expected target remote {expected_remote}, got {actual_remote}.",
                run_id=run_id,
            )

    def _upsert_draft_pr(
        self,
        *,
        target_path: Path,
        repository: str,
        branch: str,
        base_branch: str,
        title: str,
        body_path: Path,
        run_id: str,
    ) -> dict[str, object]:
        open_items = _json_list(
            self._gh_runner(
                [
                    "pr",
                    "list",
                    "--repo",
                    repository,
                    "--head",
                    branch,
                    "--state",
                    "open",
                    "--json",
                    "number,url,title,isDraft,headRefName,baseRefName",
                ],
                target_path,
            ),
            run_id=run_id,
        )
        if open_items:
            current = open_items[0]
            if str(current.get("baseRefName") or "") != base_branch:
                raise GitHubPublishError(
                    "github_pr_base_mismatch",
                    f"Existing PR targets {current.get('baseRefName')}, expected {base_branch}.",
                    run_id=run_id,
                )
            if current.get("isDraft") is not True:
                raise GitHubPublishError(
                    "github_pr_not_draft",
                    "Existing pull request is no longer a draft; refusing to change review state automatically.",
                    run_id=run_id,
                )
            number = str(current.get("number") or "")
            self._gh_runner(
                ["pr", "edit", number, "--repo", repository, "--title", title, "--body-file", str(body_path)],
                target_path,
            )
            operation = "updated"
            reference = number
        else:
            closed_items = _json_list(
                self._gh_runner(
                    [
                        "pr",
                        "list",
                        "--repo",
                        repository,
                        "--head",
                        branch,
                        "--state",
                        "closed",
                        "--json",
                        "number,url,title,isDraft,headRefName,baseRefName",
                    ],
                    target_path,
                ),
                run_id=run_id,
            )
            if closed_items:
                raise GitHubPublishError(
                    "github_pr_closed",
                    "A closed pull request already exists for this branch; human action is required.",
                    run_id=run_id,
                )
            reference = self._gh_runner(
                [
                    "pr",
                    "create",
                    "--draft",
                    "--repo",
                    repository,
                    "--base",
                    base_branch,
                    "--head",
                    branch,
                    "--title",
                    title,
                    "--body-file",
                    str(body_path),
                ],
                target_path,
            ).strip()
            operation = "created"

        detail = _json_object(
            self._gh_runner(
                [
                    "pr",
                    "view",
                    reference,
                    "--repo",
                    repository,
                    "--json",
                    "number,url,title,isDraft,headRefName,baseRefName",
                ],
                target_path,
            ),
            run_id=run_id,
        )
        if detail.get("isDraft") is not True:
            raise GitHubPublishError(
                "github_pr_not_draft",
                "GitHub pull request was created or found, but it is not a draft.",
                run_id=run_id,
            )
        return {"operation": operation, **detail}


def github_repository_from_remote(remote: str) -> str:
    """Đổi HTTPS/SSH GitHub remote thành `owner/repo`."""

    value = remote.strip().rstrip("/")
    patterns = (
        r"^https://github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
        r"^git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group("slug")
    raise HarnessError("github_repository_invalid", f"Unsupported GitHub remote: {remote}")


def _changed_paths(target_path: Path) -> set[str]:
    values: set[str] = set()
    for args in (
        ("diff", "--name-only", "--"),
        ("diff", "--cached", "--name-only", "--"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        output = _git(target_path, *args, run_id="")
        values.update(_normalize_path(line) for line in output.splitlines() if line.strip())
    return {path for path in values if path and path != ".codegraph" and not path.startswith(".codegraph/")}


def _find_run_commit(target_path: Path, *, run_id: str) -> str:
    return _git(
        target_path,
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep=SDLC-Run: {run_id}",
        "-n",
        "1",
        run_id=run_id,
    ).strip()


def _git(cwd: Path, *args: str, run_id: str) -> str:
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
        stderr = getattr(exc, "stderr", "") or ""
        detail = _trim_error(str(stderr) or str(exc))
        raise HarnessError(
            "github_git_failed",
            f"Git command failed ({' '.join(args)}): {detail}",
            run_id=run_id or None,
        ) from exc
    return completed.stdout.strip()


def _run_gh(args: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise HarnessError("github_cli_failed", _trim_error(str(stderr) or str(exc))) from exc
    return completed.stdout.strip()


def _json_list(raw: str, *, run_id: str) -> list[dict[str, object]]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubPublishError("github_cli_invalid_json", "GitHub CLI returned invalid JSON.", run_id=run_id) from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise GitHubPublishError("github_cli_invalid_json", "GitHub CLI did not return a JSON list.", run_id=run_id)
    return value


def _json_object(raw: str, *, run_id: str) -> dict[str, object]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise GitHubPublishError("github_cli_invalid_json", "GitHub CLI returned invalid JSON.", run_id=run_id) from exc
    if not isinstance(value, dict):
        raise GitHubPublishError("github_cli_invalid_json", "GitHub CLI did not return a JSON object.", run_id=run_id)
    return value


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("/")


def _normalize_remote(value: str) -> str:
    return value.strip().rstrip("/").removesuffix(".git").lower()


def _trim_error(message: str) -> str:
    return " ".join(message.split())[:500]
