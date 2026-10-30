"""Kiểm thử payload provider của LLM client mà không gọi mạng thật."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sdlc_harness.core.config import load_settings
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmClient
from sdlc_harness.llm.json_utils import parse_first_json_object


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


class _IncompleteResponse(_FakeResponse):
    def read(self) -> bytes:
        raise http.client.IncompleteRead(b"", 1)


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

    def test_incomplete_response_is_reported_as_transport_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            client = LlmClient(self._settings(Path(temp_dir), mode="deepseek"))
            with patch("urllib.request.urlopen", return_value=_IncompleteResponse()):
                with self.assertRaises(HarnessError) as raised:
                    client.complete(system="system", prompt="prompt")

        self.assertEqual(raised.exception.error_code, "llm_transport_failed")

    def test_deepseek_payload_explicitly_disables_thinking(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            captured: dict[str, object] = {}

            def fake_urlopen(request, *, timeout):
                captured.update(json.loads(request.data.decode("utf-8")))
                self.assertEqual(timeout, 90)
                return _FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                client = LlmClient(self._settings(root, mode="deepseek"))
                result = client.complete(
                    system="system",
                    prompt="prompt",
                )

        self.assertEqual(result, "ok")
        self.assertEqual(captured["model"], "deepseek-flash")
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(
            client.usage_summary(),
            {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
                "reported_calls": 1,
            },
        )

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

    def test_per_call_deepseek_profile_overrides_model_effort_tokens_and_timeout(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            captured: dict[str, object] = {}
            captured_timeout = 0

            def fake_urlopen(request, *, timeout):
                nonlocal captured_timeout
                captured.update(json.loads(request.data.decode("utf-8")))
                captured_timeout = timeout
                return _FakeResponse()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                LlmClient(self._settings(root, mode="deepseek")).complete_with_metadata(
                    system="system",
                    prompt="prompt",
                    json_mode=True,
                    model="deepseek-v4-pro",
                    reasoning_effort="max",
                    max_tokens=65536,
                    timeout_seconds=300,
                )

        self.assertEqual(captured["model"], "deepseek-v4-pro")
        self.assertEqual(captured["thinking"], {"type": "enabled"})
        self.assertEqual(captured["reasoning_effort"], "max")
        self.assertEqual(captured["max_tokens"], 65536)
        self.assertNotIn("temperature", captured)
        self.assertEqual(captured_timeout, 300)


class JsonUtilsTests(unittest.TestCase):
    def test_parse_first_json_object_requires_all_expected_keys(self) -> None:
        malformed_outer_action = (
            '{"action":"design.propose_delta","arguments":{"delta":{"operations":'
            '[{"node":{"surface_contract":{"transitions":'
            '[{"from":"idle","action":"activate feature","to":"shown"}]}}}}]}}'
        )

        with self.assertRaises(json.JSONDecodeError):
            parse_first_json_object(
                malformed_outer_action,
                expected_keys={"action", "arguments"},
            )

    def test_parse_first_json_object_still_accepts_complete_embedded_object(self) -> None:
        parsed = parse_first_json_object(
            'result: {"action":"repo.read","arguments":{"path":"apps/web/src/App.tsx"}} done',
            expected_keys={"action", "arguments"},
        )

        self.assertEqual(parsed["action"], "repo.read")


if __name__ == "__main__":
    unittest.main()
