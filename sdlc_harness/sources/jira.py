"""Bộ nạp Jira issue làm nguồn đầu vào cho Phase 1B.

Module này kéo context từ Jira xuống SDLC Harness. Nhánh sync ở
`sdlc_harness/sync/` làm chiều ngược lại: đẩy checkpoint đã xử lý lên tool.
Các comment do chính Harness sinh ra có marker checkpoint riêng và bị loại khỏi
source context, tránh để lần chạy sau tự nuốt lại output của lần chạy trước.
"""

from __future__ import annotations

from base64 import b64encode
import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from ..core.config import Settings
from ..core.errors import HarnessError
from .fixtures import UseCaseSource

JsonRequester = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]

HARNESS_CHECKPOINT_MARKER = "SDLC-HARNESS-CHECKPOINT::"
_LEGACY_HARNESS_COMMENT_HEADINGS = (
    "SDLC Harness checkpoint: Requirement review",
    "SDLC Harness checkpoint: Design review",
    "SDLC Harness checkpoint: Implementation plan review",
    "SDLC Harness checkpoint: Code review",
)


def jira_source_ref_for(issue_key: str) -> str:
    """Tạo source_ref ổn định cho active-run index theo Jira issue."""

    return f"jira_issue:{issue_key.strip().upper()}"


def load_jira_issue_source(
    *,
    settings: Settings,
    issue_key: str,
    request_json: JsonRequester | None = None,
) -> UseCaseSource:
    """Fetch Jira issue và đóng gói thành source text cho Requirement Refiner."""

    normalized_key = issue_key.strip().upper()
    if not normalized_key:
        raise HarnessError("jira_source_not_found", "Missing Jira issue key.")
    _require_atlassian_settings(settings)

    requester = request_json or _request_json(settings)
    encoded_key = quote(normalized_key, safe="")
    response = requester(
        "GET",
        (
            f"{settings.atlassian_base_url}/rest/api/3/issue/{encoded_key}"
            "?fields=summary,description,status,labels,issuetype,comment,updated"
        ),
        None,
    )
    fields = response.get("fields", {})
    if not isinstance(fields, dict):
        raise HarnessError("jira_source_not_found", f"Jira issue not found: {normalized_key}")

    summary = str(fields.get("summary") or "").strip()
    description = _adf_to_text(fields.get("description")).strip()
    comments = _extract_comments(fields.get("comment"))
    if not summary or (not description and not comments):
        raise HarnessError(
            "jira_source_empty",
            f"Jira issue does not contain enough source context: {normalized_key}",
        )

    status = _named_field(fields.get("status"))
    issue_type = _named_field(fields.get("issuetype"))
    labels = fields.get("labels") if isinstance(fields.get("labels"), list) else []
    source_text = _build_source_text(
        issue_key=normalized_key,
        summary=summary,
        description=description,
        status=status,
        issue_type=issue_type,
        labels=[str(label) for label in labels],
        comments=comments,
    )

    return UseCaseSource(
        dataset="jira",
        use_case=normalized_key,
        source_ref=jira_source_ref_for(normalized_key),
        source_path=None,
        text=source_text,
        source_type="jira",
        locator=normalized_key,
        revision=str(fields.get("updated") or "").strip() or None,
    )


def _request_json(settings: Settings) -> JsonRequester:
    def request_json(method: str, url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
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
            if exc.code == 404:
                raise HarnessError("jira_source_not_found", _trim_error(detail)) from exc
            raise HarnessError("jira_source_fetch_failed", f"Jira API HTTP {exc.code}: {_trim_error(detail)}") from exc
        except URLError as exc:
            raise HarnessError("jira_source_fetch_failed", f"Jira API connection failed: {_trim_error(str(exc.reason))}") from exc
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
        raise HarnessError("jira_source_config_missing", f"Missing Atlassian config: {', '.join(missing)}.")


def _auth_header(settings: Settings) -> str:
    raw = f"{settings.atlassian_email}:{settings.atlassian_api_token}".encode("utf-8")
    return "Basic " + b64encode(raw).decode("ascii")


def _named_field(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    return ""


def _extract_comments(raw_comment: object) -> list[dict[str, str]]:
    """Chỉ giữ comment của người dùng làm source context.

    Checkpoint do chính SDLC Harness ghi lên Jira là output của workflow. Nếu
    nạp ngược chúng vào Requirement Refiner, một run mới sẽ mang theo artifact
    và run id cũ, khiến context phình to và sai nguồn sự thật.
    """

    if not isinstance(raw_comment, dict):
        return []
    comments = raw_comment.get("comments")
    if not isinstance(comments, list):
        return []
    extracted: list[dict[str, str]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = comment.get("author")
        author_name = ""
        if isinstance(author, dict):
            author_name = str(author.get("displayName") or author.get("name") or "").strip()
        body = _adf_to_text(comment.get("body")).strip()
        if body and not _is_harness_generated_comment(body):
            extracted.append({"author": author_name or "unknown", "body": body})
    return extracted


def _is_harness_generated_comment(body: str) -> bool:
    """Nhận diện cả marker mới lẫn heading checkpoint legacy của Harness."""

    normalized = " ".join(str(body or "").split())
    if HARNESS_CHECKPOINT_MARKER in normalized:
        return True
    return any(heading in normalized for heading in _LEGACY_HARNESS_COMMENT_HEADINGS)


def _adf_to_text(node: object) -> str:
    """Rút text từ Atlassian Document Format đủ dùng cho Requirement Refiner."""

    parts: list[str] = []

    def visit(value: object) -> None:
        if value is None:
            return
        if isinstance(value, str):
            parts.append(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        node_type = value.get("type")
        if node_type in {"paragraph", "heading", "listItem"} and parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        text = value.get("text")
        if isinstance(text, str):
            parts.append(text)
        if node_type == "hardBreak":
            parts.append("\n")
        visit(value.get("content"))
        if node_type in {"paragraph", "heading", "listItem"} and parts and not parts[-1].endswith("\n"):
            parts.append("\n")

    visit(node)
    return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())


def _build_source_text(
    *,
    issue_key: str,
    summary: str,
    description: str,
    status: str,
    issue_type: str,
    labels: list[str],
    comments: list[dict[str, str]],
) -> str:
    lines = [
        f"Jira Issue: {issue_key}",
        f"Summary: {summary}",
    ]
    if issue_type:
        lines.append(f"Issue Type: {issue_type}")
    if status:
        lines.append(f"Status: {status}")
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    lines.extend(["", "Description:", description or "(empty)"])
    if comments:
        lines.extend(["", "Comments:"])
        for comment in comments:
            lines.append(f"- {comment['author']}: {comment['body']}")
    return "\n".join(lines).strip()


def _trim_error(message: str) -> str:
    return " ".join(message.split())[:500]
