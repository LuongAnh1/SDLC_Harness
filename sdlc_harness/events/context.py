"""Context event cục bộ của execution dùng chung tại biên CLI, API và workflow."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Iterator

from .store import EventStore


_LOGGER = logging.getLogger("sdlc_harness.events")


@dataclass
class EventDraft:
    category: str
    event_type: str
    message: str
    data: dict[str, object] = field(default_factory=dict)
    phase: str | None = None
    status: str | None = None
    timestamp: str | None = None


@dataclass
class EventContext:
    store: EventStore
    command: str
    operation_id: str | None = None
    run_id: str | None = None
    phase: str | None = None
    status: str | None = None
    buffered: list[EventDraft] = field(default_factory=list)


_CURRENT: ContextVar[EventContext | None] = ContextVar("sdlc_event_context", default=None)


@contextmanager
def event_scope(
    *,
    runtime_root: Path,
    command: str,
    operation_id: str | None = None,
    run_id: str | None = None,
    buffered: list[EventDraft] | None = None,
) -> Iterator[EventContext]:
    current = _CURRENT.get()
    if current is not None:
        if operation_id and not current.operation_id:
            current.operation_id = operation_id
        if run_id:
            bind_current_run(run_id)
        yield current
        return
    context = EventContext(
        store=EventStore(runtime_root=runtime_root),
        command=command,
        operation_id=operation_id,
        run_id=run_id,
        buffered=list(buffered or []),
    )
    token = _CURRENT.set(context)
    try:
        if run_id:
            _flush(context)
        yield context
    finally:
        _CURRENT.reset(token)


def current_event_context() -> EventContext | None:
    return _CURRENT.get()


def bind_current_run(run_id: str) -> None:
    context = _CURRENT.get()
    if context is None:
        return
    context.run_id = run_id
    _flush(context)


def update_current_state(*, phase: str | None, status: str | None) -> None:
    context = _CURRENT.get()
    if context is None:
        return
    context.phase = phase
    context.status = status


def emit_current(
    *,
    category: str,
    event_type: str,
    message: str,
    data: dict[str, object] | None = None,
    phase: str | None = None,
    status: str | None = None,
    timestamp: str | None = None,
) -> None:
    context = _CURRENT.get()
    if context is None:
        return
    draft = EventDraft(
        category=category,
        event_type=event_type,
        message=message,
        data=dict(data or {}),
        phase=phase,
        status=status,
        timestamp=timestamp,
    )
    if not context.run_id:
        context.buffered.append(draft)
        return
    _append(context, draft)


def emit_for_run(
    *,
    runtime_root: Path,
    run_id: str,
    category: str,
    event_type: str,
    message: str,
    data: dict[str, object] | None = None,
    operation_id: str | None = None,
    phase: str | None = None,
    status: str | None = None,
    timestamp: str | None = None,
) -> None:
    try:
        EventStore(runtime_root=runtime_root).append(
            run_id=run_id,
            category=category,
            event_type=event_type,
            phase=phase,
            status=status,
            message=message,
            data=data,
            operation_id=operation_id,
            timestamp=timestamp,
        )
    except Exception:
        _LOGGER.exception("Event persistence failed for %s/%s", run_id, event_type)


def _flush(context: EventContext) -> None:
    if not context.run_id:
        return
    pending = list(context.buffered)
    context.buffered.clear()
    for draft in pending:
        _append(context, draft)


def _append(context: EventContext, draft: EventDraft) -> None:
    if not context.run_id:
        return
    try:
        context.store.append(
            run_id=context.run_id,
            category=draft.category,
            event_type=draft.event_type,
            phase=draft.phase if draft.phase is not None else context.phase,
            status=draft.status if draft.status is not None else context.status,
            message=draft.message,
            data=draft.data,
            operation_id=context.operation_id,
            timestamp=draft.timestamp,
        )
    except Exception:
        _LOGGER.exception("Event persistence failed for %s/%s", context.run_id, draft.event_type)
