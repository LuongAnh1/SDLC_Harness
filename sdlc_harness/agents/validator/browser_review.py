"""Semantic browser-stage reviews; deterministic gates stay in the Harness."""

from __future__ import annotations

import json
import re
from typing import Any

from ...core.errors import HarnessError
from ..developer.closure_planner import _parse_json_object, call_structured_json
from ..developer.polish_planner import IMAGE_INPUTS_RULE, POLISH_INCONCLUSIVE, image_inputs


POLISH_REVIEW_PROMPT_MARKER = "UI Polish Contract Review v1"
POLISH_RESULT_REVIEW_PROMPT_MARKER = "UI Polish Result Review v1"
BROWSER_QA_REVIEW_PROMPT_MARKER = "Browser QA Scenario Review v1"
JOURNEY_EXECUTION_REVIEW_PROMPT_MARKER = "Browser Journey Execution Review v1"
POLISH_REVIEW_CONTRACT_INVALID = "ui_polish_review_contract_invalid"
BROWSER_QA_REVIEW_CONTRACT_INVALID = "browser_qa_review_contract_invalid"
BROWSER_QA_INCONCLUSIVE = "browser_qa_inconclusive"
JOURNEY_EXECUTION_REVIEW_CONTRACT_INVALID = "browser_journey_review_contract_invalid"
JOURNEY_EXECUTION_INCONCLUSIVE = "browser_journey_review_inconclusive"
POLISH_FINDING_SEVERITIES = ("blocker", "warning")
POLISH_FINDING_CATEGORIES = ("scope", "behavior", "visual_authority", "design_conflict")
BROWSER_QA_VERDICTS = ("satisfied", "violated", "not_observable")
BROWSER_QA_FINDING_CATEGORIES = ("implementation", "journey_defect", "design_conflict")
JOURNEY_UNREACHABLE_VERDICTS = ("justified", "needs_journey")

_SYSTEM = (
    "You are the SDLC Harness UI Polish Validator. The contract already passed deterministic gates. Judge whether "
    "its scope is sensible, remains presentation-only and follows approved visual authority. Return one JSON object only."
)

_OUTPUT_SCHEMA = {
    "summary": "...",
    "reviewed_unit_ids": ["UP-..."],
    "findings": [
        {
            "severity": "blocker|warning",
            "category": "scope|behavior|visual_authority|design_conflict",
            "unit_ids": ["UP-..."],
            "evidence_refs": ["baseline:V-0:desktop:s00"],
            "authority_refs": ["primary|VDN-..."],
            "issue": "...",
            "expected_change": "...",
        }
    ],
    "observations": ["..."],
}

_POLISH_GUIDANCE_RULE = (
    "uiux_guidance is ui-ux-pro-max advice, not authority: never cite it in authority_refs and never block only "
    "because a guidance row is not applied. Judge against Design, the visual profile and the captures; guidance may "
    "explain a finding or observation."
)

_BROWSER_QA_SYSTEM = (
    "You are the SDLC Harness Browser QA Validator. Judge one runnable scenario from typed obligations and browser "
    "evidence. Text evidence is authoritative for semantics and state; images are context only. Return one JSON object."
)

_BROWSER_QA_OUTPUT_SCHEMA = {
    "summary": "...",
    "reviewed_obligation_ids": ["VDN-...:..."],
    "verdicts": [
        {
            "obligation_id": "VDN-...:...",
            "verdict": "satisfied|violated|not_observable",
            "evidence_refs": ["qa:BV-...:desktop:s00"],
        }
    ],
    "findings": [
        {
            "severity": "blocker|warning",
            "category": "implementation|journey_defect|design_conflict",
            "obligation_ids": ["VDN-...:..."],
            "unit_ids": ["WU-..."],
            "evidence_refs": ["qa:BV-...:desktop:s00"],
            "issue": "...",
            "expected_change": "...",
        }
    ],
    "observations": ["..."],
}

_JOURNEY_EXECUTION_OUTPUT_SCHEMA = {
    "summary": "...",
    "reviewed_view_ids": ["BV-..."],
    "reviewed_not_reachable_obligation_ids": ["VDN-...:..."],
    "view_evidence": [
        {
            "view_id": "BV-...",
            "evidence_refs": ["baseline:BV-...:desktop:s00"],
        }
    ],
    "obligation_verdicts": [
        {
            "obligation_id": "VDN-...:...",
            "verdict": "satisfied|violated|not_observable",
            "evidence_refs": ["baseline:BV-...:desktop:s00"],
        }
    ],
    "not_reachable_verdicts": [
        {
            "obligation_id": "VDN-...:...",
            "verdict": "justified|needs_journey",
            "reason": "needs_journey only: the supplied typed step, demo data or execution fact that exposes it",
        }
    ],
    "findings": [
        {
            "severity": "blocker|warning",
            "category": "implementation|journey_defect|design_conflict",
            "view_ids": ["BV-..."],
            "obligation_ids": ["VDN-...:..."],
            "unit_ids": ["WU-..."],
            "evidence_refs": ["baseline:BV-...:desktop:s00"],
            "issue": "...",
            "expected_change": "...",
        }
    ],
    "observations": ["..."],
}


