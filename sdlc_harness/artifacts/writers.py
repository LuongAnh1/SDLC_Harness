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
    designer_analysis: str,
    design_model: str,
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    requirement_packet: str,
    formal_use_case: str = "",
    version: int = 1,
) -> dict[str, str]:
    """Ghi artifact design V1 từ Designer và parser PlantUML.

    Lát này mới chứng minh Designer sinh được diagram và parser đọc được graph.
    Traceability LLM/repair loop chưa chạy, nên các report phải ghi rõ
    `traceability_pending_v1` thay vì báo pass giả.
    """

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    main_flow = _section_items(requirement_packet, "Main Flow")
    acceptance = _section_items(requirement_packet, "Acceptance Criteria")
    requirement_count = max(1, len(main_flow) + len(acceptance))

    designer_analysis_path = artifact_dir / f"designer_analysis_{suffix}.md"
    designer_analysis_path.write_text(
        _with_heading(f"Designer Analysis {suffix.upper()}", designer_analysis),
        encoding="utf-8",
    )

    design_artifact_path = artifact_dir / f"design_artifact_{suffix}.md"
    design_artifact_path.write_text(
        _with_heading(f"Design Artifact {suffix.upper()}", design_model),
        encoding="utf-8",
    )

    class_graph_path = artifact_dir / f"class_diagram_graph_{suffix}.json"
    class_graph_payload = {
        **class_graph,
        "version": f"class_diagram_graph_{suffix}",
        "created_at": utc_now_iso(),
    }
    class_graph_path.write_text(json.dumps(class_graph_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    sequence_graph_path = artifact_dir / f"sequence_diagram_graph_{suffix}.json"
    sequence_graph_payload = {
        **sequence_graph,
        "version": f"sequence_diagram_graph_{suffix}",
        "created_at": utc_now_iso(),
    }
    sequence_graph_path.write_text(json.dumps(sequence_graph_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    trace_path = artifact_dir / f"requirement_design_trace_{suffix}.json"
    trace = {
        "version": f"requirement_design_trace_{suffix}",
        "created_at": utc_now_iso(),
        "mode": "traceability_pending_v1",
        "requirement_count": requirement_count,
        "links": [],
        "inputs": {
            "requirement_packet": "requirement_packet_v1",
            "formal_use_case_attached": bool(formal_use_case.strip()),
            "class_diagram_graph": f"class_diagram_graph_{suffix}",
            "sequence_diagram_graph": f"sequence_diagram_graph_{suffix}",
        },
        "note": "Traceability LLM has not run yet; this file only records parser-ready inputs.",
    }
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    missing_path = artifact_dir / f"missing_design_links_{suffix}.json"
    missing = {
        "version": f"missing_design_links_{suffix}",
        "created_at": utc_now_iso(),
        "mode": "traceability_pending_v1",
        "missing_links": {
            "class_diagram": [],
            "sequence_diagram": [],
        },
        "note": "Traceability LLM has not run yet, so no coverage conclusion is recorded.",
    }
    missing_path.write_text(json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    quality_path = artifact_dir / f"design_quality_report_{suffix}.md"
    quality_path.write_text(
        f"""# Design Quality Report {suffix.upper()}

- Validator mode: parser_only_v1
- Requirement items prepared for traceability: {requirement_count}
- Class graph parser: passed
- Sequence graph parser: passed
- Traceability LLM: pending
- Repair iterations: 0

## Result
Design V1 đủ điều kiện tạo checkpoint review ở mức parser graph. Báo cáo này
chưa kết luận requirement đã được design cover đầy đủ; Validator traceability
và repair loop sẽ chạy ở lát Phase 2 tiếp theo.
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


def _with_heading(title: str, content: str) -> str:
    """Đảm bảo artifact có heading ổn định dù LLM trả text trần."""

    text = content.strip()
    if text.startswith("#"):
        return text + "\n"
    return f"# {title}\n\n{text}\n"


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
