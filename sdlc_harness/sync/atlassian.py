"""Real sync adapters for Jira and Confluence Cloud.

Adapters in this module push checkpoint artifacts to Atlassian tools and write a
tool sync report under the run directory. Phase 1 writes requirement review
reports; Phase 2 writes design review reports; Phase 3 writes implementation
plan review reports. Niko only reads the report through `get-status`; it never
calls Atlassian APIs directly.
"""

from __future__ import annotations

from base64 import b64encode
from html import escape
import json
from pathlib import Path
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from ..agents.designer.plantuml import extract_class_sequence_diagrams
from ..core.config import Settings
from ..storage.runtime import RuntimeStore, utc_now_iso

JsonRequester = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]

_DESIGN_ARTIFACT_KEYS = {
    "designer_analysis",
    "design_artifact",
    "requirement_graph",
    "class_diagram_graph",
    "sequence_diagram_graph",
    "requirement_design_trace",
    "missing_design_links",
    "design_quality_report",
}
_IMPLEMENTATION_PLAN_ARTIFACT_KEYS = {
    "implementation_plan",
    "implementation_plan_structured",
    "implementation_plan_review",
    "design_artifact",
    "requirement_design_trace",
    "class_diagram_graph",
    "sequence_diagram_graph",
}
_PLANTUML_APP_EXTENSION_KEY = "f0d9b32e-808f-4e58-9330-0346bc051ed9/32a28da1-3e65-4244-a865-3244636e10c6/static/plantuml-confluence"
_PLANTUML_APP_EXTENSION_ID = f"ari:cloud:ecosystem::extension/{_PLANTUML_APP_EXTENSION_KEY}"
_PLANTUML_APP_TITLE = "PlantUML Diagrams"


def real_sync_requirement_checkpoint(
    *,
    store: RuntimeStore,
    settings: Settings,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    request_json: JsonRequester | None = None,
) -> dict[str, object]:
    """Sync requirement checkpoint to Jira/Confluence and write sync report."""

    sync_dir = store.run_dir(run_id) / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    report_path = sync_dir / "tool_sync_report_v1.json"
    requester = request_json or _request_json(settings)

    targets: list[dict[str, object]] = []
    if settings.jira_sync_enabled:
        targets.append(
            _sync_jira_comment(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                previous_comment_id=_previous_target_id(report_path, "jira"),
            )
        )
    if settings.confluence_sync_enabled:
        targets.append(
            _sync_confluence_page(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                previous_page_id=_previous_target_id(report_path, "confluence"),
            )
        )
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
        targets.append(
            _sync_jira_design_comment(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                previous_comment_id=_previous_target_id_from_reports(
                    [report_path],
                    "jira",
                ),
            )
        )
    if settings.confluence_sync_enabled:
        targets.append(
            _sync_confluence_design_page(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                previous_page_id=_previous_target_id_from_reports(
                    [report_path],
                    "confluence",
                ),
            )
        )
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
        confluence_target = _sync_confluence_implementation_plan_page(
            settings,
            requester,
            store,
            run_id,
            source_ref,
            artifact_paths,
            previous_page_id=_previous_target_id_from_reports(
                [report_path],
                "confluence",
            ),
        )
        targets.append(confluence_target)
        if confluence_target.get("status") == "synced":
            confluence_url = str(confluence_target.get("url") or "")
    if settings.jira_sync_enabled:
        targets.append(
            _sync_jira_implementation_plan_comment(
                settings,
                requester,
                store,
                run_id,
                source_ref,
                artifact_paths,
                confluence_url=confluence_url,
                previous_comment_id=_previous_target_id_from_reports(
                    [report_path],
                    "jira",
                ),
            )
        )
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


