"""Closure Validator (L5.3.3): semantic review of runtime_closure_contract_v1.

The Validator judges what Harness cannot: whether the inputs are complete, providers make
sense, session wiring verifies identity at the approved level, probes prove what they
cite, demo fixtures stay out of production and unresolved inputs are justified. Harness
checks the answer mechanically: the exact reviewed input set, known cited IDs and the
finding shape. Pass/fail is derived from blocker findings, never from a free `ok` flag.
"""

from __future__ import annotations

import json
from typing import Any

from ...core.errors import HarnessError
from ..developer.closure_contract import CLOSURE_EXECUTION_FACTS, CLOSURE_REVIEW_DUTIES
from ..developer.closure_planner import _parse_json_object, call_structured_json

CLOSURE_REVIEW_PROMPT_MARKER = "Runtime Closure Contract Review v1"
CLOSURE_CODE_REVIEW_PROMPT_MARKER = "Runtime Closure Code Review v1"
CLOSURE_REVIEW_CONTRACT_INVALID = "runtime_closure_review_contract_invalid"
FINDING_SEVERITIES = ("blocker", "warning")
# A blocker in this category means the closure cannot pass without changing frozen
# Requirement/Design/Plan; Harness stops instead of routing it to repair.
FROZEN_CONTRACT_CONFLICT_CATEGORY = "frozen_contract_conflict"
FINDING_CATEGORIES = (
    "input_completeness",
    "provider_validity",
    "session_verification",
    "probe_evidence",
    "demo_isolation",
    "demo_data_sufficiency",
    "unresolved_justification",
    "approved_behavior_change",
    FROZEN_CONTRACT_CONFLICT_CATEGORY,
)
_CONTRACT_REF_FIELDS = ("input_ids", "unit_ids", "probe_ids", "stand_in_ids", "decision_ids")
_REVIEW_OUTPUT_SCHEMA = {
    "summary": "...",
    "reviewed_input_ids": ["RI-..."],
    "findings": [
        {
            "severity": "|".join(FINDING_SEVERITIES),
            "category": "|".join(FINDING_CATEGORIES),
            "input_ids": ["RI-..."],
            "unit_ids": ["CU-..."],
            "probe_ids": ["RP-..."],
            "stand_in_ids": ["VS-..."],
            "decision_ids": ["CD-..."],
            "refs": ["PRE-...|MF-...|VDN-...|SC-..."],
            "issue": "...",
            "expected_change": "...",
        }
    ],
}

_REVIEW_SYSTEM = (
    "You are the SDLC Harness Closure Validator. Requirement, Design and Plan are frozen. Judge the meaning of a "
    "runtime closure contract that already passed every mechanical gate. Return one JSON object only."
)

_REVIEW_DUTIES = CLOSURE_REVIEW_DUTIES


class RuntimeClosureValidator:
    """One semantic review call on an assembled contract."""

    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def review_contract(
        self,
        *,
        contract: dict[str, object],
        validator_view: dict[str, object],
        known_refs: set[str],
        previous_round: dict[str, object] | None = None,
        run_id: str = "",
    ) -> dict[str, object]:
        prompt = closure_review_prompt(contract=contract, validator_view=validator_view, previous_round=previous_round)
        result = call_structured_json(
            self.llm_client,
            system=_REVIEW_SYSTEM,
            prompt=prompt,
            profile_name="closure_validator",
            parse=lambda raw: parse_closure_review(raw, contract=contract, known_refs=known_refs),
            contract_error_code=CLOSURE_REVIEW_CONTRACT_INVALID,
            label="Closure Validator",
            run_id=run_id or str(contract.get("run_id") or ""),
        )
        return {**result["output"], "attempts": result["attempts"]}

    def review_code(
        self,
        *,
        contract: dict[str, object],
        code_view: dict[str, object],
        known_refs: set[str],
        run_id: str = "",
    ) -> dict[str, object]:
        """Closure code review: the executed closure against its approved contract and probe evidence."""

        payload = {
            "prompt_kind": CLOSURE_CODE_REVIEW_PROMPT_MARKER,
            "duties": [
                *_REVIEW_DUTIES,
                "Judge the written closure code and the probe evidence, not only the contract text.",
            ],
            "execution_facts": list(CLOSURE_EXECUTION_FACTS),
            "rules": [
                "accepted_contract_warnings are the Closure Validator warnings the owner accepted by approving this "
                "contract. Never raise one of them, or a choice the approved contract itself makes, as a blocker. A "
                "blocker needs written code or probe evidence that departs from the approved contract or breaks approved "
                "behavior. Repeat an accepted concern only as a warning, and only when the written code makes it worse.",
                "Closure verification passed: services booted and every approved probe answered as expected. "
                "Judge meaning only: whether each provider truly supplies its input, whether demo data and stand-ins "
                "stay in verification scope, and whether approved behavior is unchanged.",
                "Cite exact contract IDs (input_ids, unit_ids, probe_ids, stand_in_ids, decision_ids) and, in refs, exact "
                "Requirement/Design IDs. Every finding cites at least one contract ID.",
                "severity=blocker only when the closure cannot be accepted as written; otherwise warning.",
                f"Use category `{FROZEN_CONTRACT_CONFLICT_CATEGORY}` only when passing would require changing the frozen "
                "Requirement, Design or Plan; Harness then stops for the owner instead of repairing.",
                "reviewed_input_ids must list every runtime input id of the contract exactly once.",
            ],
            "output_schema": _REVIEW_OUTPUT_SCHEMA,
            "contract": contract,
            **code_view,
        }
        result = call_structured_json(
            self.llm_client,
            system=_REVIEW_SYSTEM,
            prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            profile_name="closure_validator",
            parse=lambda raw: parse_closure_review(raw, contract=contract, known_refs=known_refs),
            contract_error_code=CLOSURE_REVIEW_CONTRACT_INVALID,
            label="Closure code review",
            run_id=run_id or str(contract.get("run_id") or ""),
        )
        return {**result["output"], "attempts": result["attempts"]}


