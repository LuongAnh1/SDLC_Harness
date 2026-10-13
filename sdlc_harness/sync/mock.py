"""Mock sync adapter cho checkpoint requirement, design và implementation plan.

Sync thật đã nằm ở adapter Atlassian. File này chỉ tạo artifact giả lập để unit
test/smoke test chạy được khi chưa cấu hình Jira/Confluence thật; ownership vẫn
giữ ở SDLC_Harness, Niko chỉ đọc report trả về.
"""

from __future__ import annotations

import json

from ..storage.runtime import RuntimeStore, utc_now_iso


def mock_sync_requirement_checkpoint(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    """Tạo file giả lập Jira comment, Confluence page và sync report."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)

    jira_comment_path = sync_dir / "jira-comment.md"
    confluence_page_path = sync_dir / "confluence-page.md"
    report_path = sync_dir / "tool_sync_report_v1.json"

    jira_comment_path.write_text(
        f"""# Mock Jira Comment

Source: `{source_ref}`

Formal use case và requirement packet đã được chuẩn hóa, validator pass và đang chờ duyệt.

Requirement artifact: `{artifact_paths['requirement_packet']}`
""",
        encoding="utf-8",
    )
    formal_line = ""
    if "formal_use_case" in artifact_paths:
        formal_line = f"- Formal use case: `{artifact_paths['formal_use_case']}`\n"
    check_line = ""
    if "formal_check_report" in artifact_paths:
        check_line = f"- Formal check report: `{artifact_paths['formal_check_report']}`\n"
    confluence_page_path.write_text(
        f"""# Mock Confluence Page

Source: `{source_ref}`

Trang này giả lập nơi lưu formal use case và requirement packet để reviewer đọc trước khi chuyển sang phase design.

Artifacts:
- Use case packet: `{artifact_paths['use_case_packet']}`
{formal_line}{check_line}- Requirement packet: `{artifact_paths['requirement_packet']}`
- Trace graph: `{artifact_paths['trace_graph']}`
""",
        encoding="utf-8",
    )

    report = {
        "status": "mock_synced",
        "created_at": utc_now_iso(),
        "targets": [
            {
                "tool": "jira",
                "mode": "mock_file",
                "path": store.relative(jira_comment_path),
                "source_ref": source_ref,
            },
            {
                "tool": "confluence",
                "mode": "mock_file",
                "path": store.relative(confluence_page_path),
                "source_ref": source_ref,
            },
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "mock_synced", "report": store.relative(report_path)}


def mock_sync_design_checkpoint(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    """Tạo mock Jira/Confluence checkpoint cho Phase 2 design review."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)

    jira_comment_path = sync_dir / "design-jira-comment.md"
    confluence_page_path = sync_dir / "design-confluence-page.md"
    report_path = sync_dir / "tool_sync_report_v2.json"
    design_artifact = artifact_paths["design_artifact"]
    trace_artifact = artifact_paths["requirement_design_trace"]
    quality_report = artifact_paths["design_quality_report"]
    design_ref = _artifact_ref(design_artifact)
    trace_ref = _artifact_ref(trace_artifact)
    quality_ref = _artifact_ref(quality_report)
    analysis_ref = _artifact_ref(artifact_paths["designer_analysis"])
    missing_ref = _artifact_ref(artifact_paths["missing_design_links"])
    previous_targets = _previous_targets(report_path)
    previous_jira = previous_targets.get("jira", {})
    previous_confluence = previous_targets.get("confluence", {})

    jira_comment_path.write_text(
        f"""# Mock Jira Comment - Design Checkpoint

Run: `{run_id}`
Source: `{source_ref}`
Status: `waiting_design_approval`

Design checkpoint V0 đã được tạo và đang chờ duyệt.

Artifacts:
- Design artifact: `{design_artifact}`
- Requirement-design trace: `{trace_artifact}`
- Quality report: `{quality_report}`

Next action: approve design hoặc request change.
""",
        encoding="utf-8",
    )
    confluence_page_path.write_text(
        f"""# Mock Confluence Page - Design Checkpoint

Source: `{source_ref}`
Run: `{run_id}`

Trang này giả lập bản review thiết kế sau khi requirement checkpoint đã được duyệt.

## Design Artifacts

- Designer analysis: `{artifact_paths['designer_analysis']}`
- Design artifact: `{design_artifact}`
- Class diagram graph: `{artifact_paths['class_diagram_graph']}`
- Sequence diagram graph: `{artifact_paths['sequence_diagram_graph']}`
- Requirement-design trace: `{trace_artifact}`
- Missing design links: `{artifact_paths['missing_design_links']}`
- Quality report: `{quality_report}`

## Human Checkpoint

Review design trước khi cho phép Phase 3 sinh code/test.
""",
        encoding="utf-8",
    )

    targets = [
        {
            "tool": "jira",
            "operation": "mock_file",
            "external_id": previous_jira.get("external_id", ""),
            "url": previous_jira.get("url", ""),
            "status": "synced",
            "path": store.relative(jira_comment_path),
            "artifact_refs": [design_ref, trace_ref, quality_ref],
            "error": None,
        },
        {
            "tool": "confluence",
            "operation": "mock_file",
            "external_id": previous_confluence.get("external_id", ""),
            "url": previous_confluence.get("url", ""),
            "status": "synced",
            "path": store.relative(confluence_page_path),
            "artifact_refs": [analysis_ref, design_ref, missing_ref],
            "error": None,
        },
    ]
    report = {
        "version": "tool_sync_report_v2",
        "phase": "design_checkpoint",
        "status": "mock_synced",
        "mode": "mock",
        "targets": targets,
        "checkpoint": {
            "type": "design_approval",
            "artifact_ref": design_ref,
            "artifact_path": design_artifact,
        },
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "phase": "design_checkpoint",
        "status": "mock_synced",
        "mode": "mock",
        "report": store.relative(report_path),
        "targets": [
            {
                "tool": target["tool"],
                "status": target["status"],
                "path": target["path"],
                "url": target["url"],
                "external_id": target["external_id"],
            }
            for target in targets
        ],
    }


def mock_sync_implementation_plan_checkpoint(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    """Tạo mock Jira/Confluence checkpoint cho Phase 3 implementation plan."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)

    jira_comment_path = sync_dir / "implementation-plan-jira-comment.md"
    confluence_page_path = sync_dir / "implementation-plan-confluence-page.md"
    report_path = sync_dir / "tool_sync_report_v3.json"
    implementation_plan = artifact_paths["implementation_plan"]
    structured_plan = artifact_paths["implementation_plan_structured"]
    review_report = artifact_paths["implementation_plan_review"]
    plan_ref = _artifact_ref(implementation_plan)
    structured_ref = _artifact_ref(structured_plan)
    review_ref = _artifact_ref(review_report)
    previous_targets = _previous_targets(report_path)
    previous_jira = previous_targets.get("jira", {})
    previous_confluence = previous_targets.get("confluence", {})

    jira_comment_path.write_text(
        f"""# Mock Jira Comment - Implementation Plan Checkpoint

Run: `{run_id}`
Source: `{source_ref}`
Status: `waiting_implementation_plan_review`

Implementation plan đã được tạo, Validator.review_plan đã pass và đang chờ duyệt trước khi Phase 4 sửa target repo.

Artifacts:
- Implementation plan: `{implementation_plan}`
- Structured plan: `{structured_plan}`
- Plan review: `{review_report}`

Confluence review page: `{store.relative(confluence_page_path)}`
Next action: approve implementation plan hoặc request change.
""",
        encoding="utf-8",
    )
    confluence_page_path.write_text(
        f"""# Mock Confluence Page - Implementation Plan Checkpoint

Source: `{source_ref}`
Run: `{run_id}`

Trang này giả lập bản review implementation plan sau khi design checkpoint đã được duyệt.

## Implementation Plan Artifacts

- Implementation plan: `{implementation_plan}`
- Structured plan: `{structured_plan}`
- Plan review: `{review_report}`
- Design artifact: `{artifact_paths.get('design_artifact', '')}`
- Requirement-design trace: `{artifact_paths.get('requirement_design_trace', '')}`

## Human Checkpoint

Review implementation plan trước khi cho phép Phase 4 tạo branch, code, test và Draft PR.
""",
        encoding="utf-8",
    )

    targets = [
        {
            "tool": "jira",
            "operation": "mock_file",
            "external_id": previous_jira.get("external_id", ""),
            "url": previous_jira.get("url", ""),
            "status": "synced",
            "path": store.relative(jira_comment_path),
            "artifact_refs": [plan_ref, structured_ref, review_ref],
            "error": None,
        },
        {
            "tool": "confluence",
            "operation": "mock_file",
            "external_id": previous_confluence.get("external_id", ""),
            "url": previous_confluence.get("url", ""),
            "status": "synced",
            "path": store.relative(confluence_page_path),
            "artifact_refs": [plan_ref, structured_ref, review_ref],
            "error": None,
        },
    ]
    report = {
        "version": "tool_sync_report_v3",
        "phase": "implementation_plan_review",
        "status": "mock_synced",
        "mode": "mock",
        "targets": targets,
        "checkpoint": {
            "type": "implementation_plan_review",
            "artifact_ref": plan_ref,
            "artifact_path": implementation_plan,
        },
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "phase": "implementation_plan_review",
        "status": "mock_synced",
        "mode": "mock",
        "report": store.relative(report_path),
        "targets": [
            {
                "tool": target["tool"],
                "status": target["status"],
                "path": target["path"],
                "url": target["url"],
                "external_id": target["external_id"],
            }
            for target in targets
        ],
    }


def _previous_targets(report_path) -> dict[str, dict[str, str]]:
    """Giữ lại external id/url cũ khi mock sync chạy trước real sync trong mode both."""

    if not report_path.exists():
        return {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    result: dict[str, dict[str, str]] = {}
    for target in report.get("targets", []):
        if not isinstance(target, dict):
            continue
        tool = str(target.get("tool", ""))
        external_id = str(target.get("external_id", ""))
        if not tool or not external_id:
            continue
        result[tool] = {
            "external_id": external_id,
            "url": str(target.get("url", "")),
        }
    return result


def _artifact_ref(path: str) -> str:
    """Lấy artifact ref từ đường dẫn runtime, ví dụ `.../design_artifact_v2.md`."""

    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]

