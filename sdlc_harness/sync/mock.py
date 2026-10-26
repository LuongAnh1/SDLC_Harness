"""Mock sync adapter cho các checkpoint từ requirement đến code review.

Phase 4 còn dùng adapter này để giả lập checkpoint Code Review sau khi Draft PR
đã được publish. Sync thật đã nằm ở adapter Atlassian. File này chỉ tạo artifact
giả lập để unit test/smoke test chạy được khi chưa cấu hình Jira/Confluence thật;
ownership vẫn giữ ở SDLC_Harness, Niko chỉ đọc report trả về.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..storage.runtime import RuntimeStore, utc_now_iso
from .design_review import HUMAN_DESIGN_REVIEW_VERSION, build_human_design_review


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
    deferred_ids = context.get("deferred_to_design_question_ids")
    deferred_ids = deferred_ids if isinstance(deferred_ids, list) else []
    deferred_count = context.get("deferred_to_design_count")
    deferred_line = ""
    if deferred_count is not None:
        deferred_suffix = f" ({', '.join(str(item) for item in deferred_ids)})" if deferred_ids else ""
        deferred_line = f"\nDeferred to Design: {deferred_count} item(s){deferred_suffix}\n"
    decision = context.get("decision_summary")
    decision_line = ""
    if isinstance(decision, dict):
        decision_line = (
            f"\nLatest decision: {decision.get('id')} / {decision.get('question_id')} / {decision.get('status')}\n"
        )
    assumption_challenge = context.get("assumption_challenge")
    assumption_challenge_line = ""
    if isinstance(assumption_challenge, dict):
        assumption_challenge_line = (
            f"\nAssumption challenged: {assumption_challenge.get('assumption_id')}; "
            f"origin={assumption_challenge.get('origin')}; "
            f"reopened question={assumption_challenge.get('question_id')}; "
            f"by={assumption_challenge.get('challenged_by')}\n"
        )
    jira_comment_path.write_text(
        f"""# Mock Jira Comment

Source: `{source_ref}`

Requirement checkpoint status: `{status}`.{question_line}{remaining_line}{deferred_line}{assumption_challenge_line}{decision_line}

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

Checkpoint status: `{status}`.{question_line}{remaining_line}{deferred_line}{assumption_challenge_line}{decision_line}

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
    landscape_section = _repo_landscape_markdown(store, artifact_paths)
    bundle_section = _design_bundle_markdown(store, artifact_paths)
    compatibility_section = _compatibility_uml_markdown(store, design_artifact) if bundle_section else ""
    human_review = build_human_design_review(
        requirement_model=_read_json_artifact(store, artifact_paths.get("requirement_model", "")),
        component_view=_read_json_artifact(store, artifact_paths.get("design_component_view", "")),
        surface_view=_read_json_artifact(store, artifact_paths.get("design_surface_view", "")),
        validation_report=_read_json_artifact(store, artifact_paths.get("design_validation_report", "")),
    )
    human_review_section = _human_design_review_markdown(human_review)
    landscape_ref = _artifact_ref(artifact_paths["repo_landscape"]) if artifact_paths.get("repo_landscape") else ""
    vertical_graph = artifact_paths.get("vertical_design_graph", "")
    projection_manifest = artifact_paths.get("design_projection_manifest", "")
    finding_ledger = artifact_paths.get("design_finding_ledger", "")
    vertical_section = ""
    if vertical_graph:
        vertical_section = (
            "Vertical Design bundle:\n"
            f"- Canonical graph: `{vertical_graph}`\n"
            f"- Projection manifest: `{projection_manifest}`\n"
            f"- Finding ledger: `{finding_ledger}`"
        )
    vertical_refs = [
        _artifact_ref(path)
        for path in (vertical_graph, projection_manifest, finding_ledger)
        if path
    ]
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

{vertical_section}

{landscape_section}

Next action: approve design hoặc request change.
""",
        encoding="utf-8",
    )
    technical_appendix = f"""<details>
