"""Kiểm thử contract và scanner profile-bounded của L2 Repo Discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

import sdlc_harness.repo_context.landscape as landscape_module
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import build_profile_snapshot, get_project_profile
from sdlc_harness.repo_context import build_repo_landscape, validate_repo_landscape
from sdlc_harness.workspaces import RunWorkspaceRecord


RUN_ID = "run_20261026_120000_a1b2c3d4"


def _target(tmp_path: Path) -> tuple[Path, object, RunWorkspaceRecord]:
    root = tmp_path / "target"
    profile = get_project_profile("typescript_fullstack_v1")
    for required in profile.required_paths:
        path = root / required
        if Path(required).suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    for component in profile.components:
        for editable_root in component.editable_roots:
            (root / editable_root).mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        '{"packageManager":"npm@11.16.0","workspaces":["apps/*","packages/*"]}\n',
        encoding="utf-8",
    )
    (root / "apps/web/src/main.tsx").write_text(
        "export function AppShell() { return null; }\n",
        encoding="utf-8",
    )
    (root / "apps/api/src/app.ts").write_text(
        'export function createApp() {}\napp.get("/health", async () => ({}));\n',
        encoding="utf-8",
    )
    (root / "apps/api/src/app.test.ts").write_text("export {};\n", encoding="utf-8")
    (root / "packages/db/prisma/schema.prisma").write_text(
        "model SystemMetadata {\n  key String @id\n}\n",
        encoding="utf-8",
    )
    migration = root / "packages/db/prisma/migrations/001_init/migration.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text("CREATE TABLE metadata();\n", encoding="utf-8")

    roots = tuple((component.component_id, component.editable_roots) for component in profile.components)
    snapshot = build_profile_snapshot(
        project_profile_id=profile.profile_id,
        target_remote="https://github.com/example/target.git",
        target_base_branch="main",
    ).bind_workspace(
        target_manifest_schema=profile.target_manifest_schema,
        target_manifest_digest="sha256:" + "a" * 64,
        base_sha="b" * 40,
        resolved_component_roots=roots,
        run_id=RUN_ID,
    )
    workspace = RunWorkspaceRecord(
        run_id=RUN_ID,
        source_ref="dataset_use_case:etour:UC2",
        canonical_repo=str(root),
        worktree_path=str(root),
        branch="feature/uc2-a1b2c3d4",
        base_ref="origin/main",
        base_sha="b" * 40,
        head_sha="b" * 40,
        remote="https://github.com/example/target.git",
        lifecycle="active",
        provisioning_mode="created_for_run",
        created_at="2026-10-26T12:00:00Z",
        updated_at="2026-10-26T12:00:00Z",
    )
    return root, snapshot, workspace


def _requirement() -> dict[str, object]:
    return {
        "scenario_contract": {
            "scenarios": [
                {
                    "id": "SC-1",
                    "name": "Health status",
                    "surfaces": {
                        name: {"status": "required"}
                        for name in ("ui", "api", "business_rule", "data")
                    },
                }
            ]
        }
    }


def test_builds_deterministic_landscape_with_cross_layer_anchors(tmp_path: Path) -> None:
    root, snapshot, workspace = _target(tmp_path)

    first = build_repo_landscape(
        run_id=RUN_ID,
        target_path=root,
        workspace=workspace,
        snapshot=snapshot,
        requirement_model=_requirement(),
    )
    second = build_repo_landscape(
        run_id=RUN_ID,
        target_path=root,
        workspace=workspace,
        snapshot=snapshot,
        requirement_model=_requirement(),
    )

    assert first == second
    assert first["schema_version"] == "repo_landscape_v1"
    assert first["artifact_digest"].startswith("sha256:")
    kinds = {item["kind"] for item in first["anchors"]}
    assert {"ui_entrypoint", "ui_component", "api_route", "data_model", "migration", "test"} <= kinds
    assert first["discovery"]["mode"] == "bounded_profile_scan"
    assert first["summary"]["unmatched_surface_count"] == 0


def test_uses_existing_codegraph_only_as_bounded_enrichment(tmp_path: Path) -> None:
    root, snapshot, workspace = _target(tmp_path)
    (root / ".codegraph").mkdir()
    calls: list[str] = []

    result = build_repo_landscape(
        run_id=RUN_ID,
        target_path=root,
        workspace=workspace,
        snapshot=snapshot,
        requirement_model=_requirement(),
        codegraph_query=lambda _, query: calls.append(query) or "apps/api/src/app.ts:createApp",
    )

    assert calls == ["Health status"]
    assert result["discovery"]["mode"] == "codegraph_enriched"
    assert result["discovery"]["codegraph"]["matched_anchor_refs"]
    assert any(item["evidence_source"].startswith("codegraph+") for item in result["anchors"])


def test_symbol_inspection_stops_at_deterministic_byte_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, snapshot, workspace = _target(tmp_path)
    monkeypatch.setattr(landscape_module, "MAX_INSPECTION_BYTES", 1)

    result = build_repo_landscape(
        run_id=RUN_ID,
        target_path=root,
        workspace=workspace,
        snapshot=snapshot,
        requirement_model=_requirement(),
    )

    assert result["summary"]["file_count"] > 0
    assert result["summary"]["inspected_bytes"] <= 1
    assert "inspection_budget_truncated" in {item["code"] for item in result["uncertainties"]}


def test_rejects_workspace_drift_and_tampered_artifact(tmp_path: Path) -> None:
    root, snapshot, workspace = _target(tmp_path)
    drifted = RunWorkspaceRecord(**{**workspace.__dict__, "head_sha": "c" * 40})
    with pytest.raises(HarnessError) as drift:
        build_repo_landscape(
            run_id=RUN_ID,
            target_path=root,
            workspace=drifted,
            snapshot=snapshot,
            requirement_model=_requirement(),
        )
    assert drift.value.error_code == "repo_discovery_workspace_drift"

    result = build_repo_landscape(
        run_id=RUN_ID,
        target_path=root,
        workspace=workspace,
        snapshot=snapshot,
        requirement_model=_requirement(),
    )
    result["anchors"][0]["path"] = "../escape.ts"
    with pytest.raises(HarnessError) as invalid:
        validate_repo_landscape(result, expected_run_id=RUN_ID)
    assert invalid.value.error_code == "repo_landscape_invalid"


def test_rejects_artifact_that_does_not_match_expected_frozen_snapshot(tmp_path: Path) -> None:
    root, snapshot, workspace = _target(tmp_path)
    result = build_repo_landscape(
        run_id=RUN_ID,
        target_path=root,
        workspace=workspace,
        snapshot=snapshot,
        requirement_model=_requirement(),
    )

    with pytest.raises(HarnessError) as invalid:
        validate_repo_landscape(
            result,
            expected_run_id=RUN_ID,
            expected_snapshot={"base_sha": "c" * 40},
        )
    assert invalid.value.error_code == "repo_landscape_invalid"
