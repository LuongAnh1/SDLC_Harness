"""Lưu state, artifact path và lock cục bộ của workflow run.

RuntimeStore sở hữu layout ``.runtime``, active-run index và thao tác ghi an
toàn; lock serialize mutation theo source hoặc run. Package không định nghĩa
nghiệp vụ chuyển phase và không dùng event log làm nguồn state chính.
"""

from .locks import RuntimeActionLock
from .runtime import RuntimeStore, TERMINAL_STATUSES, utc_now_iso
from .runtime_readme import RUNTIME_LAYOUT_VERSION, RUNTIME_README, ensure_runtime_readme

__all__ = [
    "RuntimeActionLock",
    "RuntimeStore",
    "TERMINAL_STATUSES",
    "RUNTIME_LAYOUT_VERSION",
    "RUNTIME_README",
    "ensure_runtime_readme",
    "utc_now_iso",
]