<summary>Mở phụ lục kỹ thuật: contracts, trace, artifacts và quality report</summary>

## Design Artifacts

- Designer analysis: `{artifact_paths['designer_analysis']}`
- Design artifact: `{design_artifact}`
- Class diagram graph: `{artifact_paths['class_diagram_graph']}`
- Sequence diagram graph: `{artifact_paths['sequence_diagram_graph']}`
- Requirement-design trace: `{trace_artifact}`
- Missing design links: `{artifact_paths['missing_design_links']}`
- Quality report: `{quality_report}`

{vertical_section}

{landscape_section}

{bundle_section}

</details>"""
    confluence_page_path.write_text(
        f"""# Mock Confluence Page - Design Checkpoint

Source: `{source_ref}`
Run: `{run_id}`

Trang này ưu tiên cho người duyệt. Chi tiết máy đọc và kỹ thuật nằm trong phụ lục thu gọn ở cuối trang.

{human_review_section}

## Phụ lục kỹ thuật

{technical_appendix}

{compatibility_section}
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
            "artifact_refs": [design_ref, trace_ref, quality_ref, *vertical_refs, *([landscape_ref] if landscape_ref else [])],
            "error": None,
        },
        {
            "tool": "confluence",
            "operation": "mock_file",
            "external_id": previous_confluence.get("external_id", ""),
            "url": previous_confluence.get("url", ""),
            "status": "synced",
            "path": store.relative(confluence_page_path),
            "artifact_refs": [analysis_ref, design_ref, missing_ref, *vertical_refs, *([landscape_ref] if landscape_ref else [])],
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
            **({"vertical_design_graph_ref": _artifact_ref(vertical_graph)} if vertical_graph else {}),
            **({"projection_manifest_ref": _artifact_ref(projection_manifest)} if projection_manifest else {}),
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


def _repo_landscape_markdown(store: RuntimeStore, artifact_paths: dict[str, str]) -> str:
    reference = artifact_paths.get("repo_landscape", "")
    if not reference:
        return ""
    path = Path(reference)
    resolved = path.resolve() if path.is_absolute() else (store.repo_root / path).resolve()
    try:
        landscape = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "## Repository Landscape\n\n- Summary unavailable; inspect the landscape artifact."
    summary = landscape.get("summary") if isinstance(landscape, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    discovery = landscape.get("discovery") if isinstance(landscape, dict) else {}
    discovery = discovery if isinstance(discovery, dict) else {}
    return "\n".join(
        [
            "## Repository Landscape",
            "",
            f"- Mode: `{discovery.get('mode') or 'unknown'}`",
            (
                f"- Inventory: {int(summary.get('component_count') or 0)} component(s), "
                f"{int(summary.get('file_count') or 0)} file(s), "
                f"{int(summary.get('anchor_count') or 0)} anchor(s)"
            ),
            (
                f"- Conformance: {int(summary.get('blocking_count') or 0)} blocker(s), "
                f"{int(summary.get('warning_count') or 0)} warning(s), "
                f"{int(summary.get('unmatched_surface_count') or 0)} unmatched surface(s)"
            ),
            f"- Artifact: `{reference}`",
        ]
    )


def _design_bundle_markdown(store: RuntimeStore, artifact_paths: dict[str, str]) -> str:
    component_view = _read_json_artifact(store, artifact_paths.get("design_component_view", ""))
    surface_view = _read_json_artifact(store, artifact_paths.get("design_surface_view", ""))
    trace_view = _read_json_artifact(store, artifact_paths.get("requirement_design_trace", ""))
    if component_view.get("schema_version") != "design_component_view_v2":
        return ""
    lines = ["## System Architecture", ""]
    component_diagram = str(component_view.get("plantuml") or "")
    if component_diagram:
        lines.extend(["```plantuml", component_diagram, "```", ""])
    lines.extend(["| Component | Elements | Surfaces | Decisions |", "| --- | ---: | --- | --- |"])
    for component in _dict_list(component_view.get("components")):
        nodes = _dict_list(component.get("nodes"))
        surfaces = sorted({str(item.get("surface") or "") for item in nodes if str(item.get("surface") or "")})
        decisions = sorted({str(item.get("disposition") or "") for item in nodes if str(item.get("disposition") or "")})
        lines.append(f"| {component.get('component_id', '')} | {len(nodes)} | {', '.join(surfaces)} | {', '.join(decisions)} |")

    lines.extend(["", "## UI States and Interactions", ""])
    for diagram in _dict_list(surface_view.get("ui_state_diagrams")):
        lines.extend([f"### {diagram.get('scenario_id', 'UI flow')}", "", "```plantuml", str(diagram.get("plantuml") or ""), "```", ""])
    lines.extend(["| Scenario | Element | Entrypoint | States | Actions / outcomes |", "| --- | --- | --- | --- | --- |"])
    for item in _dict_list(surface_view.get("ui")):
        contract = _dict_value(item.get("surface_contract"))
        transitions = "; ".join(
            f"{entry.get('from', '')} --{entry.get('action', '')}--> {entry.get('to', '')}: {entry.get('outcome', '')}"
            for entry in _dict_list(contract.get("transitions"))
        )
        lines.append(f"| {item.get('scenario_id', '')} | {item.get('name', '')} | {contract.get('entrypoint', '')} | {', '.join(_string_list(contract.get('states')))} | {transitions} |")

    lines.extend(["", "## API Contracts", "", "| Scenario | Operation | Endpoint | Request | Responses | Authorization |", "| --- | --- | --- | --- | --- | --- |"])
    for item in _dict_list(surface_view.get("api")):
        contract = _dict_value(item.get("surface_contract"))
        responses = "; ".join(f"{entry.get('status', '')}: {entry.get('outcome', '')}" for entry in _dict_list(contract.get("responses")))
        request = "; ".join(
            f"{entry.get('name', '')}: {entry.get('type', '')} ({entry.get('location', '')})"
            for entry in _dict_list(contract.get("request_fields"))
        )
        lines.append(f"| {item.get('scenario_id', '')} | {item.get('name', '')} | {contract.get('method', '')} {contract.get('path', '')} | {request} | {responses} | {contract.get('authorization', '')} |")

    lines.extend(["", "## Business Rules", "", "| Scenario | Rule | Inputs | Decision | Outcomes |", "| --- | --- | --- | --- | --- |"])
    for item in _dict_list(surface_view.get("business_rules")):
        contract = _dict_value(item.get("surface_contract"))
        lines.append(f"| {item.get('scenario_id', '')} | {item.get('name', '')} | {', '.join(_string_list(contract.get('inputs')))} | {contract.get('decision', '')} | {', '.join(_string_list(contract.get('outcomes')))} |")

    lines.extend(["", "## Data Model and Operations", ""])
    data_diagram = str(surface_view.get("data_model_plantuml") or "")
    if data_diagram:
        lines.extend(["```plantuml", data_diagram, "```", ""])
    lines.extend(["| Scenario | Entity | Operation | Fields | Constraints | Migration |", "| --- | --- | --- | --- | --- | --- |"])
    for item in _dict_list(surface_view.get("data")):
        contract = _dict_value(item.get("surface_contract"))
        fields = ", ".join(f"{entry.get('name', '')}: {entry.get('type', '')}" for entry in _dict_list(contract.get("fields")))
        constraints = ", ".join(_string_list(contract.get("constraints"))) or "none"
        lines.append(f"| {item.get('scenario_id', '')} | {contract.get('entity', '')} | {contract.get('operation', '')} | {fields} | {constraints} | {contract.get('migration_intent', '')} |")

    lines.extend(["", "## AS-IS / GAP / TO-BE Impact", "", "| Scenario | Surface | Element | Decision | AS-IS evidence / gap | TO-BE contract |", "| --- | --- | --- | --- | --- | --- |"])
    for item in _dict_list(surface_view.get("change_impact")):
        evidence = ", ".join(_string_list(item.get("evidence_refs"))) or str(item.get("evidence_reason") or "")
        lines.append(f"| {item.get('scenario_id', '')} | {item.get('surface', '')} | {item.get('name', '')} | {item.get('disposition', '')} | {evidence} | {item.get('contract', '')} |")

    lines.extend(["", "## Requirement to Design Trace", "", "| Requirement | Approved behavior | Design elements |", "| --- | --- | --- |"])
    for item in _dict_list(trace_view.get("class_trace")):
        targets = [str(link.get("target") or "") for link in _dict_list(item.get("links")) if str(link.get("target") or "")]
        preview = targets[:4]
        suffix = f" (+{len(targets) - len(preview)} more)" if len(targets) > len(preview) else ""
        lines.append(f"| {item.get('requirement', '')} | {item.get('content', '')} | {', '.join(preview)}{suffix} |")
    not_applicable = _dict_list(surface_view.get("not_applicable"))
    if not_applicable:
        lines.extend(["", "## Not Applicable Surfaces", "", "| Scenario | Surface | Reason |", "| --- | --- | --- |"])
        for item in not_applicable:
            lines.append(f"| {item.get('scenario_id', '')} | {item.get('surface', '')} | {item.get('reason', '')} |")
    return "\n".join(lines)


def _human_design_review_markdown(review: dict[str, object]) -> str:
    if review.get("schema_version") != HUMAN_DESIGN_REVIEW_VERSION:
        return ""
    use_case = _dict_value(review.get("use_case"))
    status = _dict_value(review.get("status"))
    scope = _dict_value(review.get("scope"))
    blockers = int(status.get("blocking_count") or 0)
    warnings = int(status.get("warning_count") or 0)
    gate = f"BLOCKED - {blockers} blocker(s)" if blockers else (f"READY WITH {warnings} WARNING(S)" if warnings else "READY FOR APPROVAL")
    lines = [
        "## 1. Tóm tắt dành cho người duyệt",
        "",
        f"**Trạng thái:** {gate}",
        "",
        f"- **Use case:** {use_case.get('name') or use_case.get('id') or ''}",
        f"- **Mục tiêu:** {use_case.get('goal') or ''}",
        f"- **Người sử dụng:** {', '.join(_string_list(use_case.get('actors'))) or 'Chưa xác định'}",
        (
            f"- **Phạm vi:** {scope.get('scenario_count', 0)} scenario, {scope.get('component_count', 0)} component; "
            f"{scope.get('ui_count', 0)} UI flow, {scope.get('api_count', 0)} API contract, "
            f"{scope.get('business_rule_count', 0)} business rule, {scope.get('data_count', 0)} data operation."
        ),
        "",
        "## 2. Tính năng sẽ hoạt động như thế nào?",
        "",
        "### Luồng chính",
        "",
    ]
    for index, item in enumerate(_dict_list(review.get("main_flow")), start=1):
        lines.append(f"{index}. **{item.get('actor', '')}:** {item.get('action', '')}")
    lines.extend(
        [
            "",
            "### Các trường hợp khác luồng chính",
            "",
            "| Tình huống | Loại | Hệ thống xử lý | Kết quả nhìn thấy |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in _dict_list(review.get("exception_scenarios")):
        lines.append(
            f"| {_md_cell(str(item.get('id') or ''))} - {_md_cell(str(item.get('name') or ''))} | "
            f"{_md_cell(str(item.get('kind') or ''))} | {_md_cell(str(item.get('summary') or ''))} | "
            f"{_md_cell('; '.join(_string_list(item.get('outcomes'))))} |"
        )
    lines.extend(
        [
            "",
            "## 3. Hệ thống sẽ thay đổi ở đâu?",
            "",
            "| Khu vực | Mức thay đổi | Các phần chính |",
            "| --- | --- | --- |",
        ]
    )
    for item in _dict_list(review.get("component_changes")):
        lines.append(
            f"| {_md_cell(str(item.get('label') or item.get('component_id') or ''))} | "
            f"{_md_cell(str(item.get('change_summary') or ''))} | "
            f"{_md_cell(', '.join(_string_list(item.get('elements'))))} |"
        )
    lines.extend(["", "## 4. Các hợp đồng chính cần biết", "", "### API", "", "| Tình huống | Endpoint | Kết quả chính | Quyền truy cập |", "| --- | --- | --- | --- |"])
    for item in _dict_list(review.get("api_contracts")):
        lines.append(
            f"| {_md_cell(str(item.get('scenario_id') or ''))} | {_md_cell(str(item.get('endpoint') or ''))} | "
            f"{_md_cell('; '.join(_string_list(item.get('outcomes'))))} | {_md_cell(str(item.get('authorization') or ''))} |"
        )
    lines.extend(["", "### Quy tắc nghiệp vụ", "", "| Tình huống | Quy tắc | Quyết định |", "| --- | --- | --- |"])
    for item in _dict_list(review.get("business_rules")):
        lines.append(f"| {_md_cell(str(item.get('scenario_id') or ''))} | {_md_cell(str(item.get('name') or ''))} | {_md_cell(str(item.get('decision') or ''))} |")
    lines.extend(["", "### Dữ liệu", "", "| Tình huống | Đối tượng | Thao tác | Ảnh hưởng migration |", "| --- | --- | --- | --- |"])
    for item in _dict_list(review.get("data_changes")):
        lines.append(
            f"| {_md_cell(str(item.get('scenario_id') or ''))} | {_md_cell(str(item.get('entity') or ''))} | "
            f"{_md_cell(str(item.get('operation') or ''))} | {_md_cell(str(item.get('migration') or ''))} |"
        )
    findings = _dict_list(review.get("review_findings"))
    lines.extend(["", "## 5. Điểm cần review trước khi duyệt", ""])
    if findings:
        lines.extend(["| ID | Mức độ | Vấn đề | Nếu muốn xử lý |", "| --- | --- | --- | --- |"])
        for item in findings:
            lines.append(
                f"| {_md_cell(str(item.get('id') or ''))} | {_md_cell(str(item.get('severity') or ''))} | "
                f"{_md_cell(str(item.get('message') or ''))} | {_md_cell(str(item.get('expected_change') or ''))} |"
            )
    else:
        lines.append("Không còn blocker hoặc warning; Design sẵn sàng để duyệt.")
    lines.extend(["", "**Quyết định cần từ anh:** approve nếu luồng và phạm vi thay đổi đã đúng; request change nếu cần sửa trước khi sang Phase 3."])
    return "\n".join(lines)


def _compatibility_uml_markdown(store: RuntimeStore, path: str) -> str:
    design_text = _read_text_artifact(store, path)
    start = design_text.find("## Class Diagram")
    end = design_text.find("## AS-IS / GAP / TO-BE")
    if start < 0:
        return ""
    diagrams = design_text[start : end if end > start else len(design_text)].strip()
    return "\n".join(
        [
            "## Compatibility UML",
            "",
            "<details>",
            "<summary>Class and sequence diagrams</summary>",
            "",
            diagrams,
            "",
            "</details>",
        ]
    )


def _read_json_artifact(store: RuntimeStore, path: str) -> dict[str, object]:
    if not path:
        return {}
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = store.repo_root / resolved
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_text_artifact(store: RuntimeStore, path: str) -> str:
    if not path:
        return ""
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = store.repo_root / resolved
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError:
        return ""


def _dict_list(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value if str(item)] if isinstance(value, list) else []


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _artifact_ref(path: str) -> str:
    """Lấy artifact ref từ đường dẫn runtime, ví dụ `.../design_artifact_v2.md`."""

    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]

