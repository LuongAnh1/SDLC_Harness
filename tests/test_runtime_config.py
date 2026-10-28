"""Kiểm thử thứ tự ưu tiên, lưu trữ, che dữ liệu và xác thực cấu hình runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sdlc_harness.application.config_service import ConfigurationService
from sdlc_harness.core.config import load_settings
from sdlc_harness.core.config_schema import config_field_index
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.core.runtime_config import RuntimeConfigStore


def field_by_name(snapshot: dict[str, object], name: str) -> dict[str, object]:
    for section in snapshot["sections"]:
        for field in section["fields"]:
            if field["name"] == name:
                return field
    raise AssertionError(f"Missing config field: {name}")


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        self.runtime_root = self.root / ".runtime"
        self.config_path = self.runtime_root / "config.json"
        self.service = ConfigurationService(root=self.root, runtime_root=self.runtime_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_precedence_is_default_dotenv_runtime_os_then_override(self) -> None:
        (self.root / ".env").write_text(
            "SDLC_LLM_MODEL=dotenv-model\nSDLC_DESIGN_REPAIR_LIMIT=1\n",
            encoding="utf-8",
        )
        RuntimeConfigStore(self.config_path).update(
            {"SDLC_LLM_MODEL": "runtime-model", "SDLC_DESIGN_REPAIR_LIMIT": 3}
        )
        with patch.dict(os.environ, {"SDLC_LLM_MODEL": "os-model"}):
            settings = load_settings(root=self.root, runtime_root=self.runtime_root)
            overridden = load_settings(
                root=self.root,
                runtime_root=self.runtime_root,
                overrides={"SDLC_LLM_MODEL": "invocation-model"},
            )
        self.assertEqual(settings.llm_model, "os-model")
        self.assertEqual(settings.design_repair_limit, 3)
        self.assertEqual(overridden.llm_model, "invocation-model")
        self.assertEqual(settings.runtime_config_path, self.config_path.resolve())

    def test_project_profile_defaults_to_typescript_and_uses_normal_config_precedence(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            default = load_settings(root=self.root, runtime_root=self.runtime_root)
            self.assertEqual(default.project_profile_id, "typescript_fullstack_v1")

            (self.root / ".env").write_text(
                "SDLC_PROJECT_PROFILE=\n",
                encoding="utf-8",
            )
            from_dotenv = load_settings(root=self.root, runtime_root=self.runtime_root)
            self.assertEqual(from_dotenv.project_profile_id, "typescript_fullstack_v1")

            RuntimeConfigStore(self.config_path).update(
                {"SDLC_PROJECT_PROFILE": "typescript_fullstack_v1"}
            )
            from_runtime = load_settings(root=self.root, runtime_root=self.runtime_root)
            self.assertEqual(from_runtime.project_profile_id, "typescript_fullstack_v1")

            with patch.dict(os.environ, {"SDLC_PROJECT_PROFILE": ""}):
                from_os = load_settings(root=self.root, runtime_root=self.runtime_root)
                overridden = load_settings(
                    root=self.root,
                    runtime_root=self.runtime_root,
                    overrides={"SDLC_PROJECT_PROFILE": "typescript_fullstack_v1"},
                )
            self.assertEqual(from_os.project_profile_id, "typescript_fullstack_v1")
            self.assertEqual(overridden.project_profile_id, "typescript_fullstack_v1")

    def test_cutover_defaults_select_typescript_target_and_tool_loop(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(root=self.root, runtime_root=self.runtime_root)

        target = (self.root.parent / "SDLC_Target_TypeScript").resolve()
        self.assertEqual(settings.project_profile_id, "typescript_fullstack_v1")
        self.assertNotIn("SDLC_DEVELOPER_EXECUTION_MODE", config_field_index())
        self.assertEqual(settings.target_repo_path, target)
        self.assertEqual(
            settings.target_worktree_root,
            target.parent / "SDLC_Target_TypeScript-worktrees",
        )
        self.assertEqual(
            settings.target_repo_remote,
            "https://github.com/LuongAnh1/SDLC_Target_TypeScript.git",
        )
        self.assertEqual(settings.llm_mode, "fake")
        self.assertEqual(settings.llm_model, "deepseek-flash")
        self.assertEqual(settings.llm_thinking_mode, "disabled")
        self.assertEqual(settings.sync_mode, "mock")
        self.assertNotIn("SDLC_DESIGNER_LOOP_ENABLED", config_field_index())
        self.assertEqual(settings.designer_contract_version, "vertical_design_graph_v1")
        self.assertEqual(
            config_field_index()["SDLC_DESIGNER_CONTRACT_VERSION"]["options"],
            ["vertical_design_graph_v1", "vertical_design_graph_v2"],
        )
        self.assertEqual(settings.vertical_trace_mode, "enforce")
        self.assertEqual(config_field_index()["SDLC_VERTICAL_TRACE_MODE"]["options"], ["shadow", "enforce"])

    def test_invalid_vertical_trace_mode_falls_back_to_enforce(self) -> None:
        settings = load_settings(
            root=self.root,
            runtime_root=self.runtime_root,
            overrides={"SDLC_VERTICAL_TRACE_MODE": "unexpected"},
        )

        self.assertEqual(settings.vertical_trace_mode, "enforce")

    def test_invalid_thinking_mode_from_non_api_source_falls_back_to_disabled(self) -> None:
        (self.root / ".env").write_text(
            "SDLC_LLM_THINKING_MODE=unexpected\n",
            encoding="utf-8",
        )

        settings = load_settings(root=self.root, runtime_root=self.runtime_root)

        self.assertEqual(settings.llm_thinking_mode, "disabled")

    def test_unknown_project_profile_from_non_api_sources_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            env_path = self.root / ".env"
            env_path.write_text("SDLC_PROJECT_PROFILE=unknown_profile\n", encoding="utf-8")
            with self.assertRaises(HarnessError) as dotenv_error:
                load_settings(root=self.root, runtime_root=self.runtime_root)
            self.assertEqual(dotenv_error.exception.error_code, "project_profile_not_supported")

            env_path.write_text("", encoding="utf-8")
            RuntimeConfigStore(self.config_path).update({"SDLC_PROJECT_PROFILE": "unknown_profile"})
            with self.assertRaises(HarnessError) as runtime_error:
                load_settings(root=self.root, runtime_root=self.runtime_root)
            self.assertEqual(runtime_error.exception.error_code, "config_corrupted")
            self.config_path.unlink()

            with patch.dict(os.environ, {"SDLC_PROJECT_PROFILE": "unknown_profile"}):
                with self.assertRaises(HarnessError) as os_error:
                    load_settings(root=self.root, runtime_root=self.runtime_root)
            self.assertEqual(os_error.exception.error_code, "project_profile_not_supported")

            with self.assertRaises(HarnessError) as override_error:
                load_settings(
                    root=self.root,
                    runtime_root=self.runtime_root,
                    overrides={"SDLC_PROJECT_PROFILE": "unknown_profile"},
                )
            self.assertEqual(override_error.exception.error_code, "project_profile_not_supported")

    def test_update_is_atomic_and_secret_blank_or_mask_keeps_current_value(self) -> None:
        first = self.service.update(
            {
                "SDLC_LLM_API_KEY": "local-secret",
                "SDLC_DESIGN_REPAIR_LIMIT": 4,
            }
        )
        self.assertEqual(
            first["mutation"]["changed_keys"],
            ["SDLC_DESIGN_REPAIR_LIMIT", "SDLC_LLM_API_KEY"],
        )
        self.assertNotIn("local-secret", json.dumps(first))
        kept = self.service.update(
            {"SDLC_LLM_API_KEY": "", "ATLASSIAN_API_TOKEN": "********"}
        )
        self.assertEqual(kept["mutation"]["changed_keys"], [])
        self.assertEqual(RuntimeConfigStore(self.config_path).read()["SDLC_LLM_API_KEY"], "local-secret")
        self.assertEqual(list(self.runtime_root.glob(".*.tmp")), [])

    def test_snapshot_masks_secrets_and_reports_sources_and_restart_flags(self) -> None:
        (self.root / ".env").write_text(
            "SDLC_API_TOKEN=bootstrap-secret\nSDLC_LLM_MODEL=dotenv-model\n",
            encoding="utf-8",
        )
        self.service.update({"SDLC_API_QUEUE_LIMIT": 12, "ATLASSIAN_API_TOKEN": "sync-secret"})
        snapshot = self.service.snapshot()
        encoded = json.dumps(snapshot)
        self.assertNotIn("bootstrap-secret", encoded)
        self.assertNotIn("sync-secret", encoded)
        self.assertEqual(field_by_name(snapshot, "SDLC_API_TOKEN")["value"], "********")
        self.assertFalse(field_by_name(snapshot, "SDLC_API_TOKEN")["editable"])
        self.assertEqual(field_by_name(snapshot, "SDLC_LLM_MODEL")["source"], ".env")
        queue = field_by_name(snapshot, "SDLC_API_QUEUE_LIMIT")
        self.assertEqual(queue["source"], "runtime")
        self.assertTrue(queue["restart_required"])

    def test_project_profile_schema_update_and_reset_are_registry_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            profile = field_by_name(self.service.snapshot(), "SDLC_PROJECT_PROFILE")
            self.assertEqual(profile["value"], "typescript_fullstack_v1")
            self.assertEqual(profile["options"], ["typescript_fullstack_v1"])
            self.assertFalse(profile["restart_required"])

            updated = self.service.update({"SDLC_PROJECT_PROFILE": "typescript_fullstack_v1"})
            self.assertEqual(updated["mutation"]["changed_keys"], ["SDLC_PROJECT_PROFILE"])
            self.assertEqual(
                load_settings(root=self.root, runtime_root=self.runtime_root).project_profile_id,
                "typescript_fullstack_v1",
            )

            with self.assertRaises(HarnessError) as invalid:
                self.service.update({"SDLC_PROJECT_PROFILE": "unknown_profile"})
            self.assertEqual(invalid.exception.error_code, "config_invalid")
            self.assertEqual(
                RuntimeConfigStore(self.config_path).read()["SDLC_PROJECT_PROFILE"],
                "typescript_fullstack_v1",
            )

            reset = self.service.reset(keys=["SDLC_PROJECT_PROFILE"])
            self.assertEqual(reset["mutation"]["changed_keys"], ["SDLC_PROJECT_PROFILE"])
            self.assertEqual(
                load_settings(root=self.root, runtime_root=self.runtime_root).project_profile_id,
                "typescript_fullstack_v1",
            )

    def test_os_values_are_locked_and_bootstrap_values_are_read_only(self) -> None:
        with patch.dict(os.environ, {"SDLC_DESIGN_REPAIR_LIMIT": "7"}):
            with self.assertRaises(HarnessError) as locked:
                self.service.update({"SDLC_DESIGN_REPAIR_LIMIT": 2})
        self.assertEqual(locked.exception.error_code, "config_locked")
        with self.assertRaises(HarnessError) as read_only:
            self.service.update({"SDLC_API_HOST": "0.0.0.0"})
        self.assertEqual(read_only.exception.error_code, "config_read_only")

    def test_reset_accepts_keys_section_or_both(self) -> None:
        self.service.update(
            {
                "SDLC_LLM_MODEL": "model-x",
                "SDLC_LLM_TIMEOUT_SECONDS": 40,
                "SDLC_DESIGN_REPAIR_LIMIT": 5,
            }
        )
        by_key = self.service.reset(keys=["SDLC_DESIGN_REPAIR_LIMIT"])
        self.assertEqual(by_key["mutation"]["changed_keys"], ["SDLC_DESIGN_REPAIR_LIMIT"])
        by_section = self.service.reset(section="llm")
        self.assertEqual(
            by_section["mutation"]["changed_keys"],
            ["SDLC_LLM_MODEL", "SDLC_LLM_TIMEOUT_SECONDS"],
        )
        self.assertEqual(RuntimeConfigStore(self.config_path).read(), {})

    def test_snapshot_does_not_publish_absolute_local_paths(self) -> None:
        private_target = (self.root / "private-target").resolve()
        (self.root / ".env").write_text(
            f"SDLC_TARGET_REPO_PATH={private_target}\n",
            encoding="utf-8",
        )
        snapshot = self.service.snapshot()
        self.assertEqual(field_by_name(snapshot, "SDLC_TARGET_REPO_PATH")["value"], "<local-path>")
        self.assertNotIn(str(private_target), json.dumps(snapshot))

    def test_corrupted_runtime_config_fails_closed(self) -> None:
        self.runtime_root.mkdir(parents=True)
        self.config_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(HarnessError) as context:
            self.service.snapshot()
        self.assertEqual(context.exception.error_code, "config_corrupted")


if __name__ == "__main__":
    unittest.main()
