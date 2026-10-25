"""Kiểm thử contract dùng chung cho kết quả tool của role loop."""

from __future__ import annotations

import unittest

from sdlc_harness.core.errors import HarnessError
from sdlc_harness.loop import RoleToolResultV1, canonical_json_hash


class RoleToolResultV1Test(unittest.TestCase):
    def test_canonical_hash_is_stable_across_key_order(self) -> None:
        self.assertEqual(canonical_json_hash({"b": 2, "a": 1}), canonical_json_hash({"a": 1, "b": 2}))

    def test_round_trip_preserves_sanitized_audit_fields(self) -> None:
        result = RoleToolResultV1.build(
            call_id="RST-1",
            tool_id="source.read",
            status="succeeded",
            retryable=False,
            input_payload={"source_id": "SRC-1"},
            output_payload={"ok": True, "text": "private source text"},
            evidence_refs=["OBS-1", "OBS-1"],
        )

        restored = RoleToolResultV1.from_dict(result.to_dict())

        self.assertEqual(restored.evidence_refs, ("OBS-1",))
        self.assertEqual(restored.output_sha256, canonical_json_hash({"ok": True, "text": "private source text"}))
        self.assertNotIn("private source text", str(restored.to_dict()))

    def test_failed_result_requires_error(self) -> None:
        with self.assertRaises(HarnessError) as raised:
            RoleToolResultV1.build(
                call_id="RST-1",
                tool_id="source.read",
                status="failed",
                retryable=False,
                input_payload={},
                output_payload={},
            )
        self.assertEqual(raised.exception.error_code, "role_tool_result_invalid")


if __name__ == "__main__":
    unittest.main()
