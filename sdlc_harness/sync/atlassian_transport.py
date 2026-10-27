"""Atlassian HTTP, upsert, and remote checkpoint recovery."""

from __future__ import annotations

from base64 import b64encode
import json
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..core.config import Settings
from ..core.errors import HarnessError
from ..storage.runtime import RuntimeStore
from .atlassian_rendering import (
    _checkpoint_marker,
    _checkpoint_title,
    _code_review_artifact_refs,
    _confluence_code_review_storage_body,
    _confluence_design_storage_body,
    _confluence_implementation_plan_storage_body,
    _confluence_storage_body,
    _design_artifact_refs,
    _implementation_plan_artifact_refs,
    _jira_code_review_comment_body,
    _jira_comment_body,
    _jira_design_comment_body,
    _jira_implementation_plan_comment_body,
)

_LEGACY_CONFLUENCE_PREFIXES = {
    "requirement": "Requirement Packet - eTour UC1 - ",
    "design": "Design Checkpoint - eTour UC1 - ",
    "implementation_plan": "Implementation Plan Checkpoint - eTour UC1 - ",
    "code_review": "Code Review Checkpoint - eTour UC1 - ",
}
_LEGACY_JIRA_HEADINGS = {
    "requirement": "SDLC Harness checkpoint: Requirement review",
    "design": "SDLC Harness checkpoint: Design review",
    "implementation_plan": "SDLC Harness checkpoint: Implementation plan review",
    "code_review": "SDLC Harness checkpoint: Code review",
}

JsonRequester = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]

def _sync_jira_comment(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    checkpoint_context: dict[str, object] | None = None,
    previous_comment_id: str | None,
) -> dict[str, object]:
    issue_key = _jira_issue_key(settings, source_ref)
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
                checkpoint_context=checkpoint_context,
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
    issue_key = _jira_issue_key(settings, source_ref)
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
    issue_key = _jira_issue_key(settings, source_ref)
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

