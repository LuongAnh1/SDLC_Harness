"""LLM adapter tối giản cho SDLC Harness.

Phase đầu có hai chế độ: `fake` để test không tốn API và `deepseek`/
`openai_compatible` để gọi API thật qua endpoint Chat Completions tương thích
OpenAI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import urllib.error
import urllib.request

from ..core.config import Settings
from ..core.errors import HarnessError


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

        if self.settings.llm_mode == "fake":
            return LlmCompletion(content=fake_completion(prompt), finish_reason="fake")
        if self.settings.llm_mode in {"deepseek", "openai_compatible"}:
            return self._call_chat_completions(
                system=system,
                prompt=prompt,
                json_mode=json_mode,
                max_tokens=max_tokens,
            )
        raise HarnessError(
            "requirement_refiner_failed",
            f"Unsupported LLM mode: {self.settings.llm_mode}",
        )

    def _call_chat_completions(
        self,
        *,
        system: str,
        prompt: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> LlmCompletion:
        """Gọi API Chat Completions tương thích OpenAI bằng stdlib."""

        if not self.settings.llm_api_key:
            raise HarnessError(
                "requirement_refiner_failed",
                "SDLC_LLM_API_KEY is required when SDLC_LLM_MODE is not fake.",
            )
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
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

    if "Formal Syntactic Constraint" in prompt or "ReconstructedUseCase" in prompt:
        return fake_formal_use_case()
    if "Repair Requirement Packet v1" in prompt or "Requirement Validation Report" in prompt:
        return fake_refined_requirement(prompt)
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
    if "Implementation Plan LLM Review v1" in prompt:
        return fake_implementation_plan_review()
    if "Developer Implementation Plan v1" in prompt:
        return fake_developer_implementation_plan()
    if "Developer Repair v1" in prompt:
        return fake_developer_act()
    if "Developer Act v1" in prompt:
        return fake_developer_act()
    if "Code Trace LLM Review v1" in prompt:
        return fake_code_trace_review()
    if "QA Tester Generate Acceptance Tests v1" in prompt:
        return fake_qa_generated_tests()
    if "QA Tester Acceptance Review v1" in prompt:
        return fake_qa_tester_review()
    return fake_refined_requirement(prompt)


def fake_formal_use_case() -> str:
    """Formal use case hợp lệ theo grammar tối thiểu kiểu TraceDev."""

    return """UsecaseName: Delete Cultural Heritage
Description: The goal of this use case is to allow Agency Operator to delete a CulturalHeritage in order to remove an obsolete cultural object from the system
Actors: Agency Operator
Entry Conditions:
Agency Operator IS logged in
Agency Operator HAS selected CulturalHeritage from SearchCulturalHeritage results
Flow of Events:
1. Agency Operator views the list of CulturalHeritage returned by SearchCulturalHeritage.
2. Agency Operator selects one CulturalHeritage.
3. Agency Operator activates the delete function.
4. System asks for confirmation.
5. Agency Operator confirms the operation.
6. System deletes the selected CulturalHeritage.
Exit Conditions:
System notifies successful deletion of the selected CulturalHeritage.
Agency Operator cancels the operation.
Connection to the ETOUR server is interrupted.
Quality Requirement:
System blocks input controls after receiving confirmation or cancellation to avoid multiple submissions before the operation ends.""".strip()


def fake_refined_requirement(prompt: str) -> str:
    """Requirement packet ổn định để test contract mà không gọi API thật."""

    return """# Requirement Packet V1

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
class DeleteCulturalHeritageBoundary {
  +requestDeletion(heritageId : String) : void
  +showConfirmation() : void
  +showResult(message : String) : void
}

class DeleteCulturalHeritageService {
  +validateDeletion(heritageId : String) : boolean
  +deleteHeritage(heritageId : String) : void
  +cancelDeletion() : void
}

class CulturalHeritageRepository {
  +findById(heritageId : String) : CulturalHeritage
  +delete(heritageId : String) : void
}

class CulturalHeritage {
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


def fake_developer_implementation_plan() -> str:
    """Developer.plan giả lập để test Smart Developer không gọi API thật."""

    return json.dumps(
        {
            "summary": "Plan uses CodeGraph context to map the approved delete heritage design into service, boundary, repository, and tests.",
            "repo_evidence": [
                "CodeGraph context found existing CulturalHeritageRepository in src/main/java.",
                "Target repo already has a test folder under src/test/java/com/sdlc/etour/heritage.",
            ],
            "implementation_steps": [
                {
                    "order": 1,
                    "title": "Add delete flow application service",
                    "description": "Implement the approved validation, confirmation, delete, cancel, and result behavior.",
                    "files": [
                        "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java",
                        "src/main/java/com/sdlc/etour/heritage/CulturalHeritageRepository.java",
                    ],
                    "design_refs": [
                        "class:DeleteCulturalHeritageService",
                        "method:DeleteCulturalHeritageService.deleteHeritage",
                    ],
                },
                {
                    "order": 2,
                    "title": "Verify delete flow",
                    "description": "Cover success, cancellation, interruption, and duplicate-submit prevention.",
                    "files": ["src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java"],
                    "design_refs": ["sequence:delete_cultural_heritage_main:1"],
                },
            ],
            "proposed_files": [
                {
                    "path": "src/main/java/com/sdlc/etour/heritage/DeleteCulturalHeritageService.java",
                    "action": "create",
                    "reason": "Holds the approved delete use-case behavior.",
                    "related_design_elements": ["class:DeleteCulturalHeritageService"],
                },
                {
                    "path": "src/test/java/com/sdlc/etour/heritage/DeleteCulturalHeritageFlowTest.java",
                    "action": "create",
                    "reason": "Verifies the approved sequence and exception flows.",
                    "related_design_elements": ["sequence:delete_cultural_heritage_main:1"],
                },
            ],
            "design_mappings": [],
            "test_intents": [
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
            ],
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
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


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


def fake_qa_generated_tests() -> str:
    """Fake QA-generated tests so Phase 4 can execute the QA slice deterministically."""

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

