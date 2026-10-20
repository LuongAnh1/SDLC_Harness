"""Ghi artifact runtime cho các phase của SDLC Harness.

Artifact là sản phẩm mà checkpoint, Validator và tool sync tham chiếu. Phase 1
V2 được ghi bởi ``requirement_v2``; Phase 2 sinh design, graph, trace và quality
report theo từng version; Phase 3 sinh implementation plan trước khi sửa code;
Phase 4 sinh work packet, code change report, test report, code trace và repair
feedback theo từng version để không ghi đè lịch sử chạy. QA cũng có feedback
riêng khi acceptance tests fail và cần Developer sửa lại code/test.
"""

from __future__ import annotations

import json

from ..sources.fixtures import UseCaseSource
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


def write_phase2_traceability_attempt(
    *,
    store: RuntimeStore,
    run_id: str,
    design_version: int,
    attempt: dict[str, object],
) -> dict[str, str]:
    """Persist raw traceability output and metadata before normalization can fail."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    diagram = str(attempt.get("diagram") or "trace").replace(" diagram", "").replace(" ", "_")
    attempt_number = int(attempt.get("attempt") or 1)
    stem = f"traceability_{diagram}_v{design_version}_attempt{attempt_number}"
    raw_path = artifact_dir / f"{stem}_raw.txt"
    metadata_path = artifact_dir / f"{stem}_metadata.json"
    raw_path.write_text(str(attempt.get("raw_response") or "").rstrip() + "\n", encoding="utf-8")
    metadata = {
        "version": "traceability_attempt_v1",
        "created_at": utc_now_iso(),
        "design_version": f"v{design_version}",
        "diagram": attempt.get("diagram"),
        "attempt": attempt_number,
        "finish_reason": attempt.get("finish_reason"),
        "usage": attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {},
        "parse_status": attempt.get("parse_status"),
        "error": attempt.get("error"),
        "raw_response": store.relative(raw_path),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "raw_response": store.relative(raw_path),
        "metadata": store.relative(metadata_path),
    }


def write_phase2_design_artifacts(
    *,
    store: RuntimeStore,
    run_id: str,
    designer_analysis: str,
    design_model: str,
    requirement_graph: dict[str, object],
    class_graph: dict[str, object],
    sequence_graph: dict[str, object],
    class_trace: list[dict[str, object]],
    sequence_trace: list[dict[str, object]],
    missing_links: dict[str, list[dict[str, str]]],
    requirement_packet: str,
    formal_use_case: str = "",
    version: int = 1,
    repair_iterations: int = 0,
    traceability_mode: str = "traceability_v1",
    requirement_contract_version: str = "v2",
    requirement_artifact_refs: dict[str, str] | None = None,
    requirement_revision: int | None = None,
    design_validation: dict[str, object] | None = None,
) -> dict[str, str]:
    """Ghi artifact design theo version từ Designer, parser PlantUML và Validator."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    requirement_count = int(requirement_graph.get("count") or len(requirement_graph.get("requirements", [])) or 0)
    missing_class = missing_links.get("class_diagram", [])
    missing_sequence = missing_links.get("sequence_diagram", [])
    missing_count = len(missing_class) + len(missing_sequence)

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

    requirement_graph_path = artifact_dir / f"requirement_graph_{suffix}.json"
    requirement_graph_payload = {
        **requirement_graph,
        "version": f"requirement_graph_{suffix}",
        "source_graph_version": requirement_graph.get("version", "requirement_graph_v1"),
        "created_at": utc_now_iso(),
    }
    requirement_graph_path.write_text(
        json.dumps(requirement_graph_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    trace_path = artifact_dir / f"requirement_design_trace_{suffix}.json"
    trace = {
        "version": f"requirement_design_trace_{suffix}",
        "created_at": utc_now_iso(),
        "mode": traceability_mode,
        "requirement_contract_version": requirement_contract_version,
        "requirement_revision": requirement_revision,
        "requirement_count": requirement_count,
        "class_trace": class_trace,
        "sequence_trace": sequence_trace,
        "inputs": {
            "requirement_packet": (requirement_artifact_refs or {}).get(
                "requirement_packet", "requirement_packet_v2"
            ),
            "requirement_model": (requirement_artifact_refs or {}).get("requirement_model"),
            "formal_use_case": (requirement_artifact_refs or {}).get("formal_use_case"),
            "formal_use_case_attached": bool(formal_use_case.strip()),
            "requirement_graph": f"requirement_graph_{suffix}",
            "class_diagram_graph": f"class_diagram_graph_{suffix}",
            "sequence_diagram_graph": f"sequence_diagram_graph_{suffix}",
        },
    }
    trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    missing_path = artifact_dir / f"missing_design_links_{suffix}.json"
    missing = {
        "version": f"missing_design_links_{suffix}",
        "created_at": utc_now_iso(),
        "mode": traceability_mode,
        "requirement_contract_version": requirement_contract_version,
        "requirement_revision": requirement_revision,
        "missing_links": missing_links,
        "missing_count": missing_count,
    }
    missing_path.write_text(json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    validation_payload = design_validation or {
        "version": "design_validation_report_v2",
        "ok": missing_count == 0,
        "mode": "traceability_compatibility",
        "blocking_count": missing_count,
        "warning_count": 0,
        "blocking_findings": [],
        "warnings": [],
    }
    validation_path = artifact_dir / f"design_validation_report_{suffix}.json"
    validation_path.write_text(
        json.dumps(
            {**validation_payload, "created_at": utc_now_iso(), "design_version": suffix},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    blocking_count = int(validation_payload.get("blocking_count") or 0)
    warning_count = int(validation_payload.get("warning_count") or 0)
    validator_mode = str(validation_payload.get("mode") or traceability_mode)
    gate_stage = str(
        validation_payload.get("llm_review", {}).get("gate_stage")
        if isinstance(validation_payload.get("llm_review"), dict)
        else ""
    ) or str(
        validation_payload.get("rule_review", {}).get("gate_stage")
        if isinstance(validation_payload.get("rule_review"), dict)
        else ""
    )
    traceability_status = (
        "not run (blocked by deterministic preflight)"
        if traceability_mode == "not_run_preflight_blocked"
        else "passed"
    )
    semantic_status = "passed" if gate_stage in {"semantic_consensus", "bounded_adjudication"} and not blocking_count else "not reached or blocked"

    quality_path = artifact_dir / f"design_quality_report_{suffix}.md"
    quality_path.write_text(
        f"""# Design Quality Report {suffix.upper()}

- Validator mode: {validator_mode}
- Requirement contract: {requirement_contract_version}
- Requirement revision: {requirement_revision if requirement_revision is not None else "n/a"}
- Requirement items checked: {requirement_count}
- Class graph parser: passed
- Sequence graph parser: passed
- Progressive gate: {gate_stage or "legacy"}
- Traceability LLM: {traceability_status}
- Semantic review: {semantic_status}
- Missing design links: {missing_count}
- Hybrid validation blockers: {blocking_count}
- Hybrid validation warnings: {warning_count}
- Repair iterations: {repair_iterations}

## Result
{"Design validation passed." if bool(validation_payload.get("ok")) else "Design validation found blocking issues."}

## Missing Links
{_missing_links_markdown(missing_links)}

## Blocking Findings
{_design_findings_markdown(validation_payload.get("blocking_findings"))}

## Warnings
{_design_findings_markdown(validation_payload.get("warnings"))}
""",
        encoding="utf-8",
    )

    return {
        "designer_analysis": store.relative(designer_analysis_path),
        "design_artifact": store.relative(design_artifact_path),
        "requirement_graph": store.relative(requirement_graph_path),
        "class_diagram_graph": store.relative(class_graph_path),
        "sequence_diagram_graph": store.relative(sequence_graph_path),
        "requirement_design_trace": store.relative(trace_path),
        "missing_design_links": store.relative(missing_path),
        "design_validation_report": store.relative(validation_path),
        "design_quality_report": store.relative(quality_path),
    }


def write_phase3_implementation_plan(
    *,
    store: RuntimeStore,
    run_id: str,
    markdown: str,
    structured_plan: dict[str, object],
    version: int = 1,
) -> dict[str, str]:
    """Ghi implementation plan Markdown và JSON sidecar cho Phase 3."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    markdown_path = artifact_dir / f"implementation_plan_{suffix}.md"
    structured_path = artifact_dir / f"implementation_plan_structured_{suffix}.json"
    markdown_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    structured_payload = {
        **structured_plan,
        "created_at": utc_now_iso(),
        "artifact_ref": f"implementation_plan_{suffix}",
    }
    structured_path.write_text(
        json.dumps(structured_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "implementation_plan": store.relative(markdown_path),
        "implementation_plan_structured": store.relative(structured_path),
    }


def write_phase3_plan_review_report(
    *,
    store: RuntimeStore,
    run_id: str,
    review_report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi quality report của Validator.review_plan."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"implementation_plan_review_{suffix}.json"
    payload = {
        **review_report,
        "created_at": utc_now_iso(),
        "artifact_ref": f"implementation_plan_review_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_developer_work_packet(
    *,
    store: RuntimeStore,
    run_id: str,
    packet: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi DeveloperWorkPacket đã chuẩn bị từ implementation plan được duyệt."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"developer_work_packet_{suffix}.json"
    payload = {
        **packet,
        "created_at": utc_now_iso(),
        "artifact_ref": f"developer_work_packet_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_effective_design_contract(
    *,
    store: RuntimeStore,
    run_id: str,
    contract: dict[str, object],
    version: int = 1,
) -> str:
    """Freeze approved Phase 3 design adjustments before code generation."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"effective_design_contract_{suffix}.json"
    payload = {
        **contract,
        "version": f"effective_design_contract_{suffix}",
        "created_at": utc_now_iso(),
        "artifact_ref": f"effective_design_contract_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_code_change_report(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi report code change sau khi Developer.act apply file vào target repo."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"code_change_report_{suffix}.md"
    path.write_text(
        _code_change_report_markdown({**report, "artifact_version": suffix.upper()}).rstrip() + "\n",
        encoding="utf-8",
    )
    return store.relative(path)


def write_phase4_test_report(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi report Verification Runner sau khi chạy compile/unit test."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"test_report_{suffix}.md"
    path.write_text(
        _test_report_markdown({**report, "artifact_version": suffix.upper()}).rstrip() + "\n",
        encoding="utf-8",
    )
    return store.relative(path)


def write_phase4_code_trace_report(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi JSON trace requirement/design -> code/unit test cho Phase 4."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"requirement_design_code_trace_{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_qa_test_report(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi QA/Tester acceptance report sau khi code trace đã pass."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"qa_test_report_{suffix}.md"
    path.write_text(
        _qa_test_report_markdown({**report, "artifact_version": suffix.upper()}).rstrip() + "\n",
        encoding="utf-8",
    )
    return store.relative(path)


def write_phase4_qa_generated_tests(
    *,
    store: RuntimeStore,
    run_id: str,
    generated: dict[str, object],
    version: int = 1,
) -> str:
    """Write the QA-generated acceptance/system tests before execution."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"qa_generated_tests_{suffix}.json"
    payload = {
        **generated,
        "created_at": utc_now_iso(),
        "artifact_ref": f"qa_generated_tests_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_qa_execution_report(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Write the Verification Runner report after QA-generated tests are applied."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"qa_test_execution_{suffix}.md"
    path.write_text(
        _qa_execution_report_markdown({**report, "artifact_version": suffix.upper()}).rstrip() + "\n",
        encoding="utf-8",
    )
    return store.relative(path)
def write_phase4_qa_raw_response(
    *,
    store: RuntimeStore,
    run_id: str,
    raw_response: str,
    version: int = 1,
) -> str:
    """Lưu raw LLM response của QA/Tester khi parse lỗi để debug output bị cắt."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"qa_tester_raw_response_{suffix}.txt"
    path.write_text(raw_response.rstrip() + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_qa_repair_feedback(
    *,
    store: RuntimeStore,
    run_id: str,
    feedback: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi feedback QA để Developer sửa code/test theo acceptance failure."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"qa_repair_feedback_{suffix}.json"
    payload = {
        **feedback,
        "created_at": utc_now_iso(),
        "artifact_ref": f"qa_repair_feedback_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_github_pr_body(
    *,
    store: RuntimeStore,
    run_id: str,
    body: str,
    version: int = 1,
) -> str:
    """Ghi nội dung Draft PR để GitHub CLI dùng qua `--body-file`."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    path = sync_dir / f"github_pr_body_v{version}.md"
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_github_code_review(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi evidence commit/push/Draft PR trước khi sync Jira/Confluence."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    path = sync_dir / f"github_code_review_v{version}.json"
    payload = {
        **report,
        "version": f"github_code_review_v{version}",
        "updated_at": utc_now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_github_code_review_approval(
    *,
    store: RuntimeStore,
    run_id: str,
    report: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi audit Draft -> Ready riêng, không ghi đè publication artifact."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    path = sync_dir / f"github_code_review_approval_v{version}.json"
    payload = {
        **report,
        "version": f"github_code_review_approval_v{version}",
        "updated_at": utc_now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_repair_feedback(
    *,
    store: RuntimeStore,
    run_id: str,
    feedback: dict[str, object],
    version: int = 1,
) -> str:
    """Ghi feedback máy đọc được để slice sau giao Developer repair code/test."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    path = artifact_dir / f"implementation_repair_feedback_{suffix}.json"
    payload = {
        **feedback,
        "created_at": utc_now_iso(),
        "artifact_ref": f"implementation_repair_feedback_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_developer_raw_response(
    *,
    store: RuntimeStore,
    run_id: str,
    action: str,
    raw_response: str,
    version: int = 1,
) -> str:
    """Lưu raw LLM response của Developer để debug khi parse/repair khó hội tụ."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    safe_action = action.strip().replace(" ", "_") or "developer"
    path = artifact_dir / f"developer_{safe_action}_raw_response_{suffix}.txt"
    path.write_text(raw_response.rstrip() + "\n", encoding="utf-8")
    return store.relative(path)


def write_phase4_developer_llm_metadata(
    *,
    store: RuntimeStore,
    run_id: str,
    action: str,
    metadata: dict[str, object],
    version: int = 1,
) -> str:
    """Lưu finish_reason/usage của từng lần gọi Developer structured output."""

    artifact_dir = store.run_dir(run_id) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"v{version}"
    safe_action = action.strip().replace(" ", "_") or "developer"
    path = artifact_dir / f"developer_{safe_action}_llm_metadata_{suffix}.json"
    payload = {
        **metadata,
        "created_at": utc_now_iso(),
        "artifact_ref": f"developer_{safe_action}_llm_metadata_{suffix}",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return store.relative(path)


def _with_heading(title: str, content: str) -> str:
    """Đảm bảo artifact có heading ổn định dù LLM trả text trần."""

    text = content.strip()
    if text.startswith("#"):
        return text + "\n"
    return f"# {title}\n\n{text}\n"


def _code_change_report_markdown(report: dict[str, object]) -> str:
    files = _dict_items(report.get("files"))
    lines = [
        f"# Code Change Report {report.get('artifact_version', 'V1')}",
        "",
        "## Run Context",
        "",
        f"- Run id: `{report.get('run_id', '')}`",
        f"- Source ref: `{report.get('source_ref', '')}`",
        f"- Issue key: `{report.get('issue_key', '')}`",
        f"- Target repo: `{report.get('target_repo', '')}`",
        f"- Branch: `{report.get('branch', '')}`",
    ]
    if report.get("repair_iteration"):
        lines.extend(
            [
                f"- Repair iteration: `{report.get('repair_iteration')}`",
                f"- Repair feedback: `{report.get('repair_feedback', '')}`",
                f"- Developer work packet: `{report.get('developer_work_packet', '')}`",
                f"- Developer raw response: `{report.get('developer_raw_response', '')}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Developer Summary",
            "",
            str(report.get("summary") or "").strip() or "Developer.act did not provide a summary.",
            "",
            "## Files Applied",
            "",
            "| File | Operation | Reason | Covered design elements | Covered requirements |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in files:
        covered = ", ".join(str(value) for value in item.get("covered_design_elements", []) if str(value).strip())
        covered_requirements = ", ".join(
            str(value) for value in item.get("covered_requirement_ids", []) if str(value).strip()
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(f"`{item.get('path') or ''}`"),
                    _table_cell(str(item.get("operation") or "")),
                    _table_cell(str(item.get("reason") or "")),
                    _table_cell(covered),
                    _table_cell(covered_requirements),
                ]
            )
            + " |"
        )
    if report.get("repair_iteration"):
        lines.extend(
            [
                "",
                "## Verification Status",
                "",
                "- Tests: rerun immediately after this repair by Phase 4 workflow.",
                "- Commit: not created in Slice 4.6.",
                "- Draft PR: not created in Slice 4.6.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Verification Status",
                "",
                "- Tests: not run in Slice 4.2.",
                "- Commit: not created in Slice 4.2.",
                "- Draft PR: not created in Slice 4.2.",
                "",
            ]
        )
    return "\n".join(lines)


def _test_report_markdown(report: dict[str, object]) -> str:
    planned_stage_ids = report.get("planned_stage_ids")
    executed_stage_ids = report.get("executed_stage_ids")
    stage_results = report.get("stage_results")
    lines = [
        f"# Test Report {report.get('artifact_version', 'V1')}",
        "",
        "## Run Context",
        "",
        f"- Run id: `{report.get('run_id', '')}`",
        f"- Source ref: `{report.get('source_ref', '')}`",
        f"- Issue key: `{report.get('issue_key', '')}`",
        f"- Target repo: `{report.get('target_repo', '')}`",
        f"- Branch: `{report.get('branch', '')}`",
    ]
    if report.get("repair_iteration"):
        lines.extend(
            [
                f"- Repair iteration: `{report.get('repair_iteration')}`",
                f"- Repair feedback: `{report.get('repair_feedback', '')}`",
                f"- Developer work packet: `{report.get('developer_work_packet', '')}`",
            ]
        )
    if report.get("repair_iteration"):
        scope_lines = [
            "- Runner: Verification Runner after Developer.repair.",
            "- QA/Tester Agent: not run in Slice 4.6.",
            "- Code traceability Validator: runs after this test report if status passed.",
            "- Commit/Draft PR: not created in Slice 4.6.",
        ]
    else:
        scope_lines = [
            "- Runner: Verification Runner.",
            "- QA/Tester Agent: not run in Slice 4.3.",
            "- Code traceability Validator: not run in Slice 4.3.",
            "- Commit/Draft PR: not created in Slice 4.3.",
        ]
    lines.extend(
        [
            "",
            "## Verification Result",
            "",
            f"- Status: `{report.get('status', '')}`",
            f"- Command: `{report.get('command', '')}`",
            f"- Exit code: `{report.get('exit_code', '')}`",
            f"- Duration: `{report.get('duration_seconds', '')}` seconds",
            f"- Reason: {report.get('reason', '')}",
            f"- Discovered test entrypoints: `{len(report.get('discovered_test_entrypoints') or [])}`",
            f"- Executed test entrypoints: `{len(report.get('executed_test_entrypoints') or [])}`",
        ]
    )
    if isinstance(stage_results, list) and stage_results:
        lines.extend(
            [
                f"- Planned stages: `{', '.join(str(item) for item in planned_stage_ids or [])}`",
                f"- Executed stages: `{', '.join(str(item) for item in executed_stage_ids or [])}`",
                "",
                "## Verification Stages",
                "",
            ]
        )
        for item in stage_results:
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {item.get('stage_id', '')}",
                    "",
                    f"- Status: `{item.get('status', '')}`",
                    f"- Argv: `{json.dumps(item.get('argv') or [], ensure_ascii=False)}`",
                    f"- Cwd: `{item.get('cwd', '')}`",
                    f"- Timeout: `{item.get('timeout_seconds', '')}` seconds",
                    f"- Exit code: `{item.get('exit_code', '')}`",
                    f"- Duration: `{item.get('duration_seconds', '')}` seconds",
                    f"- Reason: {item.get('reason', '')}",
                    f"- Selected test files: `{json.dumps(item.get('selected_test_files') or [], ensure_ascii=False)}`",
                    f"- Verified test files: `{json.dumps(item.get('verified_test_files') or [], ensure_ascii=False)}`",
                    "",
                    "#### Stdout",
                    "",
                    "```text",
                    str(item.get("stdout") or "").rstrip(),
                    "```",
                    "",
                    "#### Stderr",
                    "",
                    "```text",
                    str(item.get("stderr") or "").rstrip(),
                    "```",
                    "",
                ]
            )
    lines.extend(
        [
            "## Test Entrypoints",
            "",
            *(
                [f"- `{item}`" for item in report.get('executed_test_entrypoints', [])]
                if isinstance(report.get('executed_test_entrypoints'), list)
                else []
            ),
            "",
            "## Stdout",
            "",
            "```text",
            str(report.get("stdout") or "").rstrip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            str(report.get("stderr") or "").rstrip(),
            "```",
            "",
            "## Scope",
            "",
            *scope_lines,
            "",
        ]
    )
    return "\n".join(lines)


def _qa_test_report_markdown(report: dict[str, object]) -> str:
    scenarios = _dict_items(report.get("scenarios"))
    evidence = [str(item).strip() for item in report.get("evidence", []) if str(item).strip()] if isinstance(report.get("evidence"), list) else []
    risks = [str(item).strip() for item in report.get("risks", []) if str(item).strip()] if isinstance(report.get("risks"), list) else []
    context_budget = report.get("review_context")
    context_budget = context_budget if isinstance(context_budget, dict) else {}
    requirement_contract = report.get("requirement_contract")
    requirement_contract = requirement_contract if isinstance(requirement_contract, dict) else {}
    requirement_ids = [
        str(item).strip() for item in report.get("requirement_ids", []) if str(item).strip()
    ] if isinstance(report.get("requirement_ids"), list) else []
    generation_profile = report.get("generation_profile")
    generation_profile = generation_profile if isinstance(generation_profile, dict) else {}
    lines = [
        f"# QA Test Report {report.get('artifact_version', 'V1')}",
        "",
        "## Run Context",
        "",
        f"- Run id: `{report.get('run_id', '')}`",
        f"- Source ref: `{report.get('source_ref', '')}`",
        f"- Issue key: `{report.get('issue_key', '')}`",
        f"- Target repo: `{report.get('target_repo', '')}`",
        f"- Branch: `{report.get('branch', '')}`",
        f"- Requirement contract: `{requirement_contract.get('version', 'v2')}` revision `{requirement_contract.get('revision', 0)}`",
        f"- Stable requirements covered: `{len(requirement_ids)}`",
        f"- Test framework: `{generation_profile.get('framework', 'legacy')}`",
        "",
        "## Generated Test Execution",
        "",
        f"- Generated tests artifact: `{report.get('qa_generated_tests', '')}`",
        f"- Execution artifact: `{report.get('qa_test_execution', '')}`",
        f"- Execution status: `{report.get('execution_status', '')}`",
        f"- Selected test files: `{json.dumps(report.get('selected_test_files') or [], ensure_ascii=False)}`",
        f"- Verified test files: `{json.dumps(report.get('verified_test_files') or [], ensure_ascii=False)}`",
        "",
        "### Applied Test Files",
        "",
    ]
    applied_tests = _dict_items(report.get("applied_tests"))
    lines.extend([f"- `{item.get('path', '')}`" for item in applied_tests] or ["- none"])
    lines.extend(
        [
            "",
            "## Acceptance Result",
            "",
            f"- Status: `{report.get('status', '')}`",
            f"- Summary: {report.get('summary', '')}",
            "",
            "## Scenarios",
            "",
            "| Scenario | Requirement IDs | Result | Evidence | Risk |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for item in scenarios:
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(str(item.get("name") or "")),
                    _table_cell(", ".join(str(value) for value in item.get("requirement_ids", []) if str(value).strip())),
                    _table_cell(str(item.get("result") or "")),
                    _table_cell(str(item.get("evidence") or "")),
                    _table_cell(str(item.get("risk") or "")),
                ]
            )
            + " |"
        )
    if not scenarios:
        lines.append("| _none_ |  |  |  |  |")
    lines.extend(["", "## Evidence", ""])
    lines.extend([f"- {item}" for item in evidence] or ["- none"])
    lines.extend(["", "## Risks", ""])
    lines.extend([f"- {item}" for item in risks] or ["- none"])
    lines.extend(
        [
            "",
            "## Context Budget",
            "",
            f"- Packet mode: `{context_budget.get('packet_mode', '')}`",
            f"- Sections: `{context_budget.get('section_count', '')}`",
            f"- Code files: `{context_budget.get('code_file_count', '')}`",
            f"- Chars sent: `{context_budget.get('chars_sent', '')}`",
            f"- Chars total: `{context_budget.get('chars_total', '')}`",
            f"- Any truncated: `{str(context_budget.get('any_truncated', False)).lower()}`",
            f"- Omitted full artifacts: `{', '.join(str(item) for item in context_budget.get('omitted_full_artifacts', []) if str(item).strip())}`",
            "",
            "## Scope",
            "",
            "- QA/Tester Agent generates acceptance/system tests from a compact packet, applies them inside the run's QA path policy, and executes them through Verification Runner.",
            "- Full design and implementation plan are references, not copied into the QA prompt in Slice 4.7A.",
            "- Developer QA repair loop starts from `qa_repair_needed`; failed QA tests only block after the configured limit.",
            "- Commit/Draft PR is not created in Slice 4.7A.",
            "",
        ]
    )
    return "\n".join(lines)


def _qa_execution_report_markdown(report: dict[str, object]) -> str:
    applied_tests = _dict_items(report.get("applied_tests"))
    stage_results = _dict_items(report.get("stage_results"))
    lines = [
        f"# QA Test Execution {report.get('artifact_version', 'V1')}",
        "",
        "## Run Context",
        "",
        f"- Run id: `{report.get('run_id', '')}`",
        f"- Source ref: `{report.get('source_ref', '')}`",
        f"- Issue key: `{report.get('issue_key', '')}`",
        f"- Target repo: `{report.get('target_repo', '')}`",
        f"- Branch: `{report.get('branch', '')}`",
        f"- Generated tests artifact: `{report.get('qa_generated_tests', '')}`",
        "",
        "## Applied QA Test Files",
        "",
    ]
    lines.extend([f"- `{item.get('path', '')}`" for item in applied_tests] or ["- none"])
    lines.extend(
        [
            "",
            "## Verification Result",
            "",
            f"- Status: `{report.get('status', '')}`",
            f"- Command: `{report.get('command', '')}`",
            f"- Exit code: `{report.get('exit_code', '')}`",
            f"- Duration: `{report.get('duration_seconds', '')}` seconds",
            f"- Reason: {report.get('reason', '')}",
            f"- Selected test files: `{json.dumps(report.get('selected_test_files') or [], ensure_ascii=False)}`",
            f"- Verified test files: `{json.dumps(report.get('verified_test_files') or [], ensure_ascii=False)}`",
            "",
            "## Verification Stages",
            "",
        ]
    )
    for item in stage_results:
        lines.extend(
            [
                f"### {item.get('stage_id', '')}",
                "",
                f"- Status: `{item.get('status', '')}`",
                f"- Argv: `{json.dumps(item.get('argv') or [], ensure_ascii=False)}`",
                f"- Cwd: `{item.get('cwd', '')}`",
                f"- Timeout: `{item.get('timeout_seconds', '')}` seconds",
                f"- Selected test files: `{json.dumps(item.get('selected_test_files') or [], ensure_ascii=False)}`",
                f"- Verified test files: `{json.dumps(item.get('verified_test_files') or [], ensure_ascii=False)}`",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Stdout",
            "",
            "```text",
            str(report.get("stdout") or "").rstrip(),
            "```",
            "",
            "## Stderr",
            "",
            "```text",
            str(report.get("stderr") or "").rstrip(),
            "```",
            "",
            "## Scope",
            "",
            "- Runner: Verification Runner after QA-generated acceptance tests are applied.",
            "- Developer QA repair loop starts from `qa_repair_needed`; failed QA tests only block after the configured limit.",
            "- Commit/Draft PR is not created in Slice 4.7A.",
            "",
        ]
    )
    return "\n".join(lines)


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _excerpt(content: str, *, limit: int = 1400) -> str:
    """Cắt artifact dài thành đoạn đủ đọc cho implementation plan."""

    text = content.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n... truncated for implementation planning ..."


def _traceability_result_text(missing_count: int) -> str:
    if missing_count == 0:
        return "Validator traceability V1 không phát hiện missing link ở class hoặc sequence diagram."
    return "Validator traceability V1 còn phát hiện missing link cần Designer repair hoặc human review."


def _design_findings_markdown(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "- None"
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("id") or "finding")
        category = str(item.get("category") or "design")
        message = str(item.get("message") or "").strip()
        expected = str(item.get("expected_change") or "").strip()
        lines.append(f"- **{finding_id} / {category}:** {message}")
        if expected:
            lines.append(f"  - Required correction: {expected}")
    return "\n".join(lines) if lines else "- None"


def _missing_links_markdown(missing_links: dict[str, list[dict[str, str]]]) -> str:
    lines: list[str] = []
    for diagram_key, title in (
        ("class_diagram", "Class Diagram"),
        ("sequence_diagram", "Sequence Diagram"),
    ):
        items = missing_links.get(diagram_key, [])
        lines.append(f"### {title}")
        if not items:
            lines.append("- none")
            continue
        for item in items:
            lines.append(f"- {item.get('requirement')}: {item.get('content')}")
    return "\n".join(lines)


