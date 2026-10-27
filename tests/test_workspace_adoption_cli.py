"""Khóa JSON contract của lệnh nhận nuôi workspace legacy."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sdlc_harness.cli import main as cli_main
from sdlc_harness.profiles import build_profile_snapshot
from sdlc_harness.storage.runtime import RuntimeStore


class WorkspaceAdoptionCliTests(unittest.TestCase):
    def test_cli_routes_run_id_and_prints_json_only(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        runtime_root = Path(temp_dir.name) / ".runtime"
        run_id = "run_20261013_160421_f661babf"
        RuntimeStore(repo_root=repo_root, runtime_root=runtime_root).write_state(
            {
                "run_id": run_id,
                "designer_contract_version": "vertical_design_graph_v1",
                "profile_snapshot": build_profile_snapshot(
                    project_profile_id="typescript_fullstack_v1",
                    target_remote="https://github.com/example/target.git",
                    target_base_branch="main",
                ).to_dict(),
            }
        )
        response = {
            "ok": True,
            "run_id": run_id,
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
                        run_id,
                        "--runtime-root",
                        str(runtime_root),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), response)
        workflow_type.return_value.adopt.assert_called_once_with(
            run_id=run_id
        )


if __name__ == "__main__":
    unittest.main()
