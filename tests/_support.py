"""Hạ tầng dùng chung cho test, không chứa hành vi production."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable


PopulateRepository = Callable[[Path], None]


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class MaterializedGitRepository:
    remote: Path
    canonical: Path
    seed: Path | None = None


class GitRepositoryTemplate:
    """Giữ bare remote nền và cấp bản sao mutable riêng cho từng test."""

    def __init__(self, populate: PopulateRepository):
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        seed = root / "seed"
        remote = root / "remote.git"
        seed.mkdir()
        remote.mkdir()
        populate(seed)
        git(remote, "init", "--bare")
        git(seed, "init")
        git(seed, "config", "user.name", "SDLC Test")
        git(seed, "config", "user.email", "sdlc-test@example.com")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "initial")
        git(seed, "branch", "-M", "main")
        git(seed, "remote", "add", "origin", str(remote))
        git(seed, "push", "-u", "origin", "main")
        self._remote = remote

    def close(self) -> None:
        self._temp_dir.cleanup()

    def materialize(
        self,
        root: Path,
        *,
        canonical_name: str = "target_repo",
        include_seed: bool = False,
    ) -> MaterializedGitRepository:
        root.mkdir(parents=True, exist_ok=True)
        remote = root / "target.git"
        canonical = root / canonical_name
        shutil.copytree(self._remote, remote)
        git(root, "clone", "-q", "-b", "main", str(remote), str(canonical))
        git(canonical, "config", "user.name", "SDLC Test")
        git(canonical, "config", "user.email", "sdlc-test@example.com")
        seed: Path | None = None
        if include_seed:
            seed = root / "seed"
            git(root, "clone", "-q", "-b", "main", str(remote), str(seed))
            git(seed, "config", "user.name", "SDLC Test")
            git(seed, "config", "user.email", "sdlc-test@example.com")
        return MaterializedGitRepository(remote=remote, canonical=canonical, seed=seed)


class RuntimeDirectoryTemplate:
    """Sao chép checkpoint runtime bất biến và đổi các path máy cục bộ."""

    def __init__(self, runtime_root: Path):
        self._source_runtime_root = runtime_root.resolve()
        self._temp_dir = tempfile.TemporaryDirectory()
        self._snapshot_root = Path(self._temp_dir.name) / "runtime"
        shutil.copytree(self._source_runtime_root, self._snapshot_root)

    def close(self) -> None:
        self._temp_dir.cleanup()

    def materialize(self, destination: Path, *, replacements: dict[str, str] | None = None) -> None:
        destination = destination.resolve()
        if destination.exists():
            raise AssertionError(f"Runtime destination already exists: {destination}")
        shutil.copytree(self._snapshot_root, destination)
        substitutions = {
            str(self._source_runtime_root): str(destination),
            self._source_runtime_root.as_posix(): destination.as_posix(),
            **(replacements or {}),
        }
        for path in sorted(destination.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            rewritten_name = self._rewrite_text(path.name, substitutions)
            if rewritten_name != path.name:
                path.rename(path.with_name(rewritten_name))
        for path in destination.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                rewritten = self._rewrite_value(payload, substitutions)
                path.write_text(
                    json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                continue
            if path.suffix in {".md", ".txt", ".jsonl"}:
                content = path.read_text(encoding="utf-8")
                path.write_text(self._rewrite_text(content, substitutions), encoding="utf-8")

    def _rewrite_value(self, value: object, substitutions: dict[str, str]) -> object:
        if isinstance(value, dict):
            return {key: self._rewrite_value(item, substitutions) for key, item in value.items()}
        if isinstance(value, list):
            return [self._rewrite_value(item, substitutions) for item in value]
        if isinstance(value, str):
            return self._rewrite_text(value, substitutions)
        return value

    @staticmethod
    def _rewrite_text(value: str, substitutions: dict[str, str]) -> str:
        for source, destination in substitutions.items():
            value = value.replace(source, destination)
        return value


class DirectoryOverlayTemplate:
    """Lưu cây file làm overlay, bỏ metadata Git machine-local."""

    def __init__(self, source: Path):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._snapshot_root = Path(self._temp_dir.name) / "overlay"
        shutil.copytree(source, self._snapshot_root, ignore=shutil.ignore_patterns(".git"))

    def apply(self, destination: Path) -> None:
        shutil.copytree(self._snapshot_root, destination, dirs_exist_ok=True)

    def close(self) -> None:
        self._temp_dir.cleanup()


def create_fast_run_workspace(manager: object, *, run_id: str, state: dict[str, object]):
    """Tạo worktree thật, bỏ các guard đã được khóa ở workspace contract suite."""

    from sdlc_harness.storage.runtime import utc_now_iso
    from sdlc_harness.workspaces import RunWorkspaceRecord, branch_name_for_run

    canonical = manager.settings.target_repo_path.resolve()
    worktree = manager.expected_worktree_path(run_id)
    base_ref = f"origin/{manager.settings.target_base_branch}"
    base_sha = git(canonical, "rev-parse", "--verify", base_ref)
    branch = branch_name_for_run(
        prefix=manager.settings.implementation_branch_prefix,
        source_ref=str(state.get("source_ref") or ""),
        issue_key=manager._source_identity(state),
        run_id=run_id,
    )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(canonical, "worktree", "add", "-q", "-b", branch, str(worktree), base_sha)
    now = utc_now_iso()
    record = RunWorkspaceRecord(
        run_id=run_id,
        source_ref=str(state.get("source_ref") or ""),
        canonical_repo=str(canonical),
        worktree_path=str(worktree),
        branch=branch,
        base_ref=base_ref,
        base_sha=base_sha,
        head_sha=base_sha,
        remote=manager.settings.target_repo_remote,
        lifecycle="active",
        provisioning_mode="created_for_run",
        created_at=now,
        updated_at=now,
    )
    manager._write_record_locked(record)
    manager._reconcile_state(record, state=state)
    return record

