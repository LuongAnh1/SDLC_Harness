"""Snapshot profile bất biến gắn với vòng đời của một workflow run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re

from ..core.errors import HarnessError
from .model import LanguageProfile, ProjectProfile
from .registry import get_language_profile, get_project_profile


PROFILE_SNAPSHOT_STATE_KEY = "profile_snapshot"
PROFILE_SNAPSHOT_VERSION = "profile_snapshot_v1"
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _invalid(message: str, *, run_id: str | None = None) -> HarnessError:
    return HarnessError("profile_snapshot_invalid", message, run_id=run_id)


def _required_text(value: object, *, field_name: str, run_id: str | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"Profile snapshot field {field_name} must be non-empty canonical text.", run_id=run_id)
    return value


@dataclass(frozen=True)
class ProfileSnapshotV1:
    """Identity tối thiểu để mọi action của run dùng cùng một execution profile."""

    project_profile_id: str
    project_profile_schema: str
    language_profile_id: str
    language_profile_schema: str
    target_remote: str
    target_base_branch: str
    target_manifest_schema: str | None = None
    target_manifest_digest: str | None = None
    base_sha: str | None = None
    resolved_component_roots: tuple[tuple[str, tuple[str, ...]], ...] | None = None
    version: str = PROFILE_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        version = _required_text(self.version, field_name="version")
        if version != PROFILE_SNAPSHOT_VERSION:
            raise _invalid(f"Unsupported profile snapshot version: {version}")
        for field_name in (
            "project_profile_id",
            "project_profile_schema",
            "language_profile_id",
            "language_profile_schema",
            "target_remote",
            "target_base_branch",
        ):
            _required_text(getattr(self, field_name), field_name=field_name)

        binding = (
            self.target_manifest_schema,
            self.target_manifest_digest,
            self.base_sha,
            self.resolved_component_roots,
        )
        if any(value is not None for value in binding) and not all(value is not None for value in binding):
            raise _invalid("Profile snapshot workspace binding must be complete.")
        if self.target_manifest_schema is not None:
            _required_text(self.target_manifest_schema, field_name="target_manifest_schema")
            if not isinstance(self.target_manifest_digest, str) or not _SHA256_PATTERN.fullmatch(
                self.target_manifest_digest
            ):
                raise _invalid("Profile snapshot target_manifest_digest must be a sha256 digest.")
            if not isinstance(self.base_sha, str) or not _GIT_SHA_PATTERN.fullmatch(self.base_sha):
                raise _invalid("Profile snapshot base_sha must be a Git object id.")
            roots = self.resolved_component_roots
            if not isinstance(roots, tuple) or not roots:
                raise _invalid("Profile snapshot resolved_component_roots must not be empty.")
            component_ids: list[str] = []
            for item in roots:
                if not isinstance(item, tuple) or len(item) != 2:
                    raise _invalid("Profile snapshot resolved_component_roots has invalid entries.")
                component_id, editable_roots = item
                _required_text(component_id, field_name="resolved_component_roots.component_id")
                if not isinstance(editable_roots, tuple) or not editable_roots:
                    raise _invalid("Profile snapshot component roots must not be empty.")
                for root in editable_roots:
                    _required_text(root, field_name="resolved_component_roots.root")
                component_ids.append(component_id)
            if len(set(component_ids)) != len(component_ids):
                raise _invalid("Profile snapshot resolved_component_roots contains duplicate components.")
            object.__setattr__(
                self,
                "resolved_component_roots",
                tuple(sorted(roots, key=lambda item: item[0])),
            )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "project_profile_id": self.project_profile_id,
            "project_profile_schema": self.project_profile_schema,
            "language_profile_id": self.language_profile_id,
            "language_profile_schema": self.language_profile_schema,
            "target_remote": self.target_remote,
            "target_base_branch": self.target_base_branch,
        }
        if self.is_workspace_bound:
            payload.update(
                {
                    "target_manifest_schema": self.target_manifest_schema,
                    "target_manifest_digest": self.target_manifest_digest,
                    "base_sha": self.base_sha,
                    "resolved_component_roots": {
                        component_id: list(roots)
                        for component_id, roots in self.resolved_component_roots or ()
                    },
                }
            )
        return payload

    @property
    def is_workspace_bound(self) -> bool:
        return self.target_manifest_schema is not None

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        run_id: str | None = None,
    ) -> "ProfileSnapshotV1":
        if not isinstance(value, Mapping):
            raise _invalid("Profile snapshot must be a JSON object.", run_id=run_id)
        try:
            binding_fields = (
                "target_manifest_schema",
                "target_manifest_digest",
                "base_sha",
                "resolved_component_roots",
            )
            present_binding_fields = tuple(field for field in binding_fields if field in value)
            if present_binding_fields and len(present_binding_fields) != len(binding_fields):
                raise _invalid("Profile snapshot workspace binding must be complete.", run_id=run_id)
            resolved_roots = None
            if present_binding_fields:
                raw_roots = value.get("resolved_component_roots")
                if not isinstance(raw_roots, Mapping) or not raw_roots:
                    raise _invalid(
                        "Profile snapshot resolved_component_roots must be a non-empty object.",
                        run_id=run_id,
                    )
                parsed_roots: list[tuple[str, tuple[str, ...]]] = []
                for component_id, roots in raw_roots.items():
                    component_text = _required_text(
                        component_id,
                        field_name="resolved_component_roots.component_id",
                        run_id=run_id,
                    )
                    if not isinstance(roots, list) or not roots:
                        raise _invalid(
                            "Profile snapshot component roots must be a non-empty array.",
                            run_id=run_id,
                        )
                    parsed_roots.append(
                        (
                            component_text,
                            tuple(
                                _required_text(
                                    root,
                                    field_name="resolved_component_roots.root",
                                    run_id=run_id,
                                )
                                for root in roots
                            ),
                        )
                    )
                resolved_roots = tuple(parsed_roots)
            return cls(
                version=_required_text(value.get("version"), field_name="version", run_id=run_id),
                project_profile_id=_required_text(
                    value.get("project_profile_id"),
                    field_name="project_profile_id",
                    run_id=run_id,
                ),
                project_profile_schema=_required_text(
                    value.get("project_profile_schema"),
                    field_name="project_profile_schema",
                    run_id=run_id,
                ),
                language_profile_id=_required_text(
                    value.get("language_profile_id"),
                    field_name="language_profile_id",
                    run_id=run_id,
                ),
                language_profile_schema=_required_text(
                    value.get("language_profile_schema"),
                    field_name="language_profile_schema",
                    run_id=run_id,
                ),
                target_remote=_required_text(
                    value.get("target_remote"),
                    field_name="target_remote",
                    run_id=run_id,
                ),
                target_base_branch=_required_text(
                    value.get("target_base_branch"),
                    field_name="target_base_branch",
                    run_id=run_id,
                ),
                target_manifest_schema=(
                    _required_text(
                        value.get("target_manifest_schema"),
                        field_name="target_manifest_schema",
                        run_id=run_id,
                    )
                    if present_binding_fields
                    else None
                ),
                target_manifest_digest=(
                    _required_text(
                        value.get("target_manifest_digest"),
                        field_name="target_manifest_digest",
                        run_id=run_id,
                    )
                    if present_binding_fields
                    else None
                ),
                base_sha=(
                    _required_text(value.get("base_sha"), field_name="base_sha", run_id=run_id)
                    if present_binding_fields
                    else None
                ),
                resolved_component_roots=resolved_roots,
            )
        except HarnessError as exc:
            if run_id and exc.run_id is None:
                raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc
            raise

    def resolve_profiles(
        self,
        *,
        run_id: str | None = None,
    ) -> tuple[ProjectProfile, LanguageProfile]:
        try:
            project = get_project_profile(self.project_profile_id)
            language = get_language_profile(self.language_profile_id)
        except HarnessError as exc:
            raise HarnessError(
                "profile_snapshot_mismatch",
                "Profile snapshot references a profile that is no longer registered.",
                run_id=run_id,
            ) from exc
        if (
            project.schema_version != self.project_profile_schema
            or project.language_profile_id != self.language_profile_id
            or language.schema_version != self.language_profile_schema
        ):
            raise HarnessError(
                "profile_snapshot_mismatch",
                "Profile snapshot does not match the current profile registry.",
                run_id=run_id,
            )
        if self.is_workspace_bound:
            expected_roots = tuple(
                sorted(
                    (
                        (component.component_id, component.editable_roots)
                        for component in project.components
                    ),
                    key=lambda item: item[0],
                )
            )
            if (
                self.target_manifest_schema != project.target_manifest_schema
                or self.resolved_component_roots != expected_roots
            ):
                raise HarnessError(
                    "profile_snapshot_mismatch",
                    "Profile snapshot workspace binding does not match the current profile registry.",
                    run_id=run_id,
                )
        return project, language

    def bind_workspace(
        self,
        *,
        target_manifest_schema: str,
        target_manifest_digest: str,
        base_sha: str,
        resolved_component_roots: tuple[tuple[str, tuple[str, ...]], ...],
        run_id: str | None = None,
    ) -> "ProfileSnapshotV1":
        candidate = ProfileSnapshotV1(
            project_profile_id=self.project_profile_id,
            project_profile_schema=self.project_profile_schema,
            language_profile_id=self.language_profile_id,
            language_profile_schema=self.language_profile_schema,
            target_remote=self.target_remote,
            target_base_branch=self.target_base_branch,
            target_manifest_schema=target_manifest_schema,
            target_manifest_digest=target_manifest_digest,
            base_sha=base_sha,
            resolved_component_roots=resolved_component_roots,
        )
        candidate.resolve_profiles(run_id=run_id)
        if self.is_workspace_bound and candidate != self:
            raise HarnessError(
                "profile_snapshot_mismatch",
                "Profile snapshot workspace binding does not match the resolved workspace.",
                run_id=run_id,
            )
        return candidate


def build_profile_snapshot(
    *,
    project_profile_id: str,
    target_remote: str,
    target_base_branch: str,
) -> ProfileSnapshotV1:
    """Resolve registry identity once and freeze it for a newly created run."""

    project = get_project_profile(project_profile_id)
    language = get_language_profile(project.language_profile_id)
    snapshot = ProfileSnapshotV1(
        project_profile_id=project.profile_id,
        project_profile_schema=project.schema_version,
        language_profile_id=language.profile_id,
        language_profile_schema=language.schema_version,
        target_remote=str(target_remote).strip(),
        target_base_branch=str(target_base_branch).strip(),
    )
    validated = ProfileSnapshotV1.from_dict(snapshot.to_dict())
    validated.resolve_profiles()
    return validated


def read_profile_snapshot(
    state: Mapping[str, object],
    *,
    run_id: str | None = None,
) -> ProfileSnapshotV1 | None:
    raw_snapshot = state.get(PROFILE_SNAPSHOT_STATE_KEY)
    if raw_snapshot is None:
        return None
    snapshot = ProfileSnapshotV1.from_dict(raw_snapshot, run_id=run_id)
    snapshot.resolve_profiles(run_id=run_id)
    return snapshot


def profile_status_metadata(state: Mapping[str, object]) -> dict[str, object]:
    """Tạo public metadata mà không làm status/audit hỏng vì state lỗi."""

    try:
        snapshot = read_profile_snapshot(state, run_id=str(state.get("run_id") or "") or None)
    except HarnessError:
        return {
            "classification": "invalid",
            "snapshot_version": None,
            "project_profile_id": None,
            "language_profile_id": None,
        }
    if snapshot is None:
        return {
            "classification": "legacy_java_v1",
            "snapshot_version": None,
            "project_profile_id": None,
            "language_profile_id": None,
        }
    return {
        "classification": "profiled",
        "snapshot_version": snapshot.version,
        "project_profile_id": snapshot.project_profile_id,
        "language_profile_id": snapshot.language_profile_id,
    }
