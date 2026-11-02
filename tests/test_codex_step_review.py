"""Regression tests for the controlled Codex step-review driver."""

from __future__ import annotations

import unittest

from scripts.codex_step_review import REVIEW_PROMPT, _exchange_action


class CodexStepReviewTest(unittest.TestCase):
    def test_labels_designer_tool_action(self) -> None:
        self.assertEqual(_exchange_action({"parsed_action": {"action": "repo.read"}}), "repo.read")

    def test_labels_reviewable_tool_output(self) -> None:
        self.assertEqual(_exchange_action({"parsed_output": {"action": "repo.read"}}), "repo.read")

    def test_structured_validator_output_is_not_invalid_json(self) -> None:
        self.assertEqual(
            _exchange_action({"parsed_output": {"ok": True, "assessments": [], "findings": []}}),
            "structured_output",
        )

    def test_missing_parsed_output_is_unparsed(self) -> None:
        self.assertEqual(_exchange_action({"raw_completion": "{"}), "unparsed")

    def test_review_contract_uses_harness_normalized_action(self) -> None:
        self.assertIn("judge the canonical parsed action", REVIEW_PROMPT)


if __name__ == "__main__":
    unittest.main()
