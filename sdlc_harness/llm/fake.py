"""Deterministic fake LLM responses used by tests and local fake mode."""

from __future__ import annotations

import hashlib
import json
import re

def fake_completion(prompt: str) -> str:
    """Fake output theo loại prompt để test không gọi API thật."""

    if "# Requirement Source Tool Loop" in prompt:
        return fake_requirement_source_action(prompt)
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
    if "Component-aware Implementation Plan V4" in prompt:
        return fake_component_aware_implementation_plan(prompt)
    if "Implementation Plan LLM Review v1" in prompt:
        return fake_implementation_plan_review()
    if "Developer Implementation Plan v1" in prompt:
        return fake_developer_implementation_plan(prompt)
    if "Code Trace LLM Review v1" in prompt:
        return fake_code_trace_review(prompt)
    if (
        "QA Tester Generate Acceptance Tests v1" in prompt
        or "QA Tester Generate Profiled Acceptance Tests v1" in prompt
    ):
        return fake_qa_generated_tests(prompt)
    if "QA Tester Acceptance Review v1" in prompt:
        return fake_qa_tester_review()
    return fake_refined_requirement(prompt)


def fake_requirement_source_action(prompt: str) -> str:
    """Đi theo list -> read -> complete để fake mode kiểm thử đúng tool-loop thật."""

    marker_start = "TOOL_STATE_JSON_BEGIN\n"
    marker_end = "\nTOOL_STATE_JSON_END"
    try:
        raw_state = prompt.split(marker_start, 1)[1].split(marker_end, 1)[0]
        state = json.loads(raw_state)
    except (IndexError, json.JSONDecodeError):
        return json.dumps({"action": "source.list", "arguments": {}, "summary": "List allowed sources."})
    history = state.get("history") if isinstance(state, dict) else []
    entries = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    listed = next((item for item in entries if item.get("action") == "source.list"), None)
    if listed is None:
        return json.dumps({"action": "source.list", "arguments": {}, "summary": "List allowed sources."})
    read_entry = next(
        (
            item
            for item in entries
            if item.get("action") in {"source.read", "source.search"}
            and isinstance(item.get("result"), dict)
            and item["result"].get("ok") is True
        ),
        None,
    )
    if read_entry is None:
        result = listed.get("result") if isinstance(listed.get("result"), dict) else {}
        sources = result.get("sources") if isinstance(result, dict) else []
        source = next((item for item in sources if isinstance(item, dict)), {}) if isinstance(sources, list) else {}
        return json.dumps(
            {
                "action": "source.read",
                "arguments": {
                    "source_id": source.get("source_id", "SRC-1"),
                    "start_line": 1,
                    "line_count": 200,
                },
                "summary": "Read the immutable source snapshot.",
            }
        )
    result = read_entry.get("result") if isinstance(read_entry.get("result"), dict) else {}
    observation_id = str(result.get("observation_id") or "OBS-1")
    source_ref = str(result.get("source_ref") or "fake:source")
    source_text = str(result.get("text") or "Fake requirement source.")
    use_case_match = re.search(r"Use case:\s*`([^`]+)`", prompt)
    use_case = use_case_match.group(1).strip() if use_case_match else "FakeUseCase"
    synthetic_prompt = (
        f"Source ref: `{source_ref}`\nUse case: `{use_case}`\n"
        f"## Source snapshot\n{source_text}\n## End source snapshot"
    )
    model = json.loads(fake_requirement_model_v2(synthetic_prompt))
    for evidence in model.get("evidence", []):
        if isinstance(evidence, dict):
            evidence["observation_ref"] = observation_id
    return json.dumps(
        {
            "action": "complete",
            "arguments": {"requirement_model": model},
            "summary": "Build the grounded Requirement Model V2.",
        },
        ensure_ascii=False,
    )


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
            "source_id": "SRC-1",
            "source_ref": source_ref,
            "artifact_ref": "fake_source_snapshot",
            "fingerprint": "fake_fingerprint",
        },
        "provenance_ledger": {
            "schema_version": "requirement_provenance_v1",
            "sources": [
                {
                    "id": "SRC-1",
                    "type": "dataset",
                    "source_ref": source_ref,
                    "locator": source_ref,
                    "revision": None,
                    "artifact_ref": "fake_source_snapshot",
                    "fingerprint": "fake_fingerprint",
                }
            ],
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
        "scenario_contract": {
            "schema_version": "requirement_scenario_surface_v1",
            "scenarios": [
                {
                    "id": "SC-1",
                    "name": "Complete the source-described behavior",
                    "kind": "main",
                    "actor_refs": [],
                    "requirement_refs": ["MF-1"],
                    "acceptance_refs": ["AC-1"],
                    "evidence_refs": ["E-1"],
                    "open_question_refs": [],
                    "surfaces": {
                        "ui": {
                            "status": "not_applicable",
                            "reason": "The minimal fake source does not establish a user interface contract.",
                            "requirement_refs": [],
                            "evidence_refs": [],
                            "open_question_refs": [],
                        },
                        "api": {
                            "status": "not_applicable",
                            "reason": "The minimal fake source does not establish an API contract.",
                            "requirement_refs": [],
                            "evidence_refs": [],
                            "open_question_refs": [],
                        },
                        "business_rule": {
                            "status": "required",
                            "reason": "The source-described behavior must be implemented and verified.",
                            "requirement_refs": ["MF-1", "AC-1"],
                            "evidence_refs": ["E-1"],
                            "open_question_refs": [],
                        },
                        "data": {
                            "status": "not_applicable",
                            "reason": "The minimal fake source does not establish persisted state.",
                            "requirement_refs": [],
                            "evidence_refs": [],
                            "open_question_refs": [],
                        },
                    },
                }
            ],
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
                "source_id": "SRC-1",
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

    payload = {
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
        }
    if typescript:
        payload["implementation_signature_contract"] = (
            _fake_typescript_implementation_signature_contract(prompt)
        )
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )


def fake_component_aware_implementation_plan(prompt: str) -> str:
    """Enrich Harness-owned V4 units without changing their identity."""

    units = _fake_json_prompt_value(
        prompt=prompt,
        start="Harness-owned work units:",
        end="Vertical Design slice:",
    )
    policy = _fake_json_prompt_value(
        prompt=prompt,
        start="Planning policy:",
        end="Verification policy:",
    )
    components = {
        str(item.get("component_id") or ""): item
        for item in policy.get("components", [])
        if isinstance(item, dict)
    } if isinstance(policy, dict) else {}
    unit_items = (
        [item for item in units if isinstance(item, dict)]
        if isinstance(units, list)
        else []
    )
    needs_shared_contract = (
        "contracts" in components
        and any(
            str(item.get("component_id") or "") == "web"
            and "ui" in item.get("surfaces", [])
            for item in unit_items
        )
        and any(
            str(item.get("component_id") or "") == "api"
            and "api" in item.get("surfaces", [])
            for item in unit_items
        )
    )
    contract_owner = next(
        (
            str(item.get("unit_id") or "")
            for item in unit_items
            if str(item.get("component_id") or "") == "api"
            and "api" in item.get("surfaces", [])
        ),
        "",
    )
    updates: list[dict[str, object]] = []
    for unit in unit_items:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("unit_id") or "")
        component_id = str(unit.get("component_id") or "")
        component = components.get(component_id, {})
        targets = [item for item in unit.get("planned_code_targets", []) if isinstance(item, dict)]
        if not targets:
            root = str(component.get("source_root") or "apps/api/src").rstrip("/")
            suffix = str(component.get("source_suffix") or ".ts")
            symbol = f"Planned{component_id.title()}Change{unit_id[-4:]}"
            targets = [{"path": f"{root}/{symbol}{suffix}", "symbol": symbol}]
        if needs_shared_contract and unit_id == contract_owner:
            targets.append(
                {
                    "path": "packages/contracts/src/planned-contract.ts",
                    "symbol": "PlannedContract",
                }
            )
        acceptance_refs = [str(item) for item in unit.get("acceptance_refs", []) if str(item)]
        test_root = str(component.get("source_root") or "apps/api/src").rstrip("/")
        test_suffix = str(component.get("test_suffix") or ".test.ts")
        test_path = f"{test_root}/DeleteCulturalHeritageFlow{test_suffix}"
        update: dict[str, object] = {
            "unit_id": unit_id,
            "title": str(unit.get("title") or f"Implement {component_id} change"),
            "objective": str(unit.get("objective") or "Implement the approved Vertical Design contract."),
            "planned_code_targets": targets,
            "test_intents": [
                {
                    "stage_id": "unit",
                    "intent": f"Verify the approved behavior for {unit_id}.",
                    "acceptance_refs": acceptance_refs,
                    "target_paths": [test_path],
                }
            ],
            "repo_evidence_refs": list(unit.get("repo_evidence_refs", [])),
        }
        if "data" in unit.get("surfaces", []):
            update["migration_impact"] = {
                "status": "not_required",
                "reason": "The approved data contract does not require a schema migration in fake mode.",
                "targets": [],
            }
        updates.append(update)
    return json.dumps(
        {
            "summary": "Fake component-aware plan enriched every Harness-owned work unit.",
            "repo_evidence": ["Fake mode uses the frozen planning policy and approved graph bindings."],
            "work_unit_updates": updates,
            "implementation_signature_contract": _fake_typescript_implementation_signature_contract(
                prompt,
                unit_updates=updates,
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _fake_typescript_implementation_signature_contract(
    prompt: str,
    *,
    unit_updates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Mô phỏng Phase 3 producer; builder không được tự suy luận type từ Phase 2."""

    class_graph = _fake_json_prompt_section(
        prompt,
        start="Class graph:",
        end="Sequence graph:",
    )
    if not class_graph:
        class_graph = _fake_json_prompt_section(
            prompt,
            start="Legacy Class graph (signature contract only):",
            end="Legacy Sequence graph (signature contract only):",
        )
    component_symbols = _fake_component_symbols(prompt, unit_updates=unit_updates)
    component_aware = "Component-aware Implementation Plan V4" in prompt
    if component_aware:
        targets = _fake_json_prompt_value(
            prompt=prompt,
            start="Harness-owned signature targets:",
            end="Harness-owned work units:",
        )
        return {
            "entries": [
                {
                    "target_path": str(item.get("target_path") or ""),
                    "target_symbol": str(item.get("target_symbol") or ""),
                    "parameters": [],
                    "return_type": (
                        "JSX.Element"
                        if str(item.get("target_path") or "").endswith(".tsx")
                        else "Promise<unknown>"
                    ),
                    "type_parameters": [],
                    "async": not str(item.get("target_path") or "").endswith(".tsx"),
                }
                for item in targets
                if isinstance(item, dict)
                and str(item.get("target_path") or "")
                and str(item.get("target_symbol") or "")
            ]
            if isinstance(targets, list)
            else []
        }
    entries: list[dict[str, object]] = []
    nodes = class_graph.get("nodes") if isinstance(class_graph, dict) else []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        owner = str(node.get("className") or node.get("id") or "").strip()
        owner_kind = str(node.get("type") or "class").strip().lower()
        methods = node.get("methods")
        if not owner or not isinstance(methods, list):
            continue
        for method in methods:
            if not isinstance(method, dict):
                continue
            name = str(method.get("name") or "").strip()
            if not name:
                continue
            implementation_symbol = component_symbols.get(owner, name)
            parameters = []
            raw_parameters = method.get("parameters")
            for parameter in raw_parameters if isinstance(raw_parameters, list) else []:
                if not isinstance(parameter, dict):
                    continue
                raw_name = str(parameter.get("name") or "").strip()
                parameters.append(
                    {
                        "name": raw_name.removeprefix("...").removesuffix("?"),
                        "type": _fake_typescript_type(parameter.get("type")),
                        "optional": raw_name.endswith("?"),
                        "rest": raw_name.startswith("..."),
                    }
                )
            if component_aware and not parameters:
                parameters.append(
                    {
                        "name": "input",
                        "type": "unknown",
                        "optional": False,
                        "rest": False,
                    }
                )
            return_type = _fake_typescript_type(method.get("returnType") or "void")
            if component_aware and return_type in {"void", "Promise<void>"}:
                return_type = "unknown"
            entries.append(
                {
                    "design_element_id": f"method:{owner}.{name}",
                    "symbol_kind": "method",
                    "owner_kind": owner_kind,
                    "qualified_name": f"{owner}.{implementation_symbol}",
                    "parameters": parameters,
                    "return_type": return_type,
                    "type_parameters": [],
                    "async": False,
                }
            )
    return {
        "version": "implementation_signature_contract_v1",
        "entries": entries,
    }


def _fake_component_symbols(
    prompt: str,
    *,
    unit_updates: list[dict[str, object]] | None,
) -> dict[str, str]:
    units = _fake_json_prompt_value(
        prompt=prompt,
        start="Harness-owned work units:",
        end="Vertical Design slice:",
    )
    design = _fake_json_prompt_value(
        prompt=prompt,
        start="Vertical Design slice:",
        end="Vertical Trace slice:",
    )
    if not isinstance(units, list) or not isinstance(design, dict):
        return {}
    updates_by_id = {
        str(item.get("unit_id") or ""): item
        for item in unit_updates or []
        if str(item.get("unit_id") or "")
    }
    unit_by_node = {
        str(node_id): unit
        for unit in units
        if isinstance(unit, dict)
        for node_id in unit.get("design_node_ids", [])
    }
    result: dict[str, str] = {}
    for node in design.get("nodes", []):
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "").strip()
        unit = unit_by_node.get(str(node.get("id") or ""))
        targets = unit.get("planned_code_targets", []) if isinstance(unit, dict) else []
        if isinstance(unit, dict):
            patch = updates_by_id.get(str(unit.get("unit_id") or ""), {})
            targets = patch.get("planned_code_targets") or targets
        candidates = [item for item in targets if isinstance(item, dict) and str(item.get("symbol") or "")]
        if not name or not candidates:
            continue
        name_tokens = set(_fake_identifier_tokens(name))

        def rank(target: dict[str, object]) -> tuple[int, int, str]:
            path = str(target.get("path") or "")
            symbol = str(target.get("symbol") or "")
            stem = path.rsplit("/", 1)[-1].split(".", 1)[0]
            tokens = set(_fake_identifier_tokens(stem)) | set(_fake_identifier_tokens(symbol))
            generic_penalty = 1 if stem.lower() in {"app", "index", "main", "server"} else 0
            return len(name_tokens & tokens), -generic_penalty, path

        selected = max(candidates, key=rank)
        result[name] = str(selected.get("symbol") or "")
    return result


def _fake_identifier_tokens(value: object) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    return [item.lower() for item in re.findall(r"[A-Za-z0-9]+", text)]


def _fake_json_prompt_section(
    prompt: str,
    *,
    start: str,
    end: str,
) -> dict[str, object]:
    start_marker = f"{start}\n"
    end_marker = f"\n\n{end}"
    if start_marker not in prompt:
        return {}
    raw = prompt.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _fake_json_prompt_value(*, prompt: str, start: str, end: str) -> object:
    start_marker = f"{start}\n"
    end_marker = f"\n{end}"
    if start_marker not in prompt:
        return {}
    raw = prompt.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _fake_typescript_type(value: object) -> str:
    text = str(value or "unknown").strip()
    replacements = {
        "String": "string",
        "Boolean": "boolean",
        "Integer": "number",
        "Long": "number",
        "Double": "number",
        "Float": "number",
    }
    return replacements.get(text, text)


def fake_developer_act(prompt: str = "") -> str:
    """Sinh fake output từ chính work packet; legacy không prompt giữ fixture cũ."""

    packet = _fake_json_after_marker(prompt, "DeveloperWorkPacket:")
    if packet and str(packet.get("schema_version") or "").startswith("developer_work_packet_profiled_"):
        from ..agents.developer.fake_generation import fake_developer_files

        return json.dumps(
            {
                "summary": "Fake Developer.act generated profile-aware changes.",
                "files": fake_developer_files(packet),
            },
            ensure_ascii=False,
            indent=2,
        )
    return _fake_legacy_developer_act()


def _fake_legacy_developer_act() -> str:
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

    payload = json.loads(fake_developer_act(prompt))
    marker = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    for item in payload["files"]:
        path = str(item.get("path") or "").casefold()
        comment = "--" if path.endswith(".sql") else "//"
        item["content"] = (
            f"{str(item['content']).rstrip()}\n\n{comment} Fake repair marker: {marker}\n"
        )
    payload["summary"] = "Fake Developer.repair generated an effective code change from current feedback."
    return json.dumps(payload, ensure_ascii=False, indent=2)


def fake_code_trace_review(prompt: str = "") -> str:
    """LLM review giả lập cho code trace hybrid rule + LLM."""

    review_input = _fake_json_after_marker(
        prompt,
        "Return only valid JSON. Do not wrap the JSON in markdown.",
    )
    mappings = review_input.get("static_trace_mappings") if isinstance(review_input, dict) else []
    graph_edges: list[dict[str, object]] = []
    for mapping in mappings if isinstance(mappings, list) else []:
        if not isinstance(mapping, dict):
            continue
        design_ids = mapping.get("design_element_ids")
        design_id = str(mapping.get("design_element_id") or "")
        if not design_id and isinstance(design_ids, list) and design_ids:
            design_id = str(design_ids[0])
        graph_edges.append(
            {
                "requirement_ref": str(mapping.get("requirement_ref") or ""),
                "design_element_id": design_id,
                "code_ref": str(mapping.get("code_ref") or ""),
                "test_ref": str(mapping.get("test_ref") or ""),
                "evidence": "Static trace mapping supplied by the harness.",
            }
        )

    return json.dumps(
        {
            "ok": True,
            "confidence": 0.91,
            "summary": "Fake LLM review accepts the requirement-design-code-test graph for deterministic tests.",
            "graph_edges": graph_edges,
            "missing_links": [],
            "risks": [],
        },
        ensure_ascii=False,
        indent=2,
    )


def fake_qa_generated_tests(prompt: str = "") -> str:
    """Fake QA-generated tests so Phase 4 can execute the QA slice deterministically."""

    requirement_ids: list[str] = []
    qa_policy = _fake_json_prompt_section(
        prompt,
        start="QA Test Policy:",
        end="QA Generation Packet:",
    )
    packet: dict[str, object] = {}
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

    if qa_policy.get("language_profile_id") == "typescript_v1":
        components = qa_policy.get("components")
        code_under_test = packet.get("code_under_test") if isinstance(packet, dict) else {}
        code_files = code_under_test.get("files") if isinstance(code_under_test, dict) else []
        production_paths = (
            [
                str(item.get("path") or "").replace("\\", "/")
                for item in code_files
                if isinstance(item, dict)
            ]
            if isinstance(code_files, list)
            else []
        )
        component_items = (
            [item for item in components if isinstance(item, dict)]
            if isinstance(components, list)
            else []
        )
        component = next(
            (
                item
                for item in component_items
                if any(
                    path.startswith(f"{str(item.get('test_root') or '').rstrip('/')}/")
                    for path in production_paths
                )
            ),
            component_items[0] if component_items else {},
        )
        test_root = str(component.get("test_root") or "apps/api/src").rstrip("/")
        test_suffix = str(component.get("test_suffix") or ".test.ts")
        test_path = f"{test_root}/GeneratedQaAcceptance{test_suffix}"
        return json.dumps(
            {
                "summary": "Fake QA generated a profile-owned Vitest acceptance test.",
                "test_files": [
                    {
                        "path": test_path,
                        "content": (
                            'import { describe, expect, it } from "vitest";\n\n'
                            'describe("generated QA acceptance", () => {\n'
                            '  it("executes the approved fake scenario", () => {\n'
                            '    expect(true).toBe(true);\n'
                            '  });\n'
                            '});\n'
                        ),
                        "scenarios": ["Profiled fake acceptance scenario"],
                    }
                ],
                "scenarios": [
                    {
                        "name": "Profiled fake acceptance scenario",
                        "expected_result": "The generated Vitest scenario passes.",
                        "test_file": test_path,
                        "risk": "",
                        "requirement_ids": requirement_ids,
                    }
                ],
                "risks": [],
            },
            ensure_ascii=False,
            indent=2,
        )

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


def _fake_json_after_marker(prompt: str, marker: str) -> dict[str, object]:
    """Đọc JSON object đầu tiên sau marker mà không phụ thuộc phần prompt theo sau."""

    if marker not in prompt:
        return {}
    raw = prompt.split(marker, 1)[1].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


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


