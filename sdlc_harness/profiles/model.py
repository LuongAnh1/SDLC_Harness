"""Contract profile ngôn ngữ và dự án bất biến cho target implementation.

Profile là policy khai báo. Profile có thể nêu ID strategy hoặc verification
stage tương lai, nhưng không chứa shell command hay thực hiện lựa chọn runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Iterable, TypeVar

from ..core.errors import HarnessError


PROFILE_CONTRACT_ERROR = "profile_registry_invalid"
LANGUAGE_PROFILE_SCHEMA_VERSION = "language_profile_v1"
PROJECT_PROFILE_SCHEMA_VERSION = "project_profile_v1"
TARGET_MANIFEST_SCHEMA_VERSION = "target_profile_manifest_v1"
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[a-zA-Z]:")
_T = TypeVar("_T")


def _invalid(message: str) -> HarnessError:
    return HarnessError(PROFILE_CONTRACT_ERROR, message)


def _frozen_items(value: Iterable[_T], *, field_name: str) -> tuple[_T, ...]:
    if isinstance(value, (str, bytes)):
        raise _invalid(f"{field_name} must be a non-empty collection.")
    try:
        items = tuple(value)
    except TypeError as exc:
        raise _invalid(f"{field_name} must be a non-empty collection.") from exc
    if not items:
        raise _invalid(f"{field_name} must not be empty.")
    return items


def _require_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise _invalid(f"{field_name} must match {_IDENTIFIER_PATTERN.pattern}: {value!r}")
    return value


def _require_unique(values: tuple[str, ...], *, field_name: str) -> None:
    if len(set(values)) != len(values):
        raise _invalid(f"{field_name} contains duplicate values.")


def _require_repo_path(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"{field_name} must be a non-empty canonical repository path.")
    if (
        "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or _WINDOWS_DRIVE_PATTERN.match(value)
    ):
        raise _invalid(f"{field_name} must be a POSIX-relative repository path: {value!r}")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise _invalid(f"{field_name} contains an unsafe path segment: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise _invalid(f"{field_name} must be canonical: {value!r}")
    return value


def _require_suffix(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 2
        or not value.startswith(".")
        or value != value.strip()
        or "/" in value
        or "\\" in value
    ):
        raise _invalid(f"{field_name} contains an invalid file suffix: {value!r}")
    return value


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


@dataclass(frozen=True)
class LanguageProfile:
    """Language-specific syntax, prompt and static-analysis strategy IDs."""

    profile_id: str
    schema_version: str
    source_suffixes: tuple[str, ...]
    test_suffixes: tuple[str, ...]
    prompt_directive_id: str
    symbol_extractor_id: str
    diagnostic_classifier_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "profile_id",
            "schema_version",
            "prompt_directive_id",
            "symbol_extractor_id",
            "diagnostic_classifier_id",
        ):
            _require_identifier(getattr(self, field_name), field_name=field_name)
        if self.schema_version != LANGUAGE_PROFILE_SCHEMA_VERSION:
            raise _invalid(f"Unsupported language profile schema: {self.schema_version!r}")

        source_suffixes = _frozen_items(self.source_suffixes, field_name="source_suffixes")
        test_suffixes = _frozen_items(self.test_suffixes, field_name="test_suffixes")
        for suffix in source_suffixes:
            _require_suffix(suffix, field_name="source_suffixes")
        for suffix in test_suffixes:
            _require_suffix(suffix, field_name="test_suffixes")
        _require_unique(source_suffixes, field_name="source_suffixes")
        _require_unique(test_suffixes, field_name="test_suffixes")
        object.__setattr__(self, "source_suffixes", source_suffixes)
        object.__setattr__(self, "test_suffixes", test_suffixes)


@dataclass(frozen=True)
class ComponentProfile:
    """One project component and the repository roots an agent may edit."""

    component_id: str
    editable_roots: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.component_id, field_name="component_id")
        editable_roots = _frozen_items(self.editable_roots, field_name="editable_roots")
        for path in editable_roots:
            _require_repo_path(path, field_name="editable_roots")
        _require_unique(editable_roots, field_name="editable_roots")
        for index, left in enumerate(editable_roots):
            for right in editable_roots[index + 1 :]:
                if _paths_overlap(left, right):
                    raise _invalid(
                        f"Component {self.component_id!r} has overlapping editable roots: "
                        f"{left!r}, {right!r}"
                    )
        object.__setattr__(self, "editable_roots", editable_roots)


@dataclass(frozen=True)
class ProjectProfile:
    """Project topology and non-executable verification policy."""

    profile_id: str
    schema_version: str
    language_profile_id: str
    target_manifest_schema: str
    components: tuple[ComponentProfile, ...]
    protected_paths: tuple[str, ...]
    required_paths: tuple[str, ...]
    verification_stage_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("profile_id", "schema_version", "language_profile_id", "target_manifest_schema"):
            _require_identifier(getattr(self, field_name), field_name=field_name)
        if self.schema_version != PROJECT_PROFILE_SCHEMA_VERSION:
            raise _invalid(f"Unsupported project profile schema: {self.schema_version!r}")
        if self.target_manifest_schema != TARGET_MANIFEST_SCHEMA_VERSION:
            raise _invalid(f"Unsupported target manifest schema: {self.target_manifest_schema!r}")

        components = _frozen_items(self.components, field_name="components")
        if any(not isinstance(component, ComponentProfile) for component in components):
            raise _invalid("components must contain only ComponentProfile values.")
        component_ids = tuple(component.component_id for component in components)
        _require_unique(component_ids, field_name="component_ids")

        protected_paths = _frozen_items(self.protected_paths, field_name="protected_paths")
        required_paths = _frozen_items(self.required_paths, field_name="required_paths")
        stage_ids = _frozen_items(self.verification_stage_ids, field_name="verification_stage_ids")
        for path in protected_paths:
            _require_repo_path(path, field_name="protected_paths")
        for path in required_paths:
            _require_repo_path(path, field_name="required_paths")
        for stage_id in stage_ids:
            _require_identifier(stage_id, field_name="verification_stage_ids")
        _require_unique(protected_paths, field_name="protected_paths")
        _require_unique(required_paths, field_name="required_paths")
        _require_unique(stage_ids, field_name="verification_stage_ids")

        owned_roots: list[tuple[str, str]] = []
        for component in components:
            for root in component.editable_roots:
                for owner_id, owned_root in owned_roots:
                    if _paths_overlap(root, owned_root):
                        raise _invalid(
                            f"Editable root ownership overlaps between {owner_id!r} and "
                            f"{component.component_id!r}: {owned_root!r}, {root!r}"
                        )
                for protected_path in protected_paths:
                    if _paths_overlap(root, protected_path):
                        raise _invalid(
                            f"Editable root {root!r} overlaps protected path {protected_path!r}."
                        )
                owned_roots.append((component.component_id, root))

        object.__setattr__(self, "components", components)
        object.__setattr__(self, "protected_paths", protected_paths)
        object.__setattr__(self, "required_paths", required_paths)
        object.__setattr__(self, "verification_stage_ids", stage_ids)

    @property
    def editable_roots(self) -> tuple[str, ...]:
        return tuple(root for component in self.components for root in component.editable_roots)


@dataclass(frozen=True)
class ProfileRegistry:
    """Validated immutable registry for language and project profiles."""

    language_profiles: tuple[LanguageProfile, ...]
    project_profiles: tuple[ProjectProfile, ...]

    def __post_init__(self) -> None:
        language_profiles = _frozen_items(self.language_profiles, field_name="language_profiles")
        project_profiles = _frozen_items(self.project_profiles, field_name="project_profiles")
        if any(not isinstance(profile, LanguageProfile) for profile in language_profiles):
            raise _invalid("language_profiles must contain only LanguageProfile values.")
        if any(not isinstance(profile, ProjectProfile) for profile in project_profiles):
            raise _invalid("project_profiles must contain only ProjectProfile values.")

        language_ids = tuple(profile.profile_id for profile in language_profiles)
        project_ids = tuple(profile.profile_id for profile in project_profiles)
        _require_unique(language_ids, field_name="language_profile_ids")
        _require_unique(project_ids, field_name="project_profile_ids")
        known_language_ids = set(language_ids)
        for project in project_profiles:
            if project.language_profile_id not in known_language_ids:
                raise _invalid(
                    f"Project profile {project.profile_id!r} references unknown language profile "
                    f"{project.language_profile_id!r}."
                )

        object.__setattr__(self, "language_profiles", language_profiles)
        object.__setattr__(self, "project_profiles", project_profiles)

    def language(self, profile_id: str) -> LanguageProfile:
        for profile in self.language_profiles:
            if profile.profile_id == profile_id:
                return profile
        raise HarnessError(
            "language_profile_not_supported",
            f"Language profile is not supported: {profile_id}",
        )

    def project(self, profile_id: str) -> ProjectProfile:
        for profile in self.project_profiles:
            if profile.profile_id == profile_id:
                return profile
        raise HarnessError(
            "project_profile_not_supported",
            f"Project profile is not supported: {profile_id}",
        )

    def project_ids(self) -> tuple[str, ...]:
        return tuple(profile.profile_id for profile in self.project_profiles)
