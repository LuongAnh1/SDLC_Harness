"""Kiểm thử contract HTTP cho tạo run, action và operation của Slice 3."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import os
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sdlc_harness.application import ApplicationCommand, ApplicationReadService
from sdlc_harness.interfaces.http import ApiServerConfig, _http_status_for_error, create_server
from sdlc_harness.operations import OperationManager, OperationStore


class RecordingService:
    def __init__(self, *, secret: str, root: Path):
        self.secret = secret
        self.root = root
        self.commands: list[ApplicationCommand] = []

    def execute(self, command: ApplicationCommand) -> dict[str, object]:
        self.commands.append(command)
        run_id = str(command.arguments.get("run_id") or f"run_created_{len(self.commands)}")
        return {
            "ok": True,
            "run_id": run_id,
            "status": "waiting_requirement_approval",
            "workspace": {"worktree_path": str(self.root / "private-worktree")},
            "summary_for_user": f"Completed with {self.secret}",
        }


class WriteApiTests(unittest.TestCase):
    def test_retirement_contract_errors_map_to_conflict(self) -> None:
        self.assertEqual(_http_status_for_error("project_profile_required"), 409)
        self.assertEqual(_http_status_for_error("legacy_execution_profile_retired"), 409)

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.temp_dir = TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / ".runtime"
        self.token = "slice-3-api-token"
        self.env = patch.dict(
            os.environ,
            {
                "SDLC_API_TOKEN": self.token,
                "SDLC_LLM_API_KEY": "slice-3-llm-secret",
                "SDLC_SYNC_MODE": "real",
            },
        )
        self.env.start()
        self.read_service = ApplicationReadService(root=self.root, runtime_root=self.runtime_root)
        self.recording_service = RecordingService(secret=self.token, root=self.root)
        self.manager = OperationManager(
            store=OperationStore(runtime_root=self.runtime_root),
            application_service=self.recording_service,  # type: ignore[arg-type]
            queue_limit=8,
            public_value=self.read_service.public_value,
        )
        self.server = create_server(
            config=ApiServerConfig(
                host="127.0.0.1",
                port=0,
                token=self.token,
                queue_limit=8,
                shutdown_timeout_seconds=2,
            ),
            root=self.root,
            runtime_root=self.runtime_root,
            read_service=self.read_service,
            operation_manager=self.manager,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.env.stop()
        self.temp_dir.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: object | None = None,
        raw_body: bytes | None = None,
        token: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, dict[str, str], object | None]:
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", method=method, data=data, headers=headers)
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            response = exc
        content = response.read()
        payload = json.loads(content.decode("utf-8")) if content else None
        return response.status, dict(response.headers), payload

    def wait_operation(self, operation_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, _, payload = self.request(
                f"/api/v1/operations/{operation_id}",
                token=self.token,
            )
            self.assertEqual(status, 200)
            if payload["operation_status"] in {"succeeded", "failed", "interrupted"}:
                return payload
            time.sleep(0.01)
        self.fail(f"Operation did not finish: {operation_id}")

    def test_create_run_from_dataset_defaults_to_mock_and_returns_operation(self) -> None:
        body = {
            "source": {"type": "dataset", "dataset": "eTour", "use_case": "UC1"},
            "requested_by": "telegram:owner",
        }
        status, _, receipt = self.request(
            "/api/v1/runs",
            method="POST",
            body=body,
            token=self.token,
            idempotency_key="create-dataset-1",
        )

        self.assertEqual(status, 202)
        self.assertFalse(receipt["idempotency_replayed"])
        operation = self.wait_operation(receipt["operation_id"])
        self.assertEqual(operation["operation_status"], "succeeded")
        command = self.recording_service.commands[-1]
        self.assertEqual(command.name, "start-or-get-run")
        self.assertEqual(command.arguments["dataset"], "eTour")
        self.assertEqual(command.settings_overrides["SDLC_SYNC_MODE"], "mock")

    def test_create_run_from_jira_maps_to_application_command(self) -> None:
        status, _, receipt = self.request(
            "/api/v1/runs",
            method="POST",
            body={
                "source": {"type": "jira", "issue_key": "SDLC-9"},
                "requested_by": "dashboard:owner",
                "sync_mode": "real",
            },
            token=self.token,
            idempotency_key="create-jira-1",
        )
        self.assertEqual(status, 202)
        self.wait_operation(receipt["operation_id"])
        command = self.recording_service.commands[-1]
        self.assertEqual(command.name, "start-from-jira")
        self.assertEqual(command.arguments["issue_key"], "SDLC-9")
        self.assertEqual(command.settings_overrides["SDLC_SYNC_MODE"], "real")

    def test_all_supported_run_actions_map_to_commands_and_overrides(self) -> None:
        cases = [
            ("answer_clarification", {"question_id": "Q-1", "answer": "Use 5", "answered_by": "owner"}, "answer-clarification"),
            ("approve_checkpoint", {"checkpoint_id": "cp-1", "design_repair_limit": 3}, "approve-checkpoint"),
            ("continue_run", {"implementation_repair_limit": 4, "qa_repair_limit": 2}, "continue-run"),
            ("sync_run", {}, "sync-run"),
            ("revalidate_design", {"design_validation_trials": 3, "design_llm_call_budget": 8}, "revalidate-design"),
            ("restart_design", {"design_repair_limit": 3}, "restart-design"),
            ("revalidate_requirement", {}, "revalidate-requirement"),
            ("clarify_more", {}, "clarify-more"),
            (
                "challenge_assumption",
                {"assumption_id": "ASM-2", "instruction": "Ask me instead."},
                "challenge-assumption",
            ),
            ("revalidate_implementation_plan", {"implementation_plan_repair_limit": 2}, "revalidate-implementation-plan"),
            ("revalidate_implementation", {"implementation_repair_limit": 3}, "revalidate-implementation"),
            ("adopt_run_workspace", {}, "adopt-run-workspace"),
            ("cleanup_run_workspace", {}, "cleanup-run-workspace"),
        ]
        for index, (action, arguments, expected_command) in enumerate(cases, start=1):
            with self.subTest(action=action):
                status, _, receipt = self.request(
                    "/api/v1/runs/run_actions/actions",
                    method="POST",
                    body={
                        "action": action,
                        "requested_by": "telegram:owner",
                        "sync_mode": "mock",
                        "arguments": arguments,
                    },
                    token=self.token,
                    idempotency_key=f"action-{index}",
                )
                self.assertEqual(status, 202)
                self.wait_operation(receipt["operation_id"])
                command = self.recording_service.commands[-1]
                self.assertEqual(command.name, expected_command)
                self.assertEqual(command.arguments["run_id"], "run_actions")
        approve_command = next(
            command for command in self.recording_service.commands if command.name == "approve-checkpoint"
        )
        self.assertEqual(approve_command.arguments["checkpoint_id"], "cp-1")
        self.assertEqual(approve_command.settings_overrides["SDLC_DESIGN_REPAIR_LIMIT"], "3")
        generic_challenge_command = next(
            command for command in self.recording_service.commands if command.name == "challenge-assumption"
        )
        self.assertEqual(generic_challenge_command.arguments["assumption_id"], "ASM-2")
        self.assertEqual(generic_challenge_command.arguments["challenged_by"], "telegram:owner")

    def test_idempotency_replay_and_conflict_are_exposed_over_http(self) -> None:
        body = {"action": "sync_run", "requested_by": "owner", "arguments": {}}
        first_status, _, first = self.request(
            "/api/v1/runs/run_idem/actions",
            method="POST",
            body=body,
            token=self.token,
            idempotency_key="same-http-key",
        )
        replay_status, _, replay = self.request(
            "/api/v1/runs/run_idem/actions",
            method="POST",
            body=body,
            token=self.token,
            idempotency_key="same-http-key",
        )
        conflict_status, _, conflict = self.request(
            "/api/v1/runs/run_idem/actions",
            method="POST",
            body={**body, "requested_by": "another-owner"},
            token=self.token,
            idempotency_key="same-http-key",
        )

        self.assertEqual(first_status, 202)
        self.assertEqual(replay_status, 202)
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertTrue(replay["idempotency_replayed"])
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["error_code"], "idempotency_conflict")

    def test_operation_result_and_dashboard_are_public_projections(self) -> None:
        status, _, receipt = self.request(
            "/api/v1/runs/run_public/actions",
            method="POST",
            body={"action": "sync_run", "requested_by": "owner"},
            token=self.token,
            idempotency_key="public-result",
        )
        self.assertEqual(status, 202)
        operation = self.wait_operation(receipt["operation_id"])
        encoded = json.dumps(operation)
        self.assertNotIn(self.token, encoded)
        self.assertNotIn(str(self.root), encoded)
        self.assertEqual(operation["result"]["workspace"]["worktree_path"], "<local-path>")

        status, _, snapshot = self.request(
            "/api/v1/dashboard/snapshot?limit=10",
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(item["operation_id"] == receipt["operation_id"] for item in snapshot["operations"]))

    def test_write_validation_auth_and_missing_operation_errors_are_json(self) -> None:
        valid = {"action": "sync_run", "requested_by": "owner"}
        cases = [
            ({"path": "/api/v1/runs/run_1/actions", "body": valid, "token": None, "key": "key"}, 401, "unauthorized"),
            ({"path": "/api/v1/runs/run_1/actions", "body": valid, "token": self.token, "key": None}, 400, "idempotency_key_required"),
            ({"path": "/api/v1/runs/run_1/actions", "body": {**valid, "extra": 1}, "token": self.token, "key": "unknown"}, 400, "invalid_request"),
            ({"path": "/api/v1/runs/run_1/actions", "body": {"action": "request_change", "requested_by": "owner"}, "token": self.token, "key": "unsupported"}, 400, "invalid_request"),
            ({"path": "/api/v1/runs/run_1/actions", "body": {"action": "challenge_assumption", "requested_by": "owner", "arguments": {"assumption_id": "ASM-1"}}, "token": self.token, "key": "generic-challenge-missing-instruction"}, 400, "invalid_request"),
            ({"path": "/api/v1/runs/run_1/actions", "body": {"action": "continue_run", "requested_by": "owner", "arguments": {"qa_repair_limit": True}}, "token": self.token, "key": "bad-limit"}, 400, "invalid_request"),
            ({"path": "/api/v1/unknown", "body": {}, "token": self.token, "key": "unknown-route"}, 404, "route_not_found"),
        ]
        for request_data, expected_status, error_code in cases:
            with self.subTest(error=error_code, key=request_data["key"]):
                status, _, payload = self.request(
                    request_data["path"],
                    method="POST",
                    body=request_data["body"],
                    token=request_data["token"],
                    idempotency_key=request_data["key"],
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(payload["error_code"], error_code)

        status, _, payload = self.request(
            "/api/v1/runs/run_1/actions",
            method="POST",
            raw_body=b"{not-json",
            token=self.token,
            idempotency_key="invalid-json",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "invalid_json")

        status, _, payload = self.request("/api/v1/operations/op_missing", token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "operation_not_found")


if __name__ == "__main__":
    unittest.main()
