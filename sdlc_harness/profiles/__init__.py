"""Contract profile ngôn ngữ và dự án cho target implementation."""

from .model import ComponentProfile, LanguageProfile, ProfileRegistry, ProjectProfile
from .manifest import (
    TARGET_PROFILE_MANIFEST_PATH,
    ResolvedTargetManifest,
    TargetProfileManifestV1,
    load_and_validate_target_manifest,
)
from .registry import get_language_profile, get_project_profile, list_project_profile_ids
from .snapshot import (
    PROFILE_SNAPSHOT_STATE_KEY,
    PROFILE_SNAPSHOT_VERSION,
    ProfileSnapshotV1,
    build_profile_snapshot,
    profile_status_metadata,
    read_profile_snapshot,
)
from .retirement import (
    LEGACY_RETIRED_ALLOWED_COMMANDS,
    public_allowed_actions,
    public_run_summary,
    require_project_profile_for_start,
    require_run_command_allowed,
)

__all__ = [
    "ComponentProfile",
    "LanguageProfile",
    "ProfileRegistry",
    "ProjectProfile",
    "ResolvedTargetManifest",
    "TargetProfileManifestV1",
    "TARGET_PROFILE_MANIFEST_PATH",
    "PROFILE_SNAPSHOT_STATE_KEY",
    "PROFILE_SNAPSHOT_VERSION",
    "ProfileSnapshotV1",
    "build_profile_snapshot",
    "get_language_profile",
    "get_project_profile",
    "list_project_profile_ids",
    "load_and_validate_target_manifest",
    "profile_status_metadata",
    "read_profile_snapshot",
    "LEGACY_RETIRED_ALLOWED_COMMANDS",
    "public_allowed_actions",
    "public_run_summary",
    "require_project_profile_for_start",
    "require_run_command_allowed",
]
