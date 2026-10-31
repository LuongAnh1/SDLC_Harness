"""Deterministic npm-workspace dependency materialization for Developer units."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ...core.errors import HarnessError


def workspace_dependency_status(
    *,
    repo_root: Path,
    requirement: dict[str, object],
    run_id: str,
) -> dict[str, object]:
    """Return semantic manifest/lockfile status for one approved local dependency."""

    dependency_id = str(requirement.get("dependency_id") or "").strip()
    dependency_name = str(requirement.get("dependency_name") or "").strip()
    dependency_spec = str(requirement.get("dependency_spec") or "").strip()
    consumer_manifest_path = str(
        requirement.get("consumer_manifest_path") or requirement.get("manifest_path") or ""
    ).replace("\\", "/")
    provider_manifest_path = str(requirement.get("provider_manifest_path") or "").replace("\\", "/")
    lockfile_path = str(requirement.get("lockfile_path") or "package-lock.json").replace("\\", "/")
    package_manager = str(requirement.get("package_manager") or "npm").strip().casefold()
    if not all(
        (
            dependency_id,
            dependency_name,
            dependency_spec,
            consumer_manifest_path,
            provider_manifest_path,
            lockfile_path,
        )
    ):
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            "Approved package dependency contract is incomplete.",
            run_id=run_id or None,
        )
    if package_manager != "npm":
        raise HarnessError(
            "developer_package_manager_unsupported",
            f"Package dependency {dependency_id} requires unsupported package manager: {package_manager}",
            run_id=run_id or None,
        )

    consumer_file = _safe_repo_file(repo_root, consumer_manifest_path, run_id=run_id)
    provider_file = _safe_repo_file(repo_root, provider_manifest_path, run_id=run_id)
    lockfile = _safe_repo_file(repo_root, lockfile_path, run_id=run_id)
    consumer = _read_json_object(consumer_file, run_id=run_id, label="consumer package manifest")
    provider = _read_json_object(provider_file, run_id=run_id, label="provider package manifest")
    lock = _read_json_object(lockfile, run_id=run_id, label="npm lockfile")
    if int(lock.get("lockfileVersion") or 0) != 3 or not isinstance(lock.get("packages"), dict):
        raise HarnessError(
            "developer_package_lock_unsupported",
            f"Package dependency {dependency_id} requires package-lock.json v3.",
            run_id=run_id or None,
        )

    consumer_name = str(requirement.get("consumer_package_name") or "").strip()
    provider_name = str(requirement.get("provider_package_name") or dependency_name).strip()
    provider_version = str(provider.get("version") or "").strip()
    if consumer_name and str(consumer.get("name") or "").strip() != consumer_name:
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"Consumer package name does not match {consumer_manifest_path}.",
            run_id=run_id or None,
        )
    if str(provider.get("name") or "").strip() != provider_name or provider_version != dependency_spec:
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"Provider package does not match approved dependency {dependency_name}@{dependency_spec}.",
            run_id=run_id or None,
        )

    consumer_workspace_path = str(
        requirement.get("consumer_workspace_path") or Path(consumer_manifest_path).parent.as_posix()
    ).strip("./")
    provider_workspace_path = str(
        requirement.get("provider_workspace_path") or Path(provider_manifest_path).parent.as_posix()
    ).strip("./")
    packages = lock["packages"]
    consumer_lock = packages.get(consumer_workspace_path)
    provider_lock = packages.get(provider_workspace_path)
    provider_link = packages.get(f"node_modules/{dependency_name}")
    manifest_spec = str(_dependency_map(consumer).get(dependency_name) or "")
    lock_spec = (
        str(_dependency_map(consumer_lock).get(dependency_name) or "")
        if isinstance(consumer_lock, dict)
        else ""
    )
    link_matches = (
        isinstance(provider_lock, dict)
        and str(provider_lock.get("name") or "") == provider_name
        and str(provider_lock.get("version") or "") == dependency_spec
        and isinstance(provider_link, dict)
        and provider_link.get("link") is True
        and str(provider_link.get("resolved") or "").replace("\\", "/") == provider_workspace_path
    )
    satisfied = manifest_spec == dependency_spec and lock_spec == dependency_spec and link_matches
    return {
        "dependency_id": dependency_id,
        "status": "satisfied" if satisfied else "pending",
        "satisfied": satisfied,
        "dependency_name": dependency_name,
        "dependency_spec": dependency_spec,
        "consumer_manifest_path": consumer_manifest_path,
        "provider_manifest_path": provider_manifest_path,
        "lockfile_path": lockfile_path,
        "manifest_spec": manifest_spec,
        "lockfile_spec": lock_spec,
        "provider_link_valid": link_matches,
        "consumer_manifest_sha256": _file_hash(consumer_file),
        "lockfile_sha256": _file_hash(lockfile),
    }


def ensure_workspace_dependency(
    *,
    repo_root: Path,
    requirement: dict[str, object],
    run_id: str,
    timeout_seconds: int,
) -> dict[str, object]:
    """Materialize one approved dependency with npm and return a durable receipt."""

    before = workspace_dependency_status(
        repo_root=repo_root,
        requirement=requirement,
        run_id=run_id,
    )
    if before["satisfied"] is True:
        return {"ok": True, "operation": "noop", **before}

    consumer_path = str(before["consumer_manifest_path"])
    lockfile_path = str(before["lockfile_path"])
    consumer_file = _safe_repo_file(repo_root, consumer_path, run_id=run_id)
    lockfile = _safe_repo_file(repo_root, lockfile_path, run_id=run_id)
    originals = {consumer_file: consumer_file.read_bytes(), lockfile: lockfile.read_bytes()}
    before_dirty = _dirty_path_hashes(repo_root)
    before_dirty_contents = {
        path: (repo_root / path).read_bytes()
        for path in before_dirty
        if (repo_root / path).is_file()
    }
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise HarnessError(
            "developer_package_manager_unavailable",
            "npm executable is unavailable for approved workspace dependency materialization.",
            run_id=run_id or None,
        )
    dependency = f"{before['dependency_name']}@{before['dependency_spec']}"
    consumer_name = str(requirement.get("consumer_package_name") or "").strip()
    if not consumer_name:
        consumer = _read_json_object(consumer_file, run_id=run_id, label="consumer package manifest")
        consumer_name = str(consumer.get("name") or "").strip()
    command = [
        npm,
        "install",
        dependency,
        "--workspace",
        consumer_name,
        "--save-exact",
        "--package-lock-only",
        "--ignore-scripts",
        "--offline",
        "--no-audit",
        "--no-fund",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_seconds)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _restore_command_changes(
            repo_root=repo_root,
            before_dirty=before_dirty,
            before_dirty_contents=before_dirty_contents,
            originals=originals,
        )
        raise HarnessError(
            "developer_package_dependency_install_failed",
            f"npm could not materialize approved dependency {before['dependency_id']}: {exc}",
            run_id=run_id or None,
        ) from exc
    if completed.returncode != 0:
        _restore_command_changes(
            repo_root=repo_root,
            before_dirty=before_dirty,
            before_dirty_contents=before_dirty_contents,
            originals=originals,
        )
        detail = (completed.stderr or completed.stdout).strip()[:2000]
        raise HarnessError(
            "developer_package_dependency_install_failed",
            f"npm rejected approved dependency {before['dependency_id']}: {detail}",
            run_id=run_id or None,
        )

    after_dirty = _dirty_path_hashes(repo_root)
    changed_by_command = {
        path for path in set(before_dirty) | set(after_dirty) if before_dirty.get(path) != after_dirty.get(path)
    }
    unexpected = sorted(changed_by_command - {consumer_path, lockfile_path})
    if unexpected:
        _restore_command_changes(
            repo_root=repo_root,
            before_dirty=before_dirty,
            before_dirty_contents=before_dirty_contents,
            originals=originals,
        )
        raise HarnessError(
            "developer_package_dependency_scope_violation",
            f"npm changed paths outside the approved dependency scope: {unexpected}",
            run_id=run_id or None,
        )

    after = workspace_dependency_status(
        repo_root=repo_root,
        requirement=requirement,
        run_id=run_id,
    )
    if after["satisfied"] is not True:
        _restore_command_changes(
            repo_root=repo_root,
            before_dirty=before_dirty,
            before_dirty_contents=before_dirty_contents,
            originals=originals,
        )
        raise HarnessError(
            "developer_package_dependency_incomplete",
            f"npm did not synchronize manifest and lockfile for {before['dependency_id']}.",
            run_id=run_id or None,
        )
    return {
        "ok": True,
        "operation": "materialized",
        **after,
        "changed_paths": sorted(changed_by_command),
        "stdout": completed.stdout.strip()[:2000],
    }


def _dependency_map(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    dependencies = value.get("dependencies")
    return dependencies if isinstance(dependencies, dict) else {}


def _safe_repo_file(repo_root: Path, relative: str, *, run_id: str) -> Path:
    normalized = relative.strip().replace("\\", "/").lstrip("/")
    if not normalized or normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"Unsafe package dependency path: {relative}",
            run_id=run_id or None,
        )
    root = repo_root.resolve()
    path = (root / normalized).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"Package dependency path escapes the worktree: {relative}",
            run_id=run_id or None,
        ) from exc
    if not path.is_file():
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"Package dependency file is missing: {relative}",
            run_id=run_id or None,
        )
    return path


def _read_json_object(path: Path, *, run_id: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"Cannot read {label}: {path.name}",
            run_id=run_id or None,
        ) from exc
    if not isinstance(value, dict):
        raise HarnessError(
            "developer_package_dependency_contract_invalid",
            f"{label} must contain a JSON object: {path.name}",
            run_id=run_id or None,
        )
    return value


def _dirty_path_hashes(repo_root: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )
    rows = completed.stdout.decode("utf-8", errors="replace").split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(rows):
        row = rows[index]
        index += 1
        if not row:
            continue
        path = row[3:].replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
        if row[:2].strip().startswith(("R", "C")) and index < len(rows):
            index += 1
    return {path: _file_hash(repo_root / path) for path in paths}


def _restore_command_changes(
    *,
    repo_root: Path,
    before_dirty: dict[str, str],
    before_dirty_contents: dict[str, bytes],
    originals: dict[Path, bytes],
) -> None:
    _restore_files(originals)
    after_dirty = _dirty_path_hashes(repo_root)
    changed = {
        path
        for path in set(before_dirty) | set(after_dirty)
        if before_dirty.get(path) != after_dirty.get(path)
    }
    original_paths = {str(path.relative_to(repo_root)).replace("\\", "/") for path in originals}
    for relative in sorted(changed - original_paths):
        target = _safe_repo_path(repo_root, relative)
        if relative in before_dirty_contents:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(before_dirty_contents[relative])
            continue
        if relative in before_dirty:
            if target.is_file():
                target.unlink()
            continue
        committed = subprocess.run(
            ["git", "show", f"HEAD:{relative}"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
        )
        if committed.returncode == 0:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(committed.stdout)
        elif target.is_file():
            target.unlink()


def _restore_files(originals: dict[Path, bytes]) -> None:
    for path, content in originals.items():
        path.write_bytes(content)


def _safe_repo_path(repo_root: Path, relative: str) -> Path:
    root = repo_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HarnessError(
            "developer_package_dependency_scope_violation",
            f"npm reported an unsafe changed path: {relative}",
        ) from exc
    return path


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
