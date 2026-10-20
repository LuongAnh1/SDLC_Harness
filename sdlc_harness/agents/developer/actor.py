"""Developer actor xử lý theo batch cho Phase 4.

Developer nhận work packet đã duyệt cùng snapshot worktree hiện tại và chỉ trả
về nội dung đầy đủ của các file thay đổi trong iteration này. Python sở hữu việc
xác thực, áp dụng thay đổi, Verification và Validator.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from ...core.errors import HarnessError
from ...llm.client import strip_code_fence
from .fake_generation import fake_developer_files
from .profile_context import developer_generation_context, developer_prompt_context


_FILE_HEADER = re.compile(r"^=== File: (.+?) ===\s*$", re.MULTILINE)


class DeveloperActLlmError(HarnessError):
    """Developer output error with raw response metadata."""

    def __init__(self, message: str, *, raw_response: str = "", llm_metadata: dict[str, object] | None = None):
        super().__init__("developer_act_failed", message)
        self.raw_response = raw_response
        self.llm_metadata = llm_metadata or {}


@dataclass(frozen=True)
class DeveloperActResult:
    """One batch of complete changed files returned by Developer."""

    summary: str
    files: list[dict[str, object]]
    raw_response: str = ""
    llm_metadata: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {"summary": self.summary, "files": self.files}


class DeveloperActAgent:
    """Generate one coherent implementation or repair batch per LLM call."""

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def act(self, *, work_packet: dict[str, object]) -> DeveloperActResult:
        if not self.llm_client:
            return _deterministic_developer_act(work_packet)
        files, raw, metadata = _complete_developer_file_blocks(
            llm_client=self.llm_client,
            system=(
                "You are the Developer in a TraceDev-style SDLC harness. "
                "Return only complete changed-file blocks in the required format."
            ),
            prompt=_developer_act_prompt(work_packet),
            work_packet=work_packet,
        )
        return DeveloperActResult(
            summary=f"Developer batch returned {len(files)} changed file(s).",
            files=files,
            raw_response=raw,
            llm_metadata=metadata,
        )

    def repair(
        self,
        *,
        work_packet: dict[str, object],
        repair_feedback: dict[str, object],
        repair_iteration: int,
    ) -> DeveloperActResult:
        if not self.llm_client:
            result = _deterministic_developer_act(work_packet)
            return DeveloperActResult(
                summary=f"Fake Developer repair batch {repair_iteration} regenerated allowed files.",
                files=result.files,
            )
        files, raw, metadata = _complete_developer_file_blocks(
            llm_client=self.llm_client,
            system=(
                "You are the Developer repairing the current worktree in a TraceDev-style SDLC harness. "
                "Return only complete changed-file blocks in the required format."
            ),
            prompt=_developer_repair_prompt(
                work_packet=work_packet,
                repair_feedback=repair_feedback,
                repair_iteration=repair_iteration,
            ),
            work_packet=work_packet,
        )
        return DeveloperActResult(
            summary=f"Developer repair batch {repair_iteration} returned {len(files)} changed file(s).",
            files=files,
            raw_response=raw,
            llm_metadata=metadata,
        )


def _developer_act_prompt(work_packet: dict[str, object]) -> str:
    generation_context = developer_prompt_context(work_packet)
    example_path = str(generation_context["example_path"])
    return f"""Developer Batch Act v2

Implement one coherent slice of the approved implementation plan using current_files in the DeveloperWorkPacket.
Choose a slice that fits in one response. Other approved files may remain for later Validator-guided iterations,
but every file returned in this iteration must be internally consistent.

OUTPUT FORMAT
- Output plain text only.
- Output only files that are modified or newly created.
- Do not output JSON, Markdown fences, explanations, summaries, patches, or tool actions.
- Return the complete final content of each changed file using exactly:

=== File: {example_path} ===
<complete file content>

Rules:
- Every path must be listed in output_contract.allowed_paths and may appear only once.
- Return complete file content, never a diff.
- Preserve correct existing behavior and package/style conventions from current_files.
- Follow only the approved requirement, design, implementation plan, and stable requirement mappings.
- Do not run tests, commit, create a PR, or modify requirement/design artifacts.
- Use repository-relative paths and explicit UTF-8 where the toolchain supports it.
- Do not invent traceability metadata; Python derives it from the approved work packet.

