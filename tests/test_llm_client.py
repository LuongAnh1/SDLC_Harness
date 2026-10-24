"""Kiểm thử payload provider của LLM client mà không gọi mạng thật."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sdlc_harness.core.config import load_settings
from sdlc_harness.llm.client import LlmClient


class _FakeResponse:
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        ).encode("utf-8")


class LlmClientPayloadTests(unittest.TestCase):
    def _settings(self, root: Path, *, mode: str):
        return load_settings(
            root=root,
            runtime_root=root / ".runtime",
            overrides={
                "SDLC_LLM_MODE": mode,
                "SDLC_LLM_API_KEY": "test-key",
                "SDLC_LLM_MODEL": "deepseek-flash",
                "SDLC_LLM_THINKING_MODE": "disabled",
            },
        )

    def test_deepseek_payload_explicitly_disables_thinking(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            captured: dict[str, object] = {}

            def fake_urlopen(request, *, timeout):
                captured.update(json.loads(request.data.decode("utf-8")))
                self.assertEqual(timeout, 90)
                return _FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = LlmClient(self._settings(root, mode="deepseek")).complete(
                    system="system",
                    prompt="prompt",
                )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["model"], "deepseek-flash")
        self.assertEqual(captured["thinking"], {"type": "disabled"})

    def test_generic_openai_compatible_payload_omits_deepseek_extension(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            captured: dict[str, object] = {}

            def fake_urlopen(request, *, timeout):
                captured.update(json.loads(request.data.decode("utf-8")))
                return _FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                LlmClient(self._settings(root, mode="openai_compatible")).complete(
                    system="system",
                    prompt="prompt",
                )

        self.assertNotIn("thinking", captured)


if __name__ == "__main__":
    unittest.main()
