"""Contract profile ngôn ngữ và dự án cho target implementation."""

from .model import ComponentProfile, LanguageProfile, ProfileRegistry, ProjectProfile
from .registry import get_language_profile, get_project_profile, list_project_profile_ids

__all__ = [
    "ComponentProfile",
    "LanguageProfile",
    "ProfileRegistry",
    "ProjectProfile",
    "get_language_profile",
    "get_project_profile",
    "list_project_profile_ids",
]
