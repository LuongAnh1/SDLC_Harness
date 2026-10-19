"""Giữ mặc định unit test độc lập với cấu hình SDLC thật trên máy."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_live_workflow_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use stable legacy defaults unless a test explicitly supplies an override."""

    monkeypatch.setenv("SDLC_DEVELOPER_EXECUTION_MODE", "legacy")
