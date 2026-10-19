"""Contract profile ngôn ngữ và dự án cho target implementation."""

from .model import ComponentProfile, LanguageProfile, ProfileRegistry, ProjectProfile
from .registry import get_language_profile, get_project_profile, list_project_profile_ids
from .snapshot import (
    PROFILE_SNAPSHOT_STATE_KEY,
    PROFILE_SNAPSHOT_VERSION,
    ProfileSnapshotV1,
    build_profile_snapshot,
    profile_status_metadata,
    read_profile_snapshot,
)

__all__ = [
    "ComponentProfile",
    "LanguageProfile",
    "ProfileRegistry",
    "ProjectProfile",
    "PROFILE_SNAPSHOT_STATE_KEY",
    "PROFILE_SNAPSHOT_VERSION",
    "ProfileSnapshotV1",
    "build_profile_snapshot",
    "get_language_profile",
    "get_project_profile",
    "list_project_profile_ids",
    "profile_status_metadata",
    "read_profile_snapshot",
]
