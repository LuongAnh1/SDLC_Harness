"""Kiểm thử contract HTTP cho cấu hình runtime của Slice 6."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sdlc_harness.core.config import load_settings
from sdlc_harness.interfaces.http import ApiServerConfig, create_server


class ConfigApiTests(unittest.TestCase):
    def setUp(self) -> None:
        clean_environment = dict(os.environ)
        clean_environment.pop("SDLC_PROJECT_PROFILE", None)
        self.environment = patch.dict(os.environ, clean_environment, clear=True)
        self.environment.start()
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "repo"
        self.root.mkdir()
        self.runtime_root = self.root / ".runtime"
        self.token = "slice-6-api-token"
        (self.root / ".env").write_text(
            "SDLC_API_TOKEN=slice-6-api-token\nSDLC_LLM_API_KEY=hidden-llm-secret\n",
            encoding="utf-8",
        )
        self.server = create_server(
            config=ApiServerConfig(host="127.0.0.1", port=0, token=self.token),
            root=self.root,
            runtime_root=self.runtime_root,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp_dir.cleanup()
        self.environment.stop()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(f"{self.base_url}{path}", method=method, data=data, headers=headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload

    def test_get_requires_auth_returns_schema_and_never_leaks_secrets(self) -> None:
        status, unauthorized = self.request("/api/v1/config")
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error_code"], "unauthorized")

        status, payload = self.request("/api/v1/config", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], "runtime_config_v1")
        self.assertEqual(
            [section["id"] for section in payload["sections"]],
            [
                "api",
                "llm",
                "validation",
                "phase1_llm",
                "phase2_llm",
                "phase3_llm",
                "phase4_llm",
                "phase2_tools",
                "target",
                "sync",
            ],
        )
        self.assertNotIn("hidden-llm-secret", json.dumps(payload))
        target_section = next(section for section in payload["sections"] if section["id"] == "target")
        profile_field = next(
            field for field in target_section["fields"] if field["name"] == "SDLC_PROJECT_PROFILE"
        )
        self.assertEqual(profile_field["value"], "typescript_fullstack_v1")
        self.assertEqual(profile_field["source"], "default")
        self.assertEqual(profile_field["options"], ["typescript_fullstack_v1"])
        validation_section = next(section for section in payload["sections"] if section["id"] == "validation")
        self.assertNotIn(
            "SDLC_DESIGNER_LOOP_ENABLED",
            {field["name"] for field in validation_section["fields"]},
        )
        vertical_trace_field = next(
            field for field in validation_section["fields"] if field["name"] == "SDLC_VERTICAL_TRACE_MODE"
        )
        self.assertEqual(vertical_trace_field["value"], "enforce")
        self.assertEqual(vertical_trace_field["source"], "default")
        design_step_review_field = next(
            field
            for field in validation_section["fields"]
            if field["name"] == "SDLC_DESIGN_STEP_REVIEW_ENABLED"
        )
        self.assertFalse(design_step_review_field["value"])
        self.assertEqual(design_step_review_field["source"], "default")
        path_field = next(
            field for field in target_section["fields"] if field["name"] == "SDLC_TARGET_REPO_PATH"
        )
        remote_field = next(
            field for field in target_section["fields"] if field["name"] == "SDLC_TARGET_REPO_REMOTE"
        )
        self.assertEqual(path_field["value"], "../SDLC_Target_TypeScript")
        self.assertEqual(path_field["source"], "default")
        self.assertEqual(
            remote_field["value"],
            "https://github.com/LuongAnh1/SDLC_Target_TypeScript.git",
        )
        self.assertEqual(remote_field["source"], "default")

        health_status, health = self.request("/api/v1/health")
        self.assertEqual(health_status, 200)
        self.assertTrue(health["capabilities"]["config_api"])

    def test_put_applies_next_operation_settings_without_creating_operation(self) -> None:
        before = self.server.operation_manager.store.list_operations()
        status, payload = self.request(
            "/api/v1/config",
            method="PUT",
            token=self.token,
            body={
                "values": {
                    "SDLC_DESIGN_REPAIR_LIMIT": 6,
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                    "SDLC_SYNC_MODE": "both",
                }
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["mutation"]["changed_keys"],
            ["SDLC_DESIGN_REPAIR_LIMIT", "SDLC_PROJECT_PROFILE", "SDLC_SYNC_MODE"],
        )
        settings = load_settings(root=self.root, runtime_root=self.runtime_root)
        self.assertEqual(settings.design_repair_limit, 6)
        self.assertEqual(settings.project_profile_id, "typescript_fullstack_v1")
        self.assertEqual(settings.sync_mode, "both")
        self.assertEqual(self.server.operation_manager.store.list_operations(), before)

    def test_secret_blank_is_keep_and_response_is_masked(self) -> None:
        status, first = self.request(
            "/api/v1/config",
            method="PUT",
            token=self.token,
            body={"values": {"ATLASSIAN_API_TOKEN": "new-sync-secret"}},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("new-sync-secret", json.dumps(first))
        status, kept = self.request(
            "/api/v1/config",
            method="PUT",
            token=self.token,
            body={"values": {"ATLASSIAN_API_TOKEN": ""}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(kept["mutation"]["changed_keys"], [])
        self.assertEqual(load_settings(root=self.root, runtime_root=self.runtime_root).atlassian_api_token, "new-sync-secret")

    def test_reset_supports_field_section_and_both(self) -> None:
        self.request(
            "/api/v1/config",
            method="PUT",
            token=self.token,
            body={
                "values": {
                    "SDLC_LLM_MODEL": "model-x",
                    "SDLC_DESIGN_REPAIR_LIMIT": 7,
                    "SDLC_PROJECT_PROFILE": "typescript_fullstack_v1",
                }
            },
        )
        status, one = self.request(
            "/api/v1/config/reset",
            method="POST",
            token=self.token,
            body={"keys": ["SDLC_DESIGN_REPAIR_LIMIT", "SDLC_PROJECT_PROFILE"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            one["mutation"]["changed_keys"],
            ["SDLC_DESIGN_REPAIR_LIMIT", "SDLC_PROJECT_PROFILE"],
        )
        self.assertEqual(
            load_settings(root=self.root, runtime_root=self.runtime_root).project_profile_id,
            "typescript_fullstack_v1",
        )
        status, section = self.request(
            "/api/v1/config/reset",
            method="POST",
            token=self.token,
            body={"section": "llm", "keys": []},
        )
        self.assertEqual(status, 200)
        self.assertEqual(section["mutation"]["changed_keys"], ["SDLC_LLM_MODEL"])

    def test_invalid_read_only_locked_and_busy_mutations_have_stable_statuses(self) -> None:
        cases = [
            ({"UNKNOWN": "x"}, 400, "config_invalid"),
            ({"SDLC_API_HOST": "0.0.0.0"}, 400, "config_read_only"),
            ({"SDLC_DESIGN_REPAIR_LIMIT": -1}, 400, "config_invalid"),
            ({"SDLC_PROJECT_PROFILE": "unknown_profile"}, 400, "config_invalid"),
        ]
        for values, expected_status, expected_code in cases:
            status, payload = self.request(
                "/api/v1/config",
                method="PUT",
                token=self.token,
                body={"values": values},
            )
            self.assertEqual((status, payload["error_code"]), (expected_status, expected_code))

        with patch.dict(os.environ, {"SDLC_DESIGN_REPAIR_LIMIT": "8"}):
            status, payload = self.request(
                "/api/v1/config",
                method="PUT",
                token=self.token,
                body={"values": {"SDLC_DESIGN_REPAIR_LIMIT": 2}},
            )
        self.assertEqual((status, payload["error_code"]), (409, "config_locked"))

        self.server.operation_manager._running_operation_id = "op_test_busy"
        try:
            status, payload = self.request(
                "/api/v1/config/reset",
                method="POST",
                token=self.token,
                body={"section": "llm"},
            )
        finally:
            self.server.operation_manager._running_operation_id = None
        self.assertEqual((status, payload["error_code"]), (409, "config_busy"))

    def test_corrupted_config_returns_generic_contract_with_specific_error_code(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        (self.runtime_root / "config.json").write_text("{broken", encoding="utf-8")
        status, payload = self.request("/api/v1/config", token=self.token)
        self.assertEqual(status, 500)
        self.assertEqual(payload["error_code"], "config_corrupted")


if __name__ == "__main__":
    unittest.main()