DeveloperGenerationContext:
{json.dumps(generation_context, ensure_ascii=False, indent=2)}

DeveloperWorkPacket:
{json.dumps(work_packet, ensure_ascii=False, indent=2)}
""".strip()


def _developer_repair_prompt(
    *,
    work_packet: dict[str, object],
    repair_feedback: dict[str, object],
    repair_iteration: int,
) -> str:
    generation_context = developer_prompt_context(work_packet)
    example_path = str(generation_context["example_path"])
    return f"""Developer Batch Repair v2

Repair the current worktree from the latest Verification, Validator, or QA feedback below. Treat current_files as
the source of truth. Diagnose the root cause, then return one coherent set of complete changed files. When feedback
is large, fix the largest internally consistent subset that fits in one response; quality gates will rerun.

Repair iteration: {repair_iteration}

OUTPUT FORMAT
- Output plain text only.
- Output only files that are modified or newly created.
- Do not output JSON, Markdown fences, explanations, summaries, patches, or tool actions.
- Return the complete final content of each changed file using exactly:

=== File: {example_path} ===
<complete file content>

Rules:
- Every path must be listed in output_contract.allowed_paths and may appear only once.
- Return complete file content, never a diff.
- Fix the latest feedback against current_files; do not regenerate unrelated files.
- Keep production code, callers, implementations, and tests internally consistent.
- Preserve the approved requirement, design, implementation plan, and stable requirement mappings.
- Do not run tests, commit, create a PR, or modify requirement/design artifacts.
- Do not invent traceability metadata; Python derives it from the approved work packet.

DeveloperGenerationContext:
{json.dumps(generation_context, ensure_ascii=False, indent=2)}

DeveloperWorkPacket:
{json.dumps(work_packet, ensure_ascii=False, indent=2)}

