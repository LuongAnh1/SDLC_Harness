"""Correct the Requirement items whose silent interpretation the coverage reviewer judged wrong.

The reviewer judges which interpretation needs a fix (semantic); this updater asks the LLM for the corrected fields and
Python checks the mechanics: only the requirements a finding names, editable fields only, every finding addressed, and
the result still validates. Nothing here asks the user: the owner answers only the initial blocking questions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ...core.config import phase1_llm_call_options
from ...core.errors import HarnessError
from ...llm.client import LlmClient, LlmCompletion
from ...llm.json_utils import parse_first_json_object
from .clarification_v2 import ClarificationAttempt, _find_requirement
from .model_v2 import RequirementModelV2
from .validator_v2 import RequirementModelV2ValidationReport, normalize_and_validate_requirement_model_v2

_PROMPT_PATH = Path(__file__).with_name("prompts") / "requirement_interpretation_repair_v1.md"


@dataclass(frozen=True)
class InterpretationRepairResult:
    model: RequirementModelV2
    report: RequirementModelV2ValidationReport
    updates: list[dict[str, Any]]
    attempts: list[ClarificationAttempt]


class RequirementInterpretationRepairer:
    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def apply(
        self,
        *,
        model: RequirementModelV2,
        source_text_by_id: dict[str, str],
        findings: list[dict[str, Any]],
        clarification_policy_version: str | None = None,
    ) -> InterpretationRepairResult:
        source_text = source_text_by_id.get(str(model.source.get("source_id") or ""))
        if not isinstance(source_text, str):
            raise HarnessError("source_snapshot_corrupted", "Primary Requirement source snapshot is unavailable.", run_id=model.run_id)
        prompt = (
            _PROMPT_PATH.read_text(encoding="utf-8")
            .replace("{{FINDINGS}}", json.dumps(findings, ensure_ascii=False, indent=2))
            .replace("{{CURRENT_MODEL}}", json.dumps(model.to_dict(), ensure_ascii=False, indent=2))
            .replace("{{SOURCE_TEXT}}", source_text)
        )
        settings = getattr(self.llm_client, "settings", None)
        fallback = int(getattr(settings, "llm_structured_max_tokens", 8192))
        attempts: list[ClarificationAttempt] = []
        issue = ""
        for attempt_number, profile in ((1, "interpretation_repair"), (2, "refiner_final_repair")):
            purpose = "initial" if attempt_number == 1 else "repair"
            attempt_prompt = prompt if attempt_number == 1 else (
                f"{prompt}\n\nREPAIR: The previous proposal was rejected for this reason:\n{issue}\n"
                "Return a complete corrected JSON object only."
            )
            completion = self.llm_client.complete_with_metadata(
                system=(
                    "You correct Requirement Model V2 items named by review findings. Return one JSON object only. "
                    "Do not add, delete, or rename requirements."
                ),
                prompt=attempt_prompt,
                json_mode=True,
                **phase1_llm_call_options(settings, profile, fallback_max_tokens=fallback),
            )
            if completion.finish_reason.strip().lower() == "length":
                issue = "The interpretation repair output was truncated."
                attempts.append(_attempt(attempt_number, purpose, completion, "truncated", issue))
                continue
            try:
                proposal = parse_first_json_object(completion.content, expected_keys={"updates"})
            except (json.JSONDecodeError, TypeError, ValueError):
                issue = "The interpretation repair output was not parseable JSON."
                attempts.append(_attempt(attempt_number, purpose, completion, "parse_failed", issue))
                continue
            try:
                updates, validation = _apply_updates(
                    proposal,
                    model=model,
                    findings=findings,
                    source_text_by_id=source_text_by_id,
                    clarification_policy_version=clarification_policy_version,
                )
            except HarnessError as exc:
                issue = exc.message
                attempts.append(_attempt(attempt_number, purpose, completion, "invalid_update", issue))
                continue
            attempts.append(_attempt(attempt_number, purpose, completion, "parsed", ""))
            return InterpretationRepairResult(
                model=validation.model,
                report=validation.report,
                updates=updates,
                attempts=attempts,
            )
        raise HarnessError(
            "requirement_interpretation_repair_failed",
            "Interpretation repair failed after bounded repair: " + issue,
            run_id=model.run_id,
        )


def _apply_updates(
    proposal: dict[str, Any],
    *,
    model: RequirementModelV2,
    findings: list[dict[str, Any]],
    source_text_by_id: dict[str, str],
    clarification_policy_version: str | None,
):
    updates = proposal.get("updates")
    if not isinstance(updates, list) or not updates or any(not isinstance(item, dict) for item in updates):
        raise HarnessError("requirement_interpretation_repair_failed", "updates must be a non-empty list of objects.")
    ids = [str(item.get("requirement_id") or "") for item in updates]
    named = {str(ref) for finding in findings for ref in finding.get("requirement_ids", [])}
    if len(ids) != len(set(ids)) or not set(ids) <= named:
        raise HarnessError(
            "requirement_interpretation_repair_failed",
            "Update each requirement once and only requirements named by a finding: " + ", ".join(sorted(named)) + ".",
        )
    unaddressed = [
        index for index, finding in enumerate(findings) if not set(ids).intersection(finding.get("requirement_ids", []))
    ]
    if unaddressed:
        raise HarnessError(
            "requirement_interpretation_repair_failed",
            f"Every finding needs an update to one of its requirements; unaddressed findings: {unaddressed}.",
        )
    data = model.to_dict()
    for update in updates:
        requirement_id = str(update.get("requirement_id"))
        fields = update.get("fields")
        if not isinstance(fields, dict) or not fields or not str(update.get("reason") or "").strip():
            raise HarnessError(
                "requirement_interpretation_repair_failed",
                f"Update for {requirement_id} needs non-empty fields and a reason.",
            )
        item, allowed_fields, _ = _find_requirement(data, requirement_id)
        forbidden = sorted(set(fields).difference(allowed_fields))
        if forbidden:
            raise HarnessError(
                "requirement_interpretation_repair_failed",
                f"Update for {requirement_id} contains forbidden fields: {', '.join(forbidden)}.",
            )
        for key, value in fields.items():
            item[key] = deepcopy(value)
    data["revision"] = model.revision + 1
    data["validation"] = {"status": "invalid", "errors": []}
    validation = normalize_and_validate_requirement_model_v2(
        data,
        source_text_by_id,
        clarification_policy_version=clarification_policy_version,
    )
    if not validation.report.ok:
        details = "; ".join(f"{item.code}@{item.path}: {item.message}" for item in validation.report.issues)
        raise HarnessError("requirement_interpretation_repair_failed", "Updated model failed validation: " + details)
    return [
        {"requirement_id": str(item.get("requirement_id")), "reason": str(item.get("reason")).strip()}
        for item in updates
    ], validation


def _attempt(
    attempt_number: int,
    purpose: str,
    completion: LlmCompletion,
    parse_status: str,
    issue: str,
) -> ClarificationAttempt:
    return ClarificationAttempt(
        attempt=attempt_number,
        purpose=purpose,  # type: ignore[arg-type]
        finish_reason=completion.finish_reason,
        usage=completion.usage if isinstance(completion.usage, dict) else {},
        parse_status=parse_status,  # type: ignore[arg-type]
        issue=issue,
    )
