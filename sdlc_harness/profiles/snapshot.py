"""Snapshot profile bất biến gắn với vòng đời của một workflow run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..core.errors import HarnessError
from .model import LanguageProfile, ProjectProfile
from .registry import get_language_profile, get_project_profile


PROFILE_SNAPSHOT_STATE_KEY = "profile_snapshot"
PROFILE_SNAPSHOT_VERSION = "profile_snapshot_v1"


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

    def to_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "project_profile_id": self.project_profile_id,
            "project_profile_schema": self.project_profile_schema,
            "language_profile_id": self.language_profile_id,
            "language_profile_schema": self.language_profile_schema,
            "target_remote": self.target_remote,
            "target_base_branch": self.target_base_branch,
        }

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
        return project, language


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
