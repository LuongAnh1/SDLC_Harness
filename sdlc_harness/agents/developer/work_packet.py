"""Chuẩn bị DeveloperWorkPacket trước khi bước Developer.act sinh code.

Phase 3 tạo implementation plan để human review; Phase 4 cần chuyển plan đó
thành gói đầu vào ổn định cho Developer. Module này chỉ gom dữ liệu đã duyệt,
snapshot file đích và output contract. Nó chưa sửa target repo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.errors import HarnessError
from ...storage.runtime import utc_now_iso

MAX_FILE_CONTENT_CHARS = 40000
ALLOWED_CODE_PATH_PREFIXES = ("src/main/java/", "src/test/java/")


def build_developer_work_packet(
    *,
    run_id: str,
    source_ref: str,
    issue_key: str,
    structured_plan: dict[str, object],
    target_repo_path: Path,
    approved_plan_refs: dict[str, str],
) -> dict[str, object]:
    """Tạo packet máy đọc được từ implementation plan đã duyệt."""

    design_mappings = _dict_items(structured_plan.get("design_mappings"))
    proposed_files = _merge_mapping_targets_into_proposed_files(
        proposed_files=_normalize_proposed_files(structured_plan.get("proposed_files"), run_id=run_id),
        design_mappings=design_mappings,
    )
    enriched_files = [
        _enrich_proposed_file(target_repo_path=target_repo_path, file_item=item, run_id=run_id)
        for item in proposed_files
    ]
    allowed_paths = [str(item["path"]) for item in enriched_files]
    return {
        "version": "developer_work_packet_v1",
        "mode": "approved_implementation_plan_to_developer_act_v1",
        "run_id": run_id,
        "source_ref": source_ref,
        "issue_key": issue_key,
        "created_at": utc_now_iso(),
        "target_repository": _dict_value(structured_plan.get("target_repository")),
        "approved_plan": approved_plan_refs,
        "implementation_steps": _dict_items(structured_plan.get("implementation_steps")),
        "proposed_files": enriched_files,
        "design_mappings_by_file": _group_design_mappings_by_file(design_mappings),
        "test_intents": _dict_items(structured_plan.get("test_intents")),
        "repo_evidence": _string_items(structured_plan.get("repo_evidence")),
        "current_files": [
            {
                "path": item["path"],
                "exists": item["exists"],
                "content": item["current_content"],
                "content_truncated": item["current_content_truncated"],
            }
            for item in enriched_files
        ],
        "output_contract": {
            "format": "changed_files_json_v1",
            "changed_files_only": True,
            "allowed_paths": allowed_paths,
            "required_fields": ["path", "operation", "content", "reason", "covered_design_elements"],
        },
        "constraints": [
            "Chỉ tạo/sửa file nằm trong output_contract.allowed_paths ở slice Developer.act V1.",
            "Không sửa file ngoài target repo.",
            "Không tự đổi requirement/design đã được duyệt trong Phase 4.",
            "Code, test và script build/test phải chạy được khi repo nằm trong đường dẫn có khoảng trắng hoặc Unicode.",
            "Ưu tiên repo-relative paths, không hard-code absolute path theo máy cá nhân.",
            "Khi đọc/ghi/compile source hoặc chạy test, dùng UTF-8 rõ ràng nếu toolchain hỗ trợ.",
            "Nếu thiếu ngữ cảnh để code an toàn, Developer phải trả lỗi thay vì đoán.",
        ],
    }


def _normalize_proposed_files(value: object, *, run_id: str) -> list[dict[str, object]]:
    files = _dict_items(value)
    if not files:
        raise HarnessError(
            "implementation_plan_missing_proposed_files",
            "Approved implementation plan has no proposed_files for Developer.act.",
            run_id=run_id,
        )
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in files:
        path = _clean_repo_path(str(item.get("path") or ""), run_id=run_id)
        if path in seen:
            continue
        seen.add(path)
        normalized.append({**item, "path": path})
    return normalized


def _enrich_proposed_file(
    *,
    target_repo_path: Path,
    file_item: dict[str, object],
    run_id: str,
) -> dict[str, object]:
    rel_path = _clean_repo_path(str(file_item.get("path") or ""), run_id=run_id)
    absolute_path = (target_repo_path / rel_path).resolve()
    try:
        absolute_path.relative_to(target_repo_path.resolve())
    except ValueError as exc:
        raise HarnessError(
            "developer_work_packet_unsafe_path",
            f"Proposed file escapes target repo: {rel_path}",
            run_id=run_id,
        ) from exc
    exists = absolute_path.exists() and absolute_path.is_file()
    content = ""
    truncated = False
    if exists:
        raw = absolute_path.read_text(encoding="utf-8", errors="replace")
        content = raw
        if len(raw) > MAX_FILE_CONTENT_CHARS:
            content = raw[:MAX_FILE_CONTENT_CHARS].rstrip()
            truncated = True
    return {
        **file_item,
        "path": rel_path,
        "exists": exists,
        "current_content_ref": rel_path if exists else "",
        "current_content": content,
        "current_content_truncated": truncated,
        "current_content_chars": len(content),
    }


def _group_design_mappings_by_file(mappings: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in mappings:
        target = str(item.get("planned_code_target") or "")
        file_path = _target_file_from_mapping(target)
        if not file_path:
            continue
        grouped.setdefault(file_path, []).append(item)
    return grouped


def _merge_mapping_targets_into_proposed_files(
    *,
    proposed_files: list[dict[str, object]],
    design_mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged = list(proposed_files)
    seen = {str(item.get("path") or "").strip().replace("\\", "/") for item in merged}
    for mapping in design_mappings:
        target = str(mapping.get("planned_code_target") or "")
        file_path = _target_file_from_mapping(target)
        if not file_path or file_path in seen:
            continue
        seen.add(file_path)
        merged.append(
            {
                "path": file_path,
                "operation": "create_or_update",
                "purpose": f"Implement mapped design element `{mapping.get('design_element_id') or ''}`.",
            }
        )
    return merged


def _target_file_from_mapping(target: str) -> str:
    file_path = target.split("#", 1)[0].strip().replace("\\", "/")
    if not file_path:
        return ""
    try:
        return _clean_repo_path(file_path, run_id="")
    except HarnessError:
        return ""


def _clean_repo_path(raw_path: str, *, run_id: str) -> str:
    normalized = raw_path.strip().replace("\\", "/").lstrip("/")
    if (
        not normalized
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized == ".."
        or not normalized.startswith(ALLOWED_CODE_PATH_PREFIXES)
    ):
        raise HarnessError(
            "developer_work_packet_unsafe_path",
            f"Unsafe or unsupported proposed file path: {raw_path}",
            run_id=run_id or None,
        )
    return normalized


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
