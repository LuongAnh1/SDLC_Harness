"""Contract and security tests for the Slice 2 read-only HTTP API."""

from __future__ import annotations

from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sdlc_harness.application import ApplicationReadService
from sdlc_harness.application.read_service import MAX_ARTIFACT_BYTES
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.events import EventStore
from sdlc_harness.interfaces.http import ApiServerConfig, create_server
from sdlc_harness.storage.runtime import RuntimeStore


class ReadApiFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.temp_dir = TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.api_token = "slice-2-test-token"
        self.env = patch.dict(
            os.environ,
            {
                "SDLC_API_TOKEN": self.api_token,
                "SDLC_LLM_API_KEY": "slice-2-llm-secret",
                "ATLASSIAN_API_TOKEN": "slice-2-atlassian-secret",
            },
        )
        self.env.start()
        self.store = RuntimeStore(repo_root=self.root, runtime_root=self.runtime_root)
        self.run_id = "run_20261018_slice2"
        self.artifact_path = self.store.artifact_path(self.run_id, "requirement-model.json")
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps({"title": "Public requirement", "stable_id": "UC-S2"}),
            encoding="utf-8",
        )
        hidden_path = self.store.artifact_path(self.run_id, "developer-work-packet.json")
        hidden_path.write_text('{"prompt": "private"}', encoding="utf-8")
        self.state = {
            "ok": True,
            "run_id": self.run_id,
            "source_ref": "jira:SDLC-42",
            "requirement_contract_version": "v2",
            "created_at": "2026-10-18T01:00:00Z",
            "updated_at": "2026-10-18T02:00:00Z",
            "current_phase": "design_review",
            "status": "waiting_design_approval",
            "latest_artifacts": {
                "requirement_model": str(self.artifact_path),
                "developer_work_packet": str(hidden_path),
            },
            "pending_checkpoint": {"id": "cp-design", "type": "design_approval"},
            "allowed_next_actions": ["approve_checkpoint", "get_status"],
            "last_sync": {
                "status": "success",
                "url": "https://example.atlassian.net/wiki/page",
                "token": self.api_token,
            },
            "last_error": None,
            "quality_reports": {},
            "workspace": {"worktree_path": str(self.root / "private-worktree")},
            "summary_for_user": (
                f"See https://example.test/result at {self.root}; "
                "credential=slice-2-llm-secret"
            ),
            "llm": {"mode": "fake", "model": "test"},
            "developer_execution": {},
        }
        self.store.write_state(self.state)
        self.read_service = ApplicationReadService(root=self.root, runtime_root=self.runtime_root)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp_dir.cleanup()


