"""Bằng chứng contract tất định và cơ chế finding ổn định cho Phase 4."""

from __future__ import annotations

import hashlib
import json

from ...core.errors import HarnessError
from ...profiles import (
    IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION,
    implementation_signature_contract_digest,
    signature_shape,
)
from .typescript_ast import TypeScriptAstEvidence
from ...core.coerce import dict_items as _dicts, string_items as _strings




def build_validation_claims(
    *,
    work_packet: dict[str, object],
    trace_mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Build the fixed claim catalogue every semantic trial must use."""

    claims: list[dict[str, object]] = []
    seen: set[str] = set()
    for mapping in trace_mappings:
        requirement_ref = str(mapping.get("requirement_ref") or "approved_requirement_packet").strip()
        design_ids = _strings(mapping.get("design_element_ids"))
        if not design_ids:
            design_id = str(mapping.get("design_element_id") or "").strip()
            design_ids = [design_id] if design_id else []
        code_targets = _strings(mapping.get("planned_code_targets"))
        if not code_targets:
            target = str(mapping.get("planned_code_target") or mapping.get("source_file") or "").strip()
            code_targets = [target] if target else []
        raw_key = "|".join([requirement_ref, *design_ids, *code_targets])
        claim_id = "CODE-" + hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:12].upper()
        if claim_id in seen:
            continue
        seen.add(claim_id)
        claims.append(
            {
                "claim_id": claim_id,
                "requirement_ref": requirement_ref,
                "requirement_content": str(mapping.get("requirement_content") or "").strip(),
                "design_element_ids": design_ids,
                "design_targets": _strings(mapping.get("design_targets")),
                "code_targets": code_targets,
                "test_intents": _strings(mapping.get("test_intents")),
            }
        )
    return claims


def deterministic_typescript_contract_findings(
    *,
    work_packet: dict[str, object],
    ast_evidence: TypeScriptAstEvidence,
) -> list[dict[str, object]]:
    """So implementation signature đã duyệt với exact TypeScript AST evidence."""

    contract = work_packet.get("implementation_signature_contract")
    if not isinstance(contract, dict):
        raise HarnessError(
            "implementation_signature_contract_invalid",
            "Profiled V2 work packet is missing its implementation signature contract.",
        )
    if contract.get("version") != IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION:
        raise HarnessError(
            "implementation_signature_contract_invalid",
            "Implementation signature contract version is invalid.",
        )
    if contract.get("digest") != implementation_signature_contract_digest(contract):
        raise HarnessError(
            "implementation_signature_contract_invalid",
            "Implementation signature contract digest is invalid.",
        )
    entries = contract.get("entries")
    if not isinstance(entries, list):
        raise HarnessError(
            "implementation_signature_contract_invalid",
            "Implementation signature contract entries must be an array.",
        )

    findings: list[dict[str, object]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise HarnessError(
                "implementation_signature_contract_invalid",
                "Implementation signature contract contains an invalid entry.",
            )
        source_path = str(raw.get("source_path") or "").strip().replace("\\", "/")
        qualified_name = str(raw.get("qualified_name") or "").strip()
        symbol_kind = str(raw.get("symbol_kind") or "").strip()
        design_element_id = str(raw.get("design_element_id") or "").strip()
        if not source_path or not qualified_name or symbol_kind not in {"function", "method"}:
            raise HarnessError(
                "implementation_signature_contract_invalid",
                "Implementation signature contract entry identity is invalid.",
            )
        actual = ast_evidence.signatures_for(
            source_path,
            qualified_name=qualified_name,
            symbol_kind=symbol_kind,
        )
        requirement_ref = _requirement_for_design(work_packet, design_element_id)
        if not actual:
            finding = {
                "type": "missing_code_symbol",
                "blocking": True,
                "owner": "developer",
                "recommended_action": "developer_repair",
                "message": (
                    f"Approved signature contract requires {qualified_name} in {source_path}, "
                    "but exact qualified AST evidence is absent."
                ),
                "requirement_ref": requirement_ref,
                "design_element_id": design_element_id,
                "code_ref": f"{source_path}#{qualified_name}",
                "evidence_ref": str(raw.get("contract_id") or IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION),
                "evidence_quote": _signature_text(raw),
                "expected_signature": raw,
                "actual_signatures": [],
            }
            finding["canonical_key"] = canonical_finding_key(finding)
            findings.append(finding)
            continue

        if any(_typescript_signature_matches(expected=raw, actual=item) for item in actual):
            continue
        finding = {
            "type": "contract_signature_mismatch",
            "blocking": True,
            "owner": "developer",
            "recommended_action": "developer_repair",
            "message": (
                f"Approved TypeScript signature {_signature_text(raw)} does not match "
                f"the declarations in {source_path}."
            ),
            "requirement_ref": requirement_ref,
            "design_element_id": design_element_id,
            "code_ref": f"{source_path}#{qualified_name}",
            "evidence_ref": str(raw.get("contract_id") or IMPLEMENTATION_SIGNATURE_CONTRACT_VERSION),
            "evidence_quote": _signature_text(raw),
            "expected_signature": raw,
            "actual_signatures": [dict(item) for item in actual],
        }
        finding["canonical_key"] = canonical_finding_key(finding)
        findings.append(finding)
    return _dedupe_findings(findings)


def _signature_text(entry: dict[str, object]) -> str:
    parameters = []
    for item in _dicts(entry.get("parameters")):
        prefix = "..." if item.get("rest") is True else ""
        suffix = "?" if item.get("optional") is True else ""
        parameters.append(
            f"{prefix}{str(item.get('name') or '').strip()}{suffix}: "
            f"{str(item.get('type') or '').strip()}"
        )
    type_parameters = _strings(entry.get("type_parameters"))
    generic = f"<{', '.join(type_parameters)}>" if type_parameters else ""
    async_prefix = (
        "async "
        if entry.get("async") is True and str(entry.get("owner_kind") or "") != "interface"
        else ""
    )
    return (
        f"{async_prefix}{entry.get('qualified_name')}{generic}"
        f"({', '.join(parameters)}): {entry.get('return_type')}"
    )


def _typescript_signature_matches(
    *,
    expected: dict[str, object],
    actual: dict[str, object],
) -> bool:
    """So shape chính xác, riêng interface dùng Promise thay cho modifier async."""

    expected_shape = signature_shape(expected)
    actual_shape = signature_shape(actual)
    if str(expected.get("owner_kind") or "").strip().lower() == "interface":
        expected_shape = {**expected_shape, "async": False}
    return actual_shape == expected_shape


def snapshot_fingerprint(
    *,
    work_packet: dict[str, object],
    code_snippets: list[dict[str, object]],
    approved_context: dict[str, object],
    verification_evidence: dict[str, object],
    symbol_evidence: dict[str, object] | None = None,
) -> str:
    payload = {
        "requirement_contract": work_packet.get("requirement_contract"),
        "canonical_requirements": work_packet.get("canonical_requirements"),
        "implementation_steps": work_packet.get("implementation_steps"),
        "implementation_signature_contract": work_packet.get(
            "implementation_signature_contract"
        ),
        "code": [
            {"path": item.get("path"), "content": item.get("content"), "chars_total": item.get("chars_total")}
            for item in code_snippets
        ],
        "approved_refs": {
            key: value.get("artifact_path")
            for key, value in approved_context.items()
            if isinstance(value, dict) and value.get("artifact_path")
        },
        "verification": verification_evidence,
        "symbol_evidence": symbol_evidence or {},
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_finding_ledger(
    *,
    previous: dict[str, object] | None,
    snapshot_id: str,
    active_findings: list[dict[str, object]],
    disputed_findings: list[dict[str, object]],
    rejected_findings: list[dict[str, object]],
) -> dict[str, object]:
    previous_entries = {
        str(item.get("canonical_key") or ""): dict(item)
        for item in _dicts((previous or {}).get("entries"))
        if str(item.get("canonical_key") or "")
    }
    current: dict[str, tuple[str, dict[str, object]]] = {}
    for status, values in (
        ("active", active_findings),
        ("disputed", disputed_findings),
        ("rejected", rejected_findings),
    ):
        for item in values:
            key = str(item.get("canonical_key") or canonical_finding_key(item))
            current[key] = (status, {**item, "canonical_key": key})
    entries: list[dict[str, object]] = []
    for key in sorted(set(previous_entries) | set(current)):
        prior = previous_entries.get(key, {})
        if key in current:
            status, finding = current[key]
            if status == "active" and prior.get("status") == "resolved":
                status = "regressed"
            entries.append(
                {
                    **prior,
                    **finding,
                    "canonical_key": key,
                    "status": status,
                    "first_seen_snapshot": prior.get("first_seen_snapshot") or snapshot_id,
                    "last_seen_snapshot": snapshot_id,
                    "occurrences": int(prior.get("occurrences") or 0) + 1,
                }
            )
        else:
            entries.append(
                {
                    **prior,
                    "canonical_key": key,
                    "status": "resolved",
                    "resolved_snapshot": snapshot_id,
                    "last_seen_snapshot": snapshot_id,
                }
            )
    return {
        "version": "implementation_validation_ledger_v1",
        "snapshot_fingerprint": snapshot_id,
        "entries": entries,
        "counts": {
            status: sum(1 for item in entries if item.get("status") == status)
            for status in ("active", "resolved", "disputed", "rejected", "regressed")
        },
    }


def canonical_finding_key(item: dict[str, object]) -> str:
    parts = [
        str(item.get("claim_id") or "").strip(),
        str(item.get("requirement_ref") or "").strip(),
        str(item.get("design_element_id") or "").strip(),
        str(item.get("code_ref") or item.get("source_file") or "").strip().replace("\\", "/"),
        str(item.get("type") or "").strip(),
    ]
    return "|".join(part.casefold() for part in parts)


def _requirement_for_design(work_packet: dict[str, object], design_element_id: str) -> str:
    for item in _dicts(work_packet.get("requirement_coverage")):
        if design_element_id in _strings(item.get("design_element_ids")):
            return str(item.get("requirement_id") or "approved_requirement_packet")
    return "approved_requirement_packet"


def _dedupe_findings(values: list[dict[str, object]]) -> list[dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in values:
        key = str(item.get("canonical_key") or canonical_finding_key(item))
        result[key] = {**item, "canonical_key": key}
    return list(result.values())

