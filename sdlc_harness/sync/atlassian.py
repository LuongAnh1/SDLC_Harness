"""Các adapter đồng bộ thật cho Jira và Confluence Cloud.

Các adapter trong module này đẩy artifact checkpoint lên công cụ Atlassian và
ghi báo cáo đồng bộ công cụ dưới thư mục run. Phase 1 ghi review requirement,
Phase 2 ghi review thiết kế, Phase 3 ghi review implementation plan, còn Phase 4
ghi Code Review dựa trên Draft PR đã publish. Định danh phía remote ổn định theo
`source_ref + phase`, nên khi thiếu báo cáo cục bộ hệ thống không tự động tạo
trùng Jira comment hoặc Confluence page. Renderer chỉ gửi artifact ref và
định danh branch/commit/PR; đường dẫn worktree cục bộ trên máy được loại bỏ.
Niko không bao giờ gọi Atlassian trực tiếp.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..core.config import Settings
from ..storage.runtime import RuntimeStore, utc_now_iso
from .atlassian_rendering import (
    _artifact_ref,
    _design_artifact_refs,
    _implementation_plan_artifact_refs,
    _read_json_artifact,
)
from .atlassian_transport import (
    _aggregate_status,
    _failed_target,
    _previous_target_id,
    _previous_target_id_from_reports,
    _request_json,
    _resolve_checkpoint_target_id,
    _sync_confluence_code_review_page,
    _sync_confluence_design_page,
    _sync_confluence_implementation_plan_page,
    _sync_confluence_page,
    _sync_jira_code_review_comment,
    _sync_jira_comment,
    _sync_jira_design_comment,
    _sync_jira_implementation_plan_comment,
)

JsonRequester = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def real_sync_requirement_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    checkpoint_context: dict[str, object] | None = None,
    request_json: JsonRequester | None = None,
) -> dict[str, object]:
    """Sync requirement checkpoint to Jira/Confluence and write sync report."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    report_path = sync_dir / "tool_sync_report_v1.json"
    requester = request_json or _request_json(settings)

    targets: list[dict[str, object]] = []
    if settings.jira_sync_enabled:
        try:
            previous_comment_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="requirement",
                tool="jira",
                local_target_id=_previous_target_id(report_path, "jira"),
            )
            targets.append(
                _sync_jira_comment(
                    settings,
                    requester,
                    store,
                    run_id,
                    source_ref,
                    artifact_paths,
                    checkpoint_context=checkpoint_context,
                    previous_comment_id=previous_comment_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            targets.append(_failed_target("jira", source_ref, exc, issue_key=settings.jira_sync_issue_key))
    if settings.confluence_sync_enabled:
        try:
            previous_page_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="requirement",
                tool="confluence",
                local_target_id=_previous_target_id(report_path, "confluence"),
            )
            targets.append(
                _sync_confluence_page(
                    settings,
                    requester,
                    store,
                    run_id,
                    source_ref,
                    artifact_paths,
                    checkpoint_context=checkpoint_context,
                    previous_page_id=previous_page_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            targets.append(_failed_target("confluence", source_ref, exc))
    if not targets:
        targets.append(
            {
                "tool": "sync",
                "mode": "real_api",
                "status": "failed",
                "error_code": "no_real_sync_targets",
                "message": "No real sync targets are enabled.",
                "source_ref": source_ref,
            }
        )

    status = _aggregate_status(targets)
    report = {
        "version": 1,
        "mode": "real",
        "status": status,
        "created_at": utc_now_iso(),
        "targets": targets,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": status,
        "mode": "real",
        "report": store.relative(report_path),
        "targets": [
            {
                "tool": target.get("tool"),
                "status": target.get("status"),
                "url": target.get("url", ""),
                "external_id": target.get("external_id", ""),
                "error_code": target.get("error_code", ""),
                "message": target.get("message", ""),
            }
            for target in targets
        ],
    }


def real_sync_design_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    request_json: JsonRequester | None = None,
) -> dict[str, object]:
    """Sync design checkpoint Phase 2 to Jira/Confluence and write v2 report."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    report_path = sync_dir / "tool_sync_report_v2.json"
    requester = request_json or _request_json(settings)

    targets: list[dict[str, object]] = []
    if settings.jira_sync_enabled:
        try:
            previous_comment_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="design",
                tool="jira",
                local_target_id=_previous_target_id_from_reports([report_path], "jira"),
            )
            targets.append(
                _sync_jira_design_comment(
                    settings,
                    requester,
                    store,
                    run_id,
                    source_ref,
                    artifact_paths,
                    previous_comment_id=previous_comment_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            targets.append(_failed_target("jira", source_ref, exc, issue_key=settings.jira_sync_issue_key))
    if settings.confluence_sync_enabled:
        try:
            previous_page_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="design",
                tool="confluence",
                local_target_id=_previous_target_id_from_reports([report_path], "confluence"),
            )
            targets.append(
                _sync_confluence_design_page(
                    settings,
                    requester,
                    store,
                    run_id,
                    source_ref,
                    artifact_paths,
                    previous_page_id=previous_page_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            targets.append(_failed_target("confluence", source_ref, exc))
    if not targets:
        targets.append(
            {
                "tool": "sync",
                "mode": "real_api",
                "status": "failed",
                "operation": "none",
                "artifact_refs": _design_artifact_refs(artifact_paths),
                "error": "No real sync targets are enabled.",
                "error_code": "no_real_sync_targets",
                "message": "No real sync targets are enabled.",
                "source_ref": source_ref,
            }
        )

    status = _aggregate_status(targets)
    design_artifact = artifact_paths.get("design_artifact", "")
    report = {
        "version": "tool_sync_report_v2",
        "phase": "design_checkpoint",
        "status": status,
        "mode": "real",
        "targets": targets,
        "checkpoint": {
            "type": "design_approval",
            "artifact_ref": _artifact_ref(design_artifact),
            "artifact_path": design_artifact,
            **(
                {"vertical_design_graph_ref": _artifact_ref(artifact_paths["vertical_design_graph"])}
                if artifact_paths.get("vertical_design_graph")
                else {}
            ),
            **(
                {"projection_manifest_ref": _artifact_ref(artifact_paths["design_projection_manifest"])}
                if artifact_paths.get("design_projection_manifest")
                else {}
            ),
            **(
                {"vertical_trace_graph_ref": _artifact_ref(artifact_paths["vertical_trace_graph"])}
                if artifact_paths.get("vertical_trace_graph")
                else {}
            ),
            **(
                {"vertical_trace_validation_ref": _artifact_ref(artifact_paths["vertical_trace_validation"])}
                if artifact_paths.get("vertical_trace_validation")
                else {}
            ),
        },
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "phase": "design_checkpoint",
        "status": status,
        "mode": "real",
        "report": store.relative(report_path),
        "targets": [
            {
                "tool": target.get("tool"),
                "status": target.get("status"),
                "operation": target.get("operation", ""),
                "url": target.get("url", ""),
                "external_id": target.get("external_id", ""),
            }
            for target in targets
        ],
    }


def real_sync_implementation_plan_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    request_json: JsonRequester | None = None,
) -> dict[str, object]:
    """Sync implementation plan checkpoint Phase 3 to Jira/Confluence."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    report_path = sync_dir / "tool_sync_report_v3.json"
    requester = request_json or _request_json(settings)

    targets: list[dict[str, object]] = []
    confluence_url = ""
    if settings.confluence_sync_enabled:
        try:
            previous_page_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="implementation_plan",
                tool="confluence",
                local_target_id=_previous_target_id_from_reports([report_path], "confluence"),
            )
            confluence_target = _sync_confluence_implementation_plan_page(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                previous_page_id=previous_page_id,
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            confluence_target = _failed_target("confluence", source_ref, exc)
        targets.append(confluence_target)
        if confluence_target.get("status") == "synced":
            confluence_url = str(confluence_target.get("url") or "")
    if settings.jira_sync_enabled:
        try:
            previous_comment_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="implementation_plan",
                tool="jira",
                local_target_id=_previous_target_id_from_reports([report_path], "jira"),
            )
            targets.append(
                _sync_jira_implementation_plan_comment(
                    settings,
                    requester,
                    store,
                    run_id,
                    source_ref,
                    artifact_paths,
                    confluence_url=confluence_url,
                    previous_comment_id=previous_comment_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            targets.append(_failed_target("jira", source_ref, exc, issue_key=settings.jira_sync_issue_key))
    if not targets:
        targets.append(
            {
                "tool": "sync",
                "mode": "real_api",
                "status": "failed",
                "operation": "none",
                "artifact_refs": _implementation_plan_artifact_refs(artifact_paths),
                "error": "No real sync targets are enabled.",
                "error_code": "no_real_sync_targets",
                "message": "No real sync targets are enabled.",
                "source_ref": source_ref,
            }
        )

    status = _aggregate_status(targets)
    implementation_plan = artifact_paths.get("implementation_plan", "")
    report = {
        "version": "tool_sync_report_v3",
        "phase": "implementation_plan_review",
        "status": status,
        "mode": "real",
        "targets": targets,
        "checkpoint": {
            "type": "implementation_plan_review",
            "artifact_ref": _artifact_ref(implementation_plan),
            "artifact_path": implementation_plan,
        },
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "phase": "implementation_plan_review",
        "status": status,
        "mode": "real",
        "report": store.relative(report_path),
        "targets": [
            {
                "tool": target.get("tool"),
                "status": target.get("status"),
                "operation": target.get("operation", ""),
                "url": target.get("url", ""),
                "external_id": target.get("external_id", ""),
            }
            for target in targets
        ],
    }


def real_sync_code_review_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    review_status: str = "code_review_ready",
    request_json: JsonRequester | None = None,
) -> dict[str, object]:
    """Sync checkpoint review code sau khi GitHub Draft PR đã publish thành công."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    report_path = sync_dir / "tool_sync_report_v4.json"
    requester = request_json or _request_json(settings)
    github_report = _read_json_artifact(store, artifact_paths, "github_code_review")
    pull_request = github_report.get("pull_request")
    pull_request = pull_request if isinstance(pull_request, dict) else {}
    commit = github_report.get("commit")
    commit = commit if isinstance(commit, dict) else {}
    targets: list[dict[str, object]] = [
        {
            "tool": "github",
            "mode": "reference",
            "status": "referenced",
            "operation": str(pull_request.get("operation") or "referenced"),
            "external_id": str(pull_request.get("number") or ""),
            "url": str(pull_request.get("url") or ""),
            "source_ref": source_ref,
            "commit_sha": str(commit.get("sha") or ""),
        }
    ]
    sync_targets: list[dict[str, object]] = []
    confluence_url = ""

    if settings.confluence_sync_enabled:
        try:
            previous_page_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="code_review",
                tool="confluence",
                local_target_id=_previous_target_id(report_path, "confluence"),
            )
            confluence_target = _sync_confluence_code_review_page(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                review_status=review_status,
                previous_page_id=previous_page_id,
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            confluence_target = _failed_target("confluence", source_ref, exc)
        sync_targets.append(confluence_target)
        if confluence_target.get("status") == "synced":
            confluence_url = str(confluence_target.get("url") or "")

    if settings.jira_sync_enabled:
        try:
            previous_comment_id = _resolve_checkpoint_target_id(
                settings=settings,
                request_json=requester,
                source_ref=source_ref,
                phase="code_review",
                tool="jira",
                local_target_id=_previous_target_id(report_path, "jira"),
            )
            jira_target = _sync_jira_code_review_comment(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                confluence_url=confluence_url,
                review_status=review_status,
                previous_comment_id=previous_comment_id,
            )
        except Exception as exc:  # noqa: BLE001 - lookup remote phải thành report.
            jira_target = _failed_target("jira", source_ref, exc, issue_key=settings.jira_sync_issue_key)
        sync_targets.append(jira_target)

    if not sync_targets:
        sync_targets.append(
            {
                "tool": "sync",
                "mode": "real_api",
                "status": "failed",
                "operation": "none",
                "error_code": "no_real_sync_targets",
                "message": "No real sync targets are enabled.",
                "source_ref": source_ref,
            }
        )
    targets.extend(sync_targets)
    status = _aggregate_status(sync_targets)
    report = {
        "version": "tool_sync_report_v4",
        "phase": "code_review",
        "status": status,
        "mode": "real",
        "targets": targets,
        "checkpoint": {
            "type": "code_review",
            "status": review_status,
            "artifact_ref": _artifact_ref(
                artifact_paths.get("github_code_review_approval", "")
                if review_status == "code_review_approved"
                else artifact_paths.get("github_code_review", "")
            ),
            "artifact_path": (
                artifact_paths.get("github_code_review_approval", "")
                if review_status == "code_review_approved"
                else artifact_paths.get("github_code_review", "")
            ),
        },
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "phase": "code_review",
        "status": status,
        "mode": "real",
        "review_status": review_status,
        "report": store.relative(report_path),
        "targets": [
            {
                "tool": target.get("tool"),
                "status": target.get("status"),
                "operation": target.get("operation", ""),
                "url": target.get("url", ""),
                "external_id": target.get("external_id", ""),
                "error_code": target.get("error_code", ""),
                "message": target.get("message", ""),
            }
            for target in targets
        ],
    }










































































































































































