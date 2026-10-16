"""Mock sync adapter cho các checkpoint từ requirement đến code review.

Phase 4 còn dùng adapter này để giả lập checkpoint Code Review sau khi Draft PR
đã được publish. Sync thật đã nằm ở adapter Atlassian. File này chỉ tạo artifact
giả lập để unit test/smoke test chạy được khi chưa cấu hình Jira/Confluence thật;
ownership vẫn giữ ở SDLC_Harness, Niko chỉ đọc report trả về.
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
    checkpoint_context: dict[str, object] | None = None,
) -> dict[str, object]:
    """Tạo file giả lập Jira comment, Confluence page và sync report."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)

    jira_comment_path = sync_dir / "jira-comment.md"
    confluence_page_path = sync_dir / "confluence-page.md"
    report_path = sync_dir / "tool_sync_report_v1.json"

    context = checkpoint_context or {}
    status = str(context.get("status") or "waiting_requirement_approval")
    current_question = context.get("current_question")
    question_text = current_question.get("text") if isinstance(current_question, dict) else ""
    question_line = f"\nCurrent clarification question: {question_text}\n" if question_text else ""
    remaining = context.get("remaining_blocking_count")
    remaining_line = f"\nRemaining blocking questions: {remaining}\n" if remaining is not None else ""
    decision = context.get("decision_summary")
    decision_line = ""
    if isinstance(decision, dict):
        decision_line = (
            f"\nLatest decision: {decision.get('id')} / {decision.get('question_id')} / {decision.get('status')}\n"
        )
    jira_comment_path.write_text(
        f"""# Mock Jira Comment

Source: `{source_ref}`

Requirement checkpoint status: `{status}`.{question_line}{remaining_line}{decision_line}

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
    trace_line = ""
    if "trace_graph" in artifact_paths:
        trace_line = f"- Trace graph: `{artifact_paths['trace_graph']}`\n"
    model_line = ""
    if "requirement_model" in artifact_paths:
        model_line = f"- Canonical model: `{artifact_paths['requirement_model']}`\n"
    confluence_page_path.write_text(
        f"""# Mock Confluence Page

Source: `{source_ref}`

Trang này giả lập nơi lưu formal use case và requirement packet để reviewer đọc trước khi chuyển sang phase design.

Checkpoint status: `{status}`.{question_line}{remaining_line}{decision_line}

Artifacts:
- Use case packet: `{artifact_paths['use_case_packet']}`
{formal_line}{check_line}- Requirement packet: `{artifact_paths['requirement_packet']}`
{model_line}{trace_line}
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

Review implementation plan trước khi cho phép Phase 4 sửa code, chạy test và tạo Draft PR trong branch/worktree đã cấp cho run.
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


def mock_sync_code_review_checkpoint(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    review_status: str = "code_review_ready",
) -> dict[str, object]:
    """Tạo mock checkpoint Code Review mà không chạm Jira/Confluence thật."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    jira_comment_path = sync_dir / "code-review-jira-comment.md"
    confluence_page_path = sync_dir / "code-review-confluence-page.md"
    report_path = sync_dir / "tool_sync_report_v4.json"
    github_path = artifact_paths["github_code_review"]
    github = json.loads((store.repo_root / github_path).read_text(encoding="utf-8"))
    trace = json.loads(
        (store.repo_root / artifact_paths["requirement_design_code_trace"]).read_text(encoding="utf-8")
    )
    requirement_contract = trace.get("requirement_contract", {})
    requirement_contract = requirement_contract if isinstance(requirement_contract, dict) else {}
    trace_stats = trace.get("stats", {})
    trace_stats = trace_stats if isinstance(trace_stats, dict) else {}
    requirement_ids = sorted(
        {
            str(item.get("requirement_ref") or "").strip()
            for item in trace.get("mappings", [])
            if isinstance(item, dict) and str(item.get("requirement_ref") or "").strip()
        }
    )
    coverage_line = (
        f"{trace_stats.get('requirements_covered', 0)}/{trace_stats.get('requirement_count', 0)} stable IDs"
    )
    requirement_ids_line = ", ".join(requirement_ids) or "none"
    pull_request = github.get("pull_request", {})
    commit = github.get("commit", {})
    previous_targets = _previous_targets(report_path)
    approved = review_status == "code_review_approved"
    pr_label = "Ready PR" if approved else "Draft PR"
    next_action = (
        "Human reviews and merges the PR directly on GitHub."
        if approved
        else "Human approves the checkpoint to mark the Draft PR ready for review."
    )

    jira_comment_path.write_text(
        f"""# Mock Jira Comment - Code Review Checkpoint

Run: `{run_id}`
Source: `{source_ref}`
Status: `{review_status}`
{pr_label}: `{pull_request.get('url', '')}`
Branch: `{github.get('branch', '')}`
Commit: `{commit.get('sha', '')}`
Requirement contract: `{requirement_contract.get('version', 'v1')}` revision `{requirement_contract.get('revision', 0)}`
Canonical coverage: `{coverage_line}`
Stable requirement IDs: `{requirement_ids_line}`
Next action: {next_action}
""",
        encoding="utf-8",
    )
    confluence_page_path.write_text(
        f"""# Mock Confluence Page - Code Review Checkpoint

Run: `{run_id}`
Source: `{source_ref}`
Status: `{review_status}`
{pr_label}: `{pull_request.get('url', '')}`
Requirement contract: `{requirement_contract.get('version', 'v1')}` revision `{requirement_contract.get('revision', 0)}`
Canonical coverage: `{coverage_line}`
Stable requirement IDs: `{requirement_ids_line}`

Evidence:
- Verification: `{artifact_paths['test_report']}`
- Validator trace: `{artifact_paths['requirement_design_code_trace']}`
- QA: `{artifact_paths['qa_test_report']}`
""",
        encoding="utf-8",
    )
    targets = [
        {
            "tool": "github",
            "mode": "reference",
            "status": "referenced",
            "operation": str(pull_request.get("operation") or "referenced"),
            "external_id": str(pull_request.get("number") or ""),
            "url": str(pull_request.get("url") or ""),
        },
        {
            "tool": "jira",
            "mode": "mock_file",
            "status": "synced",
            "operation": "mock_file",
            "external_id": previous_targets.get("jira", {}).get("external_id", ""),
            "url": previous_targets.get("jira", {}).get("url", ""),
            "path": store.relative(jira_comment_path),
        },
        {
            "tool": "confluence",
            "mode": "mock_file",
            "status": "synced",
            "operation": "mock_file",
            "external_id": previous_targets.get("confluence", {}).get("external_id", ""),
            "url": previous_targets.get("confluence", {}).get("url", ""),
            "path": store.relative(confluence_page_path),
        },
    ]
    checkpoint_path = (
        artifact_paths.get("github_code_review_approval", "")
        if approved
        else github_path
    )
    report = {
        "version": "tool_sync_report_v4",
        "phase": "code_review",
        "status": "mock_synced",
        "mode": "mock",
        "targets": targets,
        "checkpoint": {
            "type": "code_review",
            "status": review_status,
            "artifact_ref": _artifact_ref(checkpoint_path),
            "artifact_path": checkpoint_path,
        },
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "phase": "code_review",
        "status": "mock_synced",
        "mode": "mock",
        "review_status": review_status,
        "report": store.relative(report_path),
        "targets": targets,
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

