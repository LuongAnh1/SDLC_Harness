"""Kiểm thử public package imports trong Python process sạch."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


class ImportContractTests(unittest.TestCase):
    def test_profiles_and_core_public_exports_import_without_order_dependency(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        script = (
            "from sdlc_harness.profiles import get_project_profile; "
            "from sdlc_harness.core import Settings, HarnessError, load_settings, repo_root; "
            "assert get_project_profile('typescript_fullstack_v1').profile_id == 'typescript_fullstack_v1'; "
            "assert Settings and HarnessError and load_settings and repo_root"
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
