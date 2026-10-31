from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from sdlc_harness.agents.developer.package_dependency import (
    ensure_workspace_dependency,
    workspace_dependency_status,
)
from sdlc_harness.core.errors import HarnessError


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _workspace(tmp_path: Path, *, satisfied: bool) -> dict[str, object]:
    dependency = "@example/storage"
    spec = "2.4.1"
    consumer_dependencies = {dependency: spec} if satisfied else {}
    _write_json(
        tmp_path / "apps/api/package.json",
        {"name": "@example/api", "version": "1.0.0", "dependencies": consumer_dependencies},
    )
    _write_json(
        tmp_path / "packages/db/package.json",
        {"name": dependency, "version": spec},
    )
    _write_json(
        tmp_path / "package-lock.json",
        {
            "name": "workspace",
            "lockfileVersion": 3,
            "packages": {
                "": {"workspaces": ["apps/*", "packages/*"]},
                "apps/api": {
                    "name": "@example/api",
                    "version": "1.0.0",
                    "dependencies": consumer_dependencies,
                },
                "packages/db": {"name": dependency, "version": spec},
                f"node_modules/{dependency}": {"resolved": "packages/db", "link": True},
            },
        },
    )
    return {
        "dependency_id": "DEP-EXAMPLE",
        "package_manager": "npm",
        "lockfile_path": "package-lock.json",
        "consumer_manifest_path": "apps/api/package.json",
        "consumer_workspace_path": "apps/api",
        "consumer_package_name": "@example/api",
        "dependency_name": dependency,
        "dependency_spec": spec,
        "provider_manifest_path": "packages/db/package.json",
        "provider_workspace_path": "packages/db",
        "provider_package_name": dependency,
    }


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)


def test_workspace_dependency_status_requires_manifest_and_lockfile(tmp_path: Path) -> None:
    requirement = _workspace(tmp_path, satisfied=False)

    status = workspace_dependency_status(
        repo_root=tmp_path,
        requirement=requirement,
        run_id="run_package_status",
    )

    assert status["status"] == "pending"
    assert status["manifest_spec"] == ""
    assert status["lockfile_spec"] == ""
    assert status["provider_link_valid"] is True


def test_ensure_workspace_dependency_uses_npm_and_synchronizes_contract(tmp_path: Path) -> None:
    requirement = _workspace(tmp_path, satisfied=False)
    _init_git(tmp_path)
    real_run = subprocess.run

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return real_run(command, **kwargs)
        consumer_path = tmp_path / "apps/api/package.json"
        lock_path = tmp_path / "package-lock.json"
        consumer = json.loads(consumer_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        consumer["dependencies"]["@example/storage"] = "2.4.1"
        lock["packages"]["apps/api"]["dependencies"]["@example/storage"] = "2.4.1"
        _write_json(consumer_path, consumer)
        _write_json(lock_path, lock)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with (
        patch("sdlc_harness.agents.developer.package_dependency.shutil.which", return_value="npm.cmd"),
        patch(
            "sdlc_harness.agents.developer.package_dependency.subprocess.run",
            side_effect=fake_run,
        ) as run,
    ):
        receipt = ensure_workspace_dependency(
            repo_root=tmp_path,
            requirement=requirement,
            run_id="run_package_ensure",
            timeout_seconds=120,
        )

    npm_command = next(call.args[0] for call in run.call_args_list if call.args[0][0] != "git")
    assert npm_command == [
        "npm.cmd",
        "install",
        "@example/storage@2.4.1",
        "--workspace",
        "@example/api",
        "--save-exact",
        "--package-lock-only",
        "--ignore-scripts",
        "--offline",
        "--no-audit",
        "--no-fund",
    ]
    assert receipt["operation"] == "materialized"
    assert receipt["changed_paths"] == ["apps/api/package.json", "package-lock.json"]


def test_ensure_workspace_dependency_is_noop_when_contract_is_satisfied(tmp_path: Path) -> None:
    requirement = _workspace(tmp_path, satisfied=True)

    with patch("sdlc_harness.agents.developer.package_dependency.subprocess.run") as run:
        receipt = ensure_workspace_dependency(
            repo_root=tmp_path,
            requirement=requirement,
            run_id="run_package_noop",
            timeout_seconds=120,
        )

    run.assert_not_called()
    assert receipt["operation"] == "noop"
    assert receipt["satisfied"] is True


def test_ensure_workspace_dependency_rolls_back_unexpected_npm_change(tmp_path: Path) -> None:
    requirement = _workspace(tmp_path, satisfied=False)
    readme = tmp_path / "README.md"
    readme.write_text("original\n", encoding="utf-8")
    _init_git(tmp_path)
    real_run = subprocess.run

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return real_run(command, **kwargs)
        consumer_path = tmp_path / "apps/api/package.json"
        lock_path = tmp_path / "package-lock.json"
        consumer = json.loads(consumer_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        consumer["dependencies"]["@example/storage"] = "2.4.1"
        lock["packages"]["apps/api"]["dependencies"]["@example/storage"] = "2.4.1"
        _write_json(consumer_path, consumer)
        _write_json(lock_path, lock)
        readme.write_text("unexpected\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    with (
        patch("sdlc_harness.agents.developer.package_dependency.shutil.which", return_value="npm.cmd"),
        patch(
            "sdlc_harness.agents.developer.package_dependency.subprocess.run",
            side_effect=fake_run,
        ),
        pytest.raises(HarnessError) as caught,
    ):
        ensure_workspace_dependency(
            repo_root=tmp_path,
            requirement=requirement,
            run_id="run_package_scope_rollback",
            timeout_seconds=120,
        )

    assert caught.value.error_code == "developer_package_dependency_scope_violation"
    assert readme.read_text(encoding="utf-8") == "original\n"
    status = workspace_dependency_status(
        repo_root=tmp_path,
        requirement=requirement,
        run_id="run_package_scope_rollback",
    )
    assert status["satisfied"] is False
