"""Biên đọc/ghi HTTP cho Niko, Ops Dashboard và các operation SDLC."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
from pathlib import Path
import re
import signal
import sys
from threading import Thread
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..application import (
    ApplicationCommand,
    ApplicationReadService,
    ApplicationService,
    ConfigurationService,
)
from ..application.action_specs import ACTION_SPECS, OVERRIDE_SETTINGS
from ..application.read_service import DEFAULT_RUN_LIMIT, api_capabilities
from ..events import DEFAULT_EVENT_LIMIT, MAX_EVENT_LIMIT
from ..core.config import Settings, load_settings, repo_root
from ..core.errors import HarnessError
from ..operations import OperationManager, OperationStore
from ..ops.frontend import read_dashboard_asset


MAX_REQUEST_BODY_BYTES = 1024 * 1024
_RUN_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)$")
_EVENT_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)/events$")
_RUN_OPERATIONS_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)/operations$")
_ARTIFACT_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)/artifacts/([^/]+)$")
_ACTION_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)/actions$")
_OPERATION_ROUTE = re.compile(r"^/api/v1/operations/([^/]+)$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
_LOGGER = logging.getLogger("sdlc_harness.http")



@dataclass(frozen=True)
class ApiServerConfig:
    host: str = "127.0.0.1"
    port: int = 7788
    token: str = ""
    queue_limit: int = 32
    shutdown_timeout_seconds: int = 30
    max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES

    @classmethod
    def from_settings(cls, settings: Settings) -> "ApiServerConfig":
        return cls(
            host=settings.api_host,
            port=settings.api_port,
            token=settings.api_token,
            queue_limit=settings.api_queue_limit,
            shutdown_timeout_seconds=settings.api_shutdown_timeout_seconds,
        )

    def validate(self) -> None:
        if not self.host.strip():
            raise HarnessError("api_config_invalid", "SDLC_API_HOST must not be empty.")
        if not 0 <= self.port <= 65535:
            raise HarnessError("api_config_invalid", "SDLC_API_PORT must be from 1 to 65535.")
        if self.port == 0 and self.host != "127.0.0.1":
            raise HarnessError("api_config_invalid", "Ephemeral API ports are only allowed on 127.0.0.1.")
        if not _is_loopback_host(self.host) and not self.token:
            raise HarnessError(
                "api_token_required",
                "SDLC_API_TOKEN is required when SDLC_API_HOST is not loopback.",
            )
        if self.shutdown_timeout_seconds < 1:
            raise HarnessError("api_config_invalid", "API shutdown timeout must be at least one second.")
        if self.queue_limit < 1:
            raise HarnessError("api_config_invalid", "API queue limit must be at least one.")
        if self.max_request_body_bytes < 1:
            raise HarnessError("api_config_invalid", "API request body limit must be positive.")


class SdlcHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        config: ApiServerConfig,
        read_service: ApplicationReadService,
        operation_manager: OperationManager,
        config_service: ConfigurationService,
        runtime_root: Path,
    ):
        self.config = config
        self.read_service = read_service
        self.operation_manager = operation_manager
        self.config_service = config_service
        self.runtime_root = runtime_root
        super().__init__(server_address, SdlcRequestHandler)

    def server_close(self) -> None:
        self.operation_manager.shutdown(timeout_seconds=self.config.shutdown_timeout_seconds)
        super().server_close()


class SdlcRequestHandler(BaseHTTPRequestHandler):
    """Thin HTTP adapter; application read behavior lives in ApplicationReadService."""

    server: SdlcHttpServer
    protocol_version = "HTTP/1.1"
    server_version = "sdlc-harness"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            if self._request_body_too_large():
                raise HarnessError("payload_too_large", "Request body exceeds the API limit.")
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            asset = read_dashboard_asset(path)
            if asset is not None:
                self._require_no_query(parsed.query)
                body, media_type = asset
                self._write_bytes(200, body, media_type=media_type)
                return
            if path == "/api/v1/health":
                self._require_no_query(parsed.query)
                self._write_json(200, self._health_payload())
                return
            self._require_authorized()
            if path == "/api/v1/config":
                self._require_no_query(parsed.query)
                self._write_json(200, self.server.config_service.snapshot())
                return
            if path == "/api/v1/runs":
                limit = self._read_limit(parsed.query)
                self._write_json(200, self.server.read_service.list_runs(limit=limit))
                return
            if path == "/api/v1/dashboard/snapshot":
                limit = self._read_limit(parsed.query)
                self._write_json(
                    200,
                    self.server.read_service.dashboard_snapshot(
                        limit=limit,
                        operations=self.server.operation_manager.list_public_summaries(limit=limit),
                    ),
                )
                return
            if path == "/api/v1/dashboard/schema":
                self._require_no_query(parsed.query)
                self._write_json(200, self.server.read_service.dashboard_schema())
                return
            operation_match = _OPERATION_ROUTE.fullmatch(path)
            if operation_match:
                self._require_no_query(parsed.query)
                operation_id = unquote(operation_match.group(1))
                self._write_json(
                    200,
                    self.server.operation_manager.get_public_operation(operation_id),
                )
                return
            event_match = _EVENT_ROUTE.fullmatch(path)
            if event_match:
                run_id = unquote(event_match.group(1))
                after, limit = self._read_event_query(parsed.query)
                self._write_json(
                    200,
                    self.server.read_service.list_events(
                        run_id=run_id,
                        after=after,
                        limit=limit,
                    ),
                )
                return
            run_operations_match = _RUN_OPERATIONS_ROUTE.fullmatch(path)
            if run_operations_match:
                run_id = _validated_identifier(unquote(run_operations_match.group(1)), kind="run id")
                limit = self._read_limit(parsed.query)
                self.server.read_service.get_run(run_id=run_id)
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "run_id": run_id,
                        "operations": self.server.operation_manager.list_public_summaries(
                            limit=limit,
                            run_id=run_id,
                        ),
                    },
                )
                return
            run_match = _RUN_ROUTE.fullmatch(path)
            if run_match:
                self._require_no_query(parsed.query)
                run_id = unquote(run_match.group(1))
                self._write_json(200, self.server.read_service.get_run(run_id=run_id))
                return
            artifact_match = _ARTIFACT_ROUTE.fullmatch(path)
            if artifact_match:
                self._require_no_query(parsed.query)
                run_id = unquote(artifact_match.group(1))
                artifact_key = unquote(artifact_match.group(2))
                self._write_json(
                    200,
                    self.server.read_service.read_artifact(
                        run_id=run_id,
                        artifact_key=artifact_key,
                    ),
                )
                return
            raise HarnessError("route_not_found", "API route was not found.")
        except HarnessError as exc:
            self._write_harness_error(exc)
        except Exception:
            _LOGGER.exception("Unhandled HTTP read error")
            self._write_json(
                500,
                {"ok": False, "error_code": "internal_error", "message": "Internal server error."},
            )

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._unsupported_write_method()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            self._require_authorized()
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            self._require_no_query(parsed.query)
            body = self._read_json_body()
            if path == "/api/v1/config/reset":
                self._write_json(200, self._reset_config(body))
                return
            idempotency_key = self.headers.get("Idempotency-Key", "")
            if path == "/api/v1/runs":
                receipt = self._submit_create_run(body, idempotency_key=idempotency_key)
                self._write_json(202, receipt)
                return
            action_match = _ACTION_ROUTE.fullmatch(path)
            if action_match:
                run_id = _validated_identifier(unquote(action_match.group(1)), kind="run id")
                receipt = self._submit_run_action(
                    run_id=run_id,
                    body=body,
                    idempotency_key=idempotency_key,
                )
                self._write_json(202, receipt)
                return
            raise HarnessError("route_not_found", "API route was not found.")
        except HarnessError as exc:
            self._write_harness_error(exc)
        except Exception:
            _LOGGER.exception("Unhandled HTTP write error")
            self._write_json(
                500,
                {"ok": False, "error_code": "internal_error", "message": "Internal server error."},
            )

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            self._require_authorized()
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/") or "/"
            self._require_no_query(parsed.query)
            if path != "/api/v1/config":
                raise HarnessError("method_not_allowed", "HTTP method is not available for this API route.")
            body = self._read_json_body()
            _require_fields(body, allowed={"values"}, required={"values"})
            values = body.get("values")
            if not isinstance(values, dict):
                raise HarnessError("config_invalid", "values must be a JSON object.")
            payload = self.server.operation_manager.run_when_idle(
                lambda: self.server.config_service.update(values)
            )
            self._write_json(200, payload)
        except HarnessError as exc:
            self._write_harness_error(exc)
        except Exception:
            _LOGGER.exception("Unhandled HTTP config update error")
            self._write_json(
                500,
                {"ok": False, "error_code": "internal_error", "message": "Internal server error."},
            )

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._unsupported_write_method()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._unsupported_write_method()

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._unsupported_write_method()

    def log_message(self, format: str, *args: object) -> None:
        _LOGGER.info("%s - %s", self.address_string(), format % args)

    def version_string(self) -> str:
        return "sdlc-harness"

    def _unsupported_write_method(self) -> None:
        try:
            if self._request_body_too_large():
                raise HarnessError("payload_too_large", "Request body exceeds the API limit.")
            raise HarnessError("method_not_allowed", "HTTP method is not available in API V1.")
        except HarnessError as exc:
            self._write_harness_error(exc)

    def _health_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "service": "sdlc-harness",
            "version": __version__,
            "api_version": "v1",
            "status": "ready",
            "queue": self.server.operation_manager.queue_status(),
            "capabilities": api_capabilities(),
        }

    def _submit_create_run(
        self,
        body: dict[str, object],
        *,
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_fields(body, allowed={"source", "requested_by", "sync_mode"}, required={"source", "requested_by"})
        requested_by = _required_text(body, "requested_by")
        sync_mode = _sync_mode(body.get("sync_mode"))
        source = body.get("source")
        if not isinstance(source, dict):
            raise HarnessError("invalid_request", "source must be a JSON object.")
        source_type = _required_text(source, "type")
        if source_type == "dataset":
            _require_fields(source, allowed={"type", "dataset", "use_case"}, required={"type", "dataset", "use_case"})
            normalized_source = {
                "type": "dataset",
                "dataset": _required_text(source, "dataset"),
                "use_case": _required_text(source, "use_case"),
            }
            public_command = "start_or_get_run"
            command = ApplicationCommand(
                name="start-or-get-run",
                arguments={
                    "dataset": normalized_source["dataset"],
                    "use_case": normalized_source["use_case"],
                    "requested_by": requested_by,
                },
                settings_overrides={"SDLC_SYNC_MODE": sync_mode},
                runtime_root=self.server.runtime_root,
            )
        elif source_type == "jira":
            _require_fields(source, allowed={"type", "issue_key"}, required={"type", "issue_key"})
            normalized_source = {"type": "jira", "issue_key": _required_text(source, "issue_key")}
            public_command = "start_from_jira"
            command = ApplicationCommand(
                name="start-from-jira",
                arguments={"issue_key": normalized_source["issue_key"], "requested_by": requested_by},
                settings_overrides={"SDLC_SYNC_MODE": sync_mode},
                runtime_root=self.server.runtime_root,
            )
        else:
            raise HarnessError("invalid_request", "source.type must be `dataset` or `jira`.")
        normalized = {
            "route": "create_run",
            "source": normalized_source,
            "requested_by": requested_by,
            "sync_mode": sync_mode,
        }
        return self.server.operation_manager.submit(
            command=command,
            public_command=public_command,
            run_id=None,
            requested_by=requested_by,
            sync_mode=sync_mode,
            idempotency_key=idempotency_key,
            canonical_request=normalized,
        )

    def _submit_run_action(
        self,
        *,
        run_id: str,
        body: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        _require_fields(
            body,
            allowed={"action", "requested_by", "sync_mode", "arguments"},
            required={"action", "requested_by"},
        )
        action = _required_text(body, "action")
        requested_by = _required_text(body, "requested_by")
        sync_mode = _sync_mode(body.get("sync_mode"))
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            raise HarnessError("invalid_request", "arguments must be a JSON object.")
        spec = ACTION_SPECS.get(action)
        if spec is None:
            raise HarnessError("invalid_request", f"Unsupported run action: {action}", run_id=run_id)
        required = set(spec["required"])
        optional = set(spec["optional"])
        overrides = set(spec["overrides"])
        _require_fields(arguments, allowed=required | optional | overrides, required=required)
        command_arguments: dict[str, object] = {"run_id": run_id}
        normalized_arguments: dict[str, object] = {}
        for key in sorted(required | optional):
            if key not in arguments:
                continue
            value = _required_text(arguments, key)
            command_arguments[key] = value
            normalized_arguments[key] = value
        settings_overrides = {"SDLC_SYNC_MODE": sync_mode}
        for key in sorted(overrides):
            if key not in arguments:
                continue
            setting_key, minimum = OVERRIDE_SETTINGS[key]
            value = _integer_argument(arguments, key, minimum=minimum)
            settings_overrides[setting_key] = str(value)
            normalized_arguments[key] = value
        command = ApplicationCommand(
            name=str(spec["command"]),
            arguments=command_arguments,
            settings_overrides=settings_overrides,
            runtime_root=self.server.runtime_root,
        )
        normalized = {
            "route": "run_action",
            "run_id": run_id,
            "action": action,
            "requested_by": requested_by,
            "sync_mode": sync_mode,
            "arguments": normalized_arguments,
        }
        return self.server.operation_manager.submit(
            command=command,
            public_command=action,
            run_id=run_id,
            requested_by=requested_by,
            sync_mode=sync_mode,
            idempotency_key=idempotency_key,
            canonical_request=normalized,
        )

    def _reset_config(self, body: dict[str, object]) -> dict[str, object]:
        _require_fields(body, allowed={"keys", "section"}, required=set())
        raw_keys = body.get("keys")
        if raw_keys is None:
            keys: list[str] = []
        elif isinstance(raw_keys, list):
            keys = list(raw_keys)
        else:
            raise HarnessError("config_invalid", "keys must be a JSON array.")
        raw_section = body.get("section")
        if raw_section is None:
            section = None
        elif isinstance(raw_section, str) and raw_section.strip():
            section = raw_section.strip()
        else:
            raise HarnessError("config_invalid", "section must be a non-empty string.")
        return self.server.operation_manager.run_when_idle(
            lambda: self.server.config_service.reset(keys=keys, section=section)
        )

    def _read_json_body(self) -> dict[str, object]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise HarnessError("invalid_http_request", "Content-Length is required for JSON requests.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HarnessError("invalid_http_request", "Content-Length must be an integer.") from exc
        if length < 0:
            raise HarnessError("invalid_http_request", "Content-Length must not be negative.")
        if length > self.server.config.max_request_body_bytes:
            raise HarnessError("payload_too_large", "Request body exceeds the API limit.")
        if length == 0:
            raise HarnessError("invalid_json", "JSON request body is required.")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError("invalid_json", "Request body must be valid UTF-8 JSON.") from exc
        if not isinstance(value, dict):
            raise HarnessError("invalid_request", "Request body must be a JSON object.")
        return value

    def _require_authorized(self) -> None:
        expected = self.server.config.token
        if not expected:
            return
        header = self.headers.get("Authorization", "")
        scheme, separator, credential = header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(credential, expected):
            raise HarnessError("unauthorized", "Missing or invalid bearer token.")

    def _request_body_too_large(self) -> bool:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return False
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HarnessError("invalid_http_request", "Content-Length must be an integer.") from exc
        if length < 0:
            raise HarnessError("invalid_http_request", "Content-Length must not be negative.")
        return length > self.server.config.max_request_body_bytes

    @staticmethod
    def _require_no_query(query: str) -> None:
        if query:
            raise HarnessError("invalid_query", "This endpoint does not accept query parameters.")

    @staticmethod
    def _read_limit(query: str) -> int:
        if not query:
            return DEFAULT_RUN_LIMIT
        values = parse_qs(query, keep_blank_values=True)
        if set(values) != {"limit"} or len(values["limit"]) != 1:
            raise HarnessError("invalid_query", "Only one `limit` query parameter is supported.")
        try:
            return int(values["limit"][0])
        except ValueError as exc:
            raise HarnessError("invalid_query", "limit must be an integer.") from exc

    @staticmethod
    def _read_event_query(query: str) -> tuple[int, int]:
        if not query:
            return 0, DEFAULT_EVENT_LIMIT
        values = parse_qs(query, keep_blank_values=True)
        if not set(values).issubset({"after", "limit"}) or any(len(items) != 1 for items in values.values()):
            raise HarnessError("invalid_query", "Only one `after` and one `limit` query parameter are supported.")
        try:
            after = int(values.get("after", ["0"])[0])
            limit = int(values.get("limit", [str(DEFAULT_EVENT_LIMIT)])[0])
        except ValueError as exc:
            raise HarnessError("invalid_query", "after and limit must be integers.") from exc
        if after < 0:
            raise HarnessError("invalid_query", "after must be an integer >= 0.")
        if not 1 <= limit <= MAX_EVENT_LIMIT:
            raise HarnessError("invalid_query", f"limit must be an integer from 1 to {MAX_EVENT_LIMIT}.")
        return after, limit

    def _write_harness_error(self, exc: HarnessError) -> None:
        status = _http_status_for_error(exc.error_code)
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        self._write_json(status, exc.to_response(), headers=headers)

    def _write_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _write_bytes(self, status: int, body: bytes, *, media_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def create_server(
    *,
    config: ApiServerConfig | None = None,
    root: Path | None = None,
    runtime_root: Path | None = None,
    read_service: ApplicationReadService | None = None,
    operation_manager: OperationManager | None = None,
    config_service: ConfigurationService | None = None,
) -> SdlcHttpServer:
    root_path = (root or repo_root()).resolve()
    settings = load_settings(root=root_path, runtime_root=runtime_root)
    server_config = config or ApiServerConfig.from_settings(settings)
    server_config.validate()
    service = read_service or ApplicationReadService(root=root_path, runtime_root=settings.runtime_root)
    manager = operation_manager or OperationManager(
        store=OperationStore(runtime_root=settings.runtime_root),
        application_service=ApplicationService(root=root_path),
        queue_limit=server_config.queue_limit,
        public_value=service.public_value,
    )
    runtime_config_service = config_service or ConfigurationService(
        root=root_path,
        runtime_root=settings.runtime_root,
    )
    return SdlcHttpServer(
        (server_config.host, server_config.port),
        config=server_config,
        read_service=service,
        operation_manager=manager,
        config_service=runtime_config_service,
        runtime_root=settings.runtime_root,
    )


def serve_forever(*, server_factory: Callable[[], SdlcHttpServer] | None = None) -> int:
    server = (server_factory or create_server)()
    shutdown_thread: Thread | None = None

    def request_shutdown(signum: int, frame: object) -> None:
        nonlocal shutdown_thread
        if shutdown_thread is None or not shutdown_thread.is_alive():
            shutdown_thread = Thread(target=server.shutdown, name="sdlc-api-shutdown", daemon=True)
            shutdown_thread.start()

    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
        if shutdown_thread is not None:
            shutdown_thread.join(timeout=server.config.shutdown_timeout_seconds)
        return 0
    finally:
        server.server_close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def build_parser() -> argparse.ArgumentParser:
    """Build the API process parser without changing config-file defaults."""

    parser = argparse.ArgumentParser(prog="sdlc-harness-api")
    parser.add_argument("--host", default=None, help="Override SDLC_API_HOST for this process.")
    parser.add_argument("--port", type=int, default=None, help="Override SDLC_API_PORT for this process.")
    parser.add_argument(
        "--runtime-root",
        default=None,
        help="Use an isolated runtime root instead of the repository .runtime directory.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        runtime_root = Path(args.runtime_root).expanduser() if args.runtime_root else None
        settings = load_settings(root=repo_root(), runtime_root=runtime_root)
        defaults = ApiServerConfig.from_settings(settings)
        config = ApiServerConfig(
            host=args.host if args.host is not None else defaults.host,
            port=args.port if args.port is not None else defaults.port,
            token=defaults.token,
            queue_limit=defaults.queue_limit,
            shutdown_timeout_seconds=defaults.shutdown_timeout_seconds,
            max_request_body_bytes=defaults.max_request_body_bytes,
        )
        config.validate()
        return serve_forever(
            server_factory=lambda: create_server(
                config=config,
                root=repo_root(),
                runtime_root=runtime_root,
            )
        )
    except HarnessError as exc:
        print(json.dumps(exc.to_response(), ensure_ascii=False), file=sys.stderr)
        return 1
    except OSError as exc:
        payload = {"ok": False, "error_code": "api_start_failed", "message": str(exc)}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1


def _http_status_for_error(error_code: str) -> int:
    if error_code == "unauthorized":
        return 401
    if error_code in {"run_not_found", "artifact_not_found", "operation_not_found", "route_not_found"}:
        return 404
    if error_code == "method_not_allowed":
        return 405
    if error_code in {"payload_too_large", "artifact_too_large"}:
        return 413
    if error_code in {
        "idempotency_conflict",
        "run_operation_in_progress",
        "run_action_locked",
        "source_action_locked",
        "config_busy",
        "config_locked",
        "legacy_execution_profile_retired",
        "legacy_developer_executor_retired",
        "project_profile_required",
    }:
        return 409
    if error_code in {"operation_queue_full", "operation_manager_stopping"}:
        return 503
    if error_code in {
        "idempotency_key_invalid",
        "idempotency_key_required",
        "invalid_http_request",
        "invalid_json",
        "invalid_path_parameter",
        "invalid_query",
        "invalid_request",
        "config_invalid",
        "config_read_only",
    }:
        return 400
    return 500


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validated_identifier(value: str, *, kind: str) -> str:
    text = str(value).strip()
    if not text or not _SAFE_IDENTIFIER.fullmatch(text):
        raise HarnessError("invalid_path_parameter", f"Invalid {kind}.")
    return text


def _require_fields(
    value: dict[str, object],
    *,
    allowed: set[str],
    required: set[str],
) -> None:
    keys = {str(key) for key in value}
    unknown = sorted(keys - allowed)
    missing = sorted(required - keys)
    if unknown:
        raise HarnessError("invalid_request", f"Unknown request fields: {', '.join(unknown)}")
    if missing:
        raise HarnessError("invalid_request", f"Missing request fields: {', '.join(missing)}")


def _required_text(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise HarnessError("invalid_request", f"{key} must be a non-empty string.")
    return item.strip()


def _sync_mode(value: object) -> str:
    if value is None:
        return "mock"
    if not isinstance(value, str) or value not in {"mock", "real", "both"}:
        raise HarnessError("invalid_request", "sync_mode must be `mock`, `real`, or `both`.")
    return value


def _integer_argument(value: dict[str, object], key: str, *, minimum: int) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
        raise HarnessError("invalid_request", f"{key} must be an integer >= {minimum}.")
    return item


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
