"""LLM adapter tối giản cho SDLC Harness.

Phase đầu có hai chế độ: `fake` để test không tốn API và `deepseek`/
`openai_compatible` để gọi API thật qua endpoint Chat Completions tương thích
OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import urllib.error
import urllib.request
import time

from ..core.config import Settings
from ..core.errors import HarnessError
from ..events import emit_current


@dataclass(frozen=True)
class LlmCompletion:
    """Nội dung cùng metadata cần để chẩn đoán output bị cắt hoặc sai schema."""

    content: str
    finish_reason: str = ""
    usage: dict[str, object] = field(default_factory=dict)


class LlmClient:
    """Adapter nhỏ để workflow không phụ thuộc trực tiếp SDK của provider."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, *, system: str, prompt: str) -> str:
        """Sinh text từ LLM hoặc fake output dùng cho unit/smoke test."""

        return self.complete_with_metadata(system=system, prompt=prompt).content

    def complete_with_metadata(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        """Sinh completion và giữ finish_reason/usage cho structured-output flow."""

        return self.complete_messages(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            json_mode=json_mode,
            max_tokens=max_tokens,
        )

    def complete_messages(
        self,
        *,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        """Complete a bounded transcript for controlled agent loops."""

        metadata = {
            "mode": self.settings.llm_mode,
            "model": self.settings.llm_model,
            "json_mode": json_mode,
        }
        emit_current(
            category="llm",
            event_type="llm_call_started",
            message="LLM call started.",
            data=metadata,
        )
        started = time.monotonic()
        try:
            if self.settings.llm_mode == "fake":
                prompt = next(
                    (str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"),
                    "",
                )
                completion = LlmCompletion(content=fake_completion(prompt), finish_reason="fake")
            elif self.settings.llm_mode in {"deepseek", "openai_compatible"}:
                completion = self._call_chat_completions_messages(
                    messages=messages,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                )
            else:
                raise HarnessError(
                    "requirement_refiner_failed",
                    f"Unsupported LLM mode: {self.settings.llm_mode}",
                )
        except Exception as exc:
            emit_current(
                category="llm",
                event_type="llm_call_failed",
                message="LLM call failed.",
                data={
                    **metadata,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_code": exc.error_code if isinstance(exc, HarnessError) else "internal_error",
                },
            )
            raise
        usage = completion.usage
        emit_current(
            category="llm",
            event_type="llm_call_completed",
            message="LLM call completed.",
            data={
                **metadata,
                "finish_reason": completion.finish_reason,
                "duration_seconds": round(time.monotonic() - started, 3),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        )
        return completion

    def _call_chat_completions(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        """Gọi API Chat Completions tương thích OpenAI bằng stdlib."""

        return self._call_chat_completions_messages(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            json_mode=json_mode,
            max_tokens=max_tokens,
        )

    def _call_chat_completions_messages(
        self,
        *,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        """Gọi Chat Completions với transcript đã được Harness kiểm soát."""

        if not self.settings.llm_api_key:
            raise HarnessError(
                "requirement_refiner_failed",
                "SDLC_LLM_API_KEY is required when SDLC_LLM_MODE is not fake.",
            )
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise HarnessError(
                "requirement_refiner_failed",
                f"LLM HTTP error {exc.code}: {body[:500]}",
            ) from exc
        except OSError as exc:
            raise HarnessError(
                "requirement_refiner_failed",
                f"LLM request failed: {exc}",
            ) from exc

        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = str(choice.get("finish_reason") or "")
            usage_value = data.get("usage")
            usage = usage_value if isinstance(usage_value, dict) else {}
        except (AttributeError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise HarnessError(
                "requirement_refiner_failed",
                "LLM response did not match Chat Completions schema.",
            ) from exc
        return LlmCompletion(
            content=strip_code_fence(str(content)).strip(),
            finish_reason=finish_reason,
            usage=usage,
        )


def strip_code_fence(text: str) -> str:
    """Bỏ markdown code fence nếu model bọc toàn bộ output trong fence."""

    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def fake_completion(prompt: str) -> str:
    """Fake output theo loại prompt để test không gọi API thật."""

    if "Requirement Model V2 Structured Output" in prompt or "Requirement Model V2 Bounded Repair" in prompt:
        return fake_requirement_model_v2(prompt)
    if "UML Audit / Report" in prompt and "updated Class Diagram" in prompt:
        return fake_design_model()
    if "both a Class Diagram and Sequence Diagrams" in prompt:
        return fake_design_model()
    if "very concise architecture recommendation" in prompt:
        return fake_architecture_recommendation()
    if "requirements-to-design traceability" in prompt and "Class diagram:" in prompt:
        return fake_traceability_class(prompt)
    if "scenario-based traceability" in prompt and "sequence diagram:" in prompt:
        return fake_traceability_sequence(prompt)
    if "Design Semantic Review v2" in prompt:
        return fake_design_semantic_review()
    if "Progressive Semantic Claim Review" in prompt:
        return fake_progressive_semantic_review(prompt)
    if "Progressive Targeted Repair Verification" in prompt:
        return fake_progressive_targeted_review(prompt)
    if "Progressive Bounded Semantic Adjudication" in prompt:
        return fake_progressive_adjudication(prompt)
    if "Implementation Plan LLM Review v1" in prompt:
        return fake_implementation_plan_review()
    if "Developer Implementation Plan v1" in prompt:
        return fake_developer_implementation_plan(prompt)
    if "Developer Batch Repair v2" in prompt:
        return fake_developer_repair(prompt)
    if "Developer Batch Act v2" in prompt:
        return fake_developer_act()
    if "Developer Repair v1" in prompt:
        return fake_developer_repair(prompt)
    if "Developer Act v1" in prompt:
        return fake_developer_act()
    if "Code Trace LLM Review v1" in prompt:
        return fake_code_trace_review()
    if "QA Tester Generate Acceptance Tests v1" in prompt:
        return fake_qa_generated_tests(prompt)
    if "QA Tester Acceptance Review v1" in prompt:
        return fake_qa_tester_review()
    return fake_refined_requirement(prompt)


def fake_progressive_semantic_review(prompt: str) -> str:
    """Return one satisfied assessment for every immutable experiment claim."""

    context = _fake_progressive_context(prompt)
    claims = context.get("claims") if isinstance(context, dict) else []
    assessments = []
    for item in claims if isinstance(claims, list) else []:
        if not isinstance(item, dict):
            continue
        refs = item.get("allowed_design_refs")
        assessments.append(
            {
                "claim_id": str(item.get("claim_id") or ""),
                "verdict": "satisfied",
                "category": "none",
                "message": "Covered by the traced fake design.",
                "expected_change": "",
                "design_refs": list(refs[:1]) if isinstance(refs, list) else [],
            }
        )
    return json.dumps({"assessments": assessments}, ensure_ascii=False)


def fake_progressive_targeted_review(prompt: str) -> str:
    context = _fake_progressive_context(prompt)
    candidates = context.get("candidates") if isinstance(context, dict) else []
    return json.dumps(
        {
            "results": [
                {
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "verdict": "resolved",
                    "evidence_refs": list(item.get("design_refs") or [])[:1],
                    "reason": "Resolved in the fake revised design.",
                }
                for item in candidates if isinstance(item, dict)
            ]
        },
        ensure_ascii=False,
    )


def fake_progressive_adjudication(prompt: str) -> str:
    context = _fake_progressive_context(prompt)
    candidates = context.get("candidates") if isinstance(context, dict) else []
    return json.dumps(
        {
            "decisions": [
                {
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "decision": "refuted",
                    "requirement_refs": [str(item.get("requirement_id") or "")],
                    "design_refs": list(item.get("design_refs") or []),
                    "reason": "The minority observation is not supported by the fake fixtures.",
                }
                for item in candidates if isinstance(item, dict)
            ]
        },
        ensure_ascii=False,
    )


def _fake_progressive_context(prompt: str) -> dict[str, object]:
    marker = "INPUT:\n"
    if marker not in prompt:
        return {}
    try:
        value = json.loads(prompt.split(marker, 1)[1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def fake_requirement_model_v2(prompt: str) -> str:
    """Sinh Requirement Model V2 tối thiểu, có evidence từ source snapshot."""

    source_ref_match = re.search(r"Source ref:\s*`([^`]+)`", prompt)
    source_ref = source_ref_match.group(1).strip() if source_ref_match else "fake:source"
    use_case_match = re.search(r"Use case:\s*`([^`]+)`", prompt)
    use_case_name = use_case_match.group(1).strip() if use_case_match else "FakeUseCase"
    marker = "## Source snapshot"
    source_text = prompt.split(marker, 1)[1] if marker in prompt else prompt
    source_text = source_text.split("\n## ", 1)[0].strip()
    evidence_quote = next((line.strip() for line in source_text.splitlines() if line.strip()), "Fake source requirement.")
    payload = {
        "schema_version": "requirement_model_v2",
        "revision": 1,
        "run_id": "fake_run",
        "source": {
            "source_ref": source_ref,
            "artifact_ref": "fake_source_snapshot",
            "fingerprint": "fake_fingerprint",
        },
        "use_case": {
            "id": "UC-1",
            "name": use_case_name,
            "goal": {
                "id": "GOAL-1",
                "text": evidence_quote,
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
            },
            "actors": [],
        },
        "preconditions": [
            {
                "id": "PRE-1",
                "text": evidence_quote,
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
            }
        ],
        "flows": {
            "main": [
                {
                    "id": "MF-1",
                    "actor": "System",
                    "action": evidence_quote,
                    "target": None,
                    "evidence_refs": ["E-1"],
                    "open_question_refs": [],
                }
            ],
            "alternatives": [],
            "exceptions": [],
        },
        "postconditions": [],
        "business_rules": [],
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "text": evidence_quote,
                "status": "explicit",
                "evidence_refs": ["E-1"],
                "open_question_refs": [],
                "verifies": ["MF-1"],
            }
        ],
        "quality_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "decisions": [],
        "evidence": [
            {
                "id": "E-1",
                "source_ref": source_ref,
                "location": "source snapshot",
                "quote": evidence_quote,
            }
        ],
        "validation": {"status": "valid", "errors": []},
    }
    return json.dumps(payload, ensure_ascii=False)


def fake_refined_requirement(prompt: str) -> str:
    """Requirement projection V2 ổn định cho các test downstream độc lập."""

    return """# Requirement Packet V2

## Goal
Cho phép Agency Operator xóa một cultural heritage object đã chọn trong hệ thống eTour.

## Actors
- Agency Operator
- eTour System

## Preconditions
- Agency Operator đã đăng nhập.
- Agency Operator đã tìm và chọn một cultural heritage object từ kết quả SearchCulturalHeritage.

## Main Flow
1. Agency Operator mở danh sách cultural heritage object từ kết quả tìm kiếm.
2. Agency Operator chọn một cultural heritage object.
3. Agency Operator kích hoạt chức năng xóa.
4. Hệ thống yêu cầu xác nhận thao tác xóa.
5. Agency Operator xác nhận thao tác.
6. Hệ thống xóa cultural heritage object đã chọn.
7. Hệ thống thông báo thao tác xóa thành công.

## Alternative / Exception Flows
- Agency Operator có thể hủy thao tác trước khi xác nhận.
- Kết nối tới server eTour có thể bị gián đoạn trong quá trình thao tác.

## Postconditions
- Cultural heritage object đã chọn không còn khả dụng trong hệ thống sau khi xóa thành công.
- Hệ thống hiển thị thông báo xóa thành công.

## Acceptance Criteria
- Khi Agency Operator xác nhận, hệ thống xóa cultural heritage object đã chọn.
- Khi xóa thành công, hệ thống hiển thị thông báo thành công.

## Quality / Non-functional Requirements
- Hệ thống chặn nhiều lần gửi xác nhận trong khi thao tác xóa/hủy đang được xử lý.

## Open Questions
- UC1 chưa nêu rõ quyền chi tiết của Agency Operator ngoài trạng thái đã đăng nhập.
- UC1 chưa mô tả kết quả mong muốn nếu mất kết nối tới server eTour giữa lúc xóa.
- UC1 chưa mô tả nội dung thông báo khi người dùng hủy thao tác.

## Source Evidence
- "Delete a cultural object in the system."
- "The agency has logged."
- "Asks confirmation" và "Confirm the operation".
- "The system shall notify the successful elimination".
- "Operator Agency cancels the operation."
- "Interruption of connection to the server ETOUR."
""".strip()


def fake_architecture_recommendation() -> str:
    """Khuyến nghị kiến trúc giả lập cho Designer.analysis."""

    return (
        "Use a layered architecture with a boundary/controller, domain service, "
        "and repository. This separates user interaction, business rules, and "
        "persistence concerns while keeping the use case small enough for direct "
        "traceability."
    )


def fake_design_model() -> str:
    """Design PlantUML ổn định cho Designer.act trong fake mode."""

    return """## Class Diagram
@startuml
class DeleteCulturalHeritageBoundary <<api>> {
  +requestDeletion(heritageId : String) : void
  +showConfirmation() : void
  +showResult(message : String) : void
}

class DeleteCulturalHeritageService <<api>> {
  +validateDeletion(heritageId : String) : boolean
  +deleteHeritage(heritageId : String) : void
  +cancelDeletion() : void
}

class CulturalHeritageRepository <<database>> {
  +findById(heritageId : String) : CulturalHeritage
  +delete(heritageId : String) : void
}

class CulturalHeritage <<contracts>> {
  - id : String
  - name : String
}

DeleteCulturalHeritageBoundary --> DeleteCulturalHeritageService
DeleteCulturalHeritageService --> CulturalHeritageRepository
CulturalHeritageRepository --> CulturalHeritage
@enduml

## Sequence Diagrams
### Sequence Diagram for Use Case:
@startuml
actor "Agency Operator" as AO
participant DeleteCulturalHeritageBoundary as Boundary
participant DeleteCulturalHeritageService as Service
database CulturalHeritageRepository as Repository

AO -> Boundary: requestDeletion(heritageId)
Boundary -> Boundary: showConfirmation()
AO -> Boundary: confirm deletion
Boundary -> Service: validateDeletion(heritageId)
Service -> Repository: findById(heritageId)
Repository --> Service: CulturalHeritage
Service -> Repository: delete(heritageId)
Repository --> Service: deletion saved
Service --> Boundary: success
Boundary --> AO: showResult(success message)
@enduml""".strip()


def fake_traceability_class(prompt: str) -> str:
    """Trace class giả lập theo requirement JSON trong prompt."""

    requirements = _extract_requirements_from_trace_prompt(prompt, next_marker="Class diagram:")
    trace = []
    for requirement in requirements:
        content = str(requirement.get("content", ""))
        trace.append(
            {
                "requirement": requirement.get("id"),
                "content": content,
                "links": _fake_class_links(content),
            }
        )
    return json.dumps(trace, ensure_ascii=False, indent=2)


def fake_traceability_sequence(prompt: str) -> str:
    """Trace sequence giả lập theo requirement JSON trong prompt."""

    requirements = _extract_requirements_from_trace_prompt(prompt, next_marker="sequence diagram:")
    trace = []
    for requirement in requirements:
        content = str(requirement.get("content", ""))
        trace.append(
            {
                "requirement": requirement.get("id"),
                "content": content,
                "links": _fake_sequence_links(content),
            }
        )
    return json.dumps(trace, ensure_ascii=False, indent=2)


def fake_implementation_plan_review() -> str:
    """LLM review giả lập cho Phase 3 implementation plan."""

    return json.dumps(
        {
            "ok": True,
            "summary": (
                "Implementation plan covers the approved requirement/design graph, "
                "keeps Phase 3 read-only, and gives Phase 4 enough code/test targets."
            ),
            "coverage_issues": [],
            "traceability_issues": [],
            "implementation_quality_issues": [],
            "test_issues": [],
            "scope_issues": [],
            "developer_feedback": "",
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_design_semantic_review() -> str:
    """Semantic design review fixture for offline Phase 2 tests."""

    return json.dumps(
        {
            "ok": True,
            "summary": "The fake design is behaviorally consistent with the approved requirement.",
            "findings": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_developer_implementation_plan(prompt: str = "") -> str:
    """Developer.plan giả lập để test Smart Developer không gọi API thật."""

    typescript = '"prompt_directive_id": "typescript_prompt_v1"' in prompt
    stage_only = '"policy_version": "verification_stage_policy_v1"' in prompt
    service_path = (
        "apps/api/src/DeleteCulturalHeritageService.ts"
        if typescript
        else "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java"
    )
    repository_path = (
        "packages/db/src/CulturalHeritageRepository.ts"
        if typescript
        else "src/main/java/com/sdlc/etour/heritage/CulturalHeritageRepository.java"
    )
    flow_test_path = (
        "apps/api/src/DeleteCulturalHeritageFlow.test.ts"
        if typescript
        else "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java"
    )
    source_evidence = (
        "Planning profile routes services to apps/api/src and repositories to packages/db/src."
        if typescript
        else "CodeGraph context found existing CulturalHeritageRepository in src/main/java."
    )
    test_evidence = (
        "Planning profile co-locates API tests under apps/api/src."
        if typescript
        else "Target repo already has a test folder under src/test/java/com/sdlc/etour/heritage."
    )

    return json.dumps(
        {
            "summary": "Plan uses CodeGraph context to map the approved delete heritage design into service, boundary, repository, and tests.",
            "repo_evidence": [source_evidence, test_evidence],
            "implementation_steps": [
                {
                    "order": 1,
                    "title": "Add delete flow application service",
                    "description": "Implement the approved validation, confirmation, delete, cancel, and result behavior.",
                    "files": [service_path, repository_path],
                    "design_refs": [
                        "class:DeleteCulturalHeritageService",
                        "method:DeleteCulturalHeritageService.deleteHeritage",
                    ],
                },
                {
                    "order": 2,
                    "title": "Verify delete flow",
                    "description": "Cover success, cancellation, interruption, and duplicate-submit prevention.",
                    "files": [flow_test_path],
                    "design_refs": ["sequence:delete_cultural_heritage_main:1"],
                },
            ],
            "proposed_files": [
                {
                    "path": service_path,
                    "action": "create",
                    "reason": "Holds the approved delete use-case behavior.",
                    "related_design_elements": ["class:DeleteCulturalHeritageService"],
                },
                {
                    "path": flow_test_path,
                    "action": "create",
                    "reason": "Verifies the approved sequence and exception flows.",
                    "related_design_elements": ["sequence:delete_cultural_heritage_main:1"],
                },
            ],
            "design_mappings": [],
            "test_intents": (
                [
                    {
                        "scope": "unit_or_service",
                        "intent": "Validate delete success, cancel, connection interruption, and duplicate submission prevention.",
                        "stage_id": "unit",
                    },
                    {
                        "scope": "regression",
                        "intent": "Run profile-owned static verification after Phase 4 code changes.",
                        "stage_id": "typecheck",
                    },
                ]
                if stage_only
                else [
                    {
                        "scope": "unit_or_service",
                        "intent": "Validate delete success, cancel, connection interruption, and duplicate submission prevention.",
                        "command": "powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
                    },
                    {
                        "scope": "regression",
                        "intent": "Run the target repo regression test command after Phase 4 code changes.",
                        "command": "powershell -ExecutionPolicy Bypass -File scripts/test.ps1",
                    },
                ]
            ),
            "assumptions": ["Repository can expose a delete operation or be extended in Phase 4."],
            "open_questions": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_developer_act() -> str:
    """Developer.act giả lập để test Phase 4 không gọi API thật."""

    return json.dumps(
        {
            "summary": "Fake Developer.act generated delete heritage code changes from the approved work packet.",
            "files": [
                {
                    "path": "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java",
                    "operation": "create_or_update",
                    "content": """package com.sdlc.etour.heritage;

import java.util.HashSet;
import java.util.Objects;
import java.util.Set;

public final class DeleteCulturalHeritageService {
    private final Set<String> deletedHeritageIds = new HashSet<>();
    private boolean operationInProgress;

    public boolean validateDeletion(String heritageId) {
        return heritageId != null && !heritageId.isBlank() && !operationInProgress;
    }

    public boolean deleteHeritage(String heritageId) {
        if (!validateDeletion(heritageId)) {
            return false;
        }
        operationInProgress = true;
        try {
            deletedHeritageIds.add(Objects.requireNonNull(heritageId));
            return true;
        } finally {
            operationInProgress = false;
        }
    }

    public void cancelDeletion() {
        operationInProgress = false;
    }

    public boolean isDeleted(String heritageId) {
        return deletedHeritageIds.contains(heritageId);
    }
}
""",
                    "reason": "Implements the approved delete, validate, cancel and duplicate-submit guard behavior.",
                    "covered_design_elements": [
                        "class:DeleteCulturalHeritageService",
                        "method:DeleteCulturalHeritageService.deleteHeritage",
                    ],
                },
                {
                    "path": "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageBoundary.java",
                    "operation": "create_or_update",
                    "content": """package com.sdlc.etour.heritage;

public final class DeleteCulturalHeritageBoundary {
    private final DeleteCulturalHeritageService service;
    private boolean confirmationShown;
    private String lastResult = "";

    public DeleteCulturalHeritageBoundary(DeleteCulturalHeritageService service) {
        this.service = service;
    }

    public boolean requestDeletion(String heritageId) {
        showConfirmation();
        boolean success = service.deleteHeritage(heritageId);
        showResult(success ? "success" : "failed");
        return success;
    }

    public void showConfirmation() {
        confirmationShown = true;
    }

    public boolean confirmdeletion(String heritageId) {
        return requestDeletion(heritageId);
    }

    public void showResult(String result) {
        lastResult = result;
    }

    public boolean wasConfirmationShown() {
        return confirmationShown;
    }

    public String lastResult() {
        return lastResult;
    }
}
""",
                    "reason": "Implements the approved boundary interaction from the sequence diagram.",
                    "covered_design_elements": [
                        "class:DeleteCulturalHeritageBoundary",
                        "method:DeleteCulturalHeritageBoundary.requestDeletion",
                        "method:DeleteCulturalHeritageBoundary.showConfirmation",
                        "method:DeleteCulturalHeritageBoundary.showResult",
                    ],
                },
                {
                    "path": "src/main/java/com/sdlc/etour/heritage/CulturalHeritageRepository.java",
                    "operation": "create_or_update",
                    "content": """package com.sdlc.etour.heritage;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public final class CulturalHeritageRepository {
    private final Map<String, CulturalHeritage> records = new HashMap<>();

    public Optional<CulturalHeritage> findById(String heritageId) {
        return Optional.ofNullable(records.get(heritageId));
    }

    public void save(String heritageId, CulturalHeritage heritage) {
        records.put(heritageId, heritage);
    }

    public boolean delete(String heritageId) {
        return records.remove(heritageId) != null;
    }
}
""",
                    "reason": "Implements repository operations referenced by the approved design.",
                    "covered_design_elements": [
                        "class:CulturalHeritageRepository",
                        "method:CulturalHeritageRepository.findById",
                        "method:CulturalHeritageRepository.delete",
                    ],
                },
                {
                    "path": "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java",
                    "operation": "create_or_update",
                    "content": """package com.sdlc.etour.heritage;

public final class DeleteCulturalHeritageFlowTest {
    public static void main(String[] args) {
        DeleteCulturalHeritageService service = new DeleteCulturalHeritageService();
        if (!service.deleteHeritage("heritage-1")) {
            throw new IllegalStateException("Expected delete operation to succeed");
        }
        if (!service.isDeleted("heritage-1")) {
            throw new IllegalStateException("Expected heritage id to be marked deleted");
        }
    }
}
""",
                    "reason": "Captures the approved success path for later test runner integration.",
                    "covered_design_elements": ["sequence:delete_cultural_heritage_main:1"],
                },
                {
                    "path": "src/test/java/com/sdlc/etour/heritage/CulturalHeritageRepositoryTest.java",
                    "operation": "create_or_update",
                    "content": """package com.sdlc.etour.heritage;

public final class CulturalHeritageRepositoryTest {
    public static void main(String[] args) {
        CulturalHeritageRepository repository = new CulturalHeritageRepository();
        repository.save("heritage-1", new CulturalHeritage());
        if (repository.findById("heritage-1").isEmpty()) {
            throw new IllegalStateException("Expected saved heritage to be found");
        }
        if (!repository.delete("heritage-1")) {
            throw new IllegalStateException("Expected repository delete to succeed");
        }
    }
}
""",
                    "reason": "Covers the repository file required by the approved implementation plan.",
                    "covered_design_elements": [
                        "class:CulturalHeritageRepository",
                        "method:CulturalHeritageRepository.findById",
                        "method:CulturalHeritageRepository.delete",
                    ],
                },
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_developer_repair(prompt: str) -> str:
    """Return a valid repair batch that differs from the current fake act output."""

    payload = json.loads(fake_developer_act())
    marker = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    for item in payload["files"]:
        item["content"] = f"{str(item['content']).rstrip()}\n\n// Fake repair marker: {marker}\n"
    payload["summary"] = "Fake Developer.repair generated an effective code change from current feedback."
    return json.dumps(payload, ensure_ascii=False, indent=2)


def fake_code_trace_review() -> str:
    """LLM review giả lập cho code trace hybrid rule + LLM."""

    return json.dumps(
        {
            "ok": True,
            "confidence": 0.91,
            "summary": "Fake LLM review accepts the requirement-design-code-test graph for deterministic tests.",
            "graph_edges": [
                {
                    "requirement_ref": "approved_requirement_packet",
                    "design_element_id": "class:DeleteCulturalHeritageService",
                    "code_ref": "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java#deleteHeritage",
                    "test_ref": "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java",
                    "evidence": "Service and flow test references align with the approved delete use case.",
                }
            ],
            "missing_links": [],
            "risks": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_qa_generated_tests(prompt: str = "") -> str:
    """Fake QA-generated tests so Phase 4 can execute the QA slice deterministically."""

    requirement_ids: list[str] = []
    marker = "QA Generation Packet:"
    if marker in prompt:
        try:
            packet = json.loads(prompt.split(marker, 1)[1].strip())
        except json.JSONDecodeError:
            packet = {}
        canonical = packet.get("canonical_requirements") if isinstance(packet, dict) else None
        if isinstance(canonical, list):
            requirement_ids = [
                str(item.get("id") or "").strip()
                for item in canonical
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]

    return json.dumps(
        {
            "summary": "Fake QA generated acceptance tests for the implemented delete heritage flow.",
            "test_files": [
                {
                    "path": "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageQaAcceptanceTest.java",
                    "content": """package com.sdlc.etour.heritage;

public final class DeleteCulturalHeritageQaAcceptanceTest {
    public static void main(String[] args) {
        DeleteCulturalHeritageService service = new DeleteCulturalHeritageService();
        if (!service.deleteHeritage(\"qa-heritage-1\")) {
            throw new IllegalStateException(\"Expected delete operation to succeed\");
        }
        if (!service.isDeleted(\"qa-heritage-1\")) {
            throw new IllegalStateException(\"Expected deleted heritage to be observable\");
        }
    }
}
""",
                    "scenarios": ["Delete cultural heritage success path"],
                }
            ],
            "scenarios": [
                {
                    "name": "Delete cultural heritage success path",
                    "expected_result": "Selected heritage is marked deleted after confirmation.",
                    "test_file": "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageQaAcceptanceTest.java",
                    "risk": "",
                    "requirement_ids": requirement_ids,
                }
            ],
            "risks": [],
        },
        ensure_ascii=False,
        indent=2,
    )
def fake_qa_tester_review() -> str:
    """QA review giả lập để Phase 4 có acceptance gate ổn định trong test."""

    return json.dumps(
        {
            "status": "pass",
            "summary": "Fake QA accepts the implemented use case after verification and code trace have passed.",
            "scenarios": [
                {
                    "name": "Delete cultural heritage happy path",
                    "result": "pass",
                    "evidence": "Requirement, design, test report, and code trace artifacts are present.",
                    "risk": "",
                }
            ],
            "evidence": ["Verification Runner passed before QA acceptance review."],
            "risks": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def _extract_requirements_from_trace_prompt(prompt: str, *, next_marker: str) -> list[dict[str, object]]:
    marker = "Requirements:"
    start = prompt.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = prompt.find(next_marker, start)
    if end < 0:
        end = len(prompt)
    raw = prompt[start:end].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _fake_class_links(content: str) -> list[dict[str, str]]:
    lowered = content.lower()
    if any(term in lowered for term in ("cancel", "hủy", "huy")):
        target = "DeleteCulturalHeritageService.cancelDeletion"
    elif any(term in lowered for term in ("confirm", "xác nhận", "xac nhan", "confirmation")):
        target = "DeleteCulturalHeritageBoundary.showConfirmation"
    elif any(term in lowered for term in ("notify", "thông báo", "thong bao", "success")):
        target = "DeleteCulturalHeritageBoundary.showResult"
    elif any(term in lowered for term in ("delete", "xóa", "xoa", "elimination")):
        target = "DeleteCulturalHeritageService.deleteHeritage"
    elif any(term in lowered for term in ("logged", "đăng nhập", "dang nhap", "selected", "chọn", "chon")):
        target = "DeleteCulturalHeritageBoundary.requestDeletion"
    elif any(term in lowered for term in ("connection", "kết nối", "ket noi", "interruption")):
        target = "DeleteCulturalHeritageService.validateDeletion"
    else:
        target = "DeleteCulturalHeritageService.validateDeletion"
    return [{"target": target, "reason": "Fake trace follows Rules A-D for deterministic tests."}]


def _fake_sequence_links(content: str) -> list[dict[str, str]]:
    lowered = content.lower()
    if any(term in lowered for term in ("cancel", "hủy", "huy")):
        target = "confirm deletion"
    elif any(term in lowered for term in ("confirm", "xác nhận", "xac nhan", "confirmation")):
        target = "showConfirmation()"
    elif any(term in lowered for term in ("notify", "thông báo", "thong bao", "success")):
        target = "showResult(success message)"
    elif any(term in lowered for term in ("delete", "xóa", "xoa", "elimination")):
        target = "delete(heritageId)"
    elif any(term in lowered for term in ("logged", "đăng nhập", "dang nhap", "selected", "chọn", "chon")):
        target = "requestDeletion(heritageId)"
    elif any(term in lowered for term in ("connection", "kết nối", "ket noi", "interruption")):
        target = "validateDeletion(heritageId)"
    else:
        target = "validateDeletion(heritageId)"
    return [{"target": target, "reason": "Fake trace follows Rules A-D for deterministic tests."}]

