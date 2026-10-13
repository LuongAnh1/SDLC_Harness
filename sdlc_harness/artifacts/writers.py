"""Ghi artifact của Phase 1.

Artifact là sản phẩm mà checkpoint và tool sync tham chiếu. Phase 1 hiện sinh cả
formal use case cho execution graph và requirement packet cho người duyệt.
"""

from __future__ import annotations

import json

from ..sources.fixtures import UseCaseSource
from ..agents.requirements.refiner import FormalRefinementResult
from ..agents.requirements.validator import ValidationReport
from ..storage.runtime import RuntimeStore, utc_now_iso


def write_use_case_packet(*, store: RuntimeStore, run_id: str, source: UseCaseSource) -> str:
    """Lưu bản use case thô đã được đóng gói để truy vết."""

    path = store.run_dir(run_id) / "artifacts" / "use_case_packet_v1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    source_location = store.relative(source.source_path) if source.source_path else source.source_ref
    content = f"""# Use Case Packet V1

- Dataset: {source.dataset}
- Use case: {source.use_case}
- Source ref: `{source.source_ref}`
- Source location: `{source_location}`

## Raw Use Case

{source.text}
"""
    path.write_text(content, encoding="utf-8")
    return store.relative(path)


def write_formal_use_case(*, store: RuntimeStore, run_id: str, formal_use_case: str) -> str:
    """Lưu formal use case theo grammar TraceDev để phase sau dùng."""

    path = store.run_dir(run_id) / "artifacts" / "formal_use_case_v1.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(formal_use_case.rstrip() + "\n", encoding="utf-8")
    return store.relative(path)


def write_formal_check_report(
    *,
    store: RuntimeStore,
    run_id: str,
    refinement_result: FormalRefinementResult,
) -> str:
    """Lưu report kiểm tra formal grammar và trạng thái repair."""

    path = store.run_dir(run_id) / "artifacts" / "formal_check_report_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(refinement_result.to_report_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return store.relative(path)


def write_requirement_packet(
    *,
    store: RuntimeStore,
    run_id: str,
    requirement_packet: str,
    validation_report: ValidationReport,
) -> str:
    """Lưu requirement packet và kèm report validator ở cuối file."""

    path = store.run_dir(run_id) / "artifacts" / "requirement_packet_v1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = requirement_packet.rstrip() + "\n\n---\n\n"
    content += "## Validator Report\n\n"
    content += f"- ok: {str(validation_report.ok).lower()}\n"
    if validation_report.issues:
        for issue in validation_report.issues:
            content += f"- issue: {issue}\n"
    else:
        content += "- issue: none\n"
    path.write_text(content, encoding="utf-8")
    return store.relative(path)


