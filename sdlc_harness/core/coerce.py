"""Lenient readers for JSON-shaped values: wrong types become empty, never an error."""

from __future__ import annotations

from typing import Any


def dict_items(value: object) -> list[dict[str, Any]]:
    """The dict elements of a list, as the same objects; anything else is []."""

    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def dict_copies(value: object) -> list[dict[str, Any]]:
    """Shallow copies of the dict elements of a list; anything else is []."""

    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def string_items(value: object) -> list[str]:
    """The non-blank elements of a list as stripped strings; anything else is []."""

    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def dict_value(value: object) -> dict[str, Any]:
    """The value itself when it is a dict, else {}."""

    return value if isinstance(value, dict) else {}