class UiPolishValidator:
    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def review_contract(
        self,
        *,
        contract: dict[str, object],
        validator_context: dict[str, object],
        evidence_refs: set[str],
        authority_refs: set[str],
        images: list[dict[str, object]],
        previous_round: dict[str, object] | None = None,
        run_id: str = "",
    ) -> dict[str, object]:
        result = call_structured_json(
            self.llm_client,
            system=_SYSTEM,
            prompt=polish_review_prompt(
                contract=contract,
                validator_context=validator_context,
                previous_round=previous_round,
                images=images,
            ),
            profile_name="polish_validator",
            parse=lambda raw: parse_polish_review(
                raw,
                contract=contract,
                evidence_refs=evidence_refs,
                authority_refs=authority_refs,
            ),
            contract_error_code=POLISH_REVIEW_CONTRACT_INVALID,
            label="UI Polish Validator",
            run_id=run_id,
            inconclusive_code=POLISH_INCONCLUSIVE,
            images=images,
        )
        return {**result["output"], "attempts": result["attempts"]}

    def review_result(
        self,
        *,
        contract: dict[str, object],
        result_context: dict[str, object],
        evidence_refs: set[str],
        visual_evidence_refs: set[str],
        authority_refs: set[str],
        images: list[dict[str, object]],
        run_id: str = "",
    ) -> dict[str, object]:
        result = call_structured_json(
            self.llm_client,
            system=_SYSTEM,
            prompt=polish_result_review_prompt(contract=contract, result_context=result_context, images=images),
            profile_name="polish_validator",
            parse=lambda raw: parse_polish_review(
                raw,
                contract=contract,
                evidence_refs=evidence_refs,
                visual_evidence_refs=visual_evidence_refs,
                authority_refs=authority_refs,
            ),
            contract_error_code=POLISH_REVIEW_CONTRACT_INVALID,
            label="UI Polish Result Validator",
            run_id=run_id,
            inconclusive_code=POLISH_INCONCLUSIVE,
            images=images,
        )
        return {**result["output"], "attempts": result["attempts"]}


class BrowserQaValidator:
    def __init__(self, llm_client: Any):
        self.llm_client = llm_client

    def review_scenario(
        self,
        *,
        scenario: dict[str, object],
        review_context: dict[str, object],
        evidence_refs: set[str],
        ownership: dict[str, set[str]],
        images: list[dict[str, object]],
        run_id: str = "",
    ) -> dict[str, object]:
        result = call_structured_json(
            self.llm_client,
            system=_BROWSER_QA_SYSTEM,
            prompt=browser_qa_review_prompt(
                scenario=scenario,
                review_context=review_context,
                images=images,
            ),
            profile_name="browser_qa_validator",
            parse=lambda raw: parse_browser_qa_review(
                raw,
                scenario=scenario,
                evidence_refs=evidence_refs,
                ownership=ownership,
            ),
            contract_error_code=BROWSER_QA_REVIEW_CONTRACT_INVALID,
            label="Browser QA Validator",
            run_id=run_id,
            inconclusive_code=BROWSER_QA_INCONCLUSIVE,
            images=images,
        )
        return {**result["output"], "attempts": result["attempts"]}

    def review_journey_scenario(
        self,
        *,
        scenario: dict[str, object],
        review_context: dict[str, object],
        evidence_refs: set[str],
        candidate_unit_ids: set[str],
        images: list[dict[str, object]],
        run_id: str = "",
    ) -> dict[str, object]:
        result = call_structured_json(
            self.llm_client,
            system=_BROWSER_QA_SYSTEM,
            prompt=journey_execution_review_prompt(
                scenario=scenario,
                review_context=review_context,
                images=images,
            ),
            profile_name="browser_qa_validator",
            parse=lambda raw: parse_journey_execution_review(
                raw,
                scenario=scenario,
                evidence_refs=evidence_refs,
                candidate_unit_ids=candidate_unit_ids,
            ),
            contract_error_code=JOURNEY_EXECUTION_REVIEW_CONTRACT_INVALID,
            label="Browser Journey Execution Validator",
            run_id=run_id,
            inconclusive_code=JOURNEY_EXECUTION_INCONCLUSIVE,
            images=images,
            retry_guard=_guard_journey_contract_retry,
            retry_instruction=_journey_contract_retry_instruction,
        )
        return {**result["output"], "attempts": result["attempts"]}