def write_trace_graph(
    *,
    store: RuntimeStore,
    run_id: str,
    source: UseCaseSource,
    artifact_paths: dict[str, str],
    validation_report: ValidationReport,
    formal_refinement: FormalRefinementResult,
    llm_mode: str,
    llm_model: str,
) -> str:
    """Sinh trace graph cho đường source -> formal use case -> requirement packet."""

    path = store.run_dir(run_id) / "artifacts" / "trace_graph_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    graph = {
        "version": "trace_graph_v1",
        "run_id": run_id,
        "created_at": utc_now_iso(),
        "nodes": [
            {
                "id": "source_use_case",
                "type": "source",
                "label": f"{source.dataset}/{source.use_case}",
                "ref": source.source_ref,
            },
            {
                "id": "requirement_refiner",
                "type": "agent_step",
                "label": "Requirement Refiner",
                "llm_mode": llm_mode,
                "llm_model": llm_model,
            },
            {
                "id": "formal_use_case_v1",
                "type": "artifact",
                "label": "Formal Use Case V1",
                "path": artifact_paths["formal_use_case"],
            },
            {
                "id": "formal_use_case_validator",
                "type": "validator_step",
                "label": "Formal Use Case Validator",
                "ok": formal_refinement.final_report.ok,
                "issues": formal_refinement.final_report.issues,
                "repaired": formal_refinement.repaired,
            },
            {
                "id": "requirement_packet_validator",
                "type": "validator_step",
                "label": "Requirement Packet Validator",
                "ok": validation_report.ok,
                "issues": validation_report.issues,
            },
            {
                "id": "requirement_packet_v1",
                "type": "artifact",
                "label": "Requirement Packet V1",
                "path": artifact_paths["requirement_packet"],
            },
        ],
        "edges": [
            {"from": "source_use_case", "to": "requirement_refiner", "relation": "refines"},
            {"from": "requirement_refiner", "to": "formal_use_case_v1", "relation": "generates"},
            {"from": "formal_use_case_v1", "to": "formal_use_case_validator", "relation": "validates_formal_grammar"},
            {"from": "formal_use_case_v1", "to": "requirement_packet_v1", "relation": "summarizes_for_human_checkpoint"},
            {"from": "requirement_packet_v1", "to": "requirement_packet_validator", "relation": "validates_checkpoint_packet"},
        ],
    }
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase2_design_artifacts(
    *,
    store: RuntimeStore,
    run_id: str,
    requirement_packet: str,
    version: int = 1,
) -> dict[str, str]:
    """Sinh bộ artifact design V0 từ requirement đã duyệt.

    Đây là fake/deterministic writer cho lát đầu Phase 2. Nó cố tình tạo output
    ổn định, có class/sequence diagram và trace JSON tối thiểu để test CLI/state
    trước khi thay bằng Designer/Validator thật ở lát tiếp theo.
    """

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    goal = _section_preview(requirement_packet, "Goal", default="Thiết kế theo requirement packet đã duyệt.")
    actors = _section_items(requirement_packet, "Actors") or ["Primary Actor"]
    main_flow = _section_items(requirement_packet, "Main Flow")
    acceptance = _section_items(requirement_packet, "Acceptance Criteria")
    requirement_count = max(1, len(main_flow) + len(acceptance))

    designer_analysis_path = artifact_dir / f"designer_analysis_{suffix}.md"
    designer_analysis_path.write_text(
        f"""# Designer Analysis {suffix.upper()}

## Goal
{goal}

## Design Direction
- Tạo lớp boundary để nhận thao tác từ actor chính.
- Tạo lớp service để xử lý nghiệp vụ chính.
- Tạo repository để tách thao tác lưu/xóa dữ liệu khỏi service.
- Giữ thiết kế ở mức checkpoint review, chưa sinh code.

## Actors Considered
{_markdown_list(actors)}

## Notes
- Artifact này được sinh bằng fake Designer V0 để khóa contract Phase 2.
- Designer thật sẽ thay thế nội dung này ở lát triển khai sau.
""",
        encoding="utf-8",
    )

    design_artifact_path = artifact_dir / f"design_artifact_{suffix}.md"
    design_artifact_path.write_text(
        f"""# Design Artifact {suffix.upper()}

## Summary
Thiết kế V0 cho requirement đã duyệt: {goal}

## Class Diagram

```plantuml
@startuml
class UserActionBoundary {{
  +requestAction()
  +showConfirmation()
  +showResult()
}}

class DomainService {{
  +validateRequest()
  +executeAction()
  +handleCancellation()
}}

class DomainRepository {{
  +loadSelectedEntity()
  +persistChange()
}}

UserActionBoundary --> DomainService
DomainService --> DomainRepository
@enduml
```

## Sequence Diagram

```plantuml
@startuml
actor User
participant UserActionBoundary as Boundary
participant DomainService as Service
participant DomainRepository as Repository

User -> Boundary: request action
Boundary -> Service: validate and execute
Service -> Repository: load selected entity
Repository --> Service: entity data
Service -> Repository: persist change
Service --> Boundary: result
Boundary --> User: show result
@enduml
```

## Requirement Coverage Draft
- Requirement items considered: {requirement_count}
- Missing links in V0 fake validator: 0
""",
        encoding="utf-8",
    )

    class_graph_path = artifact_dir / f"class_diagram_graph_{suffix}.json"
    class_graph = {
        "version": f"class_diagram_graph_{suffix}",
        "created_at": utc_now_iso(),
        "nodes": [
            {"id": "UserActionBoundary", "type": "class", "methods": ["requestAction", "showConfirmation", "showResult"]},
            {"id": "DomainService", "type": "class", "methods": ["validateRequest", "executeAction", "handleCancellation"]},
            {"id": "DomainRepository", "type": "class", "methods": ["loadSelectedEntity", "persistChange"]},
        ],
        "edges": [
            {"from": "UserActionBoundary", "to": "DomainService", "relation": "uses"},
            {"from": "DomainService", "to": "DomainRepository", "relation": "uses"},
        ],
    }
    class_graph_path.write_text(json.dumps(class_graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    sequence_graph_path = artifact_dir / f"sequence_diagram_graph_{suffix}.json"
    sequence_graph = {
        "version": f"sequence_diagram_graph_{suffix}",
        "created_at": utc_now_iso(),
        "lifelines": ["User", "UserActionBoundary", "DomainService", "DomainRepository"],
        "messages": [
            {"from": "User", "to": "UserActionBoundary", "message": "request action"},
            {"from": "UserActionBoundary", "to": "DomainService", "message": "validate and execute"},
            {"from": "DomainService", "to": "DomainRepository", "message": "load selected entity"},
            {"from": "DomainService", "to": "DomainRepository", "message": "persist change"},
            {"from": "UserActionBoundary", "to": "User", "message": "show result"},
        ],
    }
    sequence_graph_path.write_text(json.dumps(sequence_graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    trace_path = artifact_dir / f"requirement_design_trace_{suffix}.json"
    trace = {
        "version": f"requirement_design_trace_{suffix}",
        "created_at": utc_now_iso(),
        "mode": "fake_validator_v0",
        "requirement_count": requirement_count,
        "links": [
            {
                "requirement_ref": "requirement_packet_v1",
                "class_targets": ["UserActionBoundary", "DomainService", "DomainRepository"],
                "sequence_targets": ["UserActionBoundary", "DomainService", "DomainRepository"],
                "status": "covered",
            }
        ],
    }
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    missing_path = artifact_dir / f"missing_design_links_{suffix}.json"
    missing = {
        "version": f"missing_design_links_{suffix}",
        "created_at": utc_now_iso(),
        "mode": "fake_validator_v0",
        "missing_links": [],
    }
    missing_path.write_text(json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    quality_path = artifact_dir / f"design_quality_report_{suffix}.md"
    quality_path.write_text(
        f"""# Design Quality Report {suffix.upper()}

- Validator mode: fake_validator_v0
- Requirement items checked: {requirement_count}
- Missing design links: 0
- Repair iterations: 0

## Result
Design V0 đủ điều kiện tạo checkpoint review. Báo cáo này chỉ khóa contract
Phase 2; Validator traceability thật sẽ thay thế ở lát sau.
""",
        encoding="utf-8",
    )

    return {
        "designer_analysis": store.relative(designer_analysis_path),
        "design_artifact": store.relative(design_artifact_path),
        "class_diagram_graph": store.relative(class_graph_path),
        "sequence_diagram_graph": store.relative(sequence_graph_path),
        "requirement_design_trace": store.relative(trace_path),
        "missing_design_links": store.relative(missing_path),
        "design_quality_report": store.relative(quality_path),
    }


def _section_preview(markdown: str, heading: str, *, default: str) -> str:
    items = _section_items(markdown, heading)
    return items[0] if items else default


def _section_items(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    in_section = False
    items: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("## "):
            title = line[3:].strip().lower()
            if in_section and title != heading.lower():
                break
            in_section = title == heading.lower()
            continue
        if not in_section or not line or line == "---":
            continue
        if line.startswith("#"):
            break
        items.append(line.lstrip("- ").strip())
    return items


def _markdown_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)
