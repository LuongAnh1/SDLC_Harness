"""Deterministic contract evidence and stable finding mechanics for Phase 4."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any


JAVA_METHOD_RE = re.compile(
    r"(?ms)^[ \t]*(?:@[\w.]+(?:\([^\n]*?\))?[ \t]*\n[ \t]*)*"
    r"(?:(?:public|protected|private|static|final|abstract|default|synchronized|native|strictfp)[ \t]+)*"
    r"(?:<[^>{};]+>[ \t]+)?"
    r"(?P<return>[A-Za-z_$][\w$.,<>? \t\[\]]*)[ \t]+"
    r"(?P<name>[A-Za-z_$][\w$]*)[ \t]*\((?P<params>[^)]*)\)[ \t]*"
    r"(?:throws[^{;]+)?[ \t]*(?:\{|;)",
)


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


def deterministic_contract_findings(
    *,
    target_path: Path,
    work_packet: dict[str, object],
    approved_context: dict[str, object],
) -> list[dict[str, object]]:
    """Compare approved Java method contracts with declarations in the worktree."""

    graph = approved_context.get("class_diagram_graph_data")
    if not isinstance(graph, dict):
        return []
    proposed_paths = {
        str(item.get("path") or "").replace("\\", "/")
        for item in _dicts(work_packet.get("proposed_files"))
        if str(item.get("path") or "").endswith(".java")
    }
    findings: list[dict[str, object]] = []
    for node in _dicts(graph.get("nodes")):
        class_name = str(node.get("className") or node.get("id") or "").strip()
        if not class_name:
            continue
        source_path = _source_path_for_class(class_name, proposed_paths)
        if not source_path:
            continue
        source_file = target_path / source_path
        if not source_file.is_file():
            continue
        source_text = source_file.read_text(encoding="utf-8", errors="replace")
        # Exact parameter matching is a hard gate only at explicit Java interface
        # boundaries. Concrete helper methods may intentionally use accumulators or
        # richer return types while preserving the approved observable behavior.
        if not re.search(rf"\binterface\s+{re.escape(class_name)}\b", source_text):
            continue
        methods = _java_methods(source_text)
        for method in _dicts(node.get("methods")):
            method_name = str(method.get("name") or "").strip()
            if not method_name:
                continue
            expected_params = [
                _normalize_java_type(str(item.get("type") or ""))
                for item in _dicts(method.get("parameters"))
            ]
            expected_return = _normalize_java_type(str(method.get("returnType") or "void"))
            same_name = [item for item in methods if item[0] == method_name]
            if not same_name:
                continue  # Existing static symbol evidence owns missing methods.
            # Java permits callers to ignore a returned value, while generated UML often
            # records such operations as void. Parameter shape is the stable call contract;
            # return-type-only differences remain semantic observations for the LLM.
            exact = any(params == expected_params for _name, params, _result in same_name)
            if exact:
                continue
            actual = [f"{method_name}({', '.join(params)}): {result}" for _name, params, result in same_name]
            design_element_id = f"method:{class_name}.{method_name}"
            message = (
                f"Approved method contract requires {method_name}({', '.join(expected_params)}): "
                f"{expected_return}, but {source_path} declares {', '.join(actual)}."
            )
            finding = {
                "type": "contract_signature_mismatch",
                "blocking": True,
                "owner": "developer",
                "recommended_action": "developer_repair",
                "message": message,
                "requirement_ref": _requirement_for_design(work_packet, design_element_id),
                "design_element_id": design_element_id,
                "code_ref": f"{source_path}#{method_name}",
                "evidence_ref": "approved_class_diagram_graph",
                "evidence_quote": f"{method_name}({', '.join(expected_params)}): {expected_return}",
                "expected_signature": {
                    "name": method_name,
                    "parameters": expected_params,
                    "return_type": expected_return,
                },
                "actual_signatures": actual,
            }
            finding["canonical_key"] = canonical_finding_key(finding)
            findings.append(finding)
    return _dedupe_findings(findings)


def prepare_semantic_trial(
    *,
    findings: list[dict[str, object]],
    observations: list[dict[str, object]],
    claims: list[dict[str, object]],
    verification_evidence: dict[str, object],
    trial_id: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Bind findings to fixed claims and reject claims contradicted by Verification."""

    accepted: list[dict[str, object]] = []
    normalized_observations = [dict(item) for item in observations]
    rejected: list[dict[str, object]] = []
    for raw in findings:
        item = dict(raw)
        item["trial_id"] = trial_id
        claim_id = _resolve_claim_id(item, claims)
        if _contradicts_verification(item, verification_evidence):
            item.update(
                {
                    "blocking": False,
                    "owner": "validator",
                    "recommended_action": "validator_mapping_fix",
                    "normalization_reason": "contradicted_by_executed_verification_evidence",
                }
            )
            rejected.append(item)
            normalized_observations.append(item)
            continue
        if not claim_id and str(item.get("type") or "") == "trace_mapping_issue":
            # This finding targets Validator's own approved-evidence mapping,
            # not a Developer implementation claim. The upstream normalizer
            # has already required an exact quote from approved context, so it
            # may remain blocking without being sent to Developer.
            claim_id = "VALIDATOR-MAPPING"
        if not claim_id:
            item.update(
                {
                    "blocking": False,
                    "owner": "validator",
                    "recommended_action": "validator_mapping_fix",
                    "normalization_reason": "finding_not_bound_to_fixed_claim",
                }
            )
            rejected.append(item)
            normalized_observations.append(item)
            continue
        item["claim_id"] = claim_id
        item["canonical_key"] = canonical_finding_key(item)
        accepted.append(item)
    return _dedupe_findings(accepted), normalized_observations, rejected


