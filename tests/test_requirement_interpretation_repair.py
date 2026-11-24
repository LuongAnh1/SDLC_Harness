"""Kiểm thử bước sửa các interpretation mà coverage reviewer đánh giá là sai (không hỏi người dùng)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sdlc_harness.agents.requirements import (
    RequirementInterpretationRepairer,
    normalize_and_validate_requirement_model_v2,
)
from sdlc_harness.core.errors import HarnessError
from sdlc_harness.llm.client import LlmCompletion

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "fixtures/etour/UC2.txt").read_text(encoding="utf-8")
FINDING = {
    "requirement_ids": ["AC-2"],
    "evidence_refs": [],
    "choice": "AC-2 stores any valid data.",
    "verdict": "needs_fix",
    "reason": "BR-1 rejects duplicates, so AC-2 contradicts it.",
    "fix": "Say that only non-duplicate data is stored.",
}


class Scripted:
    def __init__(self, *payloads: object):
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []
        self.settings = SimpleNamespace(llm_structured_max_tokens=4096)

    def complete_with_metadata(self, **kwargs: object) -> LlmCompletion:
        self.calls.append(dict(kwargs))
        value = self.payloads.pop(0)
        return LlmCompletion(
            content=value if isinstance(value, str) else json.dumps(value),
            finish_reason="stop",
            usage={},
        )


def _model():
    payload = json.loads((ROOT / "tests/fixtures/requirement_v2/uc2.json").read_text(encoding="utf-8"))
    return normalize_and_validate_requirement_model_v2(payload, {"SRC-1": SOURCE}).model


def _update(requirement_id: str = "AC-2") -> dict[str, object]:
    return {
        "requirement_id": requirement_id,
        "fields": {"text": "After valid, non-duplicate data is confirmed, the system stores the new heritage."},
        "reason": "Resolves the AC-2 finding.",
    }


def _apply(client: Scripted, findings: list[dict[str, object]] | None = None):
    return RequirementInterpretationRepairer(client).apply(
        model=_model(),
        source_text_by_id={"SRC-1": SOURCE},
        findings=findings or [FINDING],
    )


def test_repair_updates_only_named_requirements_as_a_new_revision() -> None:
    client = Scripted({"updates": [_update()]})

    result = _apply(client)

    assert result.report.ok
    assert result.model.revision == 2
    criterion = next(item for item in result.model.to_dict()["acceptance_criteria"] if item["id"] == "AC-2")
    assert "non-duplicate" in criterion["text"]
    assert result.updates == [{"requirement_id": "AC-2", "reason": "Resolves the AC-2 finding."}]
    assert "Say that only non-duplicate data is stored." in str(client.calls[0]["prompt"])


@pytest.mark.parametrize(
    ("proposal", "message"),
    [
        ({"updates": [_update("BR-1")]}, "only requirements named by a finding"),
        ({"updates": [_update(), _update()]}, "Update each requirement once"),
        ({"updates": []}, "non-empty list"),
        ({"updates": [{**_update(), "fields": {"status": "explicit"}}]}, "forbidden fields: status"),
        ({"updates": [{**_update(), "reason": " "}]}, "needs non-empty fields and a reason"),
    ],
)
def test_out_of_contract_repair_fails_closed_after_one_retry(proposal: dict[str, object], message: str) -> None:
    client = Scripted(proposal, proposal)

    with pytest.raises(HarnessError) as raised:
        _apply(client)

    assert raised.value.error_code == "requirement_interpretation_repair_failed"
    assert message in raised.value.message
    assert len(client.calls) == 2


def test_every_finding_needs_an_update() -> None:
    second = {**FINDING, "requirement_ids": ["BR-1"], "fix": "Keep BR-1 consistent."}
    client = Scripted({"updates": [_update()]}, {"updates": [_update()]})

    with pytest.raises(HarnessError, match=r"unaddressed findings: \[1\]"):
        _apply(client, [FINDING, second])
