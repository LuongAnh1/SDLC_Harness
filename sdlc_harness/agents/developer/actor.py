"""Developer actor cho Phase 4: sinh hoặc sửa file từ DeveloperWorkPacket.

`act` tạo code/test lần đầu từ implementation plan đã duyệt. `repair` nhận
feedback từ Verification Runner, Validator hoặc QA và chỉ sửa các file trong `allowed_paths`.
Agent này luôn trả JSON file đầy đủ để workflow kiểm path, apply vào target repo,
chạy Verification Runner và Validator ở lớp ngoài. Nó không tự chạy git/test,
không commit, không mở PR và không được sửa requirement/design đã duyệt.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from ...core.errors import HarnessError
from ...llm.client import strip_code_fence


class DeveloperActLlmError(HarnessError):
    """Lỗi structured output có giữ raw response và metadata để debug."""

    def __init__(
        self,
        message: str,
        *,
        raw_response: str = "",
        llm_metadata: dict[str, object] | None = None,
    ):
        super().__init__("developer_act_failed", message)
        self.raw_response = raw_response
        self.llm_metadata = llm_metadata or {}


@dataclass(frozen=True)
class DeveloperActResult:
    """Kết quả Developer.act đã parse được từ LLM/fake mode."""

    summary: str
    files: list[dict[str, object]]
    raw_response: str = ""
    llm_metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "files": self.files,
        }


class DeveloperActAgent:
    """Vai Developer ở Phase 4: tạo/sửa nội dung file, chưa commit/test/PR."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def act(self, *, work_packet: dict[str, object]) -> DeveloperActResult:
        """Sinh changed files từ DeveloperWorkPacket."""

        if not self.llm_client:
            return _deterministic_developer_act(work_packet)
        payload, raw, llm_metadata = _complete_developer_json(
            llm_client=self.llm_client,
            system=(
                "You are Developer.act in a TraceDev-style SDLC harness. "
                "Return JSON only and do not include prose outside JSON."
            ),
            prompt=_developer_act_prompt(work_packet),
        )
        return DeveloperActResult(
            summary=str(payload.get("summary") or "").strip(),
            files=_dict_items(payload.get("files")),
            raw_response=raw,
            llm_metadata=llm_metadata,
        )

    def repair(
        self,
        *,
        work_packet: dict[str, object],
        repair_feedback: dict[str, object],
        repair_iteration: int,
    ) -> DeveloperActResult:
        """Sửa code/test theo feedback Verification, Validator hoặc QA mà không đổi requirement/design."""

        if not self.llm_client:
            result = _deterministic_developer_act(work_packet)
            return DeveloperActResult(
                summary=f"Fake Developer.repair iteration {repair_iteration} regenerated allowed files.",
                files=result.files,
            )
        payload, raw, llm_metadata = _complete_developer_json(
            llm_client=self.llm_client,
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
        return DeveloperActResult(
            summary=str(payload.get("summary") or "").strip(),
            files=_dict_items(payload.get("files")),
            raw_response=raw,
            llm_metadata=llm_metadata,
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
      "covered_design_elements": ["class:...", "method:...", "sequence:..."],
      "covered_requirement_ids": ["PRE-1", "MF-1"]
    }}
  ]
}}

Rules:
- Only return files listed in output_contract.allowed_paths.
- Return complete file content, not patches.
- Do not add behavior outside the approved requirement/design.
- When requirement_contract.version is v2, treat canonical_requirements and requirement_coverage as the source of truth and return only stable IDs assigned to that file in requirement_coverage_by_file.
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

You are repairing an implementation that failed Verification Runner, Validator code trace, or QA acceptance testing.
Return JSON only with the same shape as Developer Act v1:
{{
  "summary": "short repair summary",
  "files": [
    {{
      "path": "src/main/java/... or src/test/java/...",
      "operation": "create|update|create_or_update",
      "content": "complete final file content",
      "reason": "which Validator feedback item this fixes",
      "covered_design_elements": ["class:...", "method:...", "sequence:..."],
      "covered_requirement_ids": ["PRE-1", "MF-1"]
    }}
  ]
}}

