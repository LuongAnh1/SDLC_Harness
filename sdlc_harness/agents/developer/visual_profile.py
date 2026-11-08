"""Deterministic visual-profile normalization for Developer UI execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ...core.errors import HarnessError


VISUAL_PROFILE_VERSION = "developer_visual_profile_v1"
VISUAL_PROFILE_STORE_VERSION = "developer_visual_profiles_v1"
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_RADIUS = re.compile(r"border-radius\s*:\s*([^;]+)", re.IGNORECASE)
_SHADOW = re.compile(r"box-shadow\s*:\s*([^;]+)", re.IGNORECASE)
_TOKEN_KEYS = (
    "background", "foreground", "card", "card_foreground", "primary", "on_primary",
    "secondary", "on_secondary", "accent", "on_accent", "muted", "muted_foreground",
    "border", "destructive", "on_destructive", "ring",
)
_CONTRAST_PAIRS = (
    ("background", "foreground", 4.5),
    ("card", "card_foreground", 4.5),
    ("primary", "on_primary", 4.5),
    ("accent", "on_accent", 4.5),
    ("destructive", "on_destructive", 4.5),
    ("success", "on_success", 4.5),
    ("background", "ring", 3.0),
)


def app_visual_scope_id(item: dict[str, object]) -> str:
    for binding in _dict_items(item.get("surface_contracts")):
        contract = _dict(binding.get("surface_contract"))
        runtime = _dict(contract.get("runtime_surface"))
        for raw in [runtime.get("entrypoint"), *_string_items(runtime.get("composition_path"))]:
            path = str(raw or "").split("#", 1)[0].replace("\\", "/").strip("/")
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] in {"apps", "packages"}:
                return f"web:{parts[0]}/{parts[1]}"
    for path in _item_paths(item):
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in {"apps", "packages"}:
            return f"web:{parts[0]}/{parts[1]}"
    return "web:repository"


def visual_style_target_policy(item: dict[str, object]) -> str:
    policies = {
        str(
            _dict(
                _dict(_dict(binding.get("surface_contract")).get("experience_contract")).get("visual_contract")
            ).get("style_target_policy")
            or ""
        ).strip()
        for binding in _dict_items(item.get("surface_contracts"))
        if str(binding.get("surface") or "").strip().lower() == "ui"
    }
    policies.discard("")
    if "new_stylesheet" in policies:
        return "new_stylesheet"
    if "reuse_existing" in policies:
        return "reuse_existing"
    return ""


def visual_profile_required(item: dict[str, object], capability: object) -> bool:
    cap = _dict(capability)
    has_ui = bool(app_visual_screen_ids(item)) or "ui" in {
        value.strip().lower() for value in _string_items(item.get("surfaces"))
    }
    return bool(
        has_ui
        and cap.get("visual_profile_mode") == "enforce"
        and visual_style_target_policy(item) == "new_stylesheet"
    )


def app_visual_screen_ids(item: dict[str, object]) -> list[str]:
    return list(
        dict.fromkeys(
            str(runtime.get("screen_id") or "").strip()
            for binding in _dict_items(item.get("surface_contracts"))
            if str(binding.get("surface") or "").strip().lower() == "ui"
            for runtime in [_dict(_dict(binding.get("surface_contract")).get("runtime_surface"))]
            if str(runtime.get("screen_id") or "").strip()
        )
    )


def normalize_visual_candidate(
    source: dict[str, object],
    *,
    app_scope_id: str,
    skill_fingerprint: str,
    design_refs: list[str],
) -> dict[str, object]:
    design = _dict(source.get("design_system"))
    colors = _dict(design.get("colors"))
    tokens: dict[str, str] = {}
    for key in _TOKEN_KEYS:
        value = str(colors.get(key) or "").upper()
        if not _HEX.fullmatch(value):
            raise HarnessError("developer_visual_profile_invalid", f"Visual profile token {key} is missing or invalid.")
        tokens[key] = value
    tokens.update(
        {
            "danger": tokens["destructive"],
            "on_danger": tokens["on_destructive"],
            "info": tokens["accent"],
            "on_info": tokens["on_accent"],
            "success": "#15803D",
            "on_success": "#FFFFFF",
        }
    )
    failures = [
        {"pair": [left, right], "ratio": round(_contrast(tokens[left], tokens[right]), 2), "minimum": minimum}
        for left, right, minimum in _CONTRAST_PAIRS
        if _contrast(tokens[left], tokens[right]) < minimum
    ]
    if failures:
        pairs = ", ".join("/".join(row["pair"]) for row in failures)
        raise HarnessError(
            "developer_visual_profile_invalid",
            f"Visual profile fails mechanical contrast checks for: {pairs}.",
        )

    style = _dict(design.get("style"))
    typography = _dict(design.get("typography"))
    enrichment = _dict_items(source.get("style_results"))
    enrichment_text = " ".join(" ".join(str(value) for value in row.values()) for row in enrichment)
    radius_match = _RADIUS.search(enrichment_text)
    shadow_match = _SHADOW.search(enrichment_text)
    heading = str(typography.get("heading") or "system-ui").strip()
    body = str(typography.get("body") or heading or "system-ui").strip()
    spacing = {
        str(key): str(value)
        for key, value in _dict(design.get("spacing_scale")).items()
        if str(key).strip() and str(value).strip()
    }
    query = str(source.get("query") or "").strip()
    payload: dict[str, object] = {
        "schema_version": VISUAL_PROFILE_VERSION,
        "source": "ui-ux-pro-max",
        "app_scope_id": app_scope_id,
        "skill_fingerprint": skill_fingerprint,
        "query": query,
        "design_refs": sorted(set(design_refs)),
        "style": {
            "id": str(style.get("id") or "").strip(),
            "name": str(style.get("name") or "").strip(),
            "keywords": str(style.get("keywords") or "").strip(),
            "effects": str(design.get("key_effects") or style.get("effects") or "").strip(),
        },
        "tokens": tokens,
        "typography": {
            "recommended_heading": heading,
            "recommended_body": body,
            "heading_stack": _font_stack(heading),
            "body_stack": _font_stack(body),
            "remote_font_import_allowed": False,
        },
        "spacing": spacing,
        "shape_and_effects": {
            "radius": radius_match.group(1).strip() if radius_match else "8px",
            "shadow": shadow_match.group(1).strip() if shadow_match else "0 1px 3px rgba(15, 23, 42, 0.12)",
        },
        "constraints": [
            "The exact approved Design UI contract overrides this presentation profile.",
            "Do not add remote font imports, CDN assets, animation libraries, or dependencies.",
            "Use semantic tokens; do not spread raw palette values through components.",
            "Keep motion subtle and honor prefers-reduced-motion.",
            "This profile never expands write scope, components, flows, states, or runtime behavior.",
        ],
        "contrast_checks": [
            {
                "pair": [left, right],
                "ratio": round(_contrast(tokens[left], tokens[right]), 2),
                "minimum": minimum,
            }
            for left, right, minimum in _CONTRAST_PAIRS
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    payload["candidate_id"] = "VP-" + digest.split(":", 1)[1][:12].upper()
    payload["digest"] = digest
    return payload


def read_visual_profiles(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": VISUAL_PROFILE_STORE_VERSION, "profiles": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError("developer_visual_profile_invalid", "Developer visual profile store is unreadable.") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != VISUAL_PROFILE_STORE_VERSION:
        raise HarnessError("developer_visual_profile_invalid", "Developer visual profile store has an invalid schema.")
    if not isinstance(payload.get("profiles"), dict):
        raise HarnessError("developer_visual_profile_invalid", "Developer visual profile store has an invalid profile map.")
    return payload


def _contrast(left: str, right: str) -> float:
    first, second = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def _luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _font_stack(name: str) -> str:
    clean = name.replace("'", "").replace('"', "").strip()
    return f"'{clean}', Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_items(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_items(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _item_paths(item: dict[str, object]) -> list[str]:
    return [str(path).replace("\\", "/") for path in _string_items(item.get("paths"))]
