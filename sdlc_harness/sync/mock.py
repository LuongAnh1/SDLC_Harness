"""Mock sync adapter cho Phase 1.

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
