"""Ghi event bền vững và truyền context quan sát cho một workflow run.

EventStore lưu timeline máy đọc được; context helper cho phép tầng đang chạy
phát event mà không truyền store qua mọi lời gọi. Event chỉ phục vụ quan sát và
không thay thế state nghiệp vụ trong ``storage``.
"""

from .context import (
    EventDraft,
    bind_current_run,
    current_event_context,
    emit_current,
    emit_for_run,
    event_scope,
    update_current_state,
)
from .store import DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT, EventStore

__all__ = [
    "DEFAULT_EVENT_LIMIT",
    "MAX_EVENT_LIMIT",
    "EventDraft",
    "EventStore",
    "bind_current_run",
    "current_event_context",
    "emit_current",
    "emit_for_run",
    "event_scope",
    "update_current_state",
]