LatestRepairFeedback:
{json.dumps(repair_feedback, ensure_ascii=False, indent=2)}
""".strip()


def _complete_developer_file_blocks(
    *,
    llm_client: Any,
    system: str,
    prompt: str,
    work_packet: dict[str, object],
) -> tuple[list[dict[str, object]], str, dict[str, object]]:
    """Call the LLM once per batch, with one bounded format-recovery retry."""

    settings = getattr(llm_client, "settings", None)
    max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
    completion_method = getattr(llm_client, "complete_with_metadata", None)
    attempts: list[dict[str, object]] = []
    raw_attempts: list[str] = []
    attempt_prompt = prompt
    last_error = "Developer returned no valid changed-file blocks."

    for attempt in range(1, 3):
        if callable(completion_method):
            completion = completion_method(
                system=system,
                prompt=attempt_prompt,
                json_mode=False,
                max_tokens=max_tokens,
            )
            raw = str(getattr(completion, "content", ""))
            finish_reason = str(getattr(completion, "finish_reason", "") or "")
            usage_value = getattr(completion, "usage", {})
            usage = usage_value if isinstance(usage_value, dict) else {}
        else:
            raw = str(llm_client.complete(system=system, prompt=attempt_prompt))
            finish_reason = ""
            usage = {}

        raw_attempts.append(raw)
        attempt_metadata: dict[str, object] = {
            "attempt": attempt,
            "finish_reason": finish_reason,
            "usage": usage,
            "format": "changed_file_blocks_v1",
            "max_tokens": max_tokens,
        }
        if finish_reason == "length":
            last_error = "Developer output was truncated (finish_reason=length)."
            attempt_metadata["parse_status"] = "truncated"
        else:
            try:
                files = _parse_developer_file_blocks(raw, work_packet=work_packet)
            except HarnessError as exc:
                last_error = exc.message
                attempt_metadata["parse_status"] = "invalid"
                attempt_metadata["parse_error"] = exc.message
            else:
                attempt_metadata["parse_status"] = "valid"
                attempts.append(attempt_metadata)
                return files, raw, {
                    "format": "changed_file_blocks_v1",
                    "max_tokens": max_tokens,
                    "selected_attempt": attempt,
                    "attempts": attempts,
                }
        attempts.append(attempt_metadata)
        attempt_prompt = (
            prompt
            + "\n\nFORMAT RECOVERY RETRY\n"
            + f"The previous response was rejected: {last_error}\n"
            + "Return fewer changed files if necessary. Start immediately with an exact "
            + "=== File: <approved path> === header and include complete file contents."
        )

    combined = "\n\n".join(
        f"=== attempt {index} ===\n{raw}" for index, raw in enumerate(raw_attempts, start=1)
    )
    raise DeveloperActLlmError(
        f"{last_error} Developer batch output failed after {len(attempts)} attempts.",
        raw_response=combined,
        llm_metadata={
            "format": "changed_file_blocks_v1",
            "max_tokens": max_tokens,
            "selected_attempt": None,
            "attempts": attempts,
        },
    )


def _parse_developer_file_blocks(
    raw: str,
    *,
    work_packet: dict[str, object],
) -> list[dict[str, object]]:
    text = strip_code_fence(raw).strip()
    matches = list(_FILE_HEADER.finditer(text))
    if not matches:
        return _parse_legacy_json_files(text, work_packet=work_packet)
    if text[: matches[0].start()].strip():
        raise HarnessError(
            "developer_act_failed",
            "Developer response contained text before the first changed-file header.",
        )

    files: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        path = match.group(1).strip().replace("\\", "/").lstrip("/")
        if path in seen:
            raise HarnessError("developer_act_failed", f"Developer returned duplicate file path: {path}")
        seen.add(path)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end() : end].strip()
        if not path or not content:
            raise HarnessError(
                "developer_act_failed",
                "Developer returned a changed-file block with a missing path or empty content.",
            )
        files.append(_developer_file(path=path, content=content, work_packet=work_packet))
    return files


def _parse_legacy_json_files(
    text: str,
    *,
    work_packet: dict[str, object],
) -> list[dict[str, object]]:
    """Read old JSON output during migration; new prompts never request it."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "developer_act_failed",
            "Developer returned neither changed-file blocks nor valid legacy JSON.",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise HarnessError("developer_act_failed", "Developer returned no changed files.")
    files: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in payload["files"]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
        content = item.get("content")
        if not path or not isinstance(content, str) or not content.strip():
            raise HarnessError("developer_act_failed", "Legacy Developer JSON contains an invalid file.")
        if path in seen:
            raise HarnessError("developer_act_failed", f"Developer returned duplicate file path: {path}")
        seen.add(path)
        files.append(_developer_file(path=path, content=content.strip(), work_packet=work_packet))
    if not files:
        raise HarnessError("developer_act_failed", "Developer returned no changed files.")
    return files


def _developer_file(*, path: str, content: str, work_packet: dict[str, object]) -> dict[str, object]:
    return {
        "path": path,
        "operation": "create_or_update",
        "content": content,
        "reason": "Developer batch changed this file for the current approved slice.",
        "covered_design_elements": _covered_elements_for_path(work_packet, path),
        "covered_requirement_ids": _covered_requirements_for_path(work_packet, path),
    }


def _deterministic_developer_act(work_packet: dict[str, object]) -> DeveloperActResult:
    files = [
        _developer_file(
            path=str(item["path"]),
            content=str(item["content"]),
            work_packet=work_packet,
        )
        for item in fake_developer_files(work_packet)
    ]
    context = developer_generation_context(work_packet)
    return DeveloperActResult(
        summary=f"Fake Developer generated deterministic {context['language_name']} changes.",
        files=files,
    )


def _covered_elements_for_path(work_packet: dict[str, object], path: str) -> list[str]:
    grouped = work_packet.get("design_mappings_by_file")
    if not isinstance(grouped, dict) or not isinstance(grouped.get(path), list):
        return []
    result: list[str] = []
    for item in grouped[path]:
        if isinstance(item, dict):
            value = str(item.get("design_element_id") or "").strip()
            if value and value not in result:
                result.append(value)
    return result


def _covered_requirements_for_path(work_packet: dict[str, object], path: str) -> list[str]:
    grouped = work_packet.get("requirement_coverage_by_file")
    if not isinstance(grouped, dict) or not isinstance(grouped.get(path), list):
        return []
    result: list[str] = []
    for item in grouped[path]:
        if isinstance(item, dict):
            value = str(item.get("requirement_id") or "").strip()
            if value and value not in result:
                result.append(value)
    return result