def closure_review_prompt(
    *,
    contract: dict[str, object],
    validator_view: dict[str, object],
    previous_round: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "prompt_kind": CLOSURE_REVIEW_PROMPT_MARKER,
        "duties": list(_REVIEW_DUTIES),
        "execution_facts": list(CLOSURE_EXECUTION_FACTS),
        "rules": [
            "The contract already passed Harness gates for schema, IDs, compatibility, ownership, paths and "
            "coverage. Do not re-report those; judge meaning only.",
            "Cite exact contract IDs (input_ids, unit_ids, probe_ids, stand_in_ids, decision_ids) and, in refs, exact "
            "Requirement/Design IDs from the context. Every finding cites at least one contract ID.",
            "severity=blocker only when the contract cannot be approved as written; otherwise warning.",
            "expected_change states what must become true in the contract; it must not change frozen Requirement, "
            "Design or Plan.",
            "reviewed_input_ids must list every runtime input id of the contract exactly once.",
        ],
        "output_schema": _REVIEW_OUTPUT_SCHEMA,
        "contract": contract,
        **validator_view,
    }
    if previous_round:
        payload["previous_round"] = {
            "instruction": "Re-check the previous blocking findings first, then review the whole contract again.",
            **previous_round,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def parse_closure_review(
    raw: str,
    *,
    contract: dict[str, object],
    known_refs: set[str],
) -> dict[str, object]:
    payload = _parse_json_object(raw)
    input_ids = [str(item.get("input_id") or "") for item in _dicts(contract.get("runtime_inputs"))]
    contract_ids = {
        "input_ids": set(input_ids),
        "unit_ids": {str(item.get("unit_id") or "") for item in _dicts(contract.get("closure_units"))},
        "probe_ids": {str(item.get("probe_id") or "") for item in _dicts(contract.get("runtime_probes"))},
        "stand_in_ids": {str(item.get("stand_in_id") or "") for item in _dicts(contract.get("verification_stand_ins"))},
        "decision_ids": {str(item.get("id") or "") for item in _dicts(contract.get("demo_decisions"))},
    }
    problems: list[str] = []
    reviewed = payload.get("reviewed_input_ids")
    if not isinstance(reviewed, list) or sorted(str(item) for item in reviewed) != sorted(input_ids):
        problems.append(f"reviewed_input_ids must be exactly {sorted(input_ids)}.")
    findings_value = payload.get("findings")
    if not isinstance(findings_value, list):
        problems.append("`findings` must be a list.")
        findings_value = []
    findings: list[dict[str, object]] = []
    for index, item in enumerate(findings_value, start=1):
        label = f"findings[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{label} must be an object.")
            continue
        finding = {
            "severity": str(item.get("severity") or "").strip(),
            "category": str(item.get("category") or "").strip(),
            **{field: _string_list(item.get(field)) for field in (*_CONTRACT_REF_FIELDS, "refs")},
            "issue": str(item.get("issue") or "").strip(),
            "expected_change": str(item.get("expected_change") or "").strip(),
        }
        if finding["severity"] not in FINDING_SEVERITIES:
            problems.append(f"{label}.severity must be one of {list(FINDING_SEVERITIES)}.")
        if finding["category"] not in FINDING_CATEGORIES:
            problems.append(f"{label}.category must be one of {list(FINDING_CATEGORIES)}.")
        if not finding["issue"] or not finding["expected_change"]:
            problems.append(f"{label} needs issue and expected_change.")
        if not any(finding[field] for field in _CONTRACT_REF_FIELDS):
            problems.append(f"{label} must cite at least one contract ID.")
        for field in _CONTRACT_REF_FIELDS:
            unknown = sorted(set(finding[field]) - contract_ids[field])
            if unknown:
                problems.append(f"{label}.{field} cites unknown IDs {unknown}.")
        unknown_refs = sorted(set(finding["refs"]) - known_refs)
        if unknown_refs:
            problems.append(f"{label}.refs cites unknown Requirement/Design IDs {unknown_refs}.")
        findings.append(finding)
    if problems:
        raise HarnessError(CLOSURE_REVIEW_CONTRACT_INVALID, " ".join(problems))
    blockers = [item for item in findings if item["severity"] == "blocker"]
    return {
        "ok": not blockers,
        "summary": str(payload.get("summary") or "").strip(),
        "reviewed_input_ids": sorted(input_ids),
        "findings": findings,
        "blocking_findings": blockers,
        "warnings": [item for item in findings if item["severity"] == "warning"],
    }


def _string_list(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _dicts(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
