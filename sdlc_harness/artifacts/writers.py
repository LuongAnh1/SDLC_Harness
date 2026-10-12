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
