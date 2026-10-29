"""Chuẩn bị DeveloperWorkPacket trước khi bước Developer.act sinh code.

Phase 3 tạo implementation plan để human review; Phase 4 cần chuyển plan đó
thành gói đầu vào ổn định cho Developer. Module này chỉ gom dữ liệu đã duyệt,
snapshot file đích và output contract. Khi repair, helper refresh snapshot từ
target repo thật để Developer không sửa dựa trên code cũ.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ...core.errors import HarnessError
from ...profiles import (
    COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION,
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    PROFILED_IMPLEMENTATION_PLAN_VERSION,
    UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    PlanningPathPolicy,
    VerificationStagePolicy,
    is_profiled_work_packet_schema,
    profiled_plan_contract_issues,
)
from ...storage.runtime import utc_now_iso

MAX_FILE_CONTENT_CHARS = 40000
PROFILED_EDITABLE_PATH_POLICY_VERSION = "editable_path_policy_v1"


def build_developer_work_packet(
    *,
    run_id: str,
    source_ref: str,
    issue_key: str,
    structured_plan: dict[str, object],
    target_repo_path: Path,
    run_workspace: dict[str, object],
    approved_plan_refs: dict[str, str],
    canonical_requirements: list[dict[str, object]] | None = None,
    requirement_contract: dict[str, object] | None = None,
    requirement_coverage: list[dict[str, object]] | None = None,
    effective_design_contract: dict[str, object] | None = None,
    planning_policy: PlanningPathPolicy,
    verification_policy: VerificationStagePolicy,
) -> dict[str, object]:
    """Tạo packet máy đọc được từ implementation plan đã duyệt."""

    contract_issues = profiled_plan_contract_issues(
        structured_plan,
        planning_policy=planning_policy,
        verification_policy=verification_policy,
        allow_legacy=True,
    )
    if contract_issues:
        raise HarnessError(
            "implementation_plan_profile_invalid",
            "Profiled implementation plan failed Phase 4 boundary validation: "
            + "; ".join(contract_issues),
            run_id=run_id,
        )

    design_mappings = [
        item
        for item in _dict_items(structured_plan.get("design_mappings"))
        if item.get("implementation_status") != "waived_by_design_adjustment"
    ]
    implementation_steps = _dict_items(structured_plan.get("implementation_steps"))
    proposed_files = _merge_mapping_targets_into_proposed_files(
        proposed_files=_merge_step_targets_into_proposed_files(
            proposed_files=_normalize_proposed_files(
                structured_plan.get("proposed_files"),
                run_id=run_id,
                planning_policy=planning_policy,
            ),
            implementation_steps=implementation_steps,
            run_id=run_id,
            planning_policy=planning_policy,
        ),
        design_mappings=design_mappings,
        planning_policy=planning_policy,
    )
    enriched_files = [
        _enrich_proposed_file(
            target_repo_path=target_repo_path,
            file_item=item,
            run_id=run_id,
            planning_policy=planning_policy,
        )
        for item in proposed_files
    ]
    allowed_paths = [str(item["path"]) for item in enriched_files]
    canonical_requirements = canonical_requirements or []
    requirement_contract = requirement_contract or {"version": "v1", "revision": 0, "artifact_refs": {}}
    requirement_coverage = requirement_coverage or []
    required_fields = ["path", "content"]
    component_aware_contract = (
        structured_plan.get("version") == COMPONENT_AWARE_PROFILED_IMPLEMENTATION_PLAN_VERSION
    )
    current_profiled_contract = structured_plan.get("version") == PROFILED_IMPLEMENTATION_PLAN_VERSION
    legacy_profiled_contract = (
        structured_plan.get("version") == LEGACY_PROFILED_IMPLEMENTATION_PLAN_VERSION
    )
    return {
        "version": (
            "developer_work_packet_v3"
            if component_aware_contract
            else "developer_work_packet_v2"
            if current_profiled_contract
            else "developer_work_packet_v1"
        ),
        "schema_version": (
            UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION
            if component_aware_contract
            else PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION
            if current_profiled_contract
            else LEGACY_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION
            if legacy_profiled_contract
            else "developer_work_packet_legacy_v1"
        ),
        "mode": "approved_implementation_plan_to_developer_act_v1",
        "run_id": run_id,
        "source_ref": source_ref,
        "issue_key": issue_key,
        "created_at": utc_now_iso(),
        "target_repository": _dict_value(structured_plan.get("target_repository")),
        "run_workspace": {
            "version": run_workspace.get("version"),
            "workspace_mode": run_workspace.get("workspace_mode"),
            "workspace_ref": _dict_value(structured_plan.get("target_repository")).get("workspace_ref"),
            "branch": run_workspace.get("branch"),
            "base_ref": run_workspace.get("base_ref"),
            "base_sha": run_workspace.get("base_sha"),
            "head_sha": run_workspace.get("head_sha"),
            "remote": run_workspace.get("remote"),
        },
        "approved_plan": approved_plan_refs,
        "effective_design_contract": effective_design_contract or {},
        "design_adjustments": _dict_items(structured_plan.get("design_adjustments")),
        "requirement_contract": requirement_contract,
        "canonical_requirements": canonical_requirements,
        "requirement_coverage": requirement_coverage,
        "requirement_coverage_by_file": _group_requirement_coverage_by_file(
            requirement_coverage,
            planning_policy=planning_policy,
        ),
        "implementation_steps": implementation_steps,
        **(
            {
                "execution_units": _build_execution_units(
                    structured_plan=structured_plan,
                    allowed_paths=allowed_paths,
                    planning_policy=planning_policy,
                    verification_policy=verification_policy,
                    run_id=run_id,
                )
            }
            if component_aware_contract
            else {}
        ),
        "proposed_files": enriched_files,
        "design_mappings_by_file": _group_design_mappings_by_file(
            design_mappings,
            planning_policy=planning_policy,
        ),
        **(
            {
                "implementation_signature_contract": _dict_value(
                    structured_plan.get("implementation_signature_contract")
                )
            }
            if component_aware_contract or current_profiled_contract
            else {}
        ),
        "verification_stage_ids": _string_items(structured_plan.get("verification_stage_ids")),
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
            "format": "changed_file_blocks_v1",
            "changed_files_only": True,
            "allowed_paths": allowed_paths,
            "required_fields": required_fields,
            "path_policy": _profiled_path_policy_contract(planning_policy),
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


def _build_execution_units(
    *,
    structured_plan: dict[str, object],
    allowed_paths: list[str],
    planning_policy: PlanningPathPolicy,
    verification_policy: VerificationStagePolicy,
    run_id: str,
) -> list[dict[str, object]]:
    """Project approved Plan V4 work units without inventing new execution scope."""

    work_units = _dict_items(structured_plan.get("work_units"))
    if not work_units:
        raise HarnessError(
            "developer_execution_units_missing",
            "Component-aware implementation plan has no work units for Phase 4.",
            run_id=run_id,
        )
    allowed = set(allowed_paths)
    allowed_stages = set(verification_policy.stage_ids)
    signatures = _dict_items(
        _dict_value(structured_plan.get("implementation_signature_contract")).get("entries")
    )
    ids = [str(item.get("unit_id") or "").strip() for item in work_units]
    if any(not unit_id for unit_id in ids) or len(ids) != len(set(ids)):
        raise HarnessError(
            "developer_execution_unit_identity_invalid",
            "Component-aware implementation plan has missing or duplicate work-unit IDs.",
            run_id=run_id,
        )
    known = set(ids)
    projected: list[dict[str, object]] = []
    paths_by_unit: dict[str, set[str]] = {}
    dependencies: dict[str, set[str]] = {}
    for item in sorted(work_units, key=lambda row: (int(row.get("order") or 0), str(row.get("unit_id") or ""))):
        unit_id = str(item.get("unit_id") or "").strip()
        depends_on = _string_items(item.get("depends_on"))
        if unit_id in depends_on or any(value not in known for value in depends_on):
            raise HarnessError(
                "developer_execution_unit_dependency_invalid",
                f"Execution unit has an invalid dependency: {unit_id}",
                run_id=run_id,
            )
        targets = _dict_items(item.get("planned_code_targets"))
        raw_paths = [str(target.get("path") or "") for target in targets]
        migration = _dict_value(item.get("migration_impact"))
        raw_paths.extend(_string_items(migration.get("targets")))
        test_intents = _dict_items(item.get("test_intents"))
        for intent in test_intents:
            raw_paths.extend(_string_items(intent.get("target_paths")))
        write_paths: list[str] = []
        for raw_path in raw_paths:
            path = _clean_repo_path(
                raw_path,
                run_id=run_id,
                planning_policy=planning_policy,
            )
            if path not in allowed:
                raise HarnessError(
                    "developer_execution_unit_path_invalid",
                    f"Execution unit {unit_id} references a path outside approved packet scope: {path}",
                    run_id=run_id,
                )
            if path not in write_paths:
                write_paths.append(path)
        if not write_paths:
            raise HarnessError(
                "developer_execution_unit_path_invalid",
                f"Execution unit has no approved write path: {unit_id}",
                run_id=run_id,
            )
        stage_ids = list(dict.fromkeys(str(row.get("stage_id") or "").strip() for row in test_intents))
        if any(not stage_id or stage_id not in allowed_stages for stage_id in stage_ids):
            raise HarnessError(
                "developer_execution_unit_stage_invalid",
                f"Execution unit references an unsupported verification stage: {unit_id}",
                run_id=run_id,
            )
        signature_ids = _signature_ids_for_targets(signatures=signatures, targets=targets)
        canonical = {
            "unit_id": unit_id,
            "order": int(item.get("order") or 0),
            "scenario_id": str(item.get("scenario_id") or "").strip(),
            "component_id": str(item.get("component_id") or "").strip(),
            "surfaces": _string_items(item.get("surfaces")),
            "title": str(item.get("title") or "").strip(),
            "objective": str(item.get("objective") or "").strip(),
            "depends_on": depends_on,
            "requirement_refs": _string_items(item.get("requirement_refs")),
            "acceptance_refs": _string_items(item.get("acceptance_refs")),
            "design_node_ids": _string_items(item.get("design_node_ids")),
            "design_edge_ids": _string_items(item.get("design_edge_ids")),
            "planned_code_targets": targets,
            "migration_impact": migration,
            "runtime_bindings": _dict_items(item.get("runtime_bindings")),
            "boundary_contracts": _dict_items(item.get("boundary_contracts")),
            "write_paths": write_paths,
            "test_target_paths": list(
                dict.fromkeys(
                    path
                    for row in test_intents
                    if str(row.get("stage_id") or "").strip() == "unit"
                    for path in _string_items(row.get("target_paths"))
                    if path in allowed
                )
            ),
            "required_stage_ids": stage_ids,
            "signature_entry_ids": signature_ids,
            "test_intents": test_intents,
            "repo_evidence_refs": _string_items(item.get("repo_evidence_refs")),
        }
        projected.append(canonical)
        paths_by_unit[unit_id] = set(write_paths)
        dependencies[unit_id] = set(depends_on)

    _attach_execution_unit_context(projected, allowed=allowed)

    def reaches(source: str, target: str) -> bool:
        pending = list(dependencies.get(source, set()))
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(dependencies.get(current, set()))
        return False

    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            shared = paths_by_unit[left] & paths_by_unit[right]
            if shared and not (reaches(left, right) or reaches(right, left)):
                raise HarnessError(
                    "developer_unit_write_ownership_ambiguous",
                    f"Execution units {left} and {right} share unordered write paths: {sorted(shared)}",
                    run_id=run_id,
                )
    return projected


def refresh_developer_execution_context(
    *,
    packet: dict[str, object],
    run_id: str,
) -> dict[str, object]:
    """Add derived read context without rebuilding or changing the approved write scope."""

    units = json.loads(json.dumps(_dict_items(packet.get("execution_units"))))
    if not units:
        raise HarnessError(
            "developer_execution_units_missing",
            "Developer work packet has no execution units to refresh.",
            run_id=run_id,
        )
    allowed = set(_string_items(_dict_value(packet.get("output_contract")).get("allowed_paths")))
    _attach_execution_unit_context(units, allowed=allowed)
    return {**packet, "execution_units": units}


def _attach_execution_unit_context(
    units: list[dict[str, object]],
    *,
    allowed: set[str],
) -> None:
    projected_by_id = {str(unit.get("unit_id") or ""): unit for unit in units}
    for unit in units:
        write_paths = _string_items(unit.get("write_paths"))
        design_edge_ids = set(_string_items(unit.get("design_edge_ids")))
        contract_anchor_paths: list[str] = []
        for dependency_id in _string_items(unit.get("depends_on")):
            dependency = projected_by_id[dependency_id]
            if not design_edge_ids.intersection(_string_items(dependency.get("design_edge_ids"))):
                continue
            for path in _execution_context_target_paths(dependency):
                if path in allowed and path not in write_paths and path not in contract_anchor_paths:
                    contract_anchor_paths.append(path)
        unit["context_paths"] = [*write_paths, *contract_anchor_paths]
        unit["contract_anchor_paths"] = contract_anchor_paths
        digest_payload = json.dumps(unit, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        unit["input_digest"] = "sha256:" + hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()


def _execution_context_target_paths(unit: dict[str, object]) -> list[str]:
    """Return dependency implementation targets as bounded read-only context."""

    target_paths = {
        str(target.get("path") or "").strip().replace("\\", "/")
        for target in _dict_items(unit.get("planned_code_targets"))
        if str(target.get("path") or "").strip()
    }
    for binding in _dict_items(unit.get("runtime_bindings")):
        for endpoint in (_dict_value(binding.get("target")), _dict_value(binding.get("attachment"))):
            path = str(endpoint.get("path") or endpoint.get("owner_path") or "").strip().replace("\\", "/")
            if path:
                target_paths.add(path)
    return [path for path in _string_items(unit.get("write_paths")) if path in target_paths]


def _signature_ids_for_targets(
    *,
    signatures: list[dict[str, object]],
    targets: list[dict[str, object]],
) -> list[str]:
    target_pairs = {
        (
            str(target.get("path") or "").strip().replace("\\", "/"),
            str(target.get("symbol") or "").strip(),
        )
        for target in targets
        if str(target.get("path") or "").strip()
    }
    return [
        contract_id
        for entry in signatures
        if (contract_id := str(entry.get("contract_id") or "").strip())
        and (
            (
                str(entry.get("source_path") or "").strip().replace("\\", "/"),
                str(entry.get("qualified_name") or "").strip(),
            )
            in target_pairs
            or (
                str(entry.get("source_path") or "").strip().replace("\\", "/"),
                "",
            )
            in target_pairs
        )
    ]


def refresh_developer_work_packet(
    *,
    packet: dict[str, object],
    target_repo_path: Path,
    version: int,
    run_id: str,
    planning_policy: PlanningPathPolicy | None = None,
) -> dict[str, object]:
    """Làm mới snapshot code/test trong packet trước mỗi vòng Developer.repair."""

    output_contract = _dict_value(packet.get("output_contract"))
    allowed_paths = validate_developer_work_packet_path_policy(
        packet,
        planning_policy=planning_policy,
        run_id=run_id,
    )
    if not allowed_paths:
        raise HarnessError(
            "developer_work_packet_allowed_paths_missing",
            "DeveloperWorkPacket has no allowed paths to refresh.",
            run_id=run_id,
        )

    refreshed_by_path = {
        path: _enrich_proposed_file(
            target_repo_path=target_repo_path,
            file_item={"path": path},
            run_id=run_id,
            planning_policy=planning_policy,
        )
        for path in allowed_paths
    }
    proposed_files = []
    seen: set[str] = set()
    for item in _dict_items(packet.get("proposed_files")):
        path = _clean_repo_path(
            str(item.get("path") or ""),
            run_id=run_id,
            planning_policy=planning_policy,
        )
        refreshed = refreshed_by_path.get(path)
        if not refreshed:
            continue
        proposed_files.append({**item, **refreshed})
        seen.add(path)
    for path in allowed_paths:
        if path in seen:
            continue
        proposed_files.append(refreshed_by_path[path])

    return {
        **packet,
        "version": f"developer_work_packet_v{version}",
        "mode": "approved_implementation_plan_to_developer_repair_v1",
        "refreshed_at": utc_now_iso(),
        "refreshed_from": str(packet.get("artifact_ref") or packet.get("version") or ""),
        "proposed_files": proposed_files,
        "current_files": [
            {
                "path": item["path"],
                "exists": item["exists"],
                "content": item["current_content"],
                "content_truncated": item["current_content_truncated"],
            }
            for item in proposed_files
        ],
        "output_contract": {
            **output_contract,
            "allowed_paths": allowed_paths,
        },
    }


def _normalize_proposed_files(
    value: object,
    *,
    run_id: str,
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
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
        path = _clean_repo_path(
            str(item.get("path") or ""),
            run_id=run_id,
            planning_policy=planning_policy,
        )
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
    planning_policy: PlanningPathPolicy,
) -> dict[str, object]:
    rel_path = _clean_repo_path(
        str(file_item.get("path") or ""),
        run_id=run_id,
        planning_policy=planning_policy,
    )
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


def _group_design_mappings_by_file(
    mappings: list[dict[str, object]],
    *,
    planning_policy: PlanningPathPolicy,
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in mappings:
        target = str(item.get("planned_code_target") or "")
        file_path = _target_file_from_mapping(target, planning_policy=planning_policy)
        if not file_path:
            continue
        grouped.setdefault(file_path, []).append(item)
    return grouped


def _group_requirement_coverage_by_file(
    coverage: list[dict[str, object]],
    *,
    planning_policy: PlanningPathPolicy,
) -> dict[str, list[dict[str, object]]]:
    """Project canonical V2 coverage to files without changing the approved rows."""

    grouped: dict[str, list[dict[str, object]]] = {}
    for item in coverage:
        requirement_id = str(item.get("requirement_id") or "").strip()
        if not requirement_id:
            continue
        for target in _string_items(item.get("planned_code_targets")):
            file_path = _target_file_from_mapping(target, planning_policy=planning_policy)
            if not file_path:
                continue
            projected = {
                "requirement_id": requirement_id,
                "content": str(item.get("content") or "").strip(),
                "design_element_ids": _string_items(item.get("design_element_ids")),
                "planned_code_targets": _string_items(item.get("planned_code_targets")),
                "test_intents": _string_items(item.get("test_intents")),
            }
            bucket = grouped.setdefault(file_path, [])
            if projected not in bucket:
                bucket.append(projected)
    return grouped


def _merge_mapping_targets_into_proposed_files(
    *,
    proposed_files: list[dict[str, object]],
    design_mappings: list[dict[str, object]],
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    merged = list(proposed_files)
    seen = {str(item.get("path") or "").strip().replace("\\", "/") for item in merged}
    for mapping in design_mappings:
        target = str(mapping.get("planned_code_target") or "")
        file_path = _target_file_from_mapping(target, planning_policy=planning_policy)
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


def _merge_step_targets_into_proposed_files(
    *,
    proposed_files: list[dict[str, object]],
    implementation_steps: list[dict[str, object]],
    run_id: str,
    planning_policy: PlanningPathPolicy,
) -> list[dict[str, object]]:
    merged = list(proposed_files)
    seen = {str(item.get("path") or "").strip().replace("\\", "/") for item in merged}
    for step in implementation_steps:
        raw_files = step.get("files") if isinstance(step.get("files"), list) else []
        for raw_path in raw_files:
            path = _clean_repo_path(
                str(raw_path),
                run_id=run_id,
                planning_policy=planning_policy,
            )
            if path in seen:
                continue
            seen.add(path)
            merged.append(
                {
                    "path": path,
                    "operation": "create_or_update",
                    "purpose": f"Implement plan step `{step.get('title') or step.get('order') or ''}`.",
                }
            )
    return merged


def _target_file_from_mapping(
    target: str,
    *,
    planning_policy: PlanningPathPolicy,
) -> str:
    file_path = target.split("#", 1)[0].strip().replace("\\", "/")
    if not file_path:
        return ""
    try:
        return _clean_repo_path(
            file_path,
            run_id="",
            planning_policy=planning_policy,
        )
    except HarnessError:
        return ""


def _clean_repo_path(
    raw_path: str,
    *,
    run_id: str,
    planning_policy: PlanningPathPolicy,
) -> str:
    normalized = raw_path.strip()
    invalid_shape = (
        not normalized
        or normalized != raw_path
        or "\\" in normalized
        or normalized.startswith("/")
        or normalized.endswith("/")
        or "//" in normalized
        or normalized == ".."
        or normalized.startswith("../")
        or "/../" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or (len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha())
    )
    supported = not invalid_shape and planning_policy.normalize_editable_path(normalized) == normalized
    if invalid_shape or not supported:
        raise HarnessError(
            "developer_work_packet_unsafe_path",
            f"Unsafe or unsupported proposed file path: {raw_path}",
            run_id=run_id or None,
        )
    return normalized


def validate_developer_work_packet_path_policy(
    packet: dict[str, object],
    *,
    planning_policy: PlanningPathPolicy | None,
    run_id: str,
) -> list[str]:
    """Xác minh policy và toàn bộ allowed paths trước refresh/apply/repair."""

    schema_version = str(packet.get("schema_version") or "")
    profiled = is_profiled_work_packet_schema(schema_version)
    if planning_policy is None:
        raise HarnessError(
            "developer_work_packet_path_policy_missing",
            "DeveloperWorkPacket refresh requires the frozen editable path policy.",
            run_id=run_id or None,
        )
    if not profiled:
        raise HarnessError(
            "developer_work_packet_path_policy_mismatch",
            "Phase 4 cannot use a legacy DeveloperWorkPacket.",
            run_id=run_id or None,
        )

    output_contract = _dict_value(packet.get("output_contract"))
    values = output_contract.get("allowed_paths")
    if not isinstance(values, list) or not values:
        raise HarnessError(
            "developer_work_packet_allowed_paths_missing",
            "DeveloperWorkPacket has no allowed paths.",
            run_id=run_id or None,
        )
    if profiled:
        expected = _profiled_path_policy_contract(planning_policy)
        actual = output_contract.get("path_policy")
        if actual != expected:
            raise HarnessError(
                "developer_work_packet_path_policy_mismatch",
                "DeveloperWorkPacket editable path policy does not match the frozen run profile.",
                run_id=run_id or None,
            )

    return [
        _clean_repo_path(
            str(value),
            run_id=run_id,
            planning_policy=planning_policy,
        )
        for value in values
    ]


def _profiled_path_policy_contract(policy: PlanningPathPolicy) -> dict[str, object]:
    return {
        "version": PROFILED_EDITABLE_PATH_POLICY_VERSION,
        "project_profile_id": policy.project_profile_id,
        "language_profile_id": policy.language_profile_id,
        "planning_policy_version": policy.policy_version,
        "component_roots": {
            component_id: list(roots)
            for component_id, roots in policy.component_roots
        },
        "protected_paths": list(policy.protected_paths),
    }


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
