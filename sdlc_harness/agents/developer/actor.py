"""Developer.act cho Phase 4: sinh file changes từ DeveloperWorkPacket.

Phase 4.2 mới chỉ tạo code change để human review thủ công. Agent này nhận
packet đã được chuẩn bị ở Slice 4.1, yêu cầu LLM trả JSON theo output contract,
rồi để workflow kiểm path và apply vào target repo. Nó không tự chạy git/test.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from ...core.errors import HarnessError
from ...llm.client import strip_code_fence


@dataclass(frozen=True)
class DeveloperActResult:
    """Kết quả Developer.act đã parse được từ LLM/fake mode."""

    summary: str
    files: list[dict[str, object]]
    raw_response: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "files": self.files,
        }


class DeveloperActAgent:
    """Vai Developer ở Phase 4: tạo nội dung file, chưa commit/test/PR."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def act(self, *, work_packet: dict[str, object]) -> DeveloperActResult:
        """Sinh changed files từ DeveloperWorkPacket."""

        if not self.llm_client:
            return _deterministic_developer_act(work_packet)
        raw = self.llm_client.complete(
            system=(
                "You are Developer.act in a TraceDev-style SDLC harness. "
                "Return JSON only and do not include prose outside JSON."
            ),
            prompt=_developer_act_prompt(work_packet),
        )
        payload = _parse_developer_act_payload(raw)
        return DeveloperActResult(
            summary=str(payload.get("summary") or "").strip(),
            files=_dict_items(payload.get("files")),
            raw_response=raw,
        )

    def repair(
        self,
        *,
        work_packet: dict[str, object],
        repair_feedback: dict[str, object],
        repair_iteration: int,
    ) -> DeveloperActResult:
        """Sửa code/test theo feedback traceability mà không đổi requirement/design."""

        if not self.llm_client:
            result = _deterministic_developer_act(work_packet)
            return DeveloperActResult(
                summary=f"Fake Developer.repair iteration {repair_iteration} regenerated allowed files.",
                files=result.files,
            )
        raw = self.llm_client.complete(
            system=(
                "You are Developer.update in a TraceDev-style SDLC harness. "
                "Repair only code/test files and return JSON only."
            ),
            prompt=_developer_repair_prompt(
                work_packet=work_packet,
                repair_feedback=repair_feedback,
                repair_iteration=repair_iteration,
            ),
        )
        payload = _parse_developer_act_payload(raw)
        return DeveloperActResult(
            summary=str(payload.get("summary") or "").strip(),
            files=_dict_items(payload.get("files")),
            raw_response=raw,
        )


def _developer_act_prompt(work_packet: dict[str, object]) -> str:
    return f"""Developer Act v1

You are implementing exactly one approved SDLC checkpoint.
Read the DeveloperWorkPacket and return JSON only with this shape:
{{
  "summary": "short implementation summary",
  "files": [
    {{
      "path": "src/main/java/...",
      "operation": "create|update|create_or_update",
      "content": "complete final file content",
      "reason": "why this file is changed",
      "covered_design_elements": ["class:...", "method:...", "sequence:..."]
    }}
  ]
}}

Rules:
- Only return files listed in output_contract.allowed_paths.
- Return complete file content, not patches.
- Do not add behavior outside the approved requirement/design.
- Preserve package names and existing code style from current_files.
- If a file already exists, include the full updated content.
- Generated code, tests, and helper scripts must work when the repository path contains spaces or Unicode characters.
- Use repository-relative paths; do not hard-code absolute paths from a developer machine.
- Use explicit UTF-8 for source/test/build-script handling when the language or toolchain supports it.
- If required context is missing, return an empty files list with a summary explaining why.
- Do not run tests, create commits, or mention PRs.

DeveloperWorkPacket:
{json.dumps(work_packet, ensure_ascii=False, indent=2)}
""".strip()


def _developer_repair_prompt(
    *,
    work_packet: dict[str, object],
    repair_feedback: dict[str, object],
    repair_iteration: int,
) -> str:
    return f"""Developer Repair v1

You are repairing an implementation that failed Validator code trace.
Return JSON only with the same shape as Developer Act v1:
{{
  "summary": "short repair summary",
  "files": [
    {{
      "path": "src/main/java/... or src/test/java/...",
      "operation": "create|update|create_or_update",
      "content": "complete final file content",
      "reason": "which Validator feedback item this fixes",
      "covered_design_elements": ["class:...", "method:...", "sequence:..."]
    }}
  ]
}}

Rules:
- Repair iteration: {repair_iteration}.
- Only return files listed in output_contract.allowed_paths.
- Return complete file content, not patches.
- Do not change requirements, approved design, implementation plan, branch, or tool-sync records.
- Fix the missing_links and weak trace evidence in ImplementationRepairFeedback.
- Preserve package names and existing code style from current_files.
- Generated code and tests must work when the repository path contains spaces or Unicode characters.
- Use repository-relative paths; do not hard-code absolute paths from a developer machine.
- Use explicit UTF-8 for source/test/build-script handling when the language or toolchain supports it.
- If feedback cannot be repaired in allowed_paths, return an empty files list with a summary explaining why.
- Do not run tests, create commits, or mention PRs.

DeveloperWorkPacket:
{json.dumps(work_packet, ensure_ascii=False, indent=2)}

ImplementationRepairFeedback:
{json.dumps(repair_feedback, ensure_ascii=False, indent=2)}
""".strip()


