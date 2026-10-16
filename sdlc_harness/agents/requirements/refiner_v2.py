"""Structured Requirement Model V2 refiner with one bounded recovery call."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from ...core.errors import HarnessError
from ...llm.client import LlmClient, LlmCompletion
from ...llm.json_utils import parse_first_json_object
from ...sources.fixtures import UseCaseSource
from .model_v2 import RequirementModelV2, SCHEMA_VERSION
from .validator_v2 import (
    RequirementModelV2ValidationReport,
    normalize_and_validate_requirement_model_v2,
)


_PROMPT_DIR = Path(__file__).with_name("prompts")
_INITIAL_PROMPT_PATH = _PROMPT_DIR / "requirement_model_v2.md"
_REPAIR_PROMPT_PATH = _PROMPT_DIR / "requirement_model_repair_v2.md"
_EXPECTED_ROOT_KEYS = {
    "use_case",
    "preconditions",
    "flows",
    "postconditions",
    "business_rules",
    "acceptance_criteria",
    "quality_requirements",
    "assumptions",
    "open_questions",
    "evidence",
}
_REPAIRABLE_ISSUE_CODES = {
    "invalid_schema_version",
    "invalid_revision",
    "required_field",
    "invalid_type",
    "invalid_requirement_status",
    "invalid_question_status",
    "invalid_decision_status",
    "required_id",
    "invalid_id_prefix",
    "duplicate_id",
    "dangling_reference",
    "question_dependency_cycle",
    "evidence_not_in_source",
    "evidence_source_mismatch",
    "explicit_without_evidence",
    "explicit_with_invalid_evidence",
    "human_decision_without_accepted_decision",
    "unknown_without_question",
    "unknown_contains_inferred_content",
    "resolved_question_without_decision",
}


@dataclass(frozen=True)
class RequirementModelV2Attempt:
    """Audit metadata for one structured LLM attempt."""

    attempt: int
    purpose: Literal["initial", "format_recovery", "validator_repair"]
    finish_reason: str
    usage: dict[str, object]
    json_mode_requested: bool
    max_tokens: int
    parse_status: Literal["parsed", "parse_failed", "schema_failed", "truncated"]
    validation_report: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt": self.attempt,
            "purpose": self.purpose,
            "finish_reason": self.finish_reason,
            "usage": deepcopy(self.usage),
            "json_mode_requested": self.json_mode_requested,
            "max_tokens": self.max_tokens,
            "parse_status": self.parse_status,
        }
        if self.validation_report is not None:
            payload["validation_report"] = deepcopy(self.validation_report)
        return payload


@dataclass(frozen=True)
class RequirementModelV2RefinementResult:
    """Canonical model and audit data selected after at most two LLM calls."""

    model: RequirementModelV2
    initial_report: RequirementModelV2ValidationReport | None
    final_report: RequirementModelV2ValidationReport
    repaired: bool
    attempts: list[RequirementModelV2Attempt]

    def to_report_dict(self) -> dict[str, object]:
        return {
            "repaired": self.repaired,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "initial_report": self.initial_report.to_dict() if self.initial_report else None,
            "final_report": self.final_report.to_dict(),
        }


@dataclass(frozen=True)
class _ProcessedCompletion:
    completion: LlmCompletion
    attempt: RequirementModelV2Attempt
    model: RequirementModelV2 | None
    report: RequirementModelV2ValidationReport | None
    raw_payload: dict[str, Any] | None
    schema_error: str = ""


class RequirementModelV2Refiner:
    """Create the initial canonical V2 requirement model from one source snapshot."""

    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def refine(
        self,
        source: UseCaseSource,
        *,
        run_id: str,
        source_artifact_ref: str,
    ) -> RequirementModelV2RefinementResult:
        if not run_id.strip():
            raise HarnessError("requirement_model_v2_validation_failed", "run_id must not be empty.")
        if not source_artifact_ref.strip():
            raise HarnessError(
                "requirement_model_v2_validation_failed",
                "source_artifact_ref must not be empty.",
                run_id=run_id,
            )

        max_tokens = self._structured_max_tokens()
        initial_prompt = self._initial_prompt(source)
        first_completion = self._complete(initial_prompt, max_tokens=max_tokens)
        first = self._process_completion(
            first_completion,
            attempt_number=1,
            purpose="initial",
            source=source,
            run_id=run_id,
            source_artifact_ref=source_artifact_ref,
            max_tokens=max_tokens,
        )
        attempts = [first.attempt]
        if first.report is not None and first.report.ok and first.model is not None:
            return RequirementModelV2RefinementResult(
                model=first.model,
                initial_report=first.report,
                final_report=first.report,
                repaired=False,
                attempts=attempts,
            )

        purpose = self._repair_purpose(first, run_id=run_id)
        second_prompt = self._second_prompt(
            purpose=purpose,
            source=source,
            initial_prompt=initial_prompt,
            first=first,
        )
        second_completion = self._complete(second_prompt, max_tokens=max_tokens)
        second = self._process_completion(
            second_completion,
            attempt_number=2,
            purpose=purpose,
            source=source,
            run_id=run_id,
            source_artifact_ref=source_artifact_ref,
            max_tokens=max_tokens,
        )
        attempts.append(second.attempt)
        if second.report is not None and second.report.ok and second.model is not None:
            return RequirementModelV2RefinementResult(
                model=second.model,
                initial_report=first.report,
                final_report=second.report,
                repaired=True,
                attempts=attempts,
            )
        self._raise_final_error(second, run_id=run_id)
        raise AssertionError("unreachable")

    def _complete(self, prompt: str, *, max_tokens: int) -> LlmCompletion:
        try:
            return self.llm_client.complete_with_metadata(
                system=(
                    "You are Requirement Refiner V2. Return exactly one JSON object. "
                    "Use only the supplied source evidence and never invent a human decision."
                ),
                prompt=prompt,
                json_mode=True,
                max_tokens=max_tokens,
            )
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter boundary.
            raise HarnessError("requirement_refiner_failed", str(exc)) from exc

    def _process_completion(
        self,
        completion: LlmCompletion,
        *,
        attempt_number: int,
        purpose: Literal["initial", "format_recovery", "validator_repair"],
        source: UseCaseSource,
        run_id: str,
        source_artifact_ref: str,
        max_tokens: int,
    ) -> _ProcessedCompletion:
        if completion.finish_reason.strip().lower() == "length":
            attempt = self._attempt(
                attempt_number,
                purpose,
                completion,
                max_tokens,
                parse_status="truncated",
            )
            return _ProcessedCompletion(completion, attempt, None, None, None)
        try:
            raw_payload = parse_first_json_object(completion.content, expected_keys=_EXPECTED_ROOT_KEYS)
        except (json.JSONDecodeError, TypeError, ValueError):
            attempt = self._attempt(
                attempt_number,
                purpose,
                completion,
                max_tokens,
                parse_status="parse_failed",
            )
            return _ProcessedCompletion(completion, attempt, None, None, None)

        payload = deepcopy(raw_payload)
        self._stamp_python_owned_fields(
            payload,
            source=source,
            run_id=run_id,
            source_artifact_ref=source_artifact_ref,
        )
        try:
            validation = normalize_and_validate_requirement_model_v2(payload, source.text)
        except HarnessError as exc:
            attempt = self._attempt(
                attempt_number,
                purpose,
                completion,
                max_tokens,
                parse_status="schema_failed",
                validation_report={
                    "ok": False,
                    "status": "invalid",
                    "issues": [{"code": exc.error_code, "path": "$", "message": exc.message}],
                    "blocking_question_ids": [],
                },
            )
            return _ProcessedCompletion(completion, attempt, None, None, raw_payload, exc.message)

        attempt = self._attempt(
            attempt_number,
            purpose,
            completion,
            max_tokens,
            parse_status="parsed",
            validation_report=validation.report.to_dict(),
        )
        return _ProcessedCompletion(
            completion,
            attempt,
            validation.model,
            validation.report,
            raw_payload,
        )

    def _repair_purpose(
        self,
        first: _ProcessedCompletion,
        *,
        run_id: str,
    ) -> Literal["format_recovery", "validator_repair"]:
        if first.attempt.parse_status in {"truncated", "parse_failed"}:
            return "format_recovery"
        if first.attempt.parse_status == "schema_failed":
            return "validator_repair"
        if first.report is None:
            raise HarnessError(
                "requirement_model_v2_parse_failed",
                "Requirement Model V2 output could not be parsed.",
                run_id=run_id,
            )
        non_repairable = sorted(
            {issue.code for issue in first.report.issues if issue.code not in _REPAIRABLE_ISSUE_CODES}
        )
        if non_repairable:
            raise HarnessError(
                "requirement_model_v2_validation_failed",
                "Requirement Model V2 contains non-repairable semantic violations: "
                + ", ".join(non_repairable),
                run_id=run_id,
            )
        return "validator_repair"

    def _second_prompt(
        self,
        *,
        purpose: Literal["format_recovery", "validator_repair"],
        source: UseCaseSource,
        initial_prompt: str,
        first: _ProcessedCompletion,
    ) -> str:
        if purpose == "format_recovery":
            return (
                initial_prompt
                + "\n\nRECOVERY: The previous response was truncated or was not a parseable JSON object. "
                "Generate the complete object again from the source. Do not refer to the previous response."
            )
        template = _REPAIR_PROMPT_PATH.read_text(encoding="utf-8")
        rejected = first.raw_payload if first.raw_payload is not None else {"unparseable": first.completion.content}
        report = first.attempt.validation_report or {
            "ok": False,
            "status": "invalid",
            "issues": [{"code": "requirement_model_v2_invalid", "path": "$", "message": first.schema_error}],
        }
        return self._replace_prompt_tokens(
            template,
            {
                "SOURCE_REF": source.source_ref,
                "SOURCE_TEXT": source.text,
                "REJECTED_JSON": json.dumps(rejected, ensure_ascii=False, indent=2),
                "VALIDATION_REPORT": json.dumps(report, ensure_ascii=False, indent=2),
            },
        )

    def _initial_prompt(self, source: UseCaseSource) -> str:
        template = _INITIAL_PROMPT_PATH.read_text(encoding="utf-8")
        return self._replace_prompt_tokens(
            template,
            {
                "DATASET": source.dataset,
                "USE_CASE": source.use_case,
                "SOURCE_REF": source.source_ref,
                "SOURCE_TEXT": source.text,
            },
        )

    @staticmethod
    def _replace_prompt_tokens(template: str, values: dict[str, str]) -> str:
        output = template
        for key, value in values.items():
            output = output.replace("{{" + key + "}}", value)
        return output

    @staticmethod
    def _stamp_python_owned_fields(
        payload: dict[str, Any],
        *,
        source: UseCaseSource,
        run_id: str,
        source_artifact_ref: str,
    ) -> None:
        payload["schema_version"] = SCHEMA_VERSION
        payload["revision"] = 1
        payload["run_id"] = run_id
        payload["source"] = {
            "source_ref": source.source_ref,
            "artifact_ref": source_artifact_ref,
            "fingerprint": "sha256:" + hashlib.sha256(source.text.encode("utf-8")).hexdigest(),
        }
        payload["decisions"] = []
        payload["validation"] = {"status": "invalid", "errors": []}

    @staticmethod
    def _attempt(
        attempt_number: int,
        purpose: Literal["initial", "format_recovery", "validator_repair"],
        completion: LlmCompletion,
        max_tokens: int,
        *,
        parse_status: Literal["parsed", "parse_failed", "schema_failed", "truncated"],
        validation_report: dict[str, object] | None = None,
    ) -> RequirementModelV2Attempt:
        return RequirementModelV2Attempt(
            attempt=attempt_number,
            purpose=purpose,
            finish_reason=completion.finish_reason,
            usage=deepcopy(completion.usage),
            json_mode_requested=True,
            max_tokens=max_tokens,
            parse_status=parse_status,
            validation_report=validation_report,
        )

    def _structured_max_tokens(self) -> int:
        settings = getattr(self.llm_client, "settings", None)
        return int(getattr(settings, "llm_structured_max_tokens", 8192))

    @staticmethod
    def _raise_final_error(result: _ProcessedCompletion, *, run_id: str) -> None:
        if result.attempt.parse_status == "truncated":
            raise HarnessError(
                "requirement_model_v2_output_truncated",
                "Requirement Model V2 output was truncated after the bounded recovery attempt.",
                run_id=run_id,
            )
        if result.attempt.parse_status == "parse_failed":
            raise HarnessError(
                "requirement_model_v2_parse_failed",
                "Requirement Model V2 output was not parseable after the bounded recovery attempt.",
                run_id=run_id,
            )
        issues = result.report.issues if result.report is not None else []
        details = "; ".join(f"{issue.code}@{issue.path}" for issue in issues)
        if not details:
            details = result.schema_error or "invalid structured output"
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            "Requirement Model V2 failed validation after the bounded repair attempt: " + details,
            run_id=run_id,
        )
