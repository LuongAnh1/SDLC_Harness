"""Khóa JSON contract của lệnh nhận nuôi workspace legacy."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main


class WorkspaceAdoptionCliTests(unittest.TestCase):
    def test_cli_routes_run_id_and_prints_json_only(self) -> None:
        response = {
            "ok": True,
            "run_id": "run_20261013_160421_f661babf",
            "adopted": True,
            "status": "code_review_ready",
        }
        stdout = io.StringIO()
        with patch("sdlc_harness.application.service.WorkspaceAdoptionWorkflow") as workflow_type:
            workflow_type.return_value.adopt.return_value = response
            with contextlib.redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "adopt-run-workspace",
                        "--run-id",
                        "run_20261013_160421_f661babf",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), response)
        workflow_type.return_value.adopt.assert_called_once_with(
            run_id="run_20261013_160421_f661babf"
        )


if __name__ == "__main__":
    unittest.main()
