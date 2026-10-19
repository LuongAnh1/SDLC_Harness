"""Giữ mặc định unit test độc lập với cấu hình SDLC thật trên máy."""

from __future__ import annotations

from pathlib import Path

import pytest


INTEGRATION_MODULES = {
    "test_code_review_sync.py",
    "test_config_api.py",
    "test_events.py",
    "test_operations.py",
    "test_phase1.py",
    "test_read_api.py",
    "test_requirement_clarification_v2.py",
    "test_write_api.py",
}

JOURNEY_MODULES = {
    "test_design_consensus_experiment.py",
    "test_phase2.py",
}


@pytest.fixture(autouse=True)
def isolate_live_workflow_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use stable legacy defaults unless a test explicitly supplies an override."""

    monkeypatch.setenv("SDLC_DEVELOPER_EXECUTION_MODE", "legacy")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Phân tầng theo module để fast suite không chạy local infrastructure."""

    for item in items:
        module_name = Path(str(item.path)).name
        if module_name in INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)
        if module_name in JOURNEY_MODULES:
            item.add_marker(pytest.mark.journey)