def _sync_jira_comment(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    previous_comment_id: str | None,
) -> dict[str, object]:
    issue_key = settings.jira_sync_issue_key
    operation = "updated" if previous_comment_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not issue_key:
            raise ValueError("Missing JIRA_SYNC_ISSUE_KEY.")
        payload = {
            "body": _jira_comment_body(
                store=store,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=artifact_paths,
            )
        }
        response, operation = _upsert_jira_comment(
            settings=settings,
            request_json=request_json,
            issue_key=issue_key,
            payload=payload,
            previous_comment_id=previous_comment_id,
        )
        comment_id = str(response.get("id", previous_comment_id or ""))
        return {
            "tool": "jira",
            "mode": "real_api",
            "status": "synced",
            "operation": operation,
            "issue_key": issue_key,
            "external_id": comment_id,
            "url": _jira_comment_url(settings.atlassian_base_url, issue_key, comment_id),
            "source_ref": source_ref,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi sync từng target phải thành report.
        return _failed_target("jira", source_ref, exc, issue_key=issue_key)


def _sync_jira_design_comment(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    previous_comment_id: str | None,
) -> dict[str, object]:
    issue_key = settings.jira_sync_issue_key
    operation = "updated" if previous_comment_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not issue_key:
            raise ValueError("Missing JIRA_SYNC_ISSUE_KEY.")
        payload = {
            "body": _jira_design_comment_body(
                store=store,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=artifact_paths,
            )
        }
        response, operation = _upsert_jira_comment(
            settings=settings,
            request_json=request_json,
            issue_key=issue_key,
            payload=payload,
            previous_comment_id=previous_comment_id,
        )
        comment_id = str(response.get("id", previous_comment_id or ""))
        return {
            "tool": "jira",
            "mode": "real_api",
            "operation": operation,
            "status": "synced",
            "issue_key": issue_key,
            "external_id": comment_id,
            "url": _jira_comment_url(settings.atlassian_base_url, issue_key, comment_id),
            "source_ref": source_ref,
            "artifact_refs": _design_artifact_refs(artifact_paths),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi sync từng target phải thành report.
        return _failed_target(
            "jira",
            source_ref,
            exc,
            issue_key=issue_key,
            operation=operation,
            artifact_refs=_design_artifact_refs(artifact_paths),
            error=_trim_error(str(exc)),
        )


def _sync_jira_implementation_plan_comment(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    confluence_url: str,
    previous_comment_id: str | None,
) -> dict[str, object]:
    issue_key = settings.jira_sync_issue_key
    operation = "updated" if previous_comment_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not issue_key:
            raise ValueError("Missing JIRA_SYNC_ISSUE_KEY.")
        payload = {
            "body": _jira_implementation_plan_comment_body(
                store=store,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=artifact_paths,
                confluence_url=confluence_url,
            )
        }
        response, operation = _upsert_jira_comment(
            settings=settings,
            request_json=request_json,
            issue_key=issue_key,
            payload=payload,
            previous_comment_id=previous_comment_id,
        )
        comment_id = str(response.get("id", previous_comment_id or ""))
        return {
            "tool": "jira",
            "mode": "real_api",
            "operation": operation,
            "status": "synced",
            "issue_key": issue_key,
            "external_id": comment_id,
            "url": _jira_comment_url(settings.atlassian_base_url, issue_key, comment_id),
            "source_ref": source_ref,
            "artifact_refs": _implementation_plan_artifact_refs(artifact_paths),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi sync từng target phải thành report.
        return _failed_target(
            "jira",
            source_ref,
            exc,
            issue_key=issue_key,
            operation=operation,
            artifact_refs=_implementation_plan_artifact_refs(artifact_paths),
            error=_trim_error(str(exc)),
        )


def _upsert_jira_comment(
    *,
    settings: Settings,
    request_json: JsonRequester,
    issue_key: str,
    payload: dict[str, object],
    previous_comment_id: str | None,
) -> tuple[dict[str, object], str]:
    """Update comment cũ, nhưng tự tạo mới nếu comment đã bị xóa trên Jira."""

    comment_url = f"{settings.atlassian_base_url}/rest/api/3/issue/{issue_key}/comment"
    if previous_comment_id:
        try:
            return (
                request_json(
                    "PUT",
                    f"{comment_url}/{previous_comment_id}",
                    payload,
                ),
                "updated",
            )
        except Exception as exc:
            if not _looks_like_missing_jira_comment(exc):
                raise
    return request_json("POST", comment_url, payload), "created"


def _looks_like_missing_jira_comment(exc: Exception) -> bool:
    message = str(exc).lower()
    return "404" in message and "comment" in message


def _sync_confluence_page(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    previous_page_id: str | None,
) -> dict[str, object]:
    try:
        _require_atlassian_settings(settings)
        if not settings.confluence_sync_space_id:
            raise ValueError("Missing CONFLUENCE_SYNC_SPACE_ID.")
        if not settings.confluence_sync_parent_page_id:
            raise ValueError("Missing CONFLUENCE_SYNC_PARENT_PAGE_ID.")

        title = f"Requirement Packet - eTour UC1 - {run_id}"
        body = _confluence_storage_body(store, run_id, source_ref, artifact_paths)
        if previous_page_id:
            current = request_json("GET", f"{settings.atlassian_base_url}/wiki/api/v2/pages/{previous_page_id}", None)
            version = int(current.get("version", {}).get("number", 1)) + 1
            response = request_json(
                "PUT",
                f"{settings.atlassian_base_url}/wiki/api/v2/pages/{previous_page_id}",
                {
                    "id": previous_page_id,
                    "status": "current",
                    "title": title,
                    "body": {"representation": "storage", "value": body},
                    "version": {"number": version, "message": "SDLC Harness Phase 1 re-sync"},
                },
            )
        else:
            response = request_json(
                "POST",
                f"{settings.atlassian_base_url}/wiki/api/v2/pages",
                {
                    "spaceId": settings.confluence_sync_space_id,
                    "status": "current",
                    "title": title,
                    "parentId": settings.confluence_sync_parent_page_id,
                    "body": {"representation": "storage", "value": body},
                },
            )

        page_id = str(response.get("id", previous_page_id or ""))
        return {
            "tool": "confluence",
            "mode": "real_api",
            "status": "synced",
            "external_id": page_id,
            "url": _confluence_page_url(settings.atlassian_base_url, response),
            "source_ref": source_ref,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi sync từng target phải thành report.
        return _failed_target("confluence", source_ref, exc)


def _sync_confluence_design_page(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    previous_page_id: str | None,
) -> dict[str, object]:
    operation = "updated" if previous_page_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not settings.confluence_sync_space_id:
            raise ValueError("Missing CONFLUENCE_SYNC_SPACE_ID.")
        if not settings.confluence_sync_parent_page_id:
            raise ValueError("Missing CONFLUENCE_SYNC_PARENT_PAGE_ID.")

        title = f"Design Checkpoint - eTour UC1 - {run_id}"
        body = _confluence_design_storage_body(store, run_id, source_ref, artifact_paths)
        if previous_page_id:
            current = request_json("GET", f"{settings.atlassian_base_url}/wiki/api/v2/pages/{previous_page_id}", None)
            version = int(current.get("version", {}).get("number", 1)) + 1
            response = request_json(
                "PUT",
                f"{settings.atlassian_base_url}/wiki/api/v2/pages/{previous_page_id}",
                {
                    "id": previous_page_id,
                    "status": "current",
                    "title": title,
                    "body": {"representation": "storage", "value": body},
                    "version": {"number": version, "message": "SDLC Harness Phase 2 design re-sync"},
                },
            )
        else:
            response = request_json(
                "POST",
                f"{settings.atlassian_base_url}/wiki/api/v2/pages",
                {
                    "spaceId": settings.confluence_sync_space_id,
                    "status": "current",
                    "title": title,
                    "parentId": settings.confluence_sync_parent_page_id,
                    "body": {"representation": "storage", "value": body},
                },
            )

        page_id = str(response.get("id", previous_page_id or ""))
        return {
            "tool": "confluence",
            "mode": "real_api",
            "operation": operation,
            "status": "synced",
            "external_id": page_id,
            "url": _confluence_page_url(settings.atlassian_base_url, response),
            "source_ref": source_ref,
            "artifact_refs": _design_artifact_refs(artifact_paths),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi sync từng target phải thành report.
        return _failed_target(
            "confluence",
            source_ref,
            exc,
            operation=operation,
            artifact_refs=_design_artifact_refs(artifact_paths),
            error=_trim_error(str(exc)),
        )


def _sync_confluence_implementation_plan_page(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    previous_page_id: str | None,
) -> dict[str, object]:
    operation = "updated" if previous_page_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not settings.confluence_sync_space_id:
            raise ValueError("Missing CONFLUENCE_SYNC_SPACE_ID.")
        if not settings.confluence_sync_parent_page_id:
            raise ValueError("Missing CONFLUENCE_SYNC_PARENT_PAGE_ID.")

        title = f"Implementation Plan Checkpoint - eTour UC1 - {run_id}"
        body = _confluence_implementation_plan_storage_body(store, run_id, source_ref, artifact_paths)
        if previous_page_id:
            current = request_json("GET", f"{settings.atlassian_base_url}/wiki/api/v2/pages/{previous_page_id}", None)
            version = int(current.get("version", {}).get("number", 1)) + 1
            response = request_json(
                "PUT",
                f"{settings.atlassian_base_url}/wiki/api/v2/pages/{previous_page_id}",
                {
                    "id": previous_page_id,
                    "status": "current",
                    "title": title,
                    "body": {"representation": "storage", "value": body},
                    "version": {"number": version, "message": "SDLC Harness Phase 3 implementation plan re-sync"},
                },
            )
        else:
            response = request_json(
                "POST",
                f"{settings.atlassian_base_url}/wiki/api/v2/pages",
                {
                    "spaceId": settings.confluence_sync_space_id,
                    "status": "current",
                    "title": title,
                    "parentId": settings.confluence_sync_parent_page_id,
                    "body": {"representation": "storage", "value": body},
                },
            )

        page_id = str(response.get("id", previous_page_id or ""))
        return {
            "tool": "confluence",
            "mode": "real_api",
            "operation": operation,
            "status": "synced",
            "external_id": page_id,
            "url": _confluence_page_url(settings.atlassian_base_url, response),
            "source_ref": source_ref,
            "artifact_refs": _implementation_plan_artifact_refs(artifact_paths),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi sync từng target phải thành report.
        return _failed_target(
            "confluence",
            source_ref,
            exc,
            operation=operation,
            artifact_refs=_implementation_plan_artifact_refs(artifact_paths),
            error=_trim_error(str(exc)),
        )


def _request_json(settings: Settings) -> JsonRequester:
    def request_json(method: str, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        _require_atlassian_settings(settings)
        raw_payload = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(url, data=raw_payload, method=method.upper())
        request.add_header("Authorization", _auth_header(settings))
        request.add_header("Accept", "application/json")
        if raw_payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - URL đến từ config local của anh.
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Atlassian API HTTP {exc.code}: {_trim_error(detail)}") from exc
        except URLError as exc:
            raise RuntimeError(f"Atlassian API connection failed: {_trim_error(str(exc.reason))}") from exc
        return json.loads(body) if body else {}

    return request_json


def _require_atlassian_settings(settings: Settings) -> None:
    missing = [
        name
        for name, value in {
            "ATLASSIAN_BASE_URL": settings.atlassian_base_url,
            "ATLASSIAN_EMAIL": settings.atlassian_email,
            "ATLASSIAN_API_TOKEN": settings.atlassian_api_token,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"Missing Atlassian config: {', '.join(missing)}.")


def _auth_header(settings: Settings) -> str:
    raw = f"{settings.atlassian_email}:{settings.atlassian_api_token}".encode("utf-8")
    return "Basic " + b64encode(raw).decode("ascii")


def _jira_comment_body(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    requirement_text = _read_requirement_packet(store, artifact_paths)
    goal = _section_preview(requirement_text, "Goal", default="See requirement packet.")
    alternative_flows = _section_items(requirement_text, "Alternative / Exception Flows")
    acceptance_criteria = _section_items(requirement_text, "Acceptance Criteria")
    open_questions = _section_items(requirement_text, "Open Questions")
    artifact_items = [f"{_artifact_label(name)}: {path}" for name, path in artifact_paths.items()]

    return {
        "type": "doc",
        "version": 1,
        "content": [
            _adf_heading("SDLC Harness checkpoint: Requirement review", level=2),
            _adf_paragraph("Requirement packet has been normalized and is waiting for human approval."),
            _adf_bullet_list(
                [
                    f"Run: {run_id}",
                    f"Source: {source_ref}",
                    "Status: waiting_requirement_approval",
                    "Next action: approve_checkpoint, request_change, cancel_run, or get_status",
                ]
            ),
            _adf_heading("Requirement summary", level=3),
            _adf_bullet_list(
                [
                    f"Goal: {goal}",
                    f"Alternative / exception flows: {len(alternative_flows)} item(s)",
                    f"Acceptance criteria: {len(acceptance_criteria)} item(s)",
                    f"Open questions: {len(open_questions)} item(s)",
                    "Full packet is available in the synced Confluence page and local artifacts.",
                ]
            ),
            _adf_heading("Artifacts", level=3),
            _adf_bullet_list(artifact_items),
        ],
    }


def _jira_design_comment_body(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> dict[str, object]:
    design_text = _read_artifact(store, artifact_paths, "design_artifact")
    quality_text = _read_artifact(store, artifact_paths, "design_quality_report")
    class_diagram, sequence_diagrams = _extract_design_diagrams(design_text)
    missing_summary = _section_preview(quality_text, "Result", default="See design quality report.")
    artifact_items = [
        f"{_artifact_label(name)}: {path}"
        for name, path in artifact_paths.items()
        if name in _DESIGN_ARTIFACT_KEYS or name in {"requirement_packet", "formal_use_case"}
    ]
    diagram_items = [
        f"Class diagram: {'included' if class_diagram else 'missing'}",
        f"Sequence diagrams: {len(sequence_diagrams)} item(s)",
        "Confluence page contains dedicated diagram sections for review.",
    ]
    diagram_blocks = _adf_design_diagram_blocks(class_diagram, sequence_diagrams)

    return {
        "type": "doc",
        "version": 1,
        "content": [
            _adf_heading("SDLC Harness checkpoint: Design review", level=2),
            _adf_paragraph("Design artifacts passed traceability validation and are waiting for human approval."),
            _adf_bullet_list(
                [
                    f"Run: {run_id}",
                    f"Source: {source_ref}",
                    "Status: waiting_design_approval",
                    "Next action: approve_checkpoint, request_change, cancel_run, or get_status",
                ]
            ),
            _adf_heading("Traceability summary", level=3),
            _adf_bullet_list(
                [
                    missing_summary,
                    f"Design artifact: {_artifact_ref(artifact_paths.get('design_artifact', ''))}",
                    f"Trace report: {_artifact_ref(artifact_paths.get('requirement_design_trace', ''))}",
                    f"Quality report: {_artifact_ref(artifact_paths.get('design_quality_report', ''))}",
                ]
            ),
            _adf_heading("Diagrams", level=3),
            _adf_bullet_list(diagram_items),
            *diagram_blocks,
            _adf_heading("Artifacts", level=3),
            _adf_bullet_list(artifact_items),
        ],
    }


def _jira_implementation_plan_comment_body(
    *,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    confluence_url: str,
) -> dict[str, object]:
    structured_plan = _read_json_artifact(store, artifact_paths, "implementation_plan_structured")
    review_report = _read_json_artifact(store, artifact_paths, "implementation_plan_review")
    repo = structured_plan.get("target_repository", {})
    repo = repo if isinstance(repo, dict) else {}
    mappings = structured_plan.get("design_mappings", [])
    mapping_count = len(mappings) if isinstance(mappings, list) else 0
    review_ok = bool(review_report.get("ok"))
    issue_count = int(review_report.get("issue_count") or 0)
    artifact_items = [
        f"{_artifact_label(name)}: {path}"
        for name, path in artifact_paths.items()
        if name in _IMPLEMENTATION_PLAN_ARTIFACT_KEYS
    ]
    summary_items = [
        f"Run: {run_id}",
        f"Source: {source_ref}",
        "Status: waiting_implementation_plan_review",
        f"Planned branch: {repo.get('planned_branch', '')}",
        f"Design-to-code mappings: {mapping_count}",
        f"Validator.review_plan: {'passed' if review_ok else 'failed'} ({issue_count} issue(s))",
        "Next action: approve_checkpoint, request_change, cancel_run, or get_status",
    ]
    if confluence_url:
        summary_items.append(f"Confluence review page: {confluence_url}")

    return {
        "type": "doc",
        "version": 1,
        "content": [
            _adf_heading("SDLC Harness checkpoint: Implementation plan review", level=2),
            _adf_paragraph("Implementation plan passed Validator.review_plan and is waiting for human approval."),
            _adf_bullet_list(summary_items),
            _adf_heading("Artifacts", level=3),
            _adf_bullet_list(artifact_items),
        ],
    }


def _confluence_storage_body(
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> str:
    requirement_text = _read_requirement_packet(store, artifact_paths)
    artifact_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(_artifact_label(name))}</strong></td>"
        f"<td><code>{escape(path)}</code></td>"
        "</tr>"
        for name, path in artifact_paths.items()
    )
    goal = _section_preview(requirement_text, "Goal", default="See requirement packet.")
    alternative_flows = _section_items(requirement_text, "Alternative / Exception Flows")
    acceptance_criteria = _section_items(requirement_text, "Acceptance Criteria")
    open_questions = _section_items(requirement_text, "Open Questions")
    alternative_lines = "\n".join(f"<li>{escape(item)}</li>" for item in alternative_flows) or "<li>None recorded.</li>"
    acceptance_lines = "\n".join(f"<li>{escape(item)}</li>" for item in acceptance_criteria) or "<li>None recorded.</li>"
    open_question_lines = "\n".join(f"<li>{escape(item)}</li>" for item in open_questions) or "<li>None recorded.</li>"
    requirement_body = _markdown_to_confluence_storage(requirement_text)

    return f"""
<h1>Requirement Checkpoint - eTour UC1</h1>
<p>This page is generated by SDLC_Harness Phase 1 so the requirement packet can be reviewed before design work starts.</p>
<table>
<tbody>
<tr><th>Run</th><td><code>{escape(run_id)}</code></td></tr>
<tr><th>Source</th><td><code>{escape(source_ref)}</code></td></tr>
<tr><th>Status</th><td><strong>waiting_requirement_approval</strong></td></tr>
<tr><th>Next action</th><td>Approve, request changes, cancel the run, or ask Niko for status.</td></tr>
</tbody>
</table>
<h2>Review Summary</h2>
<ul>
<li><strong>Goal:</strong> {escape(goal)}</li>
<li><strong>Alternative / exception flows:</strong> {len(alternative_flows)} item(s)</li>
<li><strong>Acceptance criteria:</strong> {len(acceptance_criteria)} item(s)</li>
<li><strong>Open questions:</strong> {len(open_questions)} item(s)</li>
</ul>
<h3>Alternative / Exception Flows</h3>
<ul>{alternative_lines}</ul>
<h3>Acceptance Criteria</h3>
<ul>{acceptance_lines}</ul>
<h3>Open Questions</h3>
<ul>{open_question_lines}</ul>
<h2>Artifacts</h2>
<table>
<tbody>
<tr><th>Artifact</th><th>Local path</th></tr>
{artifact_rows}
</tbody>
</table>
<h2>Requirement Packet</h2>
{requirement_body}
""".strip()


def _confluence_design_storage_body(
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> str:
    design_text = _read_artifact(store, artifact_paths, "design_artifact")
    quality_text = _read_artifact(store, artifact_paths, "design_quality_report")
    analysis_text = _read_artifact(store, artifact_paths, "designer_analysis")
    class_diagram, sequence_diagrams = _extract_design_diagrams(design_text)
    artifact_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(_artifact_label(name))}</strong></td>"
        f"<td><code>{escape(path)}</code></td>"
        "</tr>"
        for name, path in artifact_paths.items()
        if name in _DESIGN_ARTIFACT_KEYS or name in {"requirement_packet", "formal_use_case"}
    )

    return f"""
<h1>Design Checkpoint - eTour UC1</h1>
<p>This page is generated by SDLC_Harness Phase 2 after the requirement checkpoint has been approved.</p>
<table>
<tbody>
<tr><th>Run</th><td><code>{escape(run_id)}</code></td></tr>
<tr><th>Source</th><td><code>{escape(source_ref)}</code></td></tr>
<tr><th>Status</th><td><strong>waiting_design_approval</strong></td></tr>
<tr><th>Next action</th><td>Review design, approve, request changes, cancel the run, or ask Niko for status.</td></tr>
</tbody>
</table>
<h2>Artifacts</h2>
<table>
<tbody>
<tr><th>Artifact</th><th>Local path</th></tr>
{artifact_rows}
</tbody>
</table>
<h2>Designer Analysis</h2>
{_markdown_to_confluence_storage(analysis_text)}
<h2>Design Diagrams</h2>
{_confluence_design_diagram_sections(
    class_diagram,
    sequence_diagrams,
    fallback_markdown=design_text,
)}
<h2>Quality Report</h2>
{_markdown_to_confluence_storage(quality_text)}
""".strip()


def _confluence_implementation_plan_storage_body(
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
) -> str:
    implementation_plan = _read_artifact(store, artifact_paths, "implementation_plan")
    structured_plan = _read_json_artifact(store, artifact_paths, "implementation_plan_structured")
    review_report = _read_json_artifact(store, artifact_paths, "implementation_plan_review")
    repo = structured_plan.get("target_repository", {})
    repo = repo if isinstance(repo, dict) else {}
    mappings = _dict_list(structured_plan.get("design_mappings"))
    test_intents = _dict_list(structured_plan.get("test_intents"))
    assumptions = _string_list(structured_plan.get("assumptions"))
    open_questions = _string_list(structured_plan.get("open_questions"))
    out_of_scope = _string_list(structured_plan.get("out_of_scope"))
    mapping_count = len(mappings)
    mapping_counts = _implementation_mapping_counts(mappings)
    target_groups = _implementation_target_groups(mappings)
    unique_commands = _unique_values(str(item.get("command") or "") for item in test_intents)
    review_ok = bool(review_report.get("ok"))
    issue_count = int(review_report.get("issue_count") or 0)
    artifact_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(_artifact_label(name))}</strong></td>"
        f"<td><code>{escape(path)}</code></td>"
        "</tr>"
        for name, path in artifact_paths.items()
        if name in _IMPLEMENTATION_PLAN_ARTIFACT_KEYS
    )
    review_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(label)}</strong></td>"
        f"<td>{escape(value)}</td>"
        "</tr>"
        for label, value in (
            ("Validator ok", str(review_ok).lower()),
            ("Issue count", str(issue_count)),
            ("Coverage issues", str(len(review_report.get("coverage_issues", []) or []))),
            ("Traceability issues", str(len(review_report.get("traceability_issues", []) or []))),
            ("Executability issues", str(len(review_report.get("executability_issues", []) or []))),
            ("Scope issues", str(len(review_report.get("scope_issues", []) or []))),
            ("Implementation quality issues", str(len(review_report.get("implementation_quality_issues", []) or []))),
            ("Test issues", str(len(review_report.get("test_issues", []) or []))),
            ("Repair iterations", str(review_report.get("repair_iterations", 0))),
            ("LLM summary", str(review_report.get("summary") or "Not recorded.")),
        )
    )
    execution_rows = _confluence_implementation_execution_rows(
        repo=repo,
        target_count=len(target_groups),
        mapping_counts=mapping_counts,
        test_commands=unique_commands,
    )
    target_rows = _confluence_implementation_target_rows(target_groups)
    test_rows = _confluence_implementation_test_rows(test_intents)

    return f"""
<h1>Implementation Plan Checkpoint - eTour UC1</h1>
<p>This page is generated by SDLC_Harness Phase 3 after the design checkpoint has been approved. It is the human review surface before Phase 4 is allowed to create a branch, change code, run tests, and prepare a PR.</p>
<h2>Decision Needed</h2>
<table>
<tbody>
<tr><th>Run</th><td><code>{escape(run_id)}</code></td></tr>
<tr><th>Source</th><td><code>{escape(source_ref)}</code></td></tr>
<tr><th>Status</th><td><strong>waiting_implementation_plan_review</strong></td></tr>
<tr><th>Target repo</th><td><code>{escape(str(repo.get("local_path", "")))}</code></td></tr>
<tr><th>Base branch</th><td><code>{escape(str(repo.get("base_branch", "")))}</code></td></tr>
<tr><th>Planned branch</th><td><code>{escape(str(repo.get("planned_branch", "")))}</code></td></tr>
<tr><th>Design-to-code mappings</th><td>{mapping_count}</td></tr>
<tr><th>Review focus</th><td>Check whether the target files, implementation steps, and test plan are acceptable before code generation starts.</td></tr>
<tr><th>Next action</th><td>Approve this implementation plan, request changes, cancel the run, or ask Niko for status.</td></tr>
</tbody>
</table>
<h2>Implementation Summary</h2>
<table>
<tbody>
<tr><th>Code targets</th><td>{len(target_groups)} file(s) or method target(s) grouped for implementation.</td></tr>
<tr><th>Class mappings</th><td>{mapping_counts.get("class", 0)}</td></tr>
<tr><th>Method mappings</th><td>{mapping_counts.get("method", 0)}</td></tr>
<tr><th>Sequence-flow mappings</th><td>{mapping_counts.get("sequence_message", 0)}</td></tr>
<tr><th>Test commands</th><td>{escape(", ".join(unique_commands) if unique_commands else "Not specified.")}</td></tr>
</tbody>
</table>
<h2>Execution Steps</h2>
<table>
<tbody>
<tr><th>Step</th><th>What Phase 4 will do</th><th>Control / evidence</th></tr>
{execution_rows}
</tbody>
</table>
<h2>Code Targets</h2>
<p>Grouped from the approved design graph so the reviewer can see exactly where code will be created or updated.</p>
<table>
<tbody>
<tr><th>Code target</th><th>Planned work</th><th>Mapped design evidence</th><th>Test focus</th></tr>
{target_rows}
</tbody>
</table>
<h2>Test Plan</h2>
<table>
<tbody>
<tr><th>Scope</th><th>Intent</th><th>Command</th></tr>
{test_rows}
</tbody>
</table>
<h2>Validator Review Summary</h2>
<table>
<tbody>
{review_rows}
</tbody>
</table>
<h2>Assumptions / Open Questions</h2>
<h3>Assumptions</h3>
{_confluence_unordered_list(assumptions, empty="No assumptions recorded.")}
<h3>Open Questions</h3>
{_confluence_unordered_list(open_questions, empty="No open questions recorded.")}
<h3>Out Of Scope</h3>
{_confluence_unordered_list(out_of_scope, empty="No out-of-scope items recorded.")}
<h2>Artifacts</h2>
<table>
<tbody>
<tr><th>Artifact</th><th>Local path</th></tr>
{artifact_rows}
</tbody>
</table>
<h2>Raw Markdown Artifact</h2>
<p>The generated markdown is kept below for audit. The review sections above are rendered from the structured plan.</p>
{_confluence_code_macro(implementation_plan, language="markdown")}
""".strip()


def _implementation_mapping_counts(mappings: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {"class": 0, "method": 0, "sequence_message": 0}
    for item in mappings:
        kind = str(item.get("design_element_type") or "other")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _implementation_target_groups(mappings: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for item in mappings:
        target = str(item.get("planned_code_target") or "").strip()
        if not target:
            target = "Unspecified target"
        code_file = target.split("#", 1)[0]
        group = groups.setdefault(
            code_file,
            {
                "target": code_file,
                "types": set(),
                "elements": [],
                "test_intents": [],
            },
        )
        group["types"].add(str(item.get("design_element_type") or "other"))
        element_name = str(item.get("design_element_name") or item.get("design_element_id") or "").strip()
        if element_name:
            group["elements"].append(element_name)
        test_intent = str(item.get("test_intent") or "").strip()
        if test_intent:
            group["test_intents"].append(test_intent)
    return [groups[key] for key in sorted(groups)]


def _confluence_implementation_execution_rows(
    *,
    repo: dict[str, object],
    target_count: int,
    mapping_counts: dict[str, int],
    test_commands: list[str],
) -> str:
    commands = ", ".join(test_commands) if test_commands else "the configured target repo test command"
    rows = [
        (
            "1",
            f"Create branch {repo.get('planned_branch', '')} from {repo.get('base_branch', '')}.",
            "Target repo guard already checked the local repo path, remote, branch, and dirty worktree.",
        ),
        (
            "2",
            f"Create or update {target_count} Java code target(s) mapped from the approved class diagram.",
            f"{mapping_counts.get('class', 0)} class mapping(s) and {mapping_counts.get('method', 0)} method mapping(s).",
        ),
        (
            "3",
            "Wire the primary delete-cultural-heritage flow and its exception paths.",
            f"{mapping_counts.get('sequence_message', 0)} sequence message mapping(s) from the approved sequence diagram.",
        ),
        (
            "4",
            "Add or update tests, then run the configured command.",
            commands,
        ),
    ]
    return "\n".join(
        "<tr>"
        f"<td>{escape(step)}</td>"
        f"<td>{escape(work)}</td>"
        f"<td>{escape(control)}</td>"
        "</tr>"
        for step, work, control in rows
    )


def _confluence_implementation_target_rows(target_groups: list[dict[str, object]]) -> str:
    if not target_groups:
        return '<tr><td colspan="4">No code target mappings were generated.</td></tr>'
    rows: list[str] = []
    for group in target_groups:
        kinds = sorted(str(kind) for kind in group.get("types", set()))
        elements = _first_values([str(item) for item in group.get("elements", [])], limit=5)
        test_intents = _first_values([str(item) for item in group.get("test_intents", [])], limit=2)
        planned_work = _planned_work_label(kinds)
        rows.append(
            "<tr>"
            f"<td><code>{escape(str(group.get('target') or ''))}</code></td>"
            f"<td>{escape(planned_work)}</td>"
            f"<td>{escape('; '.join(elements))}</td>"
            f"<td>{escape('; '.join(test_intents))}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def _confluence_implementation_test_rows(test_intents: list[dict[str, object]]) -> str:
    if not test_intents:
        return '<tr><td colspan="3">No test intent was generated.</td></tr>'
    return "\n".join(
        "<tr>"
        f"<td>{escape(str(item.get('scope') or ''))}</td>"
        f"<td>{escape(str(item.get('intent') or ''))}</td>"
        f"<td><code>{escape(str(item.get('command') or ''))}</code></td>"
        "</tr>"
        for item in test_intents
    )


def _planned_work_label(kinds: list[str]) -> str:
    if "sequence_message" in kinds and ("class" in kinds or "method" in kinds):
        return "Implement class/method behavior and wire flow interactions."
    if "sequence_message" in kinds:
        return "Wire or verify sequence-flow interaction."
    if "method" in kinds:
        return "Implement or update methods from the class diagram."
    if "class" in kinds:
        return "Create or update class structure from the class diagram."
    return "Review and implement mapped design element."


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique_values(values: object) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _first_values(values: list[str], *, limit: int) -> list[str]:
    clean = _unique_values(values)
    if len(clean) <= limit:
        return clean
    return [*clean[:limit], f"+{len(clean) - limit} more"]


def _confluence_unordered_list(items: list[str], *, empty: str) -> str:
    if not items:
        return f"<p>{escape(empty)}</p>"
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def _read_requirement_packet(store: RuntimeStore, artifact_paths: dict[str, str]) -> str:
    return _read_artifact(store, artifact_paths, "requirement_packet")


def _read_artifact(store: RuntimeStore, artifact_paths: dict[str, str], key: str) -> str:
    path = artifact_paths.get(key, "")
    if not path:
        return ""
    return (store.repo_root / path).read_text(encoding="utf-8")


def _read_json_artifact(store: RuntimeStore, artifact_paths: dict[str, str], key: str) -> dict[str, object]:
    raw = _read_artifact(store, artifact_paths, key)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _section_preview(markdown: str, heading: str, *, default: str) -> str:
    items = _section_items(markdown, heading)
    if not items:
        return default
    return items[0]


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
        items.append(re.sub(r"^\d+\.\s+|^-\s+", "", line).strip())
    return items


def _markdown_to_confluence_storage(markdown: str) -> str:
    html: list[str] = []
    list_stack: list[str] = []

    def close_lists() -> None:
        while list_stack:
            html.append(f"</{list_stack.pop()}>")

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            close_lists()
            continue
        if line == "---":
            close_lists()
            html.append("<hr />")
            continue
        if line.startswith("#"):
            close_lists()
            level = min(len(line) - len(line.lstrip("#")), 6)
            text = line[level:].strip()
            html.append(f"<h{level}>{escape(text)}</h{level}>")
            continue
        ordered_match = re.match(r"^\d+\.\s+(.*)$", line)
        unordered_match = re.match(r"^-\s+(.*)$", line)
        if ordered_match or unordered_match:
            tag = "ol" if ordered_match else "ul"
            if not list_stack or list_stack[-1] != tag:
                close_lists()
                list_stack.append(tag)
                html.append(f"<{tag}>")
            text = ordered_match.group(1) if ordered_match else unordered_match.group(1)
            html.append(f"<li>{escape(text)}</li>")
            continue
        close_lists()
        html.append(f"<p>{escape(line)}</p>")

    close_lists()
    return "\n".join(html)


def _extract_design_diagrams(design_text: str) -> tuple[str, list[str]]:
    """Tách PlantUML class/sequence từ design artifact để sync lên tool dễ đọc."""

    class_diagram, sequence_diagrams = extract_class_sequence_diagrams(design_text)
    return class_diagram.strip(), [diagram.strip() for diagram in sequence_diagrams if diagram.strip()]


def _adf_design_diagram_blocks(class_diagram: str, sequence_diagrams: list[str]) -> list[dict[str, object]]:
    """Tạo block diagram trong Jira comment mà không dump lẫn vào artifact text."""

    blocks: list[dict[str, object]] = []
    if class_diagram:
        blocks.append(_adf_paragraph("Class Diagram PlantUML:"))
        blocks.append(_adf_code_block(class_diagram, language="plantuml"))
    for index, sequence_diagram in enumerate(sequence_diagrams, start=1):
        blocks.append(_adf_paragraph(f"Sequence Diagram {index} PlantUML:"))
        blocks.append(_adf_code_block(sequence_diagram, language="plantuml"))
    return blocks


def _confluence_design_diagram_sections(
    class_diagram: str,
    sequence_diagrams: list[str],
    *,
    fallback_markdown: str,
) -> str:
    """Render diagram bằng PlantUML app macro, giữ code source làm fallback."""

    sections: list[str] = []
    if class_diagram:
        sections.append("<h3>Class Diagram</h3>")
        sections.append(_confluence_plantuml_app_macro(class_diagram))
        sections.append("<h4>Fallback PlantUML Source</h4>")
        sections.append(_confluence_code_macro(class_diagram, language="plantuml"))
    for index, sequence_diagram in enumerate(sequence_diagrams, start=1):
        title = "Sequence Diagram" if len(sequence_diagrams) == 1 else f"Sequence Diagram {index}"
        sections.append(f"<h3>{escape(title)}</h3>")
        sections.append(_confluence_plantuml_app_macro(sequence_diagram))
        sections.append("<h4>Fallback PlantUML Source</h4>")
        sections.append(_confluence_code_macro(sequence_diagram, language="plantuml"))
    if sections:
        return "\n".join(sections)
    return _markdown_to_confluence_storage(fallback_markdown)


def _confluence_plantuml_app_macro(diagram_code: str) -> str:
    """Tạo Forge PlantUML macro từ storage format app PlantUML Confluence Cloud."""

    local_id = str(uuid4())
    safe_code = escape(diagram_code)
    node = (
        '<ac:adf-node type="extension">'
        f'<ac:adf-attribute key="extension-key">{_PLANTUML_APP_EXTENSION_KEY}</ac:adf-attribute>'
        '<ac:adf-attribute key="extension-type">com.atlassian.ecosystem</ac:adf-attribute>'
        '<ac:adf-attribute key="parameters">'
        f'<ac:adf-parameter key="local-id">{local_id}</ac:adf-parameter>'
        f'<ac:adf-parameter key="extension-id">{_PLANTUML_APP_EXTENSION_ID}</ac:adf-parameter>'
        f'<ac:adf-parameter key="extension-title">{_PLANTUML_APP_TITLE}</ac:adf-parameter>'
        '<ac:adf-parameter key="layout">extension</ac:adf-parameter>'
        '<ac:adf-parameter key="forge-environment">PRODUCTION</ac:adf-parameter>'
        '<ac:adf-parameter key="guest-params">'
        f'<ac:adf-parameter key="diagram-code">{safe_code}</ac:adf-parameter>'
        '</ac:adf-parameter>'
        '</ac:adf-attribute>'
        f'<ac:adf-attribute key="text">{_PLANTUML_APP_TITLE}</ac:adf-attribute>'
        '<ac:adf-attribute key="layout">default</ac:adf-attribute>'
        f'<ac:adf-attribute key="local-id">{local_id}</ac:adf-attribute>'
        '</ac:adf-node>'
    )
    return f"<ac:adf-extension>{node}<ac:adf-fallback>{node}</ac:adf-fallback></ac:adf-extension>"


def _confluence_code_macro(code: str, *, language: str) -> str:
    """Tạo Confluence code macro để diagram không bị trải thành nhiều paragraph."""

    safe_language = escape(language)
    return (
        f'<ac:structured-macro ac:name="code">'
        f'<ac:parameter ac:name="language">{safe_language}</ac:parameter>'
        f"<ac:plain-text-body><![CDATA[{_safe_cdata(code)}]]></ac:plain-text-body>"
        f"</ac:structured-macro>"
    )


def _safe_cdata(text: str) -> str:
    return text.replace("]]>", "]]]]><![CDATA[>")


def _adf_heading(text: str, *, level: int) -> dict[str, object]:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _adf_paragraph(text: str) -> dict[str, object]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _adf_code_block(text: str, *, language: str) -> dict[str, object]:
    return {
        "type": "codeBlock",
        "attrs": {"language": language},
        "content": [{"type": "text", "text": text}],
    }


def _adf_bullet_list(items: list[str]) -> dict[str, object]:
    return {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [_adf_paragraph(item)]}
            for item in items
        ],
    }


def _artifact_label(name: str) -> str:
    return name.replace("_", " ").title()


def _artifact_ref(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[0]


def _design_artifact_refs(artifact_paths: dict[str, str]) -> list[str]:
    return [
        _artifact_ref(path)
        for key, path in artifact_paths.items()
        if key in _DESIGN_ARTIFACT_KEYS and path
    ]


def _implementation_plan_artifact_refs(artifact_paths: dict[str, str]) -> list[str]:
    return [
        _artifact_ref(path)
        for key, path in artifact_paths.items()
        if key in _IMPLEMENTATION_PLAN_ARTIFACT_KEYS and path
    ]


def _previous_target_id_from_reports(report_paths: list[Path], tool: str) -> str | None:
    for report_path in report_paths:
        external_id = _previous_target_id(report_path, tool)
        if external_id:
            return external_id
    return None


def _previous_target_id(report_path: Path, tool: str) -> str | None:
    if not report_path.exists():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    for target in report.get("targets", []):
        if target.get("tool") == tool and target.get("status") == "synced":
            external_id = str(target.get("external_id", "")).strip()
            return external_id or None
    return None


def _aggregate_status(targets: list[dict[str, object]]) -> str:
    synced = [target for target in targets if target.get("status") == "synced"]
    if len(synced) == len(targets):
        return "synced"
    if synced:
        return "partial_failed"
    return "failed"


def _failed_target(tool: str, source_ref: str, exc: Exception, **extra: object) -> dict[str, object]:
    return {
        "tool": tool,
        "mode": "real_api",
        "status": "failed",
        "error_code": "sync_failed",
        "message": _trim_error(str(exc)),
        "source_ref": source_ref,
        **extra,
    }


def _jira_comment_url(base_url: str, issue_key: str, comment_id: str) -> str:
    if not comment_id:
        return f"{base_url}/browse/{issue_key}"
    return f"{base_url}/browse/{issue_key}?focusedCommentId={comment_id}"


def _confluence_page_url(base_url: str, response: dict[str, Any]) -> str:
    links = response.get("_links", {})
    webui = str(links.get("webui", ""))
    if webui.startswith("http"):
        return webui
    if webui.startswith("/wiki"):
        return base_url + webui
    if webui:
        return base_url + "/wiki" + webui
    page_id = str(response.get("id", ""))
    return f"{base_url}/wiki/pages/{page_id}"


def _trim_error(message: str) -> str:
    return " ".join(message.split())[:500]
