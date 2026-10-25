"""Contract audit tối thiểu cho kết quả tool của các role loop."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal

from ..core.errors import HarnessError
from ..storage.runtime import utc_now_iso


ROLE_TOOL_RESULT_VERSION = "role_tool_result_v1"
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESULT_STATUSES = {"succeeded", "rejected", "failed", "interrupted"}


def canonical_json_hash(value: object) -> str:
    """Băm JSON theo thứ tự ổn định để bind input/output audit."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HarnessError("role_tool_result_invalid", "Tool result payload is not canonical JSON.") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RoleToolResultV1:
    """Envelope đã sanitize; không chứa raw source hay raw model response."""

    call_id: str
    tool_id: str
    status: Literal["succeeded", "rejected", "failed", "interrupted"]
    retryable: bool
    input_sha256: str
    output_sha256: str
    evidence_refs: tuple[str, ...] = ()
    error: dict[str, object] | None = None
    recorded_at: str = ""

    @classmethod
    def build(
        cls,
        *,
        call_id: str,
        tool_id: str,
        status: Literal["succeeded", "rejected", "failed", "interrupted"],
        retryable: bool,
        input_payload: object,
        output_payload: object,
        evidence_refs: list[str] | tuple[str, ...] = (),
        error: dict[str, object] | None = None,
    ) -> "RoleToolResultV1":
        result = cls(
            call_id=call_id,
            tool_id=tool_id,
            status=status,
            retryable=retryable,
            input_sha256=canonical_json_hash(input_payload),
            output_sha256=canonical_json_hash(output_payload),
            evidence_refs=tuple(dict.fromkeys(str(item) for item in evidence_refs if str(item))),
            error=deepcopy(error),
            recorded_at=utc_now_iso(),
        )
        result.validate()
        return result

    @classmethod
    def from_dict(cls, value: object) -> "RoleToolResultV1":
        if not isinstance(value, dict):
            raise HarnessError("role_tool_result_invalid", "Role tool result must be an object.")
        if value.get("schema_version") != ROLE_TOOL_RESULT_VERSION:
            raise HarnessError("role_tool_result_invalid", "Role tool result version is invalid.")
        evidence = value.get("evidence_refs")
        error = value.get("error")
        result = cls(
            call_id=str(value.get("call_id") or ""),
            tool_id=str(value.get("tool_id") or ""),
            status=str(value.get("status") or ""),  # type: ignore[arg-type]
            retryable=value.get("retryable") is True,
            input_sha256=str(value.get("input_sha256") or ""),
            output_sha256=str(value.get("output_sha256") or ""),
            evidence_refs=tuple(evidence) if isinstance(evidence, list) else (),
            error=deepcopy(error) if isinstance(error, dict) else None,
            recorded_at=str(value.get("recorded_at") or ""),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not self.call_id or not self.tool_id or self.status not in _RESULT_STATUSES:
            raise HarnessError("role_tool_result_invalid", "Role tool result identity or status is invalid.")
        if not _HASH_PATTERN.fullmatch(self.input_sha256) or not _HASH_PATTERN.fullmatch(
            self.output_sha256
        ):
            raise HarnessError("role_tool_result_invalid", "Role tool result hash is invalid.")
        if any(not isinstance(item, str) or not item for item in self.evidence_refs):
            raise HarnessError("role_tool_result_invalid", "Role tool result evidence refs are invalid.")
        if self.status in {"failed", "interrupted"} and self.error is None:
            raise HarnessError("role_tool_result_invalid", "Failed tool result requires an error.")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": ROLE_TOOL_RESULT_VERSION,
            "call_id": self.call_id,
            "tool_id": self.tool_id,
            "status": self.status,
            "retryable": self.retryable,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "evidence_refs": list(self.evidence_refs),
            "error": deepcopy(self.error),
            "recorded_at": self.recorded_at or utc_now_iso(),
        }
