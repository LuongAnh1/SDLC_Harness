"""Kiểu cấu hình và lỗi nền tảng dùng chung toàn Harness.

Core chỉ chứa primitive ít phụ thuộc như Settings, schema cấu hình và
``HarnessError``. Import cấu hình được lazy-load để tránh vòng phụ thuộc với
``profiles``; logic workflow hoặc adapter bên ngoài không thuộc package này.
"""

from typing import TYPE_CHECKING, Any

from .errors import HarnessError

if TYPE_CHECKING:
    from .config import Settings

__all__ = ["Settings", "load_settings", "repo_root", "HarnessError"]


def __getattr__(name: str) -> Any:
    """Giữ public import cũ mà không tạo vòng core.config -> profiles -> core."""

    if name in {"Settings", "load_settings", "repo_root"}:
        from .config import Settings, load_settings, repo_root

        exports = {
            "Settings": Settings,
            "load_settings": load_settings,
            "repo_root": repo_root,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
