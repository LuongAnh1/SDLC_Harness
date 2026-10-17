"""Deterministic question selection and bounded Requirement V2 clarification updates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Literal

from ...core.errors import HarnessError
from ...llm.client import LlmClient, LlmCompletion
from ...llm.json_utils import parse_first_json_object
from ...storage.runtime import utc_now_iso
from .model_v2 import RequirementModelV2
from .validator_v2 import RequirementModelV2ValidationReport, normalize_and_validate_requirement_model_v2


_PROMPT_PATH = Path(__file__).with_name("prompts") / "requirement_clarification_v2.md"
_CATEGORY_PRIORITY = {
    "business_rule": 0,
    "data_identity": 1,
    "api_contract": 2,
    "observable_behavior": 3,
    "acceptance_criteria": 4,
}


@dataclass(frozen=True)
class CurrentRequirementQuestion:
    id: str
    text: str
    category: str
    affects: list[str]
    answer_guidance: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "text": self.text,
            "category": self.category,
            "affects": list(self.affects),
            "answer_guidance": self.answer_guidance,
        }


@dataclass(frozen=True)
class RequirementQuestionSelection:
    current_question: CurrentRequirementQuestion | None
    remaining_blocking_count: int


@dataclass(frozen=True)
class ClarificationAttempt:
    attempt: int
    purpose: Literal["initial", "repair"]
    finish_reason: str
    usage: dict[str, object]
    parse_status: Literal["parsed", "parse_failed", "truncated", "invalid_update"]
    issue: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "purpose": self.purpose,
            "finish_reason": self.finish_reason,
            "usage": deepcopy(self.usage),
            "parse_status": self.parse_status,
            "issue": self.issue,
        }


@dataclass(frozen=True)
class RequirementClarificationResult:
    model: RequirementModelV2
    final_report: RequirementModelV2ValidationReport
    decision: dict[str, Any]
    resolution: str
    follow_up_text: str | None
    attempts: list[ClarificationAttempt]

    def to_report_dict(self) -> dict[str, object]:
        return {
            "resolution": self.resolution,
            "follow_up_text": self.follow_up_text,
            "decision_id": self.decision.get("id"),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "final_report": self.final_report.to_dict(),
        }


def select_current_requirement_question(model: RequirementModelV2) -> RequirementQuestionSelection:
    """Select one dependency-ready blocking question using stable priority rules."""

    questions = [item for item in model.open_questions if isinstance(item, dict)]
    blocking = [
        item
        for item in questions
        if item.get("status") == "open" and item.get("effective_blocking") is True
    ]
    if not blocking:
        return RequirementQuestionSelection(current_question=None, remaining_blocking_count=0)
    statuses = {str(item.get("id")): str(item.get("status")) for item in questions}
    eligible = [
        item
        for item in blocking
        if all(statuses.get(str(dependency)) == "resolved" for dependency in _string_list(item.get("depends_on")))
    ]
    if not eligible:
        raise HarnessError(
            "requirement_question_dependency_invalid",
            "Blocking requirement questions remain but none has resolved dependencies.",
            run_id=model.run_id,
        )
    selected = min(eligible, key=_question_sort_key)
    return RequirementQuestionSelection(
        current_question=CurrentRequirementQuestion(
            id=str(selected.get("id") or ""),
            text=str(selected.get("question") or ""),
            category=str(selected.get("category") or ""),
            affects=_string_list(selected.get("affects")),
            answer_guidance=(
                str(selected.get("answer_guidance"))
                if selected.get("answer_guidance") is not None
                else None
            ),
        ),
        remaining_blocking_count=len(blocking),
    )


def normalize_clarification_answer(answer: str) -> str:
    normalized = unicodedata.normalize("NFKC", answer)
    return " ".join(normalized.split()).casefold()


def clarification_submission_hash(question_id: str, answer: str) -> str:
    normalized = normalize_clarification_answer(answer)
    raw = f"{question_id}\0{normalized}".encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def find_clarification_submission(model: RequirementModelV2, question_id: str, answer: str) -> dict[str, Any] | None:
    expected = clarification_submission_hash(question_id, answer)
    for decision in reversed(model.decisions):
        if decision.get("question_id") == question_id and decision.get("submission_hash") == expected:
            return deepcopy(decision)
    return None


class RequirementClarificationUpdater:
    """Apply one human answer through an LLM delta and Python-owned mutation policy."""

    def __init__(self, llm_client: LlmClient):
        self.llm_client = llm_client

    def apply(
        self,
        *,
        source_text: str,
        model: RequirementModelV2,
        question_id: str,
        answer: str,
        answered_by: str,
        answered_at: str | None = None,
    ) -> RequirementClarificationResult:
        answer_text = answer.strip()
        identity = answered_by.strip()
        if not answer_text:
            raise HarnessError("requirement_answer_empty", "Clarification answer must not be empty.", run_id=model.run_id)
        if not identity:
            raise HarnessError(
                "requirement_answered_by_empty",
                "Clarification answered_by must not be empty.",
                run_id=model.run_id,
            )
        selection = select_current_requirement_question(model)
        question = selection.current_question
        if question is None or question.id != question_id:
            raise HarnessError(
                "requirement_question_not_current",
                f"Question is not current: {question_id}",
                run_id=model.run_id,
            )

        prompt = self._prompt(source_text, model, question, answer_text)
        max_tokens = self._structured_max_tokens()
        attempts: list[ClarificationAttempt] = []
        issue = ""
        for attempt_number in (1, 2):
            purpose: Literal["initial", "repair"] = "initial" if attempt_number == 1 else "repair"
            attempt_prompt = prompt if attempt_number == 1 else prompt + (
                "\n\nREPAIR: The previous proposal was rejected for this reason:\n"
                + issue
                + "\nReturn a complete corrected JSON delta only."
            )
            completion = self._complete(attempt_prompt, max_tokens=max_tokens)
            if completion.finish_reason.strip().lower() == "length":
                issue = "Structured clarification output was truncated."
                attempts.append(self._attempt(attempt_number, purpose, completion, "truncated", issue))
                continue
            try:
                proposal = parse_first_json_object(
                    completion.content,
                    expected_keys={"resolution", "follow_up_text", "updates"},
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                issue = "Structured clarification output was not parseable JSON."
                attempts.append(self._attempt(attempt_number, purpose, completion, "parse_failed", issue))
                continue
            try:
                result = self._apply_proposal(
                    proposal=proposal,
                    source_text=source_text,
                    model=model,
                    question=question,
                    answer=answer_text,
                    answered_by=identity,
                    answered_at=answered_at or utc_now_iso(),
                )
            except HarnessError as exc:
                issue = exc.message
                attempts.append(self._attempt(attempt_number, purpose, completion, "invalid_update", issue))
                continue
            attempts.append(self._attempt(attempt_number, purpose, completion, "parsed", ""))
            return RequirementClarificationResult(
                model=result.model,
                final_report=result.report,
                decision=self._latest_decision(result.model, question.id),
                resolution=str(proposal.get("resolution")),
                follow_up_text=(
                    str(proposal.get("follow_up_text")).strip()
                    if proposal.get("follow_up_text") is not None
                    else None
                ),
                attempts=attempts,
            )
        raise HarnessError(
            "requirement_answer_update_failed",
            "Clarification update failed after bounded repair: " + issue,
            run_id=model.run_id,
        )

    def _apply_proposal(
        self,
        *,
        proposal: dict[str, Any],
        source_text: str,
        model: RequirementModelV2,
        question: CurrentRequirementQuestion,
        answer: str,
        answered_by: str,
        answered_at: str,
    ):
        resolution = proposal.get("resolution")
        updates = proposal.get("updates")
        follow_up = proposal.get("follow_up_text")
        if resolution not in {"accepted", "insufficient"} or not isinstance(updates, list):
            raise HarnessError("requirement_answer_update_failed", "Proposal must contain resolution and updates.")
        if any(not isinstance(item, dict) for item in updates):
            raise HarnessError("requirement_answer_update_failed", "Every clarification update must be an object.")
        if resolution == "insufficient":
            if not isinstance(follow_up, str) or not follow_up.strip():
                raise HarnessError(
                    "requirement_answer_update_failed",
                    "Insufficient answer requires a non-empty follow_up_text.",
                )
            if updates:
                raise HarnessError(
                    "requirement_answer_update_failed",
                    "Insufficient answer must not mutate requirements.",
                )
        else:
            if follow_up not in {None, ""}:
                raise HarnessError(
                    "requirement_answer_update_failed",
                    "Accepted answer must not include follow_up_text.",
                )
            update_ids = [str(item.get("requirement_id") or "") for item in updates]
            if len(update_ids) != len(set(update_ids)) or set(update_ids) != set(question.affects):
                raise HarnessError(
                    "requirement_answer_update_failed",
                    "Accepted answer must update exactly the current question affects IDs.",
                )

        data = model.to_dict()
        data["revision"] = model.revision + 1
        decision_id = _next_decision_id(data.get("decisions"))
        previous = _latest_question_decision(data.get("decisions"), question.id)
        if previous is not None:
            previous["status"] = "superseded"
        decision = {
            "id": decision_id,
            "question_id": question.id,
            "answer": answer,
            "answered_by": answered_by,
            "answered_at": answered_at,
            "affected_requirement_ids": list(question.affects) if resolution == "accepted" else [],
            "status": resolution,
            "supersedes_decision_id": previous.get("id") if previous is not None else None,
            "submission_hash": clarification_submission_hash(question.id, answer),
        }
        decisions = data.get("decisions")
        if not isinstance(decisions, list):
            decisions = []
            data["decisions"] = decisions
        decisions.append(decision)

        question_data = _find_question(data, question.id)
        if resolution == "insufficient":
            question_data["question"] = str(follow_up).strip()
        else:
            for update in updates:
                requirement_id = str(update.get("requirement_id") or "")
                fields = update.get("fields")
                if not isinstance(fields, dict):
                    raise HarnessError(
                        "requirement_answer_update_failed",
                        f"Update fields must be an object for {requirement_id}.",
                    )
                item, allowed_fields, requirement_like = _find_requirement(data, requirement_id)
                unknown_fields = sorted(set(fields).difference(allowed_fields))
                if unknown_fields:
                    raise HarnessError(
                        "requirement_answer_update_failed",
                        f"Update for {requirement_id} contains forbidden fields: {', '.join(unknown_fields)}.",
                    )
                for key, value in fields.items():
                    item[key] = deepcopy(value)
                refs = [ref for ref in _string_list(item.get("open_question_refs")) if ref != question.id]
                item["open_question_refs"] = refs
                if requirement_like and not refs:
                    item["status"] = "human_decision"
                    item["decision_ref"] = decision_id
            question_data["status"] = "resolved"

        data["validation"] = {"status": "invalid", "errors": []}
        validation = normalize_and_validate_requirement_model_v2(data, source_text)
        if not validation.report.ok:
            details = "; ".join(
                f"{item.code}@{item.path}: {item.message}"
                for item in validation.report.issues
            )
            raise HarnessError(
                "requirement_answer_update_failed",
                "Updated model failed validation: " + details,
                run_id=model.run_id,
            )
        return validation

    def _complete(self, prompt: str, *, max_tokens: int) -> LlmCompletion:
        try:
            return self.llm_client.complete_with_metadata(
                system=(
                    "You update one Requirement Model V2 question. Return one JSON delta only. "
                    "Do not create decisions, revisions, IDs, or unrelated requirements."
                ),
                prompt=prompt,
                json_mode=True,
                max_tokens=max_tokens,
            )
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover
            raise HarnessError("requirement_answer_update_failed", str(exc)) from exc

    def _prompt(
        self,
        source_text: str,
        model: RequirementModelV2,
        question: CurrentRequirementQuestion,
        answer: str,
    ) -> str:
        template = _PROMPT_PATH.read_text(encoding="utf-8")
        values = {
            "SOURCE_TEXT": source_text,
            "CURRENT_MODEL": json.dumps(model.to_dict(), ensure_ascii=False, indent=2),
            "QUESTION": json.dumps(question.to_dict(), ensure_ascii=False, indent=2),
            "ANSWER": answer,
        }
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", value)
        return template

    def _structured_max_tokens(self) -> int:
        settings = getattr(self.llm_client, "settings", None)
        return int(getattr(settings, "llm_structured_max_tokens", 8192))

    @staticmethod
    def _attempt(
        attempt_number: int,
        purpose: Literal["initial", "repair"],
        completion: LlmCompletion,
        parse_status: Literal["parsed", "parse_failed", "truncated", "invalid_update"],
        issue: str,
    ) -> ClarificationAttempt:
        return ClarificationAttempt(
            attempt=attempt_number,
            purpose=purpose,
            finish_reason=completion.finish_reason,
            usage=deepcopy(completion.usage),
            parse_status=parse_status,
            issue=issue,
        )

    @staticmethod
    def _latest_decision(model: RequirementModelV2, question_id: str) -> dict[str, Any]:
        decision = _latest_question_decision(model.decisions, question_id)
        if decision is None:  # pragma: no cover - constructed immediately above.
            raise AssertionError("Clarification decision was not created.")
        return deepcopy(decision)


def _question_sort_key(item: dict[str, Any]) -> tuple[int, str, int, str]:
    category = str(item.get("category") or "")
    item_id = str(item.get("id") or "")
    match = re.fullmatch(r"Q-([1-9]\d*)", item_id)
    number = int(match.group(1)) if match else 2**31
    return (_CATEGORY_PRIORITY.get(category, len(_CATEGORY_PRIORITY)), category, number, item_id)


def _next_decision_id(value: object) -> str:
    numbers: list[int] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            match = re.fullmatch(r"D-([1-9]\d*)", str(item.get("id") or ""))
            if match:
                numbers.append(int(match.group(1)))
    return f"D-{max(numbers, default=0) + 1}"


def _latest_question_decision(value: object, question_id: str) -> dict[str, Any] | None:
    decisions = value if isinstance(value, list) else []
    for item in reversed(decisions):
        if isinstance(item, dict) and item.get("question_id") == question_id:
            return item
    return None


def _find_question(data: dict[str, Any], question_id: str) -> dict[str, Any]:
    for item in data.get("open_questions", []):
        if isinstance(item, dict) and item.get("id") == question_id:
            return item
    raise HarnessError("requirement_question_not_current", f"Question does not exist: {question_id}")


def _find_requirement(data: dict[str, Any], requirement_id: str) -> tuple[dict[str, Any], set[str], bool]:
    use_case = data.get("use_case") if isinstance(data.get("use_case"), dict) else {}
    goal = use_case.get("goal")
    if isinstance(goal, dict) and goal.get("id") == requirement_id:
        return goal, {"text"}, True
    for actor in use_case.get("actors", []) if isinstance(use_case.get("actors"), list) else []:
        if isinstance(actor, dict) and actor.get("id") == requirement_id:
            return actor, {"name"}, True
    flows = data.get("flows") if isinstance(data.get("flows"), dict) else {}
    for item in flows.get("main", []) if isinstance(flows.get("main"), list) else []:
        if isinstance(item, dict) and item.get("id") == requirement_id:
            return item, {"actor", "action", "target"}, False
    for group in ("alternatives", "exceptions"):
        for item in flows.get(group, []) if isinstance(flows.get(group), list) else []:
            if isinstance(item, dict) and item.get("id") == requirement_id:
                return item, {"trigger", "steps", "outcome"}, False
    collections = (
        "preconditions",
        "postconditions",
        "business_rules",
        "acceptance_criteria",
        "quality_requirements",
        "assumptions",
    )
    for collection in collections:
        for item in data.get(collection, []) if isinstance(data.get(collection), list) else []:
            if isinstance(item, dict) and item.get("id") == requirement_id:
                allowed = {"text", "verifies"} if collection == "acceptance_criteria" else {"text"}
                return item, allowed, True
    raise HarnessError(
        "requirement_answer_update_failed",
        f"Affected requirement does not exist: {requirement_id}.",
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]
