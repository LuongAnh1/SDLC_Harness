"""Allowlisted static assets for the dependency-free Ops Dashboard."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates"
_ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/assets/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}


@lru_cache(maxsize=len(_ASSETS))
def read_dashboard_asset(path: str) -> tuple[bytes, str] | None:
    """Read one known dashboard asset; arbitrary filesystem paths are impossible."""

    asset = _ASSETS.get(path)
    if asset is None:
        return None
    filename, media_type = asset
    return (_TEMPLATE_ROOT / filename).read_bytes(), media_type
