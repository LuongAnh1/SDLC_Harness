"""Helpers parse JSON an toàn cho các LLM trả lời không hoàn toàn sạch.

Một số provider có thể bọc JSON trong markdown/prose, hoặc output bị cắt làm
`json.loads()` fail ngay cả khi phần đầu đã có object hợp lệ. Các workflow dùng
helper này để chỉ nhận object/list có schema mong đợi và block rõ ràng khi
không parse được, thay vì tự suy diễn rồi ghi state sai.
"""

from __future__ import annotations

import json
from typing import Any

from .client import strip_code_fence


def parse_first_json_object(raw: str, *, expected_keys: set[str] | None = None) -> dict[str, Any]:
    """Tìm JSON object đầu tiên có key mong đợi trong raw LLM response."""

    text = strip_code_fence(raw).strip()
    decoder = json.JSONDecoder()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and _matches_keys(parsed, expected_keys):
        return parsed
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and _matches_keys(candidate, expected_keys):
            return candidate
    raise json.JSONDecodeError("No matching JSON object found", text, 0)


def parse_first_json_array(raw: str) -> list[Any]:
    """Tìm JSON array đầu tiên trong raw LLM response."""

    text = strip_code_fence(raw).strip()
    decoder = json.JSONDecoder()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return parsed
    for index, char in enumerate(text):
        if char != "[":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, list):
            return candidate
    raise json.JSONDecodeError("No matching JSON array found", text, 0)


def _matches_keys(parsed: dict[str, Any], expected_keys: set[str] | None) -> bool:
    if not expected_keys:
        return True
    return bool(expected_keys.intersection(parsed.keys()))