def aggregate_semantic_trials(
    *,
    trials: list[dict[str, object]],
) -> dict[str, object]:
    """Aggregate adaptive trials by canonical finding key."""

    quorum = len(trials) // 2 + 1
    votes: dict[str, list[dict[str, object]]] = defaultdict(list)
    key_sets: list[set[str]] = []
    for trial in trials:
        items = _dicts(trial.get("blocking_findings"))
        key_set: set[str] = set()
        for item in items:
            key = str(item.get("canonical_key") or canonical_finding_key(item))
            item = {**item, "canonical_key": key}
            votes[key].append(item)
            key_set.add(key)
        key_sets.append(key_set)
    stable: list[dict[str, object]] = []
    disputed: list[dict[str, object]] = []
    for key, items in votes.items():
        representative = max(items, key=lambda item: len(str(item.get("evidence_quote") or "")))
        payload = {
            **representative,
            "canonical_key": key,
            "votes": len(items),
            "quorum": quorum,
            "trial_ids": sorted({str(item.get("trial_id") or "") for item in items}),
        }
        if len(items) >= quorum:
            stable.append(payload)
        else:
            payload.update(
                {
                    "blocking": False,
                    "owner": "validator",
                    "recommended_action": "validator_mapping_fix",
                    "normalization_reason": "semantic_minority_without_quorum",
                }
            )
            disputed.append(payload)
    pairwise = []
    for left in range(len(key_sets)):
        for right in range(left + 1, len(key_sets)):
            union = key_sets[left] | key_sets[right]
            score = 1.0 if not union else len(key_sets[left] & key_sets[right]) / len(union)
            pairwise.append({"left": left + 1, "right": right + 1, "jaccard": round(score, 4)})
    consistency = (
        sum(float(item["jaccard"]) for item in pairwise) / len(pairwise)
        if pairwise
        else 1.0
    )
    return {
        "stable_blockers": sorted(stable, key=lambda item: str(item.get("canonical_key"))),
        "disputed_findings": sorted(disputed, key=lambda item: str(item.get("canonical_key"))),
        "quorum": quorum,
        "valid_trials": len(trials),
        "pairwise_jaccard": pairwise,
        "consistency_score": round(consistency, 4),
    }