def _parse_developer_act_payload(raw: str) -> dict[str, object]:
    try:
        parsed = _parse_first_json_object(raw, expected_keys={"summary", "files"})
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "developer_act_failed",
            "Developer.act LLM did not return valid JSON.",
        ) from exc
    if not isinstance(parsed, dict):
        raise HarnessError("developer_act_failed", "Developer.act LLM must return a JSON object.")
    files = _dict_items(parsed.get("files"))
    if not files:
        raise HarnessError("developer_act_failed", "Developer.act returned no file changes.")
    normalized_files: list[dict[str, object]] = []
    for item in files:
        path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        operation = str(item.get("operation") or "").strip()
        content = item.get("content")
        if not path or operation not in {"create", "update", "create_or_update"} or not isinstance(content, str):
            raise HarnessError(
                "developer_act_failed",
                "Developer.act returned a file change with missing path, operation, or content.",
            )
        normalized_files.append(
            {
                "path": path,
                "operation": operation,
                "content": content,
                "reason": str(item.get("reason") or "").strip(),
                "covered_design_elements": _string_items(item.get("covered_design_elements")),
            }
        )
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "files": normalized_files,
    }


def _deterministic_developer_act(work_packet: dict[str, object]) -> DeveloperActResult:
    allowed_paths = _allowed_paths(work_packet)
    if not allowed_paths:
        raise HarnessError("developer_act_failed", "DeveloperWorkPacket has no allowed paths.")
    files: list[dict[str, object]] = []
    for path in allowed_paths:
        class_name = path.rsplit("/", 1)[-1].removesuffix(".java")
        if "/src/test/" in f"/{path}":
            content = _fake_test_content(class_name)
        else:
            content = _fake_source_content(class_name)
        files.append(
            {
                "path": path,
                "operation": "create_or_update",
                "content": content,
                "reason": "Fake mode implements the approved Phase 4 output contract.",
                "covered_design_elements": _covered_elements_for_path(work_packet, path),
            }
        )
    return DeveloperActResult(summary="Fake Developer.act generated deterministic Java changes.", files=files)


def _parse_first_json_object(raw: str, *, expected_keys: set[str]) -> dict[str, object]:
    text = strip_code_fence(raw).strip()
    decoder = json.JSONDecoder()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and expected_keys.intersection(parsed.keys()):
        return parsed
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and expected_keys.intersection(candidate.keys()):
            return candidate
    raise json.JSONDecodeError("No matching Developer.act JSON object found", text, 0)


def _allowed_paths(work_packet: dict[str, object]) -> list[str]:
    contract = work_packet.get("output_contract")
    if not isinstance(contract, dict):
        return []
    value = contract.get("allowed_paths")
    if not isinstance(value, list):
        return []
    return [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]


def _covered_elements_for_path(work_packet: dict[str, object], path: str) -> list[str]:
    grouped = work_packet.get("design_mappings_by_file")
    if not isinstance(grouped, dict):
        return []
    items = grouped.get(path)
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = str(item.get("design_element_id") or "").strip()
            if value:
                result.append(value)
    return result


def _fake_source_content(class_name: str) -> str:
    return f"""package com.sdlc.etour.heritage;

import java.util.HashSet;
import java.util.Objects;
import java.util.Set;

public final class {class_name} {{
    private final Set<String> deletedHeritageIds = new HashSet<>();
    private boolean operationInProgress;

    public boolean validateDeletion(String heritageId) {{
        return heritageId != null && !heritageId.isBlank() && !operationInProgress;
    }}

    public boolean deleteHeritage(String heritageId) {{
        if (!validateDeletion(heritageId)) {{
            return false;
        }}
        operationInProgress = true;
        try {{
            deletedHeritageIds.add(Objects.requireNonNull(heritageId));
            return true;
        }} finally {{
            operationInProgress = false;
        }}
    }}

    public void cancelDeletion() {{
        operationInProgress = false;
    }}

    public boolean isDeleted(String heritageId) {{
        return deletedHeritageIds.contains(heritageId);
    }}
}}
""".strip()


def _fake_test_content(class_name: str) -> str:
    return f"""package com.sdlc.etour.heritage;

public final class {class_name} {{
    public static void main(String[] args) {{
        DeleteCulturalHeritageService service = new DeleteCulturalHeritageService();
        if (!service.deleteHeritage("heritage-1")) {{
            throw new IllegalStateException("Expected delete operation to succeed");
        }}
        if (!service.isDeleted("heritage-1")) {{
            throw new IllegalStateException("Expected heritage id to be marked deleted");
        }}
    }}
}}
""".strip()


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
