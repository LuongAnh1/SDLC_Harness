"""Kiểm thử source discovery theo immutable project/language profile."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_profile_snapshot, get_project_profile
from sdlc_harness.repo_context import discover_profile_source_files
from sdlc_harness.workflows.phase3 import _target_source_files


RUN_ID = "run_20261020_120000_discovery"


def _workspace_bound_snapshot(target_path: Path):
    project = get_project_profile("typescript_fullstack_v1")
    for component in project.components:
        for editable_root in component.editable_roots:
            (target_path / editable_root).mkdir(parents=True, exist_ok=True)
    roots = tuple(
        (component.component_id, component.editable_roots)
        for component in project.components
    )
    return build_profile_snapshot(
        project_profile_id=project.profile_id,
        target_remote="https://github.com/example/typescript-target.git",
        target_base_branch="main",
    ).bind_workspace(
        target_manifest_schema=project.target_manifest_schema,
        target_manifest_digest="sha256:" + "a" * 64,
        base_sha="b" * 40,
        resolved_component_roots=roots,
        run_id=RUN_ID,
    )


def test_discovers_profile_source_and_test_files_in_stable_posix_order(tmp_path: Path) -> None:
    snapshot = _workspace_bound_snapshot(tmp_path)
    included = (
        "apps/web/src/App.tsx",
        "apps/web/src/App.test.tsx",
        "apps/api/src/service.ts",
        "apps/api/src/service.spec.ts",
        "packages/contracts/src/schema.ts",
        "packages/db/src/client.ts",
        "tests/e2e/feature.spec.ts",
    )
    for relative in reversed(included):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n", encoding="utf-8")
    for relative in (
        "Outside.ts",
        "apps/api/src/Legacy.java",
        "apps/web/src/helper.js",
        "packages/db/prisma/schema.prisma",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignored\n", encoding="utf-8")

    discovered = discover_profile_source_files(
        target_path=tmp_path,
        snapshot=snapshot,
        run_id=RUN_ID,
    )

    assert discovered == sorted(included)
    assert all("\\" not in path for path in discovered)
    assert _target_source_files(
        tmp_path,
        state={"profile_snapshot": snapshot.to_dict()},
        run_id=RUN_ID,
    ) == discovered


def test_allows_empty_profile_source_roots(tmp_path: Path) -> None:
    snapshot = _workspace_bound_snapshot(tmp_path)

    assert discover_profile_source_files(
        target_path=tmp_path,
        snapshot=snapshot,
        run_id=RUN_ID,
    ) == []


def test_rejects_snapshot_before_workspace_binding(tmp_path: Path) -> None:
    snapshot = build_profile_snapshot(
        project_profile_id="typescript_fullstack_v1",
        target_remote="https://github.com/example/typescript-target.git",
        target_base_branch="main",
    )

    with pytest.raises(HarnessError) as context:
        discover_profile_source_files(
            target_path=tmp_path,
            snapshot=snapshot,
            run_id=RUN_ID,
        )

    assert context.value.error_code == "profile_snapshot_mismatch"
    assert context.value.run_id == RUN_ID


def test_rejects_missing_profile_source_root(tmp_path: Path) -> None:
    snapshot = _workspace_bound_snapshot(tmp_path)
    missing = tmp_path / "apps" / "api" / "src"
    missing.rmdir()

    with pytest.raises(HarnessError) as context:
        discover_profile_source_files(
            target_path=tmp_path,
            snapshot=snapshot,
            run_id=RUN_ID,
        )

    assert context.value.error_code == "target_source_path_invalid"


def test_rejects_source_symlink_that_escapes_approved_root(tmp_path: Path) -> None:
    snapshot = _workspace_bound_snapshot(tmp_path)
    outside = tmp_path / "outside.ts"
    outside.write_text("export {};\n", encoding="utf-8")
    link = tmp_path / "apps" / "api" / "src" / "escape.ts"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        unsupported_errnos = {
            code
            for code in (
                errno.EACCES,
                errno.EPERM,
                getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None),
            )
            if code is not None
        }
        if exc.errno in unsupported_errnos or (
            os.name == "nt" and getattr(exc, "winerror", None) in {5, 1314}
        ):
            pytest.skip("Môi trường hiện tại không cho phép tạo symlink.")
        raise

    with pytest.raises(HarnessError) as context:
        discover_profile_source_files(
            target_path=tmp_path,
            snapshot=snapshot,
            run_id=RUN_ID,
        )

    assert context.value.error_code == "target_source_path_invalid"
