"""Phiên hỏi tuần tự tối đa năm câu cho Requirement clarification."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .clarification_v2 import _question_sort_key
from .model_v2 import RequirementModelV2


SESSION_VERSION = "requirement_clarification_session_v1"
SESSION_QUESTION_LIMIT = 5


def start_clarification_session(
    model: RequirementModelV2,
    *,
    previous: dict[str, object] | None = None,
) -> dict[str, object] | None:
    questions = [
        item
        for item in model.open_questions
        if isinstance(item, dict)
        and item.get("status") == "open"
        and item.get("effective_blocking") is True
    ]
    if not questions:
        return None
    questions.sort(key=_question_sort_key)
    previous_index = int(previous.get("session_index") or 0) if isinstance(previous, dict) else 0
    return {
        "schema_version": SESSION_VERSION,
        "session_index": previous_index + 1,
        "question_limit": SESSION_QUESTION_LIMIT,
        "question_ids": [str(item.get("id") or "") for item in questions[:SESSION_QUESTION_LIMIT]],
        "answered_question_ids": [],
        "decision_count": len(
            [item for item in model.decisions if isinstance(item, dict) and item.get("status") == "accepted"]
        ),
        "coverage_digest": "sha256:" + hashlib.sha256(
            json.dumps(
                [
                    {
                        "id": item.get("id"),
                        "affects": item.get("affects", []),
                        "impact": item.get("impact"),
                        "uncertainty": item.get("uncertainty"),
                    }
                    for item in questions
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "status": "active",
    }


def record_session_answer(session: dict[str, object], question_id: str) -> dict[str, object]:
    updated = dict(session)
    answered = [str(item) for item in session.get("answered_question_ids", []) if item]
    if question_id not in answered:
        answered.append(question_id)
    updated["answered_question_ids"] = answered
    updated["decision_count"] = int(session.get("decision_count") or 0) + 1
    return updated


def session_pending_ids(session: dict[str, object]) -> set[str]:
    ids = {str(item) for item in session.get("question_ids", []) if item}
    answered = {str(item) for item in session.get("answered_question_ids", []) if item}
    return ids.difference(answered)


def close_clarification_session(session: dict[str, object], *, exhausted: bool) -> dict[str, object]:
    updated = dict(session)
    updated["status"] = "exhausted" if exhausted else "completed"
    return updated
