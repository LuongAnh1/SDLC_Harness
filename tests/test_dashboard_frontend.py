"""Chạy kiểm thử view-model frontend không phụ thuộc bằng runner có sẵn của Node."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_javascript_view_models() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; browser smoke remains the frontend gate.")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/frontend/dashboard.test.mjs"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
