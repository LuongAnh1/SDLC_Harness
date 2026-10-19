"""Kiểm thử contract manifest và binding workspace của project profile."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.profiles import (
    TargetProfileManifestV1,
    build_profile_snapshot,
    get_language_profile,
    get_project_profile,
    load_and_validate_target_manifest,
)


def manifest_payload() -> dict[str, object]:
    project = get_project_profile("typescript_fullstack_v1")
    language = get_language_profile(project.language_profile_id)
    return {
        "version": project.target_manifest_schema,
        "project_profile_id": project.profile_id,
        "project_profile_schema": project.schema_version,
        "language_profile_id": language.profile_id,
        "language_profile_schema": language.schema_version,
        "components": [
            {
                "component_id": component.component_id,
                "editable_roots": list(component.editable_roots),
            }
            for component in project.components
        ],
        "protected_paths": list(project.protected_paths),
        "required_paths": list(project.required_paths),
        "verification_stage_ids": list(project.verification_stage_ids),
    }


def write_profiled_target(root: Path, *, payload: dict[str, object] | None = None) -> bytes:
    project = get_project_profile("typescript_fullstack_v1")
    for required_path in project.required_paths:
        path = root / required_path
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)
    for component in project.components:
        for editable_root in component.editable_roots:
            (root / editable_root).mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload or manifest_payload(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest_path = root / ".sdlc" / "target-profile.json"
    manifest_path.write_bytes(raw)
    return raw


class TargetProfileManifestTests(unittest.TestCase):
    def test_manifest_round_trip_matches_registry_and_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = write_profiled_target(root)
            project = get_project_profile("typescript_fullstack_v1")
            language = get_language_profile(project.language_profile_id)

            resolved = load_and_validate_target_manifest(
                worktree_path=root,
                project=project,
                language=language,
                run_id="run_profile",
            )

        self.assertIsInstance(resolved.manifest, TargetProfileManifestV1)
        self.assertEqual(resolved.digest, "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            resolved.component_roots,
            tuple((component.component_id, component.editable_roots) for component in project.components),
        )

    def test_manifest_rejects_unknown_fields_versions_and_registry_mismatch(self) -> None:
        cases = []
        unknown = manifest_payload()
        unknown["future"] = True
        cases.append((unknown, "target_profile_manifest_invalid"))
        version = manifest_payload()
        version["version"] = "target_profile_manifest_v2"
        cases.append((version, "target_profile_manifest_invalid"))
        profile = manifest_payload()
        profile["project_profile_id"] = "other_profile_v1"
        cases.append((profile, "target_profile_manifest_mismatch"))
        components = manifest_payload()
        components["components"] = list(reversed(components["components"]))  # type: ignore[arg-type]
        cases.append((components, "target_profile_manifest_mismatch"))

        project = get_project_profile("typescript_fullstack_v1")
        language = get_language_profile(project.language_profile_id)
        for payload, error_code in cases:
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_profiled_target(root, payload=payload)
                with self.assertRaises(HarnessError) as context:
                    load_and_validate_target_manifest(
                        worktree_path=root,
                        project=project,
                        language=language,
                        run_id="run_profile",
                    )
                self.assertEqual(context.exception.error_code, error_code)

    def test_manifest_rejects_missing_required_path_and_component_root(self) -> None:
        project = get_project_profile("typescript_fullstack_v1")
        language = get_language_profile(project.language_profile_id)
        for missing in ("package-lock.json", "apps/web/src"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                write_profiled_target(root)
                path = root / missing
                if path.is_dir():
                    path.rmdir()
                else:
                    path.unlink()
                with self.assertRaises(HarnessError) as context:
                    load_and_validate_target_manifest(
                        worktree_path=root,
                        project=project,
                        language=language,
                        run_id="run_profile",
                    )
                self.assertEqual(context.exception.error_code, "target_profile_manifest_mismatch")

    def test_snapshot_workspace_binding_is_all_or_none_and_immutable(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        )
        project = get_project_profile("typescript_fullstack_v1")
        roots = tuple((component.component_id, component.editable_roots) for component in project.components)
        bound = snapshot.bind_workspace(
            target_manifest_schema=project.target_manifest_schema,
            target_manifest_digest="sha256:" + "a" * 64,
            base_sha="b" * 40,
            resolved_component_roots=roots,
        )

        self.assertTrue(bound.is_workspace_bound)
        self.assertEqual(bound, type(bound).from_dict(bound.to_dict()))
        with self.assertRaises(HarnessError) as partial:
            type(bound).from_dict({**snapshot.to_dict(), "base_sha": "b" * 40})
        self.assertEqual(partial.exception.error_code, "profile_snapshot_invalid")
        with self.assertRaises(HarnessError) as changed:
            bound.bind_workspace(
                target_manifest_schema=project.target_manifest_schema,
                target_manifest_digest="sha256:" + "c" * 64,
                base_sha="b" * 40,
                resolved_component_roots=roots,
                run_id="run_profile",
            )
        self.assertEqual(changed.exception.error_code, "profile_snapshot_mismatch")

    def test_snapshot_component_root_object_order_is_semantically_irrelevant(self) -> None:
        snapshot = build_profile_snapshot(
            project_profile_id="typescript_fullstack_v1",
            target_remote="https://github.com/example/target.git",
            target_base_branch="main",
        )
        project = get_project_profile("typescript_fullstack_v1")
        roots = tuple((component.component_id, component.editable_roots) for component in project.components)
        bound = snapshot.bind_workspace(
            target_manifest_schema=project.target_manifest_schema,
            target_manifest_digest="sha256:" + "a" * 64,
            base_sha="b" * 40,
            resolved_component_roots=roots,
        )
        payload = bound.to_dict()
        serialized_roots = payload["resolved_component_roots"]
        self.assertIsInstance(serialized_roots, dict)
        payload["resolved_component_roots"] = dict(reversed(list(serialized_roots.items())))

        restored = type(bound).from_dict(payload)

        self.assertEqual(restored, bound)
        restored.resolve_profiles(run_id="run_profile")


if __name__ == "__main__":
    unittest.main()
