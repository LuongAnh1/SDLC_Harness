"""Deterministic execution closure for approved runtime environment variables."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

from ...core.errors import HarnessError
from ...profiles.runtime_config import canonical_runtime_config_impact


_ENV_EXAMPLE_LINE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=.*$")


def env_runtime_config_changes(value: object) -> list[dict[str, object]]:
    """Return the approved env-var changes from a Plan or packet impact object."""

    impact = canonical_runtime_config_impact(value)
    return [dict(item) for item in impact["changes"] if item.get("kind") == "env_var"]


def runtime_config_source_issues(
    *,
    repo_root: Path,
    changes: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Check mechanically that each approved source reads the named env var and carries its default."""

    root = repo_root.resolve()
    issues: list[dict[str, object]] = []
    for change in changes:
        name = str(change.get("name") or "")
        default = str(change.get("default") or "")
        read_by = str(change.get("read_by") or "").replace("\\", "/")
        path = _safe_repo_path(root, read_by)
        if not path.is_file():
            issues.append(_issue(change, "runtime_config_source_missing", f"Source file does not exist: {read_by}"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        env_patterns = (
            f"process.env.{name}",
            f'process.env["{name}"]',
            f"process.env['{name}']",
            f"import.meta.env.{name}",
        )
        if not any(pattern in text for pattern in env_patterns):
            issues.append(
                _issue(
                    change,
                    "runtime_config_env_read_missing",
                    f"{read_by} does not read the approved environment variable {name}.",
                )
            )
        if default not in text:
            issues.append(
                _issue(
                    change,
                    "runtime_config_default_missing",
                    f"{read_by} does not contain the approved non-secret default for {name}: {default}",
                )
            )
    return issues


def materialize_env_example(
    *,
    repo_root: Path,
    changes: list[dict[str, object]],
    run_id: str,
) -> dict[str, object]:
    """Create or update approved env-var declarations without exposing the file to the LLM."""

    root = repo_root.resolve()
    target = root / ".env.example"
    before = target.read_text(encoding="utf-8") if target.is_file() else ""
    lines = before.splitlines()
    operations: list[dict[str, object]] = []
    for change in changes:
        name = str(change.get("name") or "")
        default = str(change.get("default") or "")
        matches = [index for index, line in enumerate(lines) if _env_name(line) == name]
        if len(matches) > 1:
            raise HarnessError(
                "runtime_config_env_example_duplicate",
                f".env.example declares {name} more than once.",
                run_id=run_id,
            )
        replacement = f"{name}={default}"
        if not matches:
            lines.append(replacement)
            operation = "created" if not before and len(lines) == 1 else "appended"
        elif lines[matches[0]] == replacement:
            operation = "noop"
        else:
            lines[matches[0]] = replacement
            operation = "updated"
        operations.append(
            {
                "kind": "env_var",
                "unit_id": str(change.get("unit_id") or ""),
                "name": name,
                "default": default,
                "read_by": str(change.get("read_by") or ""),
                "operation": operation,
            }
        )
    after = "\n".join(lines)
    if lines:
        after += "\n"
    if after != before:
        _write_text_atomic(target, after)
    return {
        "schema_version": "runtime_config_execution_v1",
        "status": "materialized" if after != before else "unchanged",
        "path": ".env.example",
        "before_sha256": _text_hash(before),
        "after_sha256": _text_hash(after),
        "changed_paths": [".env.example"] if after != before else [],
        "operations": operations,
    }


def runtime_config_execution_issues(
    *,
    repo_root: Path,
    changes: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Validate source and materialized env-example closure after Developer execution."""

    issues = runtime_config_source_issues(repo_root=repo_root, changes=changes)
    env_path = repo_root.resolve() / ".env.example"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    for change in changes:
        name = str(change.get("name") or "")
        default = str(change.get("default") or "")
        values = [line.split("=", 1)[1] for line in lines if _env_name(line) == name]
        if values != [default]:
            issues.append(
                _issue(
                    change,
                    "runtime_config_env_example_mismatch",
                    f".env.example must declare exactly {name}={default}.",
                )
            )
    return issues


def worktree_dirty_paths(repo_root: Path) -> list[str]:
    """Return exact changed paths so recovery can freeze pre-existing edits."""

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
    return sorted(paths)


def _issue(change: dict[str, object], error_code: str, message: str) -> dict[str, object]:
    return {
        "error_code": error_code,
        "message": message,
        "unit_id": str(change.get("unit_id") or ""),
        "name": str(change.get("name") or ""),
        "read_by": str(change.get("read_by") or ""),
    }


def _env_name(line: str) -> str:
    match = _ENV_EXAMPLE_LINE.fullmatch(line)
    return match.group(1) if match else ""


def _safe_repo_path(root: Path, relative: str) -> Path:
    normalized = relative.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized:
        raise HarnessError("runtime_config_path_invalid", f"Unsafe runtime-config path: {relative}")
    path = (root / normalized).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HarnessError("runtime_config_path_invalid", f"Runtime-config path escapes the worktree: {relative}") from exc
    return path


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        temporary_path = Path(temporary)
        if temporary_path.exists():
            temporary_path.unlink()


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