def trial_blocker_keys(trial: dict[str, object]) -> set[str]:
    return {
        str(item.get("canonical_key") or canonical_finding_key(item))
        for item in _dicts(trial.get("blocking_findings"))
    }


def snapshot_fingerprint(
    *,
    work_packet: dict[str, object],
    code_snippets: list[dict[str, object]],
    approved_context: dict[str, object],
    verification_evidence: dict[str, object],
) -> str:
    payload = {
        "requirement_contract": work_packet.get("requirement_contract"),
        "canonical_requirements": work_packet.get("canonical_requirements"),
        "implementation_steps": work_packet.get("implementation_steps"),
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


def _resolve_claim_id(item: dict[str, object], claims: list[dict[str, object]]) -> str:
    explicit = str(item.get("claim_id") or "").strip()
    valid_ids = {str(claim.get("claim_id") or "") for claim in claims}
    if explicit in valid_ids:
        return explicit
    requirement_ref = str(item.get("requirement_ref") or "").strip()
    design_id = str(item.get("design_element_id") or "").strip()
    code_ref = str(item.get("code_ref") or "").split("#", 1)[0].replace("\\", "/")
    scored: list[tuple[int, str]] = []
    for claim in claims:
        score = 0
        if requirement_ref and requirement_ref == str(claim.get("requirement_ref") or ""):
            score += 4
        if design_id and design_id in _strings(claim.get("design_element_ids")):
            score += 3
        if code_ref and any(str(target).split("#", 1)[0] == code_ref for target in _strings(claim.get("code_targets"))):
            score += 2
        if score:
            scored.append((score, str(claim.get("claim_id") or "")))
    return max(scored)[1] if scored else ""


def _contradicts_verification(item: dict[str, object], evidence: dict[str, object]) -> bool:
    message = str(item.get("message") or "").casefold()
    markers = ("not executed", "never executed", "not run", "no test runner", "main()-only", "main-only")
    if not any(marker in message for marker in markers):
        return False
    executed = [str(value).casefold() for value in evidence.get("executed_test_entrypoints", []) if str(value)]
    code_ref = str(item.get("code_ref") or "").replace("\\", "/")
    test_name = Path(code_ref.split("#", 1)[0]).stem.casefold()
    return bool(test_name and any(value.endswith(test_name) or test_name in value for value in executed))


def _java_methods(source: str) -> list[tuple[str, list[str], str]]:
    cleaned = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    cleaned = re.sub(r"//[^\n]*", "", cleaned)
    methods: list[tuple[str, list[str], str]] = []
    for match in JAVA_METHOD_RE.finditer(cleaned):
        name = match.group("name")
        result = _normalize_java_type(match.group("return"))
        if result in {"return", "throw", "new"}:
            continue
        params = [_parameter_type(value) for value in _split_java_params(match.group("params"))]
        methods.append((name, params, result))
    return methods


def _split_java_params(raw: str) -> list[str]:
    if not raw.strip():
        return []
    values: list[str] = []
    current: list[str] = []
    depth = 0
    for char in raw:
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            values.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        values.append("".join(current).strip())
    return values


def _parameter_type(raw: str) -> str:
    value = re.sub(r"@[\w.]+(?:\([^)]*\))?", "", raw)
    value = re.sub(r"\b(?:final|volatile|transient)\b", "", value).strip()
    parts = value.rsplit(None, 1)
    return _normalize_java_type(parts[0] if len(parts) == 2 else value)


def _normalize_java_type(raw: str) -> str:
    value = re.sub(r"\s+", "", raw or "")
    value = value.replace("...", "[]")
    value = re.sub(r"\b(?:java\.lang|java\.util)\.", "", value)
    return value


def _source_path_for_class(class_name: str, paths: set[str]) -> str:
    suffix = f"/{class_name}.java"
    matches = sorted(path for path in paths if path.endswith(suffix) or path == f"{class_name}.java")
    return matches[0] if len(matches) == 1 else ""


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


def _dicts(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