def polish_review_prompt(
    *,
    contract: dict[str, object],
    validator_context: dict[str, object],
    previous_round: dict[str, object] | None = None,
    images: list[dict[str, object]] | None = None,
) -> str:
    payload: dict[str, object] = {
        "prompt_kind": POLISH_REVIEW_PROMPT_MARKER,
        "rules": [
            "Do not repeat mechanical path, ID, hash or dependency checks; the Harness already passed them.",
            "Judge whether each unit is useful visual polish, is narrow enough and cannot alter approved behavior.",
            "Design visual_contract overrides the app profile. Images are primary for visual hierarchy, layout, "
            "spacing and consistency; text evidence remains authoritative for semantics and state.",
            "A visual_authority finding cites at least one supplied evidence ref and one supplied authority ref.",
            "A blocker means the contract must change before code is written. design_conflict is always a warning: "
            "follow the reading that satisfies Requirement and record both Design IDs in observations.",
            *([_POLISH_GUIDANCE_RULE] if validator_context.get("uiux_guidance") else []),
            *([IMAGE_INPUTS_RULE] if images else []),
        ],
        "output_schema": _OUTPUT_SCHEMA,
        "contract": contract,
        "validator_context": validator_context,
        **({"image_inputs": image_inputs(images)} if images else {}),
    }
    if previous_round:
        payload["previous_round"] = previous_round
    return json.dumps(payload, ensure_ascii=False, indent=2)


def polish_result_review_prompt(
    *,
    contract: dict[str, object],
    result_context: dict[str, object],
    images: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "prompt_kind": POLISH_RESULT_REVIEW_PROMPT_MARKER,
            "rules": [
                "The Harness already checked path scope, suite/probes, production demo isolation and baseline view regressions.",
                "Text evidence and aria_diff are authoritative for behavior, role, name, state, focus and overflow; text wins on image conflict.",
                "A record's `same_as` maps a field to the record that shows its identical value; read that field there.",
                "Images are primary for visual hierarchy, layout, spacing, consistency and adherence to visual authority.",
                "A visual_authority finding cites a supplied image evidence ref and an authority ref.",
                "Every blocker cites the exact polish unit_ids that can repair it without widening the approved contract.",
                # r9 (2026-11-16): the same unringed focused control was a blocker in one review and a warning in
                # the next, justified as "no regression versus baseline". A first wording that also made every cited
                # Design visual_contract decision blocker-worthy sent the review into exhaustive "potential issue"
                # lists until max_tokens; severity is bounded by the unit objective instead.
                "Severity follows each unit's objective: a blocker is an outcome the objective names that the after "
                "evidence shows absent or contradicted, fixable inside that unit's write paths; matching the baseline "
                "never excuses it. Anything the objective does not name is at most a warning.",
                "Judge each unit's objective once against its evidence and report only defects; do not enumerate or "
                "re-check details that pass.",
                "Do not promote an accepted contract warning to blocker unless the implementation adds new contradictory evidence.",
                "design_conflict is always a warning: follow the reading that satisfies Requirement and record both Design IDs.",
                *([_POLISH_GUIDANCE_RULE] if result_context.get("uiux_guidance") else []),
                *([IMAGE_INPUTS_RULE] if images else []),
            ],
            "output_schema": _OUTPUT_SCHEMA,
            "contract": contract,
            "result_context": result_context,
            **({"image_inputs": image_inputs(images)} if images else {}),
        },
        # Compact: this prompt carries before and after evidence for every capture, and indentation alone
        # cost about a third of its characters (r9, 2026-11-16).
        ensure_ascii=False,
    )


