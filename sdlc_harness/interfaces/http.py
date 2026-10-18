"""Stdlib HTTP read API for SDLC Harness."""

from __future__ import annotations

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
from ..application import ApplicationReadService
from ..application.read_service import DEFAULT_RUN_LIMIT, api_capabilities
from ..core.config import Settings, load_settings, repo_root
from ..core.errors import HarnessError


MAX_REQUEST_BODY_BYTES = 1024 * 1024
_RUN_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)$")
_ARTIFACT_ROUTE = re.compile(r"^/api/v1/runs/([^/]+)/artifacts/([^/]+)$")
_LOGGER = logging.getLogger("sdlc_harness.http")


@dataclass(frozen=True)
class ApiServerConfig:
    host: str = "127.0.0.1"
    port: int = 7788
    token: str = ""
    shutdown_timeout_seconds: int = 30
    max_request_body_bytes: int = MAX_REQUEST_BODY_BYTES

    @classmethod
    def from_settings(cls, settings: Settings) -> "ApiServerConfig":
        return cls(
            host=settings.api_host,
            port=settings.api_port,
            token=settings.api_token,
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
    ):
        self.config = config
        self.read_service = read_service
        super().__init__(server_address, SdlcRequestHandler)


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
            if path == "/api/v1/health":
                self._require_no_query(parsed.query)
                self._write_json(200, self._health_payload())
                return
            self._require_authorized()
            if path == "/api/v1/runs":
                limit = self._read_limit(parsed.query)
                self._write_json(200, self.server.read_service.list_runs(limit=limit))
                return
            if path == "/api/v1/dashboard/snapshot":
                limit = self._read_limit(parsed.query)
                self._write_json(200, self.server.read_service.dashboard_snapshot(limit=limit))
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
        self._unsupported_write_method()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        self._unsupported_write_method()

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
            raise HarnessError("method_not_allowed", "HTTP method is not available in Read API Slice 2.")
        except HarnessError as exc:
            self._write_harness_error(exc)

    def _health_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "service": "sdlc-harness",
            "version": __version__,
            "api_version": "v1",
            "status": "ready",
            "queue": {"available": False, "status": "not_implemented"},
            "capabilities": api_capabilities(),
        }

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


def create_server(
    *,
    config: ApiServerConfig | None = None,
    root: Path | None = None,
    runtime_root: Path | None = None,
    read_service: ApplicationReadService | None = None,
) -> SdlcHttpServer:
    root_path = (root or repo_root()).resolve()
    settings = load_settings(root=root_path, runtime_root=runtime_root)
    server_config = config or ApiServerConfig.from_settings(settings)
    server_config.validate()
    service = read_service or ApplicationReadService(root=root_path, runtime_root=settings.runtime_root)
    return SdlcHttpServer((server_config.host, server_config.port), config=server_config, read_service=service)


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


def main() -> int:
    try:
        return serve_forever()
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
    if error_code in {"run_not_found", "artifact_not_found", "route_not_found"}:
        return 404
    if error_code == "method_not_allowed":
        return 405
    if error_code in {"payload_too_large", "artifact_too_large"}:
        return 413
    if error_code in {"invalid_http_request", "invalid_path_parameter", "invalid_query"}:
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