Rules:
- Repair iteration: {repair_iteration}.
- Only return files listed in output_contract.allowed_paths.
- Return complete file content, not patches.
- Do not change requirements, approved design, implementation plan, branch, or tool-sync records.
- Preserve canonical stable requirement IDs and return only IDs assigned to each file in requirement_coverage_by_file.
- Fix the compile/test diagnostics, missing_links, weak trace evidence, or QA acceptance failures in RepairFeedback.
- Preserve package names and existing code style from current_files.
- Generated code and tests must work when the repository path contains spaces or Unicode characters.
- Use repository-relative paths; do not hard-code absolute paths from a developer machine.
- Use explicit UTF-8 for source/test/build-script handling when the language or toolchain supports it.
- If feedback cannot be repaired in allowed_paths, return an empty files list with a summary explaining why.
- Do not run tests, create commits, or mention PRs.

DeveloperWorkPacket:
{json.dumps(work_packet, ensure_ascii=False, indent=2)}

RepairFeedback:
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
                "covered_requirement_ids": _string_items(item.get("covered_requirement_ids")),
            }
        )
    return {
        "summary": str(parsed.get("summary") or "").strip(),
        "files": normalized_files,
    }


def _complete_developer_json(
    *,
    llm_client: Any,
    system: str,
    prompt: str,
) -> tuple[dict[str, object], str, dict[str, object]]:
    """Yêu cầu JSON mode và retry một lần khi output bị cắt hoặc không parse được."""

    settings = getattr(llm_client, "settings", None)
    max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
    completion_method = getattr(llm_client, "complete_with_metadata", None)
    attempts: list[dict[str, object]] = []
    raw_attempts: list[str] = []
    attempt_prompt = prompt
    last_error = "Developer.act LLM did not return valid JSON."

    for attempt in range(1, 3):
        if callable(completion_method):
            completion = completion_method(
                system=system,
                prompt=attempt_prompt,
                json_mode=True,
                max_tokens=max_tokens,
            )
            raw = str(getattr(completion, "content", ""))
            finish_reason = str(getattr(completion, "finish_reason", "") or "")
            usage_value = getattr(completion, "usage", {})
            usage = usage_value if isinstance(usage_value, dict) else {}
            json_mode_requested = True
        else:
            raw = str(llm_client.complete(system=system, prompt=attempt_prompt))
            finish_reason = ""
            usage = {}
            json_mode_requested = False

        raw_attempts.append(raw)
        attempt_metadata: dict[str, object] = {
            "attempt": attempt,
            "finish_reason": finish_reason,
            "usage": usage,
            "json_mode_requested": json_mode_requested,
            "max_tokens": max_tokens,
        }
        if finish_reason == "length":
            last_error = "Developer.act LLM output was truncated (finish_reason=length)."
            attempt_metadata["parse_status"] = "truncated"
        else:
            try:
                payload = _parse_developer_act_payload(raw)
            except HarnessError as exc:
                last_error = exc.message
                attempt_metadata["parse_status"] = "invalid"
                attempt_metadata["parse_error"] = exc.message
            else:
                attempt_metadata["parse_status"] = "valid"
                attempts.append(attempt_metadata)
                return payload, raw, {
                    "json_mode_requested": json_mode_requested,
                    "max_tokens": max_tokens,
                    "selected_attempt": attempt,
                    "attempts": attempts,
                }
        attempts.append(attempt_metadata)
        attempt_prompt = (
            prompt
            + "\n\nFORMAT RECOVERY RETRY\n"
            + f"The previous attempt was rejected: {last_error}\n"
            + "Return one complete, parseable JSON object only. Escape every newline and quote inside file content. "
            + "Include only files required for the approved work or repair feedback; omit unchanged files."
        )

    raw_response = "\n\n".join(
        f"=== attempt {index} ===\n{raw}" for index, raw in enumerate(raw_attempts, start=1)
    )
    raise DeveloperActLlmError(
        f"{last_error} Structured output failed after {len(attempts)} attempts.",
        raw_response=raw_response,
        llm_metadata={
            "json_mode_requested": callable(completion_method),
            "max_tokens": max_tokens,
            "selected_attempt": None,
            "attempts": attempts,
        },
    )


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
                "covered_requirement_ids": _covered_requirements_for_path(work_packet, path),
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


def _covered_requirements_for_path(work_packet: dict[str, object], path: str) -> list[str]:
    grouped = work_packet.get("requirement_coverage_by_file")
    if not isinstance(grouped, dict):
        return []
    items = grouped.get(path)
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        requirement_id = str(item.get("requirement_id") or "").strip()
        if requirement_id and requirement_id not in result:
            result.append(requirement_id)
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