class ApplicationReadServiceTests(ReadApiFixture):
    def test_list_runs_is_sorted_limited_and_does_not_create_missing_storage(self) -> None:
        older_state = dict(self.state)
        older_state.update(
            {
                "run_id": "run_20261018_older",
                "updated_at": "2026-10-18T00:30:00Z",
                "latest_artifacts": {},
            }
        )
        self.store.write_state(older_state)

        result = self.read_service.list_runs(limit=1)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["runs"][0]["run_id"], self.run_id)

        missing_runtime = Path(self.temp_dir.name) / "missing-runtime"
        empty_service = ApplicationReadService(root=self.root, runtime_root=missing_runtime)
        self.assertEqual(empty_service.list_runs(limit=10)["runs"], [])
        self.assertFalse(missing_runtime.exists())

    def test_get_run_projects_artifact_metadata_and_redacts_private_values(self) -> None:
        result = self.read_service.get_run(run_id=self.run_id)
        encoded = json.dumps(result, ensure_ascii=False)

        metadata = result["latest_artifacts"]
        self.assertEqual(set(metadata), {"requirement_model"})
        self.assertEqual(
            metadata["requirement_model"]["url"],
            f"/api/v1/runs/{self.run_id}/artifacts/requirement_model",
        )
        self.assertEqual(metadata["requirement_model"]["media_type"], "application/json")
        self.assertGreater(metadata["requirement_model"]["size_bytes"], 0)
        self.assertIn("https://example.test/result", encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn(self.api_token, encoded)
        self.assertNotIn("slice-2-llm-secret", encoded)
        self.assertEqual(result["workspace"]["worktree_path"], "<local-path>")

    def test_read_public_json_artifact(self) -> None:
        result = self.read_service.read_artifact(
            run_id=self.run_id,
            artifact_key="requirement_model",
        )

        self.assertEqual(result["media_type"], "application/json")
        self.assertEqual(result["content"]["stable_id"], "UC-S2")

    def test_artifact_allowlist_and_run_directory_boundary_are_fail_closed(self) -> None:
        with self.assertRaises(HarnessError) as hidden:
            self.read_service.read_artifact(
                run_id=self.run_id,
                artifact_key="developer_work_packet",
            )
        self.assertEqual(hidden.exception.error_code, "artifact_not_found")

        outside = Path(self.temp_dir.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        state = self.store.read_state(self.run_id)
        state["latest_artifacts"]["test_report"] = str(outside)
        self.store.write_state(state)
        with self.assertRaises(HarnessError) as escaped:
            self.read_service.read_artifact(run_id=self.run_id, artifact_key="test_report")
        self.assertEqual(escaped.exception.error_code, "artifact_not_found")

    def test_oversized_artifact_is_rejected(self) -> None:
        large_path = self.store.artifact_path(self.run_id, "test-report.txt")
        large_path.write_text("x" * (MAX_ARTIFACT_BYTES + 1), encoding="utf-8")
        state = self.store.read_state(self.run_id)
        state["latest_artifacts"]["test_report"] = str(large_path)
        self.store.write_state(state)

        with self.assertRaises(HarnessError) as context:
            self.read_service.read_artifact(run_id=self.run_id, artifact_key="test_report")
        self.assertEqual(context.exception.error_code, "artifact_too_large")

    def test_invalid_identifiers_and_limits_are_rejected(self) -> None:
        for call in (
            lambda: self.read_service.list_runs(limit=0),
            lambda: self.read_service.list_runs(limit=201),
            lambda: self.read_service.get_run(run_id="../outside"),
            lambda: self.read_service.read_artifact(
                run_id=self.run_id,
                artifact_key="../state",
            ),
        ):
            with self.assertRaises(HarnessError):
                call()


class HttpReadApiTests(ReadApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.server = create_server(
            config=ApiServerConfig(host="127.0.0.1", port=0, token=self.api_token),
            root=self.root,
            runtime_root=self.runtime_root,
            read_service=self.read_service,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address
        self.base_url = f"http://{self.host}:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        token: str | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], object | None]:
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            data=data,
            headers=request_headers,
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        body = response.read()
        payload = json.loads(body.decode("utf-8")) if body else None
        return response.status, dict(response.headers), payload

    def test_health_is_unauthenticated_and_declares_slice_capabilities(self) -> None:
        status, headers, payload = self.request("/api/v1/health")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["capabilities"]["read_api"])
        self.assertTrue(payload["capabilities"]["write_api"])
        self.assertTrue(payload["capabilities"]["operation_queue"])
        self.assertEqual(payload["queue"]["workers"], 1)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_all_read_routes_return_public_contracts(self) -> None:
        status, _, runs = self.request("/api/v1/runs?limit=1", token=self.api_token)
        self.assertEqual(status, 200)
        self.assertEqual(runs["runs"][0]["run_id"], self.run_id)

        status, _, run = self.request(
            f"/api/v1/runs/{self.run_id}",
            token=self.api_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(run["run_id"], self.run_id)

        status, _, artifact = self.request(
            f"/api/v1/runs/{self.run_id}/artifacts/requirement_model",
            token=self.api_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(artifact["content"]["stable_id"], "UC-S2")

        status, _, snapshot = self.request(
            "/api/v1/dashboard/snapshot?limit=1",
            token=self.api_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["run_count"], 1)
        self.assertEqual(snapshot["operations"], [])

    def test_event_route_supports_cursor_limit_auth_and_read_only_behavior(self) -> None:
        EventStore(runtime_root=self.runtime_root).append(
            run_id=self.run_id,
            category="workflow",
            event_type="workflow_state_changed",
            phase="design_review",
            status="waiting_design_approval",
            message="Workflow state changed.",
            data={
                "previous_phase": "design",
                "current_phase": "design_review",
                "previous_status": "running",
                "current_status": "waiting_design_approval",
            },
        )
        event_path = self.runtime_root / "runs" / self.run_id / "events.jsonl"
        before = event_path.read_bytes()
        status, _, payload = self.request(
            f"/api/v1/runs/{self.run_id}/events?after=1&limit=1",
            token=self.api_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["after"], 1)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["next_cursor"], payload["events"][-1]["sequence"])
        self.assertEqual(event_path.read_bytes(), before)

        status, _, unauthorized = self.request(f"/api/v1/runs/{self.run_id}/events")
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error_code"], "unauthorized")
        for query in ("after=-1", "after=x", "limit=0", "limit=201", "other=1", "after=1&after=2"):
            status, _, error = self.request(
                f"/api/v1/runs/{self.run_id}/events?{query}",
                token=self.api_token,
            )
            self.assertEqual(status, 400, query)
            self.assertEqual(error["error_code"], "invalid_query")

    def test_auth_is_required_for_non_health_routes(self) -> None:
        status, headers, payload = self.request("/api/v1/runs")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error_code"], "unauthorized")
        self.assertEqual(headers["WWW-Authenticate"], "Bearer")

        status, _, payload = self.request("/api/v1/runs", token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error_code"], "unauthorized")

    def test_bad_queries_and_encoded_traversal_return_json_400(self) -> None:
        for path in (
            "/api/v1/runs?limit=zero",
            "/api/v1/runs?limit=1&limit=2",
            f"/api/v1/runs/{self.run_id}/artifacts/%2e%2e",
        ):
            status, _, payload = self.request(path, token=self.api_token)
            self.assertEqual(status, 400, path)
            self.assertFalse(payload["ok"])

    def test_missing_resources_return_json_404(self) -> None:
        for path in (
            "/api/v1/unknown",
            "/api/v1/runs/run_missing",
            f"/api/v1/runs/{self.run_id}/artifacts/test_report",
        ):
            status, _, payload = self.request(path, token=self.api_token)
            self.assertEqual(status, 404, path)
            self.assertFalse(payload["ok"])

    def test_unsupported_methods_return_json_405(self) -> None:
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            status, _, payload = self.request(
                "/api/v1/runs",
                method=method,
                token=self.api_token,
                data=b"" if method not in {"HEAD"} else None,
            )
            self.assertEqual(status, 405, method)
            if method == "HEAD":
                self.assertIsNone(payload)
            else:
                self.assertEqual(payload["error_code"], "method_not_allowed")

    def test_oversized_request_returns_json_413_without_reading_body(self) -> None:
        connection = HTTPConnection(self.host, self.port, timeout=3)
        connection.request(
            "POST",
            "/api/v1/runs",
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Length": str(self.server.config.max_request_body_bytes + 1),
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        self.assertEqual(response.status, 413)
        self.assertEqual(payload["error_code"], "payload_too_large")

    def test_unhandled_error_returns_generic_json_500(self) -> None:
        original = self.read_service.list_runs

        def fail(*, limit: int) -> dict[str, object]:
            raise RuntimeError("private failure detail")

        self.read_service.list_runs = fail  # type: ignore[method-assign]
        try:
            status, _, payload = self.request("/api/v1/runs", token=self.api_token)
        finally:
            self.read_service.list_runs = original  # type: ignore[method-assign]

        self.assertEqual(status, 500)
        self.assertEqual(payload["error_code"], "internal_error")
        self.assertNotIn("private failure detail", json.dumps(payload))

    def test_read_routes_do_not_mutate_persisted_state(self) -> None:
        state_path = self.store.state_path(self.run_id)
        before = state_path.read_bytes()
        before_mtime = state_path.stat().st_mtime_ns

        for path in (
            "/api/v1/runs",
            f"/api/v1/runs/{self.run_id}",
            f"/api/v1/runs/{self.run_id}/artifacts/requirement_model",
            f"/api/v1/runs/{self.run_id}/events",
            "/api/v1/dashboard/snapshot",
        ):
            status, _, _ = self.request(path, token=self.api_token)
            self.assertEqual(status, 200)

        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(state_path.stat().st_mtime_ns, before_mtime)

    def test_server_shutdown_stops_serving_thread(self) -> None:
        self.assertTrue(self.thread.is_alive())
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())


class ApiServerConfigurationTests(unittest.TestCase):
    def test_loopback_can_run_without_token(self) -> None:
        ApiServerConfig(host="127.0.0.1", port=7788, token="").validate()
        ApiServerConfig(host="::1", port=7788, token="").validate()

    def test_non_loopback_requires_token(self) -> None:
        with self.assertRaises(HarnessError) as context:
            ApiServerConfig(host="0.0.0.0", port=7788, token="").validate()
        self.assertEqual(context.exception.error_code, "api_token_required")


if __name__ == "__main__":
    unittest.main()
