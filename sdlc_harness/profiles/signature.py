"""Contract chữ ký implementation được human duyệt cho profiled Phase 3/4."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .planning import PlanningPathPolicy


IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION = "implementation_signature_contract_v1"
_ALLOWED_SYMBOL_KINDS = {"function", "method"}
_ALLOWED_OWNER_KINDS = {"", "class", "interface", "abstract"}


def build_implementation_signature_contract(
    *,
    planning_policy: PlanningPathPolicy,
    mappings: list[dict[str, object]],
    class_graph: dict[str, object],
    candidate: object = None,
    bind_to_mapped_symbol: bool = False,
) -> dict[str, object]:
    """Canonicalize đề xuất Phase 3; không tự suy luận type từ design graph."""

    mapping_by_id = {
        str(item.get("design_element_id") or "").strip(): item
        for item in mappings
        if str(item.get("design_element_id") or "").strip()
        and item.get("implementation_status") != "waived_by_design_adjustment"
    }
    requested = _dict_value(candidate)
    requested_entries = _dict_items(requested.get("entries"))
    entries: list[dict[str, object]] = []

    for raw in requested_entries:
        design_id = str(raw.get("design_element_id") or "").strip()
        mapping = mapping_by_id.get(design_id)
        if not mapping:
            continue
        entry = _canonical_entry(
            raw,
            mapping=mapping,
            planning_policy=planning_policy,
            bind_to_mapped_symbol=bind_to_mapped_symbol,
        )
        if entry:
            entries.append(entry)

    unique: dict[object, dict[str, object]] = {}
    for entry in entries:
        key = (
            (str(entry["source_path"]), str(entry["qualified_name"]))
            if bind_to_mapped_symbol
            else str(entry["contract_id"])
        )
        unique[key] = entry
    contract = {
        "version": IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION,
        "profile": planning_policy.metadata(),
        "entries": sorted(
            unique.values(),
            key=lambda item: (
                str(item.get("source_path") or ""),
                str(item.get("qualified_name") or ""),
                str(item.get("contract_id") or ""),
            ),
        ),
    }
    contract["digest"] = implementation_signature_contract_digest(contract)
    return contract


def implementation_signature_contract_issues(
    contract: object,
    *,
    planning_policy: PlanningPathPolicy,
    mappings: list[dict[str, object]],
    class_graph: dict[str, object] | None = None,
    enforce_design_method_arity: bool = True,
    reject_placeholder_only: bool = False,
    bind_to_mapped_symbol: bool = False,
) -> list[str]:
    """Validate contract identity, mapping binding và approved design signature."""

    issues: list[str] = []
    value = _dict_value(contract)
    if value.get("version") != IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION:
        return [
            "Implementation signature contract version must be "
            f"`{IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION}`."
        ]
    if value.get("profile") != planning_policy.metadata():
        issues.append("Implementation signature contract profile does not match the frozen run profile.")
    if value.get("digest") != implementation_signature_contract_digest(value):
        issues.append("Implementation signature contract digest is invalid.")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        return [*issues, "Implementation signature contract entries must be an array."]

    mapping_by_id = {
        str(item.get("design_element_id") or "").strip(): item
        for item in mappings
        if str(item.get("design_element_id") or "").strip()
        and item.get("implementation_status") != "waived_by_design_adjustment"
    }
    covered_methods: set[str] = set()
    covered_bindings: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    seen_method_bindings: dict[tuple[str, str], str] = {}
    for index, raw in enumerate(raw_entries):
        label = f"Signature contract entry {index + 1}"
        if not isinstance(raw, dict):
            issues.append(f"{label} must be an object.")
            continue
        design_id = str(raw.get("design_element_id") or "").strip()
        mapping = mapping_by_id.get(design_id)
        if not mapping:
            issues.append(f"{label} does not reference an approved executable design mapping: {design_id}")
            continue
        canonical = _canonical_entry(
            raw,
            mapping=mapping,
            planning_policy=planning_policy,
            bind_to_mapped_symbol=bind_to_mapped_symbol,
        )
        if not canonical or canonical != raw:
            issues.append(f"{label} is not canonical or does not match its approved mapping.")
            continue
        contract_id = str(raw.get("contract_id") or "")
        if contract_id in seen_ids:
            issues.append(f"Duplicate implementation signature contract: {contract_id}")
        seen_ids.add(contract_id)
        if str(mapping.get("design_element_type") or "") == "method":
            covered_methods.add(design_id)
            binding = (
                str(raw.get("source_path") or ""),
                str(raw.get("qualified_name") or ""),
            )
            covered_bindings.add(binding)
            if not bind_to_mapped_symbol:
                previous_design_id = seen_method_bindings.get(binding)
                if previous_design_id and previous_design_id != design_id:
                    issues.append(
                        "Approved methods share one implementation signature binding: "
                        f"{previous_design_id}, {design_id} -> {binding[0]}#{binding[1]}"
                    )
                else:
                    seen_method_bindings[binding] = design_id
        if enforce_design_method_arity and class_graph is not None and design_id.startswith("method:"):
            if not _matches_design_method(raw, class_graph=class_graph):
                issues.append(
                    f"{label} does not match the approved method identity/arity: {design_id}"
                )

    if bind_to_mapped_symbol:
        required_bindings = {
            binding
            for mapping in mapping_by_id.values()
            if str(mapping.get("design_element_type") or "") == "method"
            for binding in [_mapping_binding(mapping, planning_policy=planning_policy)]
            if binding
        }
        for path, symbol in sorted(required_bindings - covered_bindings):
            issues.append(
                f"Missing implementation signature contract for approved code target: {path}#{symbol}"
            )
    else:
        required_methods = {
            design_id
            for design_id, mapping in mapping_by_id.items()
            if str(mapping.get("design_element_type") or "") == "method"
        }
        for design_id in sorted(required_methods - covered_methods):
            issues.append(f"Missing implementation signature contract for approved method: {design_id}")
    entries = [item for item in raw_entries if isinstance(item, dict)]
    if reject_placeholder_only and len(entries) > 1 and all(
        not _dict_items(item.get("parameters"))
        and normalize_typescript_type(item.get("return_type")) in {"void", "Promise<void>"}
        for item in entries
    ):
        issues.append(
            "Implementation signature contract is placeholder-only; derive callable inputs and outputs "
            "from the approved vertical contracts and repository evidence."
        )
    if reject_placeholder_only:
        for item in entries:
            if (
                str(item.get("source_path") or "").endswith(".tsx")
                and normalize_typescript_type(item.get("return_type")) in {"void", "Promise<void>"}
            ):
                issues.append(
                    "TSX component signature must return a renderable value: "
                    f"{item.get('qualified_name') or item.get('design_element_id')}"
                )
    return issues


def normalize_typescript_type(value: object) -> str:
    """Chuẩn hóa whitespace cú pháp, không diễn giải type alias hay đổi dạng type."""

    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"\s*([<>{}\[\](),|&?:])\s*", r"\1", text)
    # TypeScript permits the final object member separator to be omitted.
    text = re.sub(r";(?=})", "", text)
    return text


def _mapping_binding(
    mapping: dict[str, object],
    *,
    planning_policy: PlanningPathPolicy,
) -> tuple[str, str] | None:
    target = str(mapping.get("planned_code_target") or "").strip().replace("\\", "/")
    path, separator, symbol = target.partition("#")
    path = planning_policy.normalize_mapping_path(path)
    return (path, symbol) if separator and path and symbol else None


def signature_shape(entry: dict[str, object]) -> dict[str, object]:
    """Trả shape dùng để so sánh; tên parameter chỉ là evidence, không là contract."""

    return {
        "parameters": [
            {
                "type": normalize_typescript_type(item.get("type")),
                "optional": item.get("optional") is True,
                "rest": item.get("rest") is True,
            }
            for item in _dict_items(entry.get("parameters"))
        ],
        "return_type": normalize_typescript_type(entry.get("return_type")),
        "type_parameters": [
            normalize_typescript_type(item) for item in _string_items(entry.get("type_parameters"))
        ],
        "async": entry.get("async") is True,
    }


def _canonical_entry(
    raw: dict[str, object],
    *,
    mapping: dict[str, object],
    planning_policy: PlanningPathPolicy,
    bind_to_mapped_symbol: bool = False,
) -> dict[str, object] | None:
    target = str(mapping.get("planned_code_target") or "").strip().replace("\\", "/")
    source_path, _, mapped_symbol = target.partition("#")
    source_path = planning_policy.normalize_mapping_path(source_path)
    if not source_path:
        return None
    design_id = str(mapping.get("design_element_id") or "").strip()
    qualified_name = str(raw.get("qualified_name") or "").strip()
    symbol_kind = str(raw.get("symbol_kind") or "").strip().lower()
    owner_kind = str(raw.get("owner_kind") or "").strip().lower()
    parameters = raw.get("parameters")
    type_parameters = raw.get("type_parameters")
    return_type = str(raw.get("return_type") or "").strip()
    if (
        not design_id
        or symbol_kind not in _ALLOWED_SYMBOL_KINDS
        or owner_kind not in _ALLOWED_OWNER_KINDS
        or not qualified_name
        or not mapped_symbol
        or not isinstance(parameters, list)
        or not isinstance(type_parameters, list)
        or not return_type
        or not all(isinstance(item, str) and item.strip() for item in type_parameters)
    ):
        return None
    normalized_parameters: list[dict[str, object]] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            return None
        name = str(parameter.get("name") or "").strip()
        parameter_type = str(parameter.get("type") or "").strip()
        if not name or not parameter_type:
            return None
        normalized_parameters.append(
            {
                "name": name.removeprefix("...").removesuffix("?"),
                "type": parameter_type,
                "optional": parameter.get("optional") is True,
                "rest": parameter.get("rest") is True,
            }
        )
    design_owner = (
        design_id.removeprefix("method:").rsplit(".", 1)[0]
        if design_id.startswith("method:") and "." in design_id
        else ""
    )
    if bind_to_mapped_symbol:
        bound_qualified_name = mapped_symbol
        symbol_kind = "method" if "." in mapped_symbol else "function"
        owner_kind = owner_kind if symbol_kind == "method" else ""
    else:
        bound_qualified_name = (
            f"{design_owner}.{mapped_symbol}"
            if owner_kind in {"class", "interface", "abstract"} and design_owner
            else mapped_symbol
        )
    entry: dict[str, object] = {
        "design_element_id": design_id,
        "source_path": source_path,
        "symbol_kind": symbol_kind,
        "owner_kind": owner_kind,
        "qualified_name": bound_qualified_name,
        "parameters": normalized_parameters,
        "return_type": return_type,
        "type_parameters": [str(item).strip() for item in type_parameters],
        # TypeScript interface methods cannot carry the `async` modifier. Their
        # asynchronous contract is represented by the Promise return type.
        "async": raw.get("async") is True and owner_kind != "interface",
    }
    entry["contract_id"] = _contract_id(entry)
    return entry


def _matches_design_method(entry: dict[str, object], *, class_graph: dict[str, object]) -> bool:
    design_id = str(entry.get("design_element_id") or "")
    if not design_id.startswith("method:") or "." not in design_id:
        return False
    owner, name = design_id.removeprefix("method:").split(".", 1)
    expected_parameters = _dict_items(entry.get("parameters"))
    for node in _dict_items(class_graph.get("nodes")):
        node_name = str(node.get("className") or node.get("id") or "").strip()
        if node_name != owner:
            continue
        for method in _dict_items(node.get("methods")):
            if str(method.get("name") or "").strip() != name:
                continue
            if len(expected_parameters) == len(_dict_items(method.get("parameters"))):
                return True
    return False


def _contract_id(entry: dict[str, object]) -> str:
    payload = {**entry, "shape": signature_shape(entry)}
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16].upper()
    return f"SIG-{digest}"


def implementation_signature_contract_digest(contract: dict[str, object]) -> str:
    """Tính digest ổn định để bind contract qua checkpoint/work packet."""

    payload = {
        "version": contract.get("version"),
        "profile": contract.get("profile"),
        "entries": contract.get("entries"),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
