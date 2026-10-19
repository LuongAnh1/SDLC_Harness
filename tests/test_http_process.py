"""Kiểm thử process entrypoint cho các API smoke run độc lập."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sdlc_harness.interfaces.http import main


class HttpProcessEntrypointTests(unittest.TestCase):
    def test_main_applies_process_overrides_without_changing_api_contract(self) -> None:
        sentinel = object()
        with TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {
                "SDLC_API_HOST": "127.0.0.1",
                "SDLC_API_PORT": "7788",
                "SDLC_API_TOKEN": "process-token",
            },
            clear=False,
        ), patch("sdlc_harness.interfaces.http.create_server", return_value=sentinel) as create_server, patch(
            "sdlc_harness.interfaces.http.serve_forever",
            return_value=0,
        ) as serve_forever:
            exit_code = main(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--runtime-root",
                    directory,
                ]
            )

            self.assertEqual(exit_code, 0)
            factory = serve_forever.call_args.kwargs["server_factory"]
            self.assertIs(factory(), sentinel)
            kwargs = create_server.call_args.kwargs
            self.assertEqual(kwargs["runtime_root"], Path(directory))
            self.assertEqual(kwargs["config"].host, "127.0.0.1")
            self.assertEqual(kwargs["config"].port, 0)
            self.assertEqual(kwargs["config"].token, "process-token")

    def test_main_rejects_invalid_process_port_as_json_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(["--port", "70000"])

        self.assertEqual(exit_code, 1)
        self.assertIn('"error_code": "api_config_invalid"', stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