def browser_qa_review_prompt(
    *,
    scenario: dict[str, object],
    review_context: dict[str, object],
    images: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "prompt_kind": BROWSER_QA_REVIEW_PROMPT_MARKER,
            "rules": [
                "Review every supplied obligation exactly once; never invent or omit an obligation ID.",
                "An obligation's `summary` names its approved trigger or statement and `contract`, when present, the "
                "approved presentation, response, outcome or focus target; judge the evidence against both.",
                "Role, accessible name, state, focus, live-region and overflow verdicts use text/ARIA/layout fields. "
                "If an image conflicts with text, text wins and the conflict is recorded in observations.",
                "Images provide visual context only; they never override text evidence.",
                "A record's `observe` field reports the transient-state locator armed on that step, while the Harness "
                "held network responses: `observed` means the record's text evidence was captured with it on screen; "
                "`missed` means it never became visible, although the step itself ran.",
                "A broken implemented behavior is implementation. A valid behavior that the typed journey cannot "
                "locate because its role, name or action is wrong is journey_defect.",
                "Every violated verdict has a finding citing the obligation, supplied evidence and only the scenario "
                "candidate Plan unit(s) that can repair the observed boundary.",
                "design_conflict is warning-only: follow the reading that satisfies Requirement and record both Design IDs.",
                "not_observable and not_reachable are reported, not blockers.",
            ],
            "output_schema": _BROWSER_QA_OUTPUT_SCHEMA,
            "scenario": scenario,
            "review_context": review_context,
            "image_inputs": [
                {"label": item.get("label"), "sha256": item.get("sha256")}
                for item in images
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def journey_execution_review_prompt(
    *,
    scenario: dict[str, object],
    review_context: dict[str, object],
    images: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "prompt_kind": JOURNEY_EXECUTION_REVIEW_PROMPT_MARKER,
            "rules": [
                "Scope: review only supplied obligations. Explicit state, behavior, focus, accessibility, responsive and "
                "layout obligations are in scope; uncited visual taste belongs only in observations.",
                "An obligation's `summary` names its approved trigger or statement and `contract`, when present, the "
                "approved presentation, response, outcome or focus target; judge the evidence against both.",
                "Evidence: text, ARIA, step status, focus and layout are authoritative; images are context only. Cite "
                "view_evidence for every supplied view; the Harness derives reached or blocked from blocker findings.",
                "Return exactly one `obligation_verdicts` entry for every obligation ID in `views[].obligation_refs`: "
                "satisfied or violated with the evidence refs that show it, or not_observable when the supplied evidence "
                "cannot show it either way. Every violated or not_observable entry needs a finding that cites it.",
                "Classification: missing or wrong implemented behavior is implementation; behavior that is present but the "
                "typed journey cannot expose because of its locator, action or capture timing is journey_defect; conflicting "
                "approved sources are design_conflict. A step_failed record alone does not decide the category.",
                "Timing: a terminal async outcome needs a later typed wait; a transient state needs an observe locator armed "
                "on its triggering action. When both are cited, both need separate evidence. A record's `observe` field "
                "reports that locator as `execution_facts` describe: `observed` means the record's text evidence was "
                "captured with it on screen; `missed` means it never became visible, so judge from the record's ARIA "
                "whether the approved state is absent from the app or the locator names it wrongly.",
                "Severity: every implementation or journey_defect finding is a blocker. design_conflict is warning-only. "
                "Anything outside supplied obligations is an observation, not a finding.",
                "For each supplied not_reachable obligation return exactly justified or needs_journey. Use justified only "
                "when approved data, typed actions and the capture behavior in `execution_facts` cannot expose it; "
                "otherwise use needs_journey and give `reason`: the supplied typed step, demo data or execution fact that "
                "exposes it. The Harness sends that reason to the Planner, so do not duplicate a needs_journey verdict in "
                "findings.",
                "Findings cite supplied views, their obligation IDs and their evidence. implementation cites candidate "
                "unit_ids; journey_defect and design_conflict use unit_ids=[]. expected_change describes only the required "
                "observable outcome; the Planner owns exact journey steps/locators and the Developer owns code changes.",
            ],
            "output_schema": _JOURNEY_EXECUTION_OUTPUT_SCHEMA,
            "scenario": scenario,
            "review_context": review_context,
            "image_inputs": [
                {"label": item.get("label"), "sha256": item.get("sha256")}
                for item in images
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def parse_polish_review(
    raw: str,
    *,
    contract: dict[str, object],
    evidence_refs: set[str],
    authority_refs: set[str],
    visual_evidence_refs: set[str] | None = None,
) -> dict[str, object]:
    payload = _parse_json_object(raw)
    unit_ids = sorted(str(item.get("unit_id") or "") for item in _dicts(contract.get("polish_units")))
    problems: list[str] = []
    reviewed = payload.get("reviewed_unit_ids")
    if not isinstance(reviewed, list) or sorted(str(item) for item in reviewed) != unit_ids:
        problems.append(f"reviewed_unit_ids must be exactly {unit_ids}.")
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        problems.append("`findings` must be a list.")
        raw_findings = []
    observations = payload.get("observations")
    if not isinstance(observations, list) or not all(isinstance(item, str) for item in observations):
        problems.append("`observations` must be a list of strings.")
        observations = []
    findings: list[dict[str, object]] = []
    for index, raw_finding in enumerate(raw_findings, start=1):
        label = f"findings[{index}]"
        if not isinstance(raw_finding, dict):
            problems.append(f"{label} must be an object.")
            continue
        finding = {
            "severity": str(raw_finding.get("severity") or "").strip(),
            "category": str(raw_finding.get("category") or "").strip(),
            "unit_ids": _strings(raw_finding.get("unit_ids")),
            "evidence_refs": _strings(raw_finding.get("evidence_refs")),
            "authority_refs": _strings(raw_finding.get("authority_refs")),
            "issue": str(raw_finding.get("issue") or "").strip(),
            "expected_change": str(raw_finding.get("expected_change") or "").strip(),
        }
        if finding["severity"] not in POLISH_FINDING_SEVERITIES:
            problems.append(f"{label}.severity is invalid.")
        if finding["category"] not in POLISH_FINDING_CATEGORIES:
            problems.append(f"{label}.category is invalid.")
        if not finding["unit_ids"] or set(finding["unit_ids"]) - set(unit_ids):
            problems.append(f"{label}.unit_ids must cite known polish units.")
        if set(finding["evidence_refs"]) - evidence_refs:
            problems.append(f"{label}.evidence_refs cites unknown evidence.")
        if set(finding["authority_refs"]) - authority_refs:
            problems.append(f"{label}.authority_refs cites unknown authority.")
        if finding["category"] == "visual_authority":
            visual_refs = evidence_refs if visual_evidence_refs is None else visual_evidence_refs
            if not set(finding["evidence_refs"]) & visual_refs or not finding["authority_refs"]:
                evidence_label = "evidence_refs" if visual_evidence_refs is None else "image evidence_refs"
                problems.append(f"{label} visual_authority findings need {evidence_label} and authority_refs.")
        if finding["category"] == "design_conflict" and finding["severity"] == "blocker":
            problems.append(f"{label} design_conflict cannot be a blocker.")
        if not finding["issue"] or not finding["expected_change"]:
            problems.append(f"{label} needs issue and expected_change.")
        findings.append(finding)
    if problems:
        raise HarnessError(POLISH_REVIEW_CONTRACT_INVALID, " ".join(problems))
    blockers = [item for item in findings if item["severity"] == "blocker"]
    return {
        "ok": not blockers,
        "summary": str(payload.get("summary") or "").strip(),
        "reviewed_unit_ids": unit_ids,
        "findings": findings,
        "blocking_findings": blockers,
        "warnings": [item for item in findings if item["severity"] == "warning"],
        "observations": list(observations),
    }


def parse_browser_qa_review(
    raw: str,
    *,
    scenario: dict[str, object],
    evidence_refs: set[str],
    ownership: dict[str, set[str]],
) -> dict[str, object]:
    payload = _parse_json_object(raw)
    expected_ids = sorted(
        str(item.get("obligation_id") or "")
        for item in _dicts(scenario.get("obligations"))
        if str(item.get("obligation_id") or "")
    )
    expected = set(expected_ids)
    problems: list[str] = []
    reviewed = payload.get("reviewed_obligation_ids")
    if not isinstance(reviewed, list) or sorted(str(item) for item in reviewed) != expected_ids:
        problems.append(f"reviewed_obligation_ids must be exactly {expected_ids}.")

    raw_verdicts = payload.get("verdicts")
    if not isinstance(raw_verdicts, list):
        problems.append("`verdicts` must be a list.")
        raw_verdicts = []
    verdicts: list[dict[str, object]] = []
    verdict_ids: list[str] = []
    for index, raw_verdict in enumerate(raw_verdicts, start=1):
        label = f"verdicts[{index}]"
        if not isinstance(raw_verdict, dict):
            problems.append(f"{label} must be an object.")
            continue
        obligation_id = str(raw_verdict.get("obligation_id") or "").strip()
        verdict = str(raw_verdict.get("verdict") or "").strip()
        refs = _strings(raw_verdict.get("evidence_refs"))
        if obligation_id not in expected:
            problems.append(f"{label}.obligation_id is not supplied by this scenario.")
        if verdict not in BROWSER_QA_VERDICTS:
            problems.append(f"{label}.verdict is invalid.")
        if set(refs) - evidence_refs:
            problems.append(f"{label}.evidence_refs cites unknown scenario evidence.")
        verdict_ids.append(obligation_id)
        verdicts.append({"obligation_id": obligation_id, "verdict": verdict, "evidence_refs": refs})
    if sorted(verdict_ids) != expected_ids:
        problems.append(f"verdict obligation IDs must be exactly {expected_ids}.")

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        problems.append("`findings` must be a list.")
        raw_findings = []
    findings: list[dict[str, object]] = []
    for index, raw_finding in enumerate(raw_findings, start=1):
        label = f"findings[{index}]"
        if not isinstance(raw_finding, dict):
            problems.append(f"{label} must be an object.")
            continue
        obligation_ids = _strings(raw_finding.get("obligation_ids"))
        unit_ids = _strings(raw_finding.get("unit_ids"))
        refs = _strings(raw_finding.get("evidence_refs"))
        finding = {
            "severity": str(raw_finding.get("severity") or "").strip(),
            "category": str(raw_finding.get("category") or "").strip(),
            "obligation_ids": obligation_ids,
            "unit_ids": unit_ids,
            "evidence_refs": refs,
            "issue": str(raw_finding.get("issue") or "").strip(),
            "expected_change": str(raw_finding.get("expected_change") or "").strip(),
        }
        if finding["severity"] not in POLISH_FINDING_SEVERITIES:
            problems.append(f"{label}.severity is invalid.")
        if finding["category"] not in BROWSER_QA_FINDING_CATEGORIES:
            problems.append(f"{label}.category is invalid.")
        if not obligation_ids or set(obligation_ids) - expected:
            problems.append(f"{label}.obligation_ids must cite supplied obligations.")
        allowed_units = {unit_id for obligation_id in obligation_ids for unit_id in ownership.get(obligation_id, set())}
        if not unit_ids or set(unit_ids) - allowed_units:
            problems.append(f"{label}.unit_ids must cite candidate Plan units for the cited obligations.")
        if set(refs) - evidence_refs:
            problems.append(f"{label}.evidence_refs cites unknown scenario evidence.")
        if not refs:
            problems.append(f"{label}.evidence_refs must cite supplied scenario evidence.")
        if finding["category"] == "design_conflict" and finding["severity"] == "blocker":
            problems.append(f"{label} design_conflict cannot be a blocker.")
        if not finding["issue"] or not finding["expected_change"]:
            problems.append(f"{label} needs issue and expected_change.")
        findings.append(finding)

    violated = {str(item["obligation_id"]) for item in verdicts if item["verdict"] == "violated"}
    cited = {obligation_id for finding in findings for obligation_id in _strings(finding.get("obligation_ids"))}
    if violated - cited:
        problems.append(f"violated verdicts need matching findings: {sorted(violated - cited)}.")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not all(isinstance(item, str) for item in observations):
        problems.append("`observations` must be a list of strings.")
        observations = []
    if problems:
        raise HarnessError(BROWSER_QA_REVIEW_CONTRACT_INVALID, " ".join(problems))
    blockers = [item for item in findings if item["severity"] == "blocker"]
    return {
        "ok": not blockers,
        "summary": str(payload.get("summary") or "").strip(),
        "reviewed_obligation_ids": expected_ids,
        "verdicts": verdicts,
        "findings": findings,
        "blocking_findings": blockers,
        "warnings": [item for item in findings if item["severity"] == "warning"],
        "observations": list(observations),
    }


def parse_journey_execution_review(
    raw: str,
    *,
    scenario: dict[str, object],
    evidence_refs: set[str],
    candidate_unit_ids: set[str],
) -> dict[str, object]:
    payload = _parse_json_object(raw)
    views = _dicts(scenario.get("views"))
    expected_view_ids = sorted(
        str(item.get("view_id") or "") for item in views if str(item.get("view_id") or "")
    )
    expected_views = set(expected_view_ids)
    view_obligations = {
        str(item.get("view_id") or ""): set(_strings(item.get("obligation_refs")))
        for item in views
        if str(item.get("view_id") or "")
    }
    expected_unreachable_ids = sorted(
        {
            str(item.get("obligation_id") or "")
            for item in _dicts(scenario.get("not_reachable"))
            if str(item.get("obligation_id") or "")
        }
    )
    expected_unreachable = set(expected_unreachable_ids)
    unreachable_reasons = {
        str(item.get("obligation_id") or ""): str(item.get("reason") or "").strip()
        for item in _dicts(scenario.get("not_reachable"))
        if str(item.get("obligation_id") or "")
    }
    problems: list[str] = []
    reviewed = payload.get("reviewed_view_ids")
    if not isinstance(reviewed, list) or sorted(str(item) for item in reviewed) != expected_view_ids:
        problems.append(f"reviewed_view_ids must be exactly {expected_view_ids}.")
    reviewed_unreachable = payload.get("reviewed_not_reachable_obligation_ids")
    if (
        not isinstance(reviewed_unreachable, list)
        or sorted(str(item) for item in reviewed_unreachable) != expected_unreachable_ids
    ):
        problems.append(
            "reviewed_not_reachable_obligation_ids must be exactly "
            f"{expected_unreachable_ids}."
        )

    raw_view_evidence = payload.get("view_evidence")
    if not isinstance(raw_view_evidence, list):
        problems.append("`view_evidence` must be a list.")
        raw_view_evidence = []
    view_evidence: list[dict[str, object]] = []
    evidence_view_ids: list[str] = []
    for index, raw_evidence in enumerate(raw_view_evidence, start=1):
        label = f"view_evidence[{index}]"
        if not isinstance(raw_evidence, dict):
            problems.append(f"{label} must be an object.")
            continue
        view_id = str(raw_evidence.get("view_id") or "").strip()
        refs = _strings(raw_evidence.get("evidence_refs"))
        if view_id not in expected_views:
            problems.append(f"{label}.view_id is not supplied by this scenario.")
        if not refs or set(refs) - evidence_refs:
            problems.append(f"{label}.evidence_refs must cite supplied scenario evidence.")
        evidence_view_ids.append(view_id)
        view_evidence.append({"view_id": view_id, "evidence_refs": refs})
    if sorted(evidence_view_ids) != expected_view_ids:
        problems.append(f"view_evidence view IDs must be exactly {expected_view_ids}.")

    # One verdict per obligation a view of this scenario claims to expose: none may be skipped silently.
    expected_obligation_ids = sorted(set().union(*view_obligations.values()))
    raw_obligation_verdicts = payload.get("obligation_verdicts")
    if not isinstance(raw_obligation_verdicts, list):
        problems.append("`obligation_verdicts` must be a list.")
        raw_obligation_verdicts = []
    obligation_verdicts: list[dict[str, object]] = []
    for index, raw_verdict in enumerate(raw_obligation_verdicts, start=1):
        label = f"obligation_verdicts[{index}]"
        if not isinstance(raw_verdict, dict):
            problems.append(f"{label} must be an object.")
            continue
        verdict = str(raw_verdict.get("verdict") or "").strip()
        refs = _strings(raw_verdict.get("evidence_refs"))
        if verdict not in BROWSER_QA_VERDICTS:
            problems.append(f"{label}.verdict is invalid.")
        if set(refs) - evidence_refs or (not refs and verdict != "not_observable"):
            problems.append(f"{label}.evidence_refs must cite supplied scenario evidence.")
        obligation_verdicts.append(
            {
                "obligation_id": str(raw_verdict.get("obligation_id") or "").strip(),
                "verdict": verdict,
                "evidence_refs": refs,
            }
        )
    if sorted(str(item["obligation_id"]) for item in obligation_verdicts) != expected_obligation_ids:
        problems.append(f"obligation_verdicts obligation IDs must be exactly {expected_obligation_ids}.")

    raw_unreachable_verdicts = payload.get("not_reachable_verdicts")
    if not isinstance(raw_unreachable_verdicts, list):
        problems.append("`not_reachable_verdicts` must be a list.")
        raw_unreachable_verdicts = []
    unreachable_verdicts: list[dict[str, str]] = []
    unreachable_verdict_ids: list[str] = []
    for index, raw_verdict in enumerate(raw_unreachable_verdicts, start=1):
        label = f"not_reachable_verdicts[{index}]"
        if not isinstance(raw_verdict, dict):
            problems.append(f"{label} must be an object.")
            continue
        obligation_id = str(raw_verdict.get("obligation_id") or "").strip()
        verdict = str(raw_verdict.get("verdict") or "").strip()
        reason = str(raw_verdict.get("reason") or "").strip()
        if obligation_id not in expected_unreachable:
            problems.append(f"{label}.obligation_id is not supplied as not_reachable.")
        if verdict not in JOURNEY_UNREACHABLE_VERDICTS:
            problems.append(f"{label}.verdict is invalid.")
        if verdict == "needs_journey" and not reason:
            problems.append(f"{label}.reason must say what exposes the obligation.")
        unreachable_verdict_ids.append(obligation_id)
        unreachable_verdicts.append(
            {"obligation_id": obligation_id, "verdict": verdict, **({"reason": reason} if reason else {})}
        )
    if sorted(unreachable_verdict_ids) != expected_unreachable_ids:
        problems.append(
            "not_reachable verdict obligation IDs must be exactly "
            f"{expected_unreachable_ids}."
        )
    needs_journey = {
        item["obligation_id"]: item.get("reason", "")
        for item in unreachable_verdicts
        if item["verdict"] == "needs_journey"
    }

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        problems.append("`findings` must be a list.")
        raw_findings = []
    findings: list[dict[str, object]] = []
    for index, raw_finding in enumerate(raw_findings, start=1):
        label = f"findings[{index}]"
        if not isinstance(raw_finding, dict):
            problems.append(f"{label} must be an object.")
            continue
        view_ids = _strings(raw_finding.get("view_ids"))
        obligation_ids = _strings(raw_finding.get("obligation_ids"))
        unit_ids = _strings(raw_finding.get("unit_ids"))
        refs = _strings(raw_finding.get("evidence_refs"))
        finding = {
            "severity": str(raw_finding.get("severity") or "").strip(),
            "category": str(raw_finding.get("category") or "").strip(),
            "view_ids": view_ids,
            "obligation_ids": obligation_ids,
            "unit_ids": unit_ids,
            "evidence_refs": refs,
            "issue": str(raw_finding.get("issue") or "").strip(),
            "expected_change": str(raw_finding.get("expected_change") or "").strip(),
        }
        if finding["severity"] not in POLISH_FINDING_SEVERITIES:
            problems.append(f"{label}.severity is invalid.")
        if finding["category"] not in BROWSER_QA_FINDING_CATEGORIES:
            problems.append(f"{label}.category is invalid.")
        if not view_ids:
            problems.append(
                f"{label}.view_ids must cite supplied views; do not duplicate needs_journey verdicts in findings."
            )
        else:
            if set(view_ids) - expected_views:
                problems.append(f"{label}.view_ids must cite supplied views.")
            if not obligation_ids or any(
                set(obligation_ids) - view_obligations.get(view_id, set())
                for view_id in view_ids
            ):
                problems.append(f"{label}.obligation_ids must belong to every cited view.")
        if finding["category"] in {"implementation", "journey_defect"} and finding["severity"] != "blocker":
            problems.append(f"{label} implementation and journey_defect findings must be blockers.")
        if finding["category"] == "design_conflict" and finding["severity"] != "warning":
            problems.append(f"{label} design_conflict findings must be warnings.")
        if finding["category"] == "implementation":
            if not unit_ids or set(unit_ids) - candidate_unit_ids:
                problems.append(f"{label}.unit_ids must cite candidate scenario units.")
        elif unit_ids:
            problems.append(f"{label}.unit_ids must be empty unless category is implementation.")
        if not refs or set(refs) - evidence_refs:
            problems.append(f"{label}.evidence_refs must cite supplied scenario evidence.")
        if not finding["issue"] or not finding["expected_change"]:
            problems.append(f"{label} needs issue and expected_change.")
        findings.append(finding)

    observations = payload.get("observations")
    if not isinstance(observations, list) or not all(isinstance(item, str) for item in observations):
        problems.append("`observations` must be a list of strings.")
        observations = []
    open_obligations = {str(item["obligation_id"]) for item in obligation_verdicts if item["verdict"] != "satisfied"}
    cited = {obligation_id for finding in findings for obligation_id in _strings(finding.get("obligation_ids"))}
    if open_obligations - cited:
        problems.append(
            f"violated or not_observable obligation verdicts need matching findings: {sorted(open_obligations - cited)}."
        )
    if problems:
        raise HarnessError(JOURNEY_EXECUTION_REVIEW_CONTRACT_INVALID, " ".join(problems))
    for obligation_id in sorted(needs_journey):
        claim = unreachable_reasons.get(obligation_id) or "no reason was given"
        findings.append(
            {
                "severity": "blocker",
                "category": "journey_defect",
                "view_ids": [],
                "obligation_ids": [obligation_id],
                "unit_ids": [],
                "evidence_refs": [],
                # The Validator's reason is the new information; the Planner's own claim alone would only bounce back.
                "issue": (
                    f"The plan lists {obligation_id} as not_reachable ({claim}), but the execution review finds "
                    f"that it can be exposed: {needs_journey[obligation_id]}"
                ),
                "expected_change": (
                    f"Plan a deterministic browser capture for {obligation_id} before implementation is judged."
                ),
            }
        )
    blockers = [item for item in findings if item["severity"] == "blocker"]
    blocked_view_ids = {
        view_id
        for finding in blockers
        for view_id in _strings(finding.get("view_ids"))
    }
    verdicts = [
        {
            "view_id": item["view_id"],
            "verdict": "blocked" if item["view_id"] in blocked_view_ids else "reached",
            "evidence_refs": item["evidence_refs"],
        }
        for item in view_evidence
    ]
    return {
        "ok": not blockers,
        "summary": str(payload.get("summary") or "").strip(),
        "reviewed_view_ids": expected_view_ids,
        "reviewed_not_reachable_obligation_ids": expected_unreachable_ids,
        "view_evidence": view_evidence,
        "verdicts": verdicts,
        "obligation_verdicts": obligation_verdicts,
        "not_reachable_verdicts": unreachable_verdicts,
        "findings": findings,
        "blocking_findings": blockers,
        "warnings": [item for item in findings if item["severity"] == "warning"],
        "observations": list(observations),
    }


_GUARDED_RETRY_ERRORS: tuple[tuple[str, frozenset[str]], ...] = (
    (r"findings\[(\d+)\]\.unit_ids must cite candidate scenario units\.", frozenset({"unit_ids"})),
    (
        r"findings\[(\d+)\]\.obligation_ids must belong to every cited view\.",
        frozenset({"view_ids", "evidence_refs", "issue"}),
    ),
)


def _journey_contract_retry_fields(contract_error: str) -> dict[int, set[str]]:
    allowed_fields: dict[int, set[str]] = {}
    other_errors = contract_error
    for pattern, fields in _GUARDED_RETRY_ERRORS:
        for one_based_index in re.findall(pattern, contract_error):
            allowed_fields.setdefault(int(one_based_index) - 1, set()).update(fields)
        other_errors = re.sub(pattern, "", other_errors)
    # Any other contract error is fixed in fields this guard would freeze: use the plain retry for the whole output.
    return {} if other_errors.strip() else allowed_fields


def _journey_contract_retry_instruction(contract_error: str) -> str:
    allowed_fields = _journey_contract_retry_fields(contract_error)
    if not allowed_fields:
        return "Correct only the fields named by that contract error."
    instructions: list[str] = []
    for index, fields in sorted(allowed_fields.items()):
        label = f"findings[{index + 1}]"
        if fields == {"view_ids", "evidence_refs", "issue"}:
            instructions.append(
                f"For the `{label}.obligation_ids` relation error, keep `{label}.obligation_ids` "
                f"unchanged and correct only `{label}.view_ids`, `{label}.evidence_refs`, and "
                f"`{label}.issue` so every cited view owns every cited obligation."
            )
        else:
            field_names = ", ".join(f"`{label}.{field}`" for field in sorted(fields))
            instructions.append(f"Correct only {field_names}.")
    return " ".join(instructions)


def _guard_journey_contract_retry(
    previous: dict[str, object],
    corrected: dict[str, object],
    contract_error: str,
) -> None:
    allowed_fields = _journey_contract_retry_fields(contract_error)
    if not allowed_fields:
        return

    before = json.loads(json.dumps(previous))
    after = json.loads(json.dumps(corrected))
    for index, fields in allowed_fields.items():
        for payload in (before, after):
            findings = payload.get("findings")
            if isinstance(findings, list) and 0 <= index < len(findings) and isinstance(findings[index], dict):
                for field in fields:
                    findings[index][field] = []
    if before != after:
        raise HarnessError(
            JOURNEY_EXECUTION_REVIEW_CONTRACT_INVALID,
            "Contract retry changed fields outside the reported mechanical correction.",
        )


def _dicts(value: object) -> list[dict[str, object]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strings(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
