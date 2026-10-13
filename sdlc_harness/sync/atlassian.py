"""Real sync adapters for Jira and Confluence Cloud.

Adapters in this module push checkpoint artifacts to Atlassian tools and write a
tool sync report under the run directory. Phase 1 writes requirement review
reports; Phase 2 writes design review reports. Niko only reads the report
through `get-status`; it never calls Atlassian APIs directly.
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
    phase1_report_path = sync_dir / "tool_sync_report_v1.json"
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
                    [report_path, phase1_report_path],
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
                    [report_path, phase1_report_path],
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
        if previous_comment_id:
            response = request_json(
                "PUT",
                f"{settings.atlassian_base_url}/rest/api/3/issue/{issue_key}/comment/{previous_comment_id}",
                payload,
            )
        else:
            response = request_json(
                "POST",
                f"{settings.atlassian_base_url}/rest/api/3/issue/{issue_key}/comment",
                payload,
            )
        comment_id = str(response.get("id", previous_comment_id or ""))
        return {
            "tool": "jira",
            "mode": "real_api",
            "status": "synced",
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
        if previous_comment_id:
            response = request_json(
                "PUT",
                f"{settings.atlassian_base_url}/rest/api/3/issue/{issue_key}/comment/{previous_comment_id}",
                payload,
            )
        else:
            response = request_json(
                "POST",
                f"{settings.atlassian_base_url}/rest/api/3/issue/{issue_key}/comment",
                payload,
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


def _read_requirement_packet(store: RuntimeStore, artifact_paths: dict[str, str]) -> str:
    return _read_artifact(store, artifact_paths, "requirement_packet")


def _read_artifact(store: RuntimeStore, artifact_paths: dict[str, str], key: str) -> str:
    path = artifact_paths.get(key, "")
    if not path:
        return ""
    return (store.repo_root / path).read_text(encoding="utf-8")


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
