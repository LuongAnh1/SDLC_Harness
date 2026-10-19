"""Đọc và xác minh handshake profile do target repository công bố."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
from typing import Mapping

from ..core.errors import HarnessError
from .model import ComponentProfile, LanguageProfile, ProjectProfile, TARGET_MANIFEST_SCHEMA_VERSION

TARGET_PROFILE_MANIFEST_PATH = ".sdlc/target-profile.json"
_MANIFEST_FIELDS = {
    "version",
    "project_profile_id",
    "project_profile_schema",
    "language_profile_id",
    "language_profile_schema",
    "components",
    "protected_paths",
    "required_paths",
    "verification_stage_ids",
}


def _invalid(message: str, *, run_id: str | None = None) -> HarnessError:
    return HarnessError("target_profile_manifest_invalid", message, run_id=run_id)


def _mismatch(message: str, *, run_id: str | None = None) -> HarnessError:
    return HarnessError("target_profile_manifest_mismatch", message, run_id=run_id)


def _text(value: object, *, field_name: str, run_id: str | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"Target profile manifest field {field_name} must be non-empty text.", run_id=run_id)
    return value


def _text_tuple(value: object, *, field_name: str, run_id: str | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _invalid(f"Target profile manifest field {field_name} must be a non-empty array.", run_id=run_id)
    items = tuple(_text(item, field_name=field_name, run_id=run_id) for item in value)
    if len(set(items)) != len(items):
        raise _invalid(f"Target profile manifest field {field_name} contains duplicates.", run_id=run_id)
    return items


@dataclass(frozen=True)
class TargetProfileManifestV1:
    """Bản assertion bất biến của target repo dành cho một project profile."""

    project_profile_id: str
    project_profile_schema: str
    language_profile_id: str
    language_profile_schema: str
    components: tuple[ComponentProfile, ...]
    protected_paths: tuple[str, ...]
    required_paths: tuple[str, ...]
    verification_stage_ids: tuple[str, ...]
    version: str = TARGET_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.version != TARGET_MANIFEST_SCHEMA_VERSION:
            raise _invalid(f"Unsupported target profile manifest version: {self.version}")

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        run_id: str | None = None,
    ) -> "TargetProfileManifestV1":
        if not isinstance(value, Mapping):
            raise _invalid("Target profile manifest must be a JSON object.", run_id=run_id)
        keys = set(value)
        if keys != _MANIFEST_FIELDS:
            missing = sorted(_MANIFEST_FIELDS - keys)
            unknown = sorted(keys - _MANIFEST_FIELDS)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unknown:
                details.append("unknown=" + ",".join(unknown))
            raise _invalid("Target profile manifest fields are invalid: " + "; ".join(details), run_id=run_id)

        raw_components = value.get("components")
        if not isinstance(raw_components, list) or not raw_components:
            raise _invalid("Target profile manifest components must be a non-empty array.", run_id=run_id)
        components: list[ComponentProfile] = []
        try:
            for index, item in enumerate(raw_components):
                if not isinstance(item, Mapping) or set(item) != {"component_id", "editable_roots"}:
                    raise _invalid(
                        f"Target profile manifest component {index} has invalid fields.",
                        run_id=run_id,
                    )
                components.append(
                    ComponentProfile(
                        component_id=_text(item.get("component_id"), field_name="component_id", run_id=run_id),
                        editable_roots=_text_tuple(
                            item.get("editable_roots"),
                            field_name="editable_roots",
                            run_id=run_id,
                        ),
                    )
                )
            return cls(
                version=_text(value.get("version"), field_name="version", run_id=run_id),
                project_profile_id=_text(
                    value.get("project_profile_id"), field_name="project_profile_id", run_id=run_id
                ),
                project_profile_schema=_text(
                    value.get("project_profile_schema"), field_name="project_profile_schema", run_id=run_id
                ),
                language_profile_id=_text(
                    value.get("language_profile_id"), field_name="language_profile_id", run_id=run_id
                ),
                language_profile_schema=_text(
                    value.get("language_profile_schema"), field_name="language_profile_schema", run_id=run_id
                ),
                components=tuple(components),
                protected_paths=_text_tuple(
                    value.get("protected_paths"), field_name="protected_paths", run_id=run_id
                ),
                required_paths=_text_tuple(value.get("required_paths"), field_name="required_paths", run_id=run_id),
                verification_stage_ids=_text_tuple(
                    value.get("verification_stage_ids"), field_name="verification_stage_ids", run_id=run_id
                ),
            )
        except HarnessError as exc:
            if exc.error_code == "profile_registry_invalid":
                raise _invalid(exc.message, run_id=run_id) from exc
            if run_id and exc.run_id is None:
                raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
            raise

    def validate_registry(
        self,
        *,
        project: ProjectProfile,
        language: LanguageProfile,
        run_id: str | None = None,
    ) -> None:
        expected = (
            project.target_manifest_schema,
            project.profile_id,
            project.schema_version,
            language.profile_id,
            language.schema_version,
            project.components,
            project.protected_paths,
            project.required_paths,
            project.verification_stage_ids,
        )
        actual = (
            self.version,
            self.project_profile_id,
            self.project_profile_schema,
            self.language_profile_id,
            self.language_profile_schema,
            self.components,
            self.protected_paths,
            self.required_paths,
            self.verification_stage_ids,
        )
        if actual != expected:
            raise _mismatch("Target profile manifest does not match the registered project profile.", run_id=run_id)


@dataclass(frozen=True)
class ResolvedTargetManifest:
    manifest: TargetProfileManifestV1
    digest: str
    component_roots: tuple[tuple[str, tuple[str, ...]], ...]


def _safe_existing_path(
    root: Path,
    relative_path: str,
    *,
    require_directory: bool,
    run_id: str | None,
) -> Path:
    candidate = root
    for part in PurePosixPath(relative_path).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise _mismatch(f"Target path contains a symbolic link: {relative_path}", run_id=run_id)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _mismatch(f"Required target path is missing or unsafe: {relative_path}", run_id=run_id) from exc
    if require_directory and not resolved.is_dir():
        raise _mismatch(f"Component root is not a directory: {relative_path}", run_id=run_id)
    return resolved


def load_and_validate_target_manifest(
    *,
    worktree_path: Path,
    project: ProjectProfile,
    language: LanguageProfile,
    run_id: str | None = None,
) -> ResolvedTargetManifest:
    """Đọc manifest trong worktree và đối chiếu registry cùng filesystem."""

    root = worktree_path.resolve()
    try:
        manifest_path = _safe_existing_path(
            root,
            TARGET_PROFILE_MANIFEST_PATH,
            require_directory=False,
            run_id=run_id,
        )
    except HarnessError as exc:
        raise _invalid(f"Target profile manifest is missing or unsafe: {TARGET_PROFILE_MANIFEST_PATH}", run_id=run_id) from exc
    if not manifest_path.is_file():
        raise _invalid(f"Target profile manifest is missing: {TARGET_PROFILE_MANIFEST_PATH}", run_id=run_id)
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid("Target profile manifest is not valid UTF-8 JSON.", run_id=run_id) from exc

    manifest = TargetProfileManifestV1.from_dict(payload, run_id=run_id)
    manifest.validate_registry(project=project, language=language, run_id=run_id)

    for required_path in project.required_paths:
        _safe_existing_path(root, required_path, require_directory=False, run_id=run_id)

    component_roots: list[tuple[str, tuple[str, ...]]] = []
    for component in project.components:
        for editable_root in component.editable_roots:
            _safe_existing_path(root, editable_root, require_directory=True, run_id=run_id)
        component_roots.append((component.component_id, component.editable_roots))

    return ResolvedTargetManifest(
        manifest=manifest,
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        component_roots=tuple(component_roots),
    )
