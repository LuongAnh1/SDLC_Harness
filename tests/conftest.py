"""Keep unit-test defaults independent from the machine's live SDLC config."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_live_workflow_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use stable legacy defaults unless a test explicitly supplies an override."""

    monkeypatch.setenv("SDLC_REQUIREMENT_CONTRACT_VERSION", "v1")
    monkeypatch.setenv("SDLC_DEVELOPER_EXECUTION_MODE", "legacy")