def _sync_jira_code_review_comment(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    confluence_url: str,
    review_status: str,
    previous_comment_id: str | None,
) -> dict[str, object]:
    issue_key = _jira_issue_key(settings, source_ref)
    operation = "updated" if previous_comment_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not issue_key:
            raise ValueError("Missing JIRA_SYNC_ISSUE_KEY.")
        payload = {
            "body": _jira_code_review_comment_body(
                store=store,
                run_id=run_id,
                source_ref=source_ref,
                artifact_paths=artifact_paths,
                confluence_url=confluence_url,
                review_status=review_status,
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
            "artifact_refs": _code_review_artifact_refs(artifact_paths),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi từng target phải thành report.
        return _failed_target(
            "jira",
            source_ref,
            exc,
            issue_key=issue_key,
            operation=operation,
            artifact_refs=_code_review_artifact_refs(artifact_paths),
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
    checkpoint_context: dict[str, object] | None = None,
    previous_page_id: str | None,
) -> dict[str, object]:
    try:
        _require_atlassian_settings(settings)
        if not settings.confluence_sync_space_id:
            raise ValueError("Missing CONFLUENCE_SYNC_SPACE_ID.")
        if not settings.confluence_sync_parent_page_id:
            raise ValueError("Missing CONFLUENCE_SYNC_PARENT_PAGE_ID.")

        title = _checkpoint_title(source_ref, "requirement")
        body = _confluence_storage_body(
            store,
            run_id,
            source_ref,
            artifact_paths,
            checkpoint_context=checkpoint_context,
        )
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

        title = _checkpoint_title(source_ref, "design")
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

        title = _checkpoint_title(source_ref, "implementation_plan")
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

def _sync_confluence_code_review_page(
    settings: Settings,
    request_json: JsonRequester,
    store: RuntimeStore,
    run_id: str,
    source_ref: str,
    artifact_paths: dict[str, str],
    *,
    review_status: str,
    previous_page_id: str | None,
) -> dict[str, object]:
    operation = "updated" if previous_page_id else "created"
    try:
        _require_atlassian_settings(settings)
        if not settings.confluence_sync_space_id:
            raise ValueError("Missing CONFLUENCE_SYNC_SPACE_ID.")
        if not settings.confluence_sync_parent_page_id:
            raise ValueError("Missing CONFLUENCE_SYNC_PARENT_PAGE_ID.")

        title = _checkpoint_title(source_ref, "code_review")
        body = _confluence_code_review_storage_body(
            store,
            run_id,
            source_ref,
            artifact_paths,
            review_status=review_status,
        )
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
                    "version": {"number": version, "message": f"SDLC Harness code review sync {run_id}"},
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
            "artifact_refs": _code_review_artifact_refs(artifact_paths),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - lỗi từng target phải thành report.
        return _failed_target(
            "confluence",
            source_ref,
            exc,
            operation=operation,
            artifact_refs=_code_review_artifact_refs(artifact_paths),
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

def _jira_issue_key(settings: Settings, source_ref: str) -> str:
    if source_ref.startswith("jira_issue:"):
        return source_ref.rsplit(":", 1)[-1].strip().upper()
    return settings.jira_sync_issue_key

def _resolve_checkpoint_target_id(
    *,
    settings: Settings,
    request_json: JsonRequester,
    source_ref: str,
    phase: str,
    tool: str,
    local_target_id: str | None,
) -> str | None:
    """Khôi phục remote id khi report local đã mất.

    Jira-backed runs dùng marker `source_ref + phase` làm identity. Với fixture
    local, report trong runtime vẫn là nguồn identity vì không có issue nguồn để
    truy vấn ngược một cách đáng tin cậy.
    """

    if local_target_id:
        return local_target_id
    if not source_ref.startswith("jira_issue:"):
        return None
    if tool == "jira":
        return _find_remote_jira_checkpoint(settings, request_json, source_ref, phase)
    if tool == "confluence":
        return _find_remote_confluence_checkpoint(settings, request_json, source_ref, phase)
    return None

def _find_remote_jira_checkpoint(
    settings: Settings,
    request_json: JsonRequester,
    source_ref: str,
    phase: str,
) -> str | None:
    issue_key = _jira_issue_key(settings, source_ref)
    response = request_json(
        "GET",
        f"{settings.atlassian_base_url}/rest/api/3/issue/{issue_key}/comment?maxResults=100",
        None,
    )
    comments = response.get("comments", [])
    marker = _checkpoint_marker(source_ref, phase)
    exact: list[str] = []
    legacy: list[str] = []
    legacy_heading = _LEGACY_JIRA_HEADINGS[phase]
    for comment in comments if isinstance(comments, list) else []:
        if not isinstance(comment, dict):
            continue
        comment_id = str(comment.get("id") or "").strip()
        body = _adf_plain_text(comment.get("body"))
        if not comment_id:
            continue
        if marker in body:
            exact.append(comment_id)
        elif legacy_heading.lower() in body.lower() and source_ref.lower() in body.lower():
            legacy.append(comment_id)
    return _single_remote_match(exact or legacy, tool="jira", source_ref=source_ref, phase=phase)

def _find_remote_confluence_checkpoint(
    settings: Settings,
    request_json: JsonRequester,
    source_ref: str,
    phase: str,
) -> str | None:
    title = _checkpoint_title(source_ref, phase)
    query = urlencode(
        {
            "space-id": settings.confluence_sync_space_id,
            "title": title,
            "status": "current",
            "limit": "100",
        }
    )
    response = request_json("GET", f"{settings.atlassian_base_url}/wiki/api/v2/pages?{query}", None)
    exact = _page_ids(response, title=title)
    if exact:
        return _single_remote_match(exact, tool="confluence", source_ref=source_ref, phase=phase)

    parent_id = settings.confluence_sync_parent_page_id
    if not parent_id:
        return None
    children = request_json(
        "GET",
        (
            f"{settings.atlassian_base_url}/wiki/api/v2/pages/{parent_id}/children"
            "?limit=250&body-format=storage"
        ),
        None,
    )
    marker = _checkpoint_marker(source_ref, phase)
    marker_matches: list[str] = []
    legacy_matches: list[str] = []
    legacy_prefix = _LEGACY_CONFLUENCE_PREFIXES[phase]
    for page in children.get("results", []) if isinstance(children.get("results"), list) else []:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "").strip()
        page_title = str(page.get("title") or "")
        body = _confluence_page_body(page)
        if not page_id:
            continue
        if marker in body:
            marker_matches.append(page_id)
        elif page_title.startswith(legacy_prefix) and source_ref.lower() in body.lower():
            legacy_matches.append(page_id)
    return _single_remote_match(
        marker_matches or legacy_matches,
        tool="confluence",
        source_ref=source_ref,
        phase=phase,
    )

def _single_remote_match(ids: list[str], *, tool: str, source_ref: str, phase: str) -> str | None:
    unique = list(dict.fromkeys(item for item in ids if item))
    if len(unique) > 1:
        raise HarnessError(
            "remote_sync_ambiguous",
            f"Found multiple {tool} checkpoints for {source_ref} phase={phase}: {', '.join(unique)}",
        )
    return unique[0] if unique else None

def _page_ids(response: dict[str, Any], *, title: str) -> list[str]:
    results = response.get("results", [])
    if not isinstance(results, list):
        return []
    return [
        str(page.get("id") or "")
        for page in results
        if isinstance(page, dict) and str(page.get("title") or "") == title and page.get("id")
    ]

def _confluence_page_body(page: dict[str, Any]) -> str:
    body = page.get("body")
    if not isinstance(body, dict):
        return ""
    storage = body.get("storage")
    if isinstance(storage, dict):
        return str(storage.get("value") or "")
    return str(body.get("value") or "")

def _adf_plain_text(node: object) -> str:
    parts: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                parts.append(text)
            visit(value.get("content"))

    visit(node)
    return "\n".join(parts)

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
    error_code = exc.error_code if isinstance(exc, HarnessError) else "sync_failed"
    return {
        "tool": tool,
        "mode": "real_api",
        "status": "failed",
        "error_code": error_code,
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

